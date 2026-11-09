#!/usr/bin/env python3
"""Preflight every existing stream repository before publishing a shared cohort."""

import argparse
import json
import os
import re
import runpy
import shutil
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from itertools import groupby


STREAM_OWNERS = ('82', '83', '84', '85', '86')
LAYOUT_PACKAGE = 'php-zts-runtime-layout'


def independent(name):
    return name.startswith('php-zts-lib-') or name.removesuffix('-debuginfo') == LAYOUT_PACKAGE


def consumer_owners(cohort, package_type, helper):
    owners = {}
    for manifest in cohort.rglob("shared-library-consumers.json"):
        consumers = json.loads(manifest.read_text()).get("consumers", {})
        for archive in manifest.parent.glob("*." + package_type):
            key = helper["package_metadata"](archive, {"deb": "debian", "apk": "alpine"}[package_type])
            consumer = consumers.get(key[0].removesuffix("-debuginfo"), {})
            stream = consumer.get("php_version") or consumer.get("php_stream")
            if stream:
                match = re.match(r"^(\d+)\.(\d+)", stream)
                owner = "".join(match.groups()) if match else None
                if owner not in STREAM_OWNERS:
                    raise RuntimeError("Invalid consumer PHP stream: " + str(stream))
                if key in owners and owners[key] != owner:
                    raise RuntimeError('Conflicting consumer ownership: ' + str(key))
                owners[key] = owner
    return owners


def package_groups(directory, package_type, owners, helper, selected_owners=None):
    kind = {'deb': 'debian', 'apk': 'alpine'}[package_type]
    groups = {}
    for path in sorted(directory.glob('*.' + package_type)):
        if path.is_symlink() or not path.is_file():
            raise RuntimeError('Invalid cohort archive: ' + str(path))
        key = helper['package_metadata'](path, kind)
        destinations = (selected_owners or STREAM_OWNERS) if independent(key[0]) else [owners.get(key)]
        if not all(destinations):
            raise RuntimeError('Package has no consumer ownership record: ' + key[0])
        if selected_owners and not set(destinations) <= set(selected_owners):
            raise RuntimeError('Package belongs to an unselected PHP owner: ' + key[0])
        for owner in destinations:
            groups.setdefault(owner, []).append(path)
    if not groups:
        raise RuntimeError("No cohort packages to publish")
    return groups


def publication_mode(cohort, package_type, plan_path=None):
    paths = [plan_path] if plan_path else sorted(cohort.rglob('shared-library-plan.json'))
    plans = [json.loads(path.read_text()) for path in paths]
    plans = [plan for plan in plans if plan.get('type') == package_type]
    if not plans:
        raise RuntimeError('Shared-library publication requires its format-specific build plan')
    modes = {(bool(plan.get('bootstrap')), bool(plan.get('abi_break'))) for plan in plans}
    if len(modes) != 1:
        raise RuntimeError('Conflicting shared-library publication plans')
    return modes.pop()


def publication_order(groups, helper, kind, bootstrap):
    ordered = []
    for owner, paths in groups.items():
        for path in paths:
            name = helper['package_metadata'](path, kind)[0].removesuffix('-debuginfo')
            if name == LAYOUT_PACKAGE:
                phase = 0
            elif name.startswith('php-zts-lib-'):
                phase = 1 if bootstrap else 3
            elif name in ('php-zts', 'php-zts-cli'):
                phase = 3 if bootstrap else 2
            else:
                phase = 2 if bootstrap else 1
            ordered.append((phase, owner, path))
    return sorted(ordered, key=lambda item: (item[0], item[1], item[2].name))


def mirror_candidates(paths, remote, host, owner, password, kind, helper):
    metadata = {path: helper['package_metadata'](path, kind) for path in paths}
    newer = set()
    for name, version, arch in metadata.values():
        for package in remote:
            if package['name'] != name or not helper['version_is_newer'](kind, package['version'], version):
                continue
            architectures = helper['get_remote_package_archs'](host, owner, password, kind, name, package['version'], strict=True)
            if not architectures:
                raise RuntimeError(f'Cannot verify newer mirror package: {owner}/{name}/{package["version"]}')
            if arch in architectures:
                newer.add(name.removesuffix('-debuginfo'))
    selected = []
    for path, (name, version, arch) in metadata.items():
        if name.removesuffix('-debuginfo') in newer:
            print(f'Keeping newer mirror package: {owner}/{path.name}', flush=True)
        else:
            selected.append(path)
    return selected


