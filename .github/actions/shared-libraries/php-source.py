#!/usr/bin/env python3
"""Capture and select exact PHP source archives in a frozen downloads snapshot."""

import argparse
import copy
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import subprocess
import tarfile
import tempfile

INDEX = '.php-sources.json'
CACHE = '.cache.json'
VERSION = re.compile(r'^\d+\.\d+\.\d+(?:(?:alpha|beta|RC)\d+)?$', re.I)


def read(path, default=None):
    return json.loads(path.read_text()) if path.is_file() else default


def write(path, value):
    content = json.dumps(value, sort_keys=True, indent=2) + '\n'
    if path.is_file() and path.read_text() == content:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, filename = tempfile.mkstemp(prefix='.php-source-', dir=path.parent)
    temporary = Path(filename)
    try:
        import os
        with os.fdopen(descriptor, 'w') as output:
            output.write(content)
        temporary.chmod(0o644)
        temporary.replace(path)
    finally:
        if temporary.exists():
            temporary.unlink()


def version_stream(version):
    if not isinstance(version, str) or not VERSION.fullmatch(version):
        raise RuntimeError('A full pinned PHP source release is required')
    if version.lower() == '8.6.0rc1':
        raise RuntimeError('PHP 8.6.0RC1 has an upstream packaging issue; use beta3 or RC2 and later')
    return '.'.join(version.split('.')[:2])


def archive_path(downloads, filename):
    if not isinstance(filename, str) or not filename or Path(filename).name != filename or '\\' in filename or '\x00' in filename:
        raise RuntimeError('PHP source archive filename must be a basename')
    path = downloads / filename
    if path.is_symlink() or not path.is_file():
        raise RuntimeError('Missing frozen PHP source archive: ' + filename)
    return path


def archive_identity(path):
    sha1, sha256 = hashlib.sha1(), hashlib.sha256()
    with path.open('rb') as source:
        for block in iter(lambda: source.read(1024 * 1024), b''):
            sha1.update(block)
            sha256.update(block)
    return sha1.hexdigest(), sha256.hexdigest()


def header_version(path):
    versions = []
    try:
        with tarfile.open(path, 'r:*') as archive:
            for member in archive:
                parts = PurePosixPath(member.name).parts
                if len(parts) not in (2, 3) or parts[-2:] != ('main', 'php_version.h'):
                    continue
                if not member.isfile() or member.size > 128 * 1024:
                    raise RuntimeError('Invalid PHP version header in source archive')
                source = archive.extractfile(member)
                if source is None:
                    raise RuntimeError('Missing PHP version header contents')
                with source:
                    contents = source.read().decode('utf-8')
                match = re.search(r'^\s*#\s*define\s+PHP_VERSION\s+"([^"\n]+)"', contents, re.M)
                if match is None:
                    raise RuntimeError('PHP source archive has no literal PHP_VERSION')
                versions.append(match[1])
    except (tarfile.TarError, EOFError, UnicodeError, OSError) as error:
        raise RuntimeError('Cannot read frozen PHP source archive: ' + path.name) from error
    if len(versions) != 1:
        raise RuntimeError('PHP source archive must contain exactly one version header')
    return versions[0]


def load_index(downloads):
    value = read(downloads / INDEX, {'schema': 1, 'sources': {}})
    if value.get('schema') != 1 or not isinstance(value.get('sources'), dict) or not isinstance(value.get('extensions', {}), dict):
        raise RuntimeError('Invalid frozen PHP source index')
    return value


def custom_git(root):
    path = Path(root) / 'config/templates/craft.yml.twig'
    text = path.read_text()
    match = re.search(r'^\s*custom-git:\s*\n((?:[ \t]+-\s*[^\n]+\n)+)', text, re.M)
    if match is None:
        return []
    result = []
    for line in match[1].splitlines():
        item = re.fullmatch(r"\s*-\s*['\"]([^'\"]+)['\"]\s*", line)
        if item is None:
            raise RuntimeError('Cannot read custom Git source snapshot recipe')
        parts = item[1].split(':', 2)
        if len(parts) != 3 or not re.fullmatch(r'ext-[a-z0-9_-]+', parts[0]) or not parts[1] or not parts[2].startswith('https://'):
            raise RuntimeError('Invalid custom Git source snapshot recipe')
        result.append(tuple(parts))
    return result


