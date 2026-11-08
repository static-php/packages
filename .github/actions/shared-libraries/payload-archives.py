#!/usr/bin/env python3
"""Check packaged ELF bytes against the sealed producer's prepared output."""

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile


def digest(path):
    value = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            value.update(block)
    return value.hexdigest()


def allowed_files(root):
    record = json.loads((root / 'build-payload.json').read_text())
    if record.get('state') != 'sealed':
        raise RuntimeError('Archive verification requires a sealed producer payload')
    allowed = {}
    for source, files in record.get('prepared', {}).items():
        for role, item in files.items():
            path = root / item['path']
            if not path.is_file() or digest(path) != item['sha256'] or path.stat().st_mode & 0o777 != item['mode']:
                raise RuntimeError('Prepared producer bytes changed: ' + str(path))
            allowed.setdefault((item['sha256'], item['mode']), []).append({'source': source, 'role': role})
    if not allowed:
        raise RuntimeError('Sealed payload contains no prepared ELF files')
    return allowed


def extract(archive, kind, directory):
    if kind == 'deb':
        subprocess.run(['dpkg-deb', '--extract', archive, directory], check=True)
        return
    with subprocess.Popen(['rpm2cpio', archive], stdout=subprocess.PIPE) as producer:
        consumer = subprocess.run(['cpio', '-idm', '--quiet', '--no-absolute-filenames'],
                                  stdin=producer.stdout, cwd=directory, stdout=subprocess.DEVNULL)
        producer.stdout.close()
        status = producer.wait()
        if status or consumer.returncode:
            raise RuntimeError('Cannot extract RPM: ' + str(archive))


def verify_extracted(directory, allowed):
    result = {}
    for path in sorted(directory.rglob('*')):
        if path.is_symlink() or not path.is_file():
            continue
        with path.open('rb') as stream:
            if stream.read(4) != b'\x7fELF':
                continue
        sha, mode = digest(path), path.stat().st_mode & 0o777
        if (sha, mode) not in allowed:
            raise RuntimeError('Packaged ELF differs from sealed producer bytes: ' + str(path.relative_to(directory)))
        logical = '/' + str(path.relative_to(directory))
        logical = logical.replace('/usr/lib/debug/usr/lib64/php-zts/', '/usr/lib/debug/usr/lib/php-zts/')
        logical = logical.replace('/usr/lib64/php-zts/', '/usr/lib/php-zts/')
        result[logical] = {'sha256': sha, 'mode': mode, 'producer': allowed[(sha, mode)]}
    return result


def audit(root, packages, kind):
    allowed = allowed_files(root)
    result = {'schema': 1, 'type': kind, 'sealed_manifest_sha256': digest(root / 'build-payload.json'), 'archives': {}}
    for archive in sorted(packages.glob('*.' + kind)):
        with tempfile.TemporaryDirectory(prefix='spp-payload-archive-') as temporary:
            directory = Path(temporary)
            extract(archive.resolve(), kind, directory)
            result['archives'][archive.name] = {'sha256': digest(archive), 'elf_files': verify_extracted(directory, allowed)}
    if not result['archives']:
        raise RuntimeError('No native archives to verify')
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--packages', type=Path, required=True)
    parser.add_argument('--type', choices=('rpm', 'deb'), required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    result = audit(args.root, args.packages, args.type)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + '\n')
    print('Verified sealed ELF identity in {} {} archives.'.format(len(result['archives']), args.type))


if __name__ == '__main__':
    main()
