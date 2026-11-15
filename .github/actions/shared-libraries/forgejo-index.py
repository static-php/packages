"""Verify served native indexes and repair a lost concurrent index update."""

from collections import defaultdict
import gzip
import io
from pathlib import Path
import runpy
import tarfile
from urllib.parse import quote


def read_index(host, owner, password, architecture, helper, kind="debian"):
    if kind == "alpine":
        url = f'{host}/api/packages/{quote(owner, safe="")}/alpine/main/php-zts/{quote(architecture, safe="")}/APKINDEX.tar.gz'
        response = helper["registry_request"]("get", url, auth=(owner, password), timeout=60)
        response.raise_for_status()
        with gzip.GzipFile(fileobj=io.BytesIO(response.content)) as stream:
            with tarfile.open(fileobj=stream, mode="r|", ignore_zeros=True) as archive:
                text = next(archive.extractfile(member).read().decode() for member in archive if member.name == "APKINDEX")
        indexed = {}
        for paragraph in text.strip().split("\n\n"):
            fields = dict(line.split(":", 1) for line in paragraph.splitlines() if ":" in line)
            if "P" in fields:
                indexed[tuple(fields[field] for field in ("P", "V", "A"))] = fields.get("C")
        return indexed
    url = f'{host}/api/packages/{quote(owner, safe="")}/debian/dists/php-zts/main/binary-{architecture}/Packages'
    response = helper['registry_request']('get', url, auth=(owner, password), timeout=60)
    response.raise_for_status()
    indexed = {}
    for paragraph in response.text.strip().split('\n\n'):
        fields = dict(line.split(': ', 1) for line in paragraph.splitlines()
                      if ': ' in line and not line[:1].isspace())
        if 'Package' in fields:
            key = tuple(fields[field] for field in ('Package', 'Version', 'Architecture'))
            indexed[key] = fields.get('SHA256')
    return indexed


def verify_indexes(host, password, groups, helper, kind="debian"):
    label = "APK" if kind == "alpine" else "Debian"
    control_checksum = (runpy.run_path(str(Path(__file__).resolve().parents[3] / "bin/freeze-migration-baseline.py"))["apk_control_checksum"]
                        if kind == "alpine" else None)
    failures = []
    for owner, paths in groups.items():
        architectures = defaultdict(dict)
        for path in paths:
            identity = helper['package_metadata'](path, kind)
            architectures[identity[2]][identity] = (path, helper['archive_sha256'](path))
        for architecture, expected in architectures.items():
            try:
                expected_index = {key: control_checksum(path.read_bytes()) if control_checksum else digest
                                  for key, (path, digest) in expected.items()}
                indexed = read_index(host, owner, password, architecture, helper, kind)
                mismatched = [key for key, (_, digest) in expected.items()
                              if key in indexed and indexed[key] != expected_index[key]]
                if mismatched:
                    raise RuntimeError(label + ' index has a different checksum: ' + str(mismatched[0]))
                missing = [key for key in expected if key not in indexed]
                if not missing:
                    print(f'Index verified: {host} {owner}/{architecture}, {len(expected)} archives', flush=True)
                    continue

                # One upload regenerates this architecture's entire index. Restore
                # the smallest missing archive, with the exact existing bytes.
                identity = min(missing, key=lambda key: expected[key][0].stat().st_size)
                name, version, arch = identity
                path, digest = expected[identity]
                before = helper['remote_package_archives'](host, owner, password, kind, name, version, True)
                matching = [item for item in before if item['arch'] == arch]
                if len(matching) != 1 or matching[0]['sha256'] != digest:
                    raise RuntimeError('Cannot repair index without the identical stored archive: ' + str(identity))
                identities = {(item['arch'], item['sha256']) for item in before}
                url = host + '/api/packages/' + quote(owner, safe='') + '/debian/pool/php-zts/main/' + '/'.join(
                    quote(value, safe='') for value in identity)
                if kind == 'alpine':
                    url = host + '/api/packages/' + quote(owner, safe='') + '/alpine/main/php-zts/' + quote(arch, safe='') + '/' + quote(name + '-' + version + '.apk', safe='')
                print(f'Repairing unindexed {label}: {host} {owner}/{name} {version} {arch}; restoring identical archive', flush=True)
                deletion_error = None
                try:
                    response = helper['registry_request']('delete', url, auth=(owner, password), timeout=60)
                    if response.status_code not in (204, 404):
                        raise RuntimeError(f'{label} index repair DELETE returned HTTP {response.status_code}')
                except Exception as error:
                    deletion_error = error
                # A lost DELETE response may mean the archive was removed. Always
                # attempt restoration before reporting an error.
                helper['upload_verified_archive'](host, owner, password, kind, path, preflighted=True)
                after = helper['remote_package_archives'](host, owner, password, kind, name, version, True)
                if not identities <= {(item['arch'], item['sha256']) for item in after}:
                    raise RuntimeError('Index repair did not preserve every original archive: ' + str(identity))
                indexed = read_index(host, owner, password, architecture, helper, kind)
                missing = [key for key, (_, digest) in expected.items() if indexed.get(key) != expected_index[key]]
                if missing:
                    detail = f'; DELETE request: {deletion_error}' if deletion_error else ''
                    raise RuntimeError(label + ' index still omits or differs from uploaded archive: ' + str(missing[0]) + detail)
                print(f'Index repaired and verified: {host} {owner}/{architecture}, {len(expected)} archives', flush=True)
            except Exception as error:
                failures.append(f'{label} index {host} {owner}/{architecture}: {error}')
    return failures