def validate_extension(downloads, name, record):
    if not isinstance(record, dict) or not isinstance(record.get('config'), dict) or not isinstance(record.get('hash'), str) or not re.fullmatch('[a-fA-F0-9]{40}', record['hash']):
        raise RuntimeError('Missing frozen extension source record: ' + name)
    expected = record['hash'].lower()
    if record.get('cache_type') in ('archive', 'file'):
        path = archive_path(downloads, record.get('filename'))
        if archive_identity(path)[0] != expected:
            raise RuntimeError('Frozen extension archive changed: ' + name)
    elif record.get('cache_type') == 'git':
        directory = record.get('dirname')
        if not isinstance(directory, str) or not directory or directory in ('.', '..') or Path(directory).name != directory or '\\' in directory or '\x00' in directory:
            raise RuntimeError('Invalid frozen extension Git directory: ' + name)
        path = downloads / directory
        if path.is_symlink() or not path.is_dir():
            raise RuntimeError('Missing frozen extension Git tree: ' + name)
        command = ['git', '-c', 'safe.directory=' + str(path.resolve()), '-C', str(path)]
        try:
            head = subprocess.check_output(command + ['rev-parse', 'HEAD'], stderr=subprocess.PIPE, universal_newlines=True).strip()
            dirty = subprocess.check_output(command + ['status', '--porcelain', '--untracked-files=all', '--ignore-submodules=none'], stderr=subprocess.PIPE, universal_newlines=True).strip()
        except subprocess.CalledProcessError as error:
            raise RuntimeError('Cannot inspect frozen extension Git tree: ' + name) from error
        if head.lower() != expected or dirty:
            raise RuntimeError('Frozen extension Git tree changed: ' + name)
    else:
        raise RuntimeError('Unsupported frozen extension source type: ' + name)


def capture_extensions(root, profile):
    root = Path(root)
    downloads = root / 'downloads'
    metadata = read(downloads / '.spc-meta.json', {})
    streams = [stream for stream in metadata.get('php_versions', {})
               if (tuple(map(int, stream.split('.'))) >= (8, 6)) == (profile == 'custom')]
    if not streams:
        return {}
    cache = read(downloads / CACHE, {})
    records = {}
    for name, revision, url in custom_git(root):
        record = cache.get(name, {}).get('source')
        validate_extension(downloads, name, record)
        if profile == 'custom' and (record.get('cache_type') != 'git' or record['config'].get('rev') != revision or record['config'].get('url') != url):
            raise RuntimeError('Custom extension source does not match its requested Git recipe: ' + name)
        records[name] = copy.deepcopy(record)
    index = load_index(downloads)
    for stream in streams:
        index.setdefault('extensions', {})[stream] = copy.deepcopy(records)
    write(downloads / INDEX, index)
    return records


def extension_records(downloads, index, stream):
    profiles = index.get('extensions', {})
    if not profiles:
        return {}
    if stream not in profiles or not isinstance(profiles[stream], dict):
        raise RuntimeError('Frozen extension sources are missing for PHP ' + stream)
    records = profiles[stream]
    for name, record in records.items():
        if not re.fullmatch(r'ext-[a-z0-9_-]+', name):
            raise RuntimeError('Invalid frozen extension source name')
        validate_extension(downloads, name, record)
    return records


def restore_stable(root):
    downloads = Path(root) / 'downloads'
    index = load_index(downloads)
    streams = sorted(stream for stream in index.get('extensions', {}) if tuple(map(int, stream.split('.'))) < (8, 6))
    if not streams:
        return
    records = extension_records(downloads, index, streams[0])
    cache = read(downloads / CACHE, {})
    for name, record in records.items():
        cache.setdefault(name, {})['source'] = copy.deepcopy(record)
    write(downloads / CACHE, cache)


def verified_record(downloads, version, entry):
    source = entry.get('source') if isinstance(entry, dict) else None
    if not isinstance(source, dict) or source.get('cache_type') != 'archive' or not isinstance(source.get('config'), dict):
        raise RuntimeError('Frozen PHP source record is not an archive')
    if not isinstance(source.get('version'), str) or source['version'].lower() != version.lower():
        raise RuntimeError('Frozen PHP source record belongs to a different release')
    path = archive_path(downloads, source.get('filename'))
    sha1, sha256 = archive_identity(path)
    if not isinstance(source.get('hash'), str) or source['hash'].lower() != sha1:
        raise RuntimeError('Frozen PHP source SHA1 differs from its recorded download')
    if entry.get('archive_sha256') != sha256 or entry.get('archive_sha1') != sha1:
        raise RuntimeError('Frozen PHP source archive bytes changed')
    actual = header_version(path)
    if actual.lower() != version.lower() or entry.get('header_version', '').lower() != actual.lower():
        raise RuntimeError('PHP source header does not match the pinned release')
    if entry.get('stream') != version_stream(version):
        raise RuntimeError('Frozen PHP source record has the wrong stream')
    return source