def stage_mirror_dependencies(cohort, directory, package_type, helper, owners=None):
    """Copy retained, already-published dependencies into an incomplete mirror."""
    kind = {'deb': 'debian', 'apk': 'alpine'}[package_type]
    present = {helper['package_metadata'](path, kind)[0] for path in directory.glob('*.' + package_type)}
    planner = None
    for manifest in sorted(cohort.glob('shared-libs-*/baseline/shared-libraries.json')):
        baseline = json.loads(manifest.read_text())
        records = [('php-zts-lib-' + identifier, value, 'published_archive', 'published_archive_sha256')
                   for identifier, value in baseline.get('libraries', {}).items()]
        if baseline.get('runtime_layout'):
            records.append((LAYOUT_PACKAGE, baseline['runtime_layout'], 'archive', 'archive_sha256'))
        for name, record, archive_key, digest_key in records:
            relative = record.get(archive_key)
            if name in present or not relative or not relative.endswith('.' + package_type):
                continue
            root = manifest.parent.resolve()
            archive = (root / relative).resolve()
            if root not in archive.parents or not archive.is_file() or helper['archive_sha256'](archive) != record.get(digest_key):
                raise RuntimeError('Retained mirror dependency checksum/path mismatch: ' + name)
            actual_name, version, arch = helper['package_metadata'](archive, kind)
            normalize = lambda value: {'amd64': 'x86_64', 'arm64': 'aarch64'}.get(value, value)
            if (actual_name != name or version != record.get('published_version', record.get('package_version'))
                    or arch not in ('all', 'noarch') and normalize(arch) != normalize(baseline.get('architecture'))):
                raise RuntimeError('Retained mirror dependency identity mismatch: ' + name)
            shutil.copy2(archive, directory / archive.name)
            present.add(name)
            debug = record.get('published_debug_archive')
            if debug and name + '-debuginfo' not in present:
                password = os.environ.get(debug.get('password_env') or '', '')
                archives = helper['remote_package_archives'](debug['host'], debug['owner'], password, kind,
                    debug['name'], debug['package_version'], True)
                matching = [item for item in archives if normalize(item['arch']) == normalize(arch)]
                if len(matching) != 1:
                    raise RuntimeError('Cannot identify retained primary debuginfo: ' + name)
                if all(any(item.get('sha256') == matching[0]['sha256'] for item in
                           helper['get_remote_package_files'](helper['host2'], owner, os.environ['FORGEJO_PASSWORD'],
                               kind, debug['name'], debug['package_version'])) for owner in (owners or STREAM_OWNERS)):
                    continue
                if planner is None:
                    planner = runpy.run_path(str(Path(__file__).resolve().parents[3] / 'bin/shared-library-plan'))
                cache = cohort / '.mirror-dependencies' / normalize(arch)
                cache.mkdir(parents=True, exist_ok=True)
                copied = Path(planner['stage_debug_archive'](debug, cache)['path'])
                shutil.copy2(copied, directory / copied.name)
                present.add(name + '-debuginfo')
            print(f'Retained primary dependency for mirror: {name} {version} {arch}', flush=True)


def publish(action, host, password, kind, groups, helper, bootstrap, mirror=False):
    if action not in ('check', 'upload'):
        raise RuntimeError('Unknown publication action: ' + action)
    digests = {path: helper['archive_sha256'](path) for paths in groups.values() for path in paths}
    selected = {}
    for owner, paths in sorted(groups.items()):
        print(f'Preflight: {host} owner {owner}, {len(paths)} packages', flush=True)
        remote = helper['get_remote_candidates'](host, owner, password, kind, paths)
        if mirror:
            paths = mirror_candidates(paths, remote, host, owner, password, kind, helper)
        allowed = helper['verify_upload_versions'](host, owner, password, kind, paths, remote)
        if len(allowed) != len(paths):
            raise RuntimeError('A version/archive identity guard rejected the cohort for owner ' + owner)
        selected[owner] = allowed
    if any(helper['archive_sha256'](path) != digest for path, digest in digests.items()):
        raise RuntimeError('A cohort archive changed during publication preflight')
    if action == 'check':
        return
    ordered = publication_order(selected, helper, kind, bootstrap)
    def upload(item):
        index, (_, owner, path) = item
        print(f'Uploading {index}/{len(ordered)}: {host} {owner}/{path.name}', flush=True)
        if helper['archive_sha256'](path) != digests[path]:
            raise RuntimeError('A cohort archive changed after publication preflight: ' + path.name)
        try:
            helper['upload_verified_archive'](host, owner, password, kind, path)
        except Exception as error:
            failure = f'Upload failed for {host} {owner}/{path.name}: {error}'
            print(failure + '; continuing with remaining packages', flush=True)
            return failure
        print(f'Completed {index}/{len(ordered)}: {host} {owner}/{path.name}', flush=True)
    # Limit contention while recovering the secondary registry's HTTP 500 failures.
    # Primary publication keeps its parallel uploads.
    failures = []
    with ThreadPoolExecutor(max_workers=1 if mirror else 4) as pool:
        for _, phase in groupby(enumerate(ordered, 1), key=lambda item: item[1][0]):
            failures.extend(error for error in pool.map(upload, phase) if error)
    if failures:
        raise RuntimeError(f'{len(failures)} package uploads failed; remaining packages were attempted: '
                           + ' | '.join(failures[:5]))


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=('check', 'upload'))
    parser.add_argument('package_type', choices=('deb', 'apk'))
    parser.add_argument('host_number', choices=('1', '2'))
    parser.add_argument('--plan', type=Path)
    parser.add_argument('--cohort', type=Path)
    parser.add_argument('--directory', type=Path, default=Path('.'))
    args = parser.parse_args(argv)
    root = Path(__file__).resolve().parents[3]
    helper = runpy.run_path(str(root / 'bin/forgejo-helper'))
    cohort = args.cohort or root / 'cohort'
    kind = {'deb': 'debian', 'apk': 'alpine'}[args.package_type]
    owners = consumer_owners(cohort, args.package_type, helper)
    selected = [stream.replace('.', '') for stream in os.environ.get('PUBLICATION_PHP_STREAMS', '').split(',') if stream]
    if set(selected) - set(STREAM_OWNERS):
        raise RuntimeError('Invalid publication PHP streams')
    if args.host_number == '2':
        stage_mirror_dependencies(cohort, args.directory, args.package_type, helper, selected)
    groups = package_groups(args.directory, args.package_type, owners, helper, selected)
    bootstrap, _ = publication_mode(cohort, args.package_type, args.plan)
    publish(args.action, helper['host' + args.host_number], os.environ['FORGEJO_PASSWORD'], kind, groups, helper, bootstrap,
            mirror=args.host_number == '2')


if __name__ == "__main__":
    main()
