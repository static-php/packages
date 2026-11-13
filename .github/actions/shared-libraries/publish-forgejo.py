#!/usr/bin/env python3
"""Preflight every existing stream repository before publishing a shared cohort."""

import argparse
import hashlib
import json
import os
import re
import runpy
import tempfile
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from itertools import groupby


STREAM_OWNERS = ('82', '83', '84', '85', '86')
LAYOUT_PACKAGE = 'php-zts-runtime-layout'


def independent(name):
    return name.startswith('php-zts-lib-') or name.removesuffix('-debuginfo') == LAYOUT_PACKAGE


def consumer_owners(cohort, package_type, helper, filenames):
    owners = {}
    for manifest in cohort.rglob("shared-library-consumers.json"):
        consumers = json.loads(manifest.read_text()).get("consumers", {})
        for archive in manifest.parent.glob("*." + package_type):
            if archive.name not in filenames:
                continue
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


def publication_order(groups, helper, kind, mirror=False):
    ordered = []
    for owner, paths in groups.items():
        for path in paths:
            package_name = helper['package_metadata'](path, kind)[0]
            name = package_name.removesuffix('-debuginfo')
            if package_name.endswith('-debuginfo'):
                phase = 4
            elif name == LAYOUT_PACKAGE:
                phase = 0
            elif name.startswith('php-zts-lib-'):
                phase = 1 if mirror else 3
            elif name in ('php-zts', 'php-zts-cli'):
                phase = 3 if mirror else 2
            else:
                phase = 2 if mirror else 1
            ordered.append((phase, owner, path))
    return sorted(ordered, key=lambda item: (item[0], item[1], item[2].name))


def verify_debian_indexes(host, password, groups, helper):
    verifier = runpy.run_path(str(Path(__file__).with_name('forgejo-index.py')))
    return verifier['verify_indexes'](host, password, groups, helper)


def publish(host, password, kind, groups, helper, mirror=False):
    digests = {}
    selected = {}
    for owner, paths in sorted(groups.items()):
        print(f'Preflight: {host} owner {owner}, {len(paths)} packages', flush=True)
        remote = helper['get_remote_candidates'](host, owner, password, kind, paths)
        if kind == 'debian':
            for path in paths:
                name, version, arch = helper['package_metadata'](path, kind)
                if not any(item['name'] == name and item['version'] == version for item in remote):
                    continue
                for published in helper['remote_package_archives'](host, owner, password, kind, name, version, True):
                    if published['arch'] != arch or published['sha256'] == helper['archive_sha256'](path):
                        continue
                    data = helper['registry_request']('get', published['url'], auth=helper['registry_auth'](owner, password),
                        timeout=60, read=lambda response: (response.raise_for_status(), response.content)[1])
                    if hashlib.sha256(data).hexdigest() != published['sha256']:
                        raise RuntimeError('Published DEB checksum mismatch: ' + name)
                    with tempfile.TemporaryDirectory(prefix='spp-deb-compare-') as temporary:
                        existing = Path(temporary) / 'published.deb'
                        existing.write_bytes(data)
                        if runpy.run_path(str(Path(__file__).resolve().parents[3] / 'bin/package-content.py'))['equivalent'](path, existing, 'deb'):
                            path.write_bytes(data)
                            print('Reusing identical published DEB: ' + path.name, flush=True)
        before = {path: helper['archive_sha256'](path) for path in paths}
        allowed = helper['verify_upload_versions'](host, owner, password, kind, paths, remote, keep_newer=mirror)
        if not mirror and len(allowed) != len(paths):
            raise RuntimeError('A version/archive identity guard rejected the cohort for owner ' + owner)
        digests.update({path: before[path] for path in allowed})
        selected[owner] = allowed
    if any(helper['archive_sha256'](path) != digest for path, digest in digests.items()):
        raise RuntimeError('A cohort archive changed during publication preflight')
    ordered = publication_order(selected, helper, kind, mirror)
    completed = {owner: [] for owner in selected}
    def upload(item):
        index, (_, owner, path) = item
        print(f'Uploading {index}/{len(ordered)}: {host} {owner}/{path.name}', flush=True)
        if helper['archive_sha256'](path) != digests[path]:
            raise RuntimeError('A cohort archive changed after publication preflight: ' + path.name)
        try:
            helper['upload_verified_archive'](host, owner, password, kind, path, preflighted=True)
        except Exception as error:
            failure = f'Upload failed for {host} {owner}/{path.name}: {error}'
            print(failure + '; continuing with remaining packages', flush=True)
            return failure
        print(f'Completed {index}/{len(ordered)}: {host} {owner}/{path.name}', flush=True)
        completed[owner].append(path)
    # Mirror cohorts are serialized by the workflow; bound concurrent large uploads.
    failures = []
    with ThreadPoolExecutor(max_workers=2 if mirror else 4) as pool:
        for _, phase in groupby(enumerate(ordered, 1), key=lambda item: item[1][0]):
            failures.extend(error for error in pool.map(upload, phase) if error)
    if kind == 'debian':
        failures.extend(verify_debian_indexes(host, password, completed, helper))
    if failures:
        raise RuntimeError(f'{len(failures)} package uploads failed; remaining packages were attempted: '
                           + ' | '.join(failures[:5]))


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('package_type', choices=('deb', 'apk'))
    parser.add_argument('host_number', choices=('1', '2'))
    parser.add_argument('--cohort', type=Path)
    parser.add_argument('--directory', type=Path, default=Path('.'))
    args = parser.parse_args(argv)
    root = Path(__file__).resolve().parents[3]
    helper = runpy.run_path(str(root / 'bin/forgejo-helper'))
    cohort = args.cohort or root / 'cohort'
    kind = {'deb': 'debian', 'apk': 'alpine'}[args.package_type]
    owners = consumer_owners(cohort, args.package_type, helper, {path.name for path in args.directory.glob('*.' + args.package_type)})
    selected = [stream.replace('.', '') for stream in os.environ.get('PUBLICATION_PHP_STREAMS', '').split(',') if stream]
    if set(selected) - set(STREAM_OWNERS):
        raise RuntimeError('Invalid publication PHP streams')
    groups = package_groups(args.directory, args.package_type, owners, helper, selected)
    publish(helper['host' + args.host_number], os.environ['FORGEJO_PASSWORD'], kind, groups, helper, mirror=args.host_number == '2')



if __name__ == "__main__":
    main()
