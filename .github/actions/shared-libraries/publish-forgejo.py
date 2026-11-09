#!/usr/bin/env python3
"""Preflight every existing stream repository before publishing a shared cohort."""

import argparse
import json
import os
import re
import runpy
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
    groups = package_groups(args.directory, args.package_type, owners, helper, selected)
    bootstrap, _ = publication_mode(cohort, args.package_type, args.plan)
    publish(args.action, helper['host' + args.host_number], os.environ['FORGEJO_PASSWORD'], kind, groups, helper, bootstrap,
            mirror=args.host_number == '2')


if __name__ == "__main__":
    main()
