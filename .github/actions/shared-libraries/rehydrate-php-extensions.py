#!/usr/bin/env python3
"""Fill missing selected bundled extension sources from the verified PHP snapshot."""

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import re
import tarfile


def read(path):
    return json.loads(path.read_text())


def safe(value):
    if not isinstance(value, str) or not value or '\\' in value or '\x00' in value:
        raise RuntimeError('Invalid PHP extension source path')
    path = PurePosixPath(value)
    if path.is_absolute() or '..' in path.parts:
        raise RuntimeError('Escaping PHP extension source path')
    return path.parts


def verify_seed(root, version, packages):
    buildroot = root / 'buildroot'
    receipt = read(buildroot / 'consumer-cache-seed.json')
    relative = '/'.join(safe(receipt.get('cached_metadata_path')))
    metadata_file = buildroot / relative
    if (receipt.get('schema') != 1 or receipt.get('state') != 'verified'
            or sorted(receipt.get('rebuild_packages', [])) != sorted(packages)
            or hashlib.sha256(metadata_file.read_bytes()).hexdigest() != receipt.get('cached_metadata_sha256')):
        raise RuntimeError('Bundled source repair requires the matching verified core seed')
    metadata = read(metadata_file)
    if (metadata.get('state') != 'sealed'
            or metadata.get('source_version', metadata.get('php_version', '')).lower() != version.lower()):
        raise RuntimeError('Bundled source repair core has a different PHP release')
    for name, entry in receipt.get('core_files', {}).items():
        path = buildroot.joinpath(*safe(name))
        if 'symlink' in entry:
            valid = path.is_symlink() and os.readlink(str(path)) == entry['symlink']
        else:
            valid = (path.is_file() and not path.is_symlink()
                     and hashlib.sha256(path.read_bytes()).hexdigest() == entry.get('sha256')
                     and path.stat().st_mode & 0o777 == entry.get('mode'))
        if not valid:
            raise RuntimeError('Bundled source repair core changed: ' + name)
    if not receipt.get('core_files'):
        raise RuntimeError('Bundled source repair has no verified core files')


def destination(source, relative):
    path = source.joinpath(*relative)
    current = source
    for part in relative:
        current = current / part
        if current.is_symlink():
            raise RuntimeError('PHP extension source destination contains a symlink')
    return path


def repair(root, version, packages):
    root = Path(root).resolve()
    if not packages or any(not re.fullmatch('[a-z0-9_-]+', name) for name in packages):
        raise RuntimeError('Invalid partial extension selection')
    verify_seed(root, version, packages)
    spec = importlib.util.spec_from_file_location('rehydration_php_source', Path(__file__).with_name('php-source.py'))
    frozen = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(frozen)
    downloads = root / 'downloads'
    index = frozen.load_index(downloads)
    entry = next((entry for release, entry in index['sources'].items() if release.lower() == version.lower()), None)
    if entry is None:
        raise RuntimeError('Bundled source repair requires a captured PHP source archive')
    record = frozen.verified_record(downloads, version, entry)
    cache = read(downloads / '.cache.json')
    locked = cache.get('php-src', {}).get('source', {})
    if any(locked.get(key) != record.get(key) for key in ['filename', 'hash', 'version', 'cache_type']):
        raise RuntimeError('Bundled source repair archive differs from the selected PHP source lock')
    archive_path = frozen.archive_path(downloads, record['filename'])
    source = root / 'source/php-src'
    if ((root / 'source').is_symlink() or source.is_symlink() or not source.is_dir() or (source / 'ext').is_symlink()
            or not str(source.resolve()).startswith(str(root) + os.sep)):
        raise RuntimeError('Missing or linked PHP source tree for partial repair')
    header = (source / 'main/php_version.h').read_text()
    match = re.search(r'^\s*#\s*define\s+PHP_VERSION\s+"([^"\n]+)"', header, re.M)
    if match is None or match[1].lower() != version.lower():
        raise RuntimeError('Configured PHP source tree has a different release')
    pending, created, directories = [], [], []
    with tarfile.open(archive_path, 'r:*') as archive:
        members = archive.getmembers()
        names = {member.name: safe(member.name) for member in members}
        headers = [parts for parts in names.values() if len(parts) == 3 and parts[-2:] == ('main', 'php_version.h')]
        if len(headers) != 1:
            raise RuntimeError('PHP source repair archive has no unique root')
        prefix = headers[0][0]
        bundled = {name for name in packages if not cache.get('ext-' + name, {}).get('source')
                   or (name == 'zip' and tuple(map(int, version.split('.')[:2])) >= (8, 5))}
        selected = {name for name in bundled if any(parts == (prefix, 'ext', name, 'config.m4') for parts in names.values())}
        seen = set()
        for member in members:
            parts = names[member.name]
            if len(parts) < 3 or parts[:2] != (prefix, 'ext') or parts[2] not in selected:
                continue
            if parts in seen:
                raise RuntimeError('Duplicate selected PHP extension source entry')
            seen.add(parts)
            if member.isdir():
                continue
            if not member.isfile() or member.size > 64 * 1024 * 1024 or '.libs' in parts or '.deps' in parts:
                raise RuntimeError('Unsupported selected PHP extension source archive entry')
            relative = parts[1:]
            target = destination(source, relative)
            if target.exists():
                if not target.is_file():
                    raise RuntimeError('PHP extension source file has a non-file destination')
                continue
            stream = archive.extractfile(member)
            if stream is None:
                raise RuntimeError('Missing selected PHP extension archive data')
            with stream:
                contents = stream.read()
            if len(contents) != member.size:
                raise RuntimeError('Truncated selected PHP extension archive data')
            pending.append((target, contents, member.mode & 0o777))
    try:
        for path, contents, mode in pending:
            missing = []
            current = path.parent
            while not current.exists():
                missing.append(current)
                current = current.parent
            for directory in reversed(missing):
                directory.mkdir()
                directories.append(directory)
            with path.open('xb') as output:
                output.write(contents)
            created.append(path)
            path.chmod(mode)
    except BaseException:
        for path in reversed(created):
            path.unlink()
        for directory in reversed(directories):
            directory.rmdir()
        raise
    return {'schema': 1, 'php_version': version, 'archive_sha256': entry['archive_sha256'],
            'selected_bundled_extensions': sorted(selected), 'created_files': {
                str(path.relative_to(source)): hashlib.sha256(contents).hexdigest() for path, contents, _ in pending}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--version', required=True)
    parser.add_argument('--packages', required=True)
    args = parser.parse_args()
    print(json.dumps(repair(args.root, args.version, [name for name in args.packages.split(',') if name]), sort_keys=True))


if __name__ == '__main__':
    main()