def capture(root, version):
    stream = version_stream(version)
    downloads = Path(root) / 'downloads'
    cache = read(downloads / CACHE, {})
    source = cache.get('php-src', {}).get('source')
    if not isinstance(source, dict) or source.get('cache_type') != 'archive' or not isinstance(source.get('config'), dict):
        raise RuntimeError('Download PHP before capturing its actual source record')
    if str(source.get('version', '')).lower() != version.lower():
        raise RuntimeError('The current php-src cache record is not the requested release')
    path = archive_path(downloads, source.get('filename'))
    sha1, sha256 = archive_identity(path)
    if str(source.get('hash', '')).lower() != sha1:
        raise RuntimeError('Downloaded PHP archive does not match its actual source record')
    actual = header_version(path)
    if actual.lower() != version.lower():
        raise RuntimeError('Downloaded PHP source header differs from the requested release')
    entry = {'stream': stream, 'source': copy.deepcopy(source), 'archive_sha1': sha1,
             'archive_sha256': sha256, 'header_version': actual,
             'provenance': {'kind': 'download-cache-capture'}}
    index = load_index(downloads)
    index['sources'][version] = entry
    write(downloads / INDEX, index)
    return entry


def bootstrap(root, version):
    downloads = Path(root) / 'downloads'
    cache = read(downloads / CACHE, {})
    existing = cache.get('php-src', {}).get('source', {})
    if isinstance(existing, dict) and str(existing.get('version', '')).lower() == version.lower():
        return capture(root, version)
    paths = sorted(path for path in downloads.glob('php-' + version + '.tar.*') if path.is_file())
    if len(paths) != 1:
        raise RuntimeError('Need exactly one frozen archive for PHP ' + version + '; capture the actual downloaded source record')
    path = archive_path(downloads, paths[0].name)
    sha1, sha256 = archive_identity(path)
    actual = header_version(path)
    if actual.lower() != version.lower():
        raise RuntimeError('Legacy PHP archive header differs from the requested release')
    entry = {'stream': version_stream(version), 'source': {'lock_type': 'source', 'cache_type': 'archive',
             'filename': path.name, 'extract': None, 'hash': sha1, 'version': actual,
             'config': {'type': 'php-release', 'domain': 'https://www.php.net'},
             'downloader': 'staticphp\\artifact\\PhpSource'},
             'archive_sha1': sha1, 'archive_sha256': sha256, 'header_version': actual,
             'provenance': {'kind': 'verified-legacy-archive', 'download_cache_record_available': False}}
    index = load_index(downloads)
    index['sources'][version] = entry
    write(downloads / INDEX, index)
    return entry


def select(root, version):
    version_stream(version)
    downloads = Path(root) / 'downloads'
    index = load_index(downloads)
    cache = read(downloads / CACHE, {})
    current = cache.get('php-src', {}).get('source', {})
    current_version = current.get('version') if isinstance(current, dict) else None
    if isinstance(current_version, str) and VERSION.fullmatch(current_version) and current_version not in index['sources']:
        try:
            capture(root, current_version)
            index = load_index(downloads)
        except RuntimeError:
            pass
    entry = next((record for release, record in index['sources'].items() if release.lower() == version.lower()), None)
    if entry is None:
        entry = bootstrap(root, version)
    source = verified_record(downloads, version, entry)
    records = extension_records(downloads, load_index(downloads), version_stream(version))
    path = downloads / CACHE
    cache = read(path, {})
    wrapper = dict(cache.get('php-src', {}))
    wrapper['source'] = copy.deepcopy(source)
    cache['php-src'] = wrapper
    for name, record in records.items():
        cache.setdefault(name, {})['source'] = copy.deepcopy(record)
    write(path, cache)
    return entry


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path('.'))
    parser.add_argument('--version')
    parser.add_argument('--profile', choices=('stable', 'custom'))
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument('--capture', action='store_true')
    action.add_argument('--select', action='store_true')
    action.add_argument('--capture-extensions', action='store_true')
    action.add_argument('--restore-stable', action='store_true')
    action.add_argument('--custom-git-args', action='store_true')
    action.add_argument('--custom-git-artifacts', action='store_true')
    args = parser.parse_args()
    if args.custom_git_args or args.custom_git_artifacts:
        recipes = custom_git(args.root)
        if args.custom_git_args:
            for recipe in recipes:
                print('--custom-git=' + ':'.join(recipe))
        else:
            print(','.join(recipe[0] for recipe in recipes))
        return
    if args.capture_extensions:
        if args.profile is None:
            parser.error('--capture-extensions requires --profile')
        records = capture_extensions(args.root, args.profile)
        print(json.dumps({'profile': args.profile, 'frozen_extension_sources': sorted(records)}))
        return
    if args.restore_stable:
        restore_stable(args.root)
        return
    if args.version is None:
        parser.error('--capture and --select require --version')
    entry = capture(args.root, args.version) if args.capture else select(args.root, args.version)
    print(json.dumps({'version': entry['header_version'], 'filename': entry['source']['filename'],
                      'archive_sha256': entry['archive_sha256'], 'provenance': entry['provenance']['kind']}, sort_keys=True))


if __name__ == '__main__':
    main()
