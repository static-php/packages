#!/usr/bin/env python3
"""Describe frozen consumer source versions before any PHP compilation."""

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import tarfile
import tempfile
import xml.etree.ElementTree as ET
import zipfile


ALIASES = {
    'brotli': ['BROTLI_EXT_VERSION'], 'lz4': ['LZ4_EXT_VERSION'],
    'snappy': ['SNAPPY_EXT_VERSION'], 'swoole': ['SWOOLE_VERSION'],
    'xhprof': ['XHPROF_VERSION'],
    'xdebug': ['XDEBUG_VERSION'],
    'mysqlnd_ed25519': ['PHP_MARIADB_AUTH_PLUGIN_VERSION'],
    'mysqlnd_parsec': ['PHP_MARIADB_AUTH_PLUGIN_VERSION'],
}
VERSION = re.compile(r'\d+\.\d+(?:\.\d+)?(?:(?:[.-]|~{1,2})?(?:alpha|beta|rc)\d*)?(?:(?:[.-]|~{1,2})?dev\d*)?', re.I)
EXCLUDED = {'third_party', 'thirdparty', 'vendor', 'tests', 'test', 'examples', 'bench', 'benchmarks'}


def sibling(filename, name):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(filename))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def read(path):
    return json.loads(Path(path).read_text())


def basename(value):
    if (not isinstance(value, str) or not value or value in ('.', '..') or Path(value).name != value
            or '\\' in value or '\x00' in value):
        raise RuntimeError('A frozen source filename must be a basename')
    return value


def digest_file(path):
    sha1, sha256 = hashlib.sha1(), hashlib.sha256()
    with path.open('rb') as source:
        for block in iter(lambda: source.read(1024 * 1024), b''):
            sha1.update(block)
            sha256.update(block)
    return sha1.hexdigest(), sha256.hexdigest()


def parse_version(value):
    match = VERSION.search(value)
    if match is None:
        raise RuntimeError('Source has no package-orderable runtime version: ' + value)
    return match[0]


class SourceReader:
    def __init__(self, downloads):
        self.downloads = Path(downloads)
        self.identities = {}
        self.contents = {}

    def source(self, record, artifact):
        if (not isinstance(record, dict) or record.get('lock_type') != 'source'
                or not isinstance(record.get('config'), dict)
                or not isinstance(record.get('hash'), str)
                or not re.fullmatch(r'[a-fA-F0-9]{40}', record['hash'])):
            raise RuntimeError('Missing frozen source lock: ' + artifact)
        kind = record.get('cache_type')
        filename = basename(record.get('dirname') if kind == 'git' else record.get('filename'))
        path = self.downloads / filename
        if path.is_symlink():
            raise RuntimeError('Frozen source may not be a symlink: ' + artifact)
        expected = record['hash'].lower()
        key = (path, kind, expected)
        if key in self.identities:
            return path, self.identities[key]
        if kind == 'archive':
            if not path.is_file():
                raise RuntimeError('Missing frozen source archive: ' + artifact)
            sha1, sha256 = digest_file(path)
            if sha1 != expected:
                raise RuntimeError('Frozen source SHA1 differs from its lock: ' + artifact)
            evidence = {'archive': filename, 'archive_sha1': sha1, 'archive_sha256': sha256}
        elif kind == 'git':
            if not path.is_dir():
                raise RuntimeError('Missing frozen source Git tree: ' + artifact)
            command = ['git', '-c', 'safe.directory=' + str(path.resolve()), '-C', str(path)]
            environment = dict(os.environ, GIT_OPTIONAL_LOCKS='0')
            head = subprocess.check_output(command + ['rev-parse', 'HEAD'], env=environment, text=True, stderr=subprocess.PIPE).strip()
            dirty = subprocess.check_output(command + ['status', '--porcelain', '--untracked-files=all', '--ignore-submodules=none'],
                                            env=environment, text=True, stderr=subprocess.PIPE).strip()
            if head.lower() != expected or dirty:
                raise RuntimeError('Frozen source Git tree differs from its lock: ' + artifact)
            evidence = {'git_directory': filename, 'git_commit': head}
        else:
            raise RuntimeError('Unsupported frozen source cache type: ' + str(kind))
        self.identities[key] = evidence
        return path, evidence

    def files(self, path):
        if path in self.contents:
            return self.contents[path]
        result = {}

        def wanted(name):
            parts = PurePosixPath(name).parts
            if any(value in EXCLUDED for value in parts) or 'tools/templates' in name:
                return False
            return name.endswith('.h') or PurePosixPath(name).name in {'package.xml', 'ffi.c'}

        if path.is_dir():
            command = ['git', '-c', 'safe.directory=' + str(path.resolve()), '-C', str(path), 'ls-files', '-z', '--recurse-submodules']
            tracked = subprocess.check_output(command, env=dict(os.environ, GIT_OPTIONAL_LOCKS='0')).decode().split('\x00')
            for name in tracked:
                if not name or not wanted(name):
                    continue
                file = path / name
                if file.is_symlink() or not file.is_file() or file.stat().st_size > 2 * 1024 * 1024:
                    continue
                result[name] = file.read_text(errors='replace')
        elif zipfile.is_zipfile(path):
            with zipfile.ZipFile(path) as archive:
                for member in archive.infolist():
                    if wanted(member.filename) and not member.is_dir() and member.file_size <= 2 * 1024 * 1024:
                        result[member.filename] = archive.read(member).decode(errors='replace')
        else:
            with tarfile.open(path, 'r:*') as archive:
                for member in archive:
                    if wanted(member.name) and member.isfile() and member.size <= 2 * 1024 * 1024:
                        with archive.extractfile(member) as source:
                            result[member.name] = source.read().decode(errors='replace')
        self.contents[path] = result
        return result


def package_xml_versions(files, strict=True):
    result = []
    for name, contents in files.items():
        if PurePosixPath(name).name != 'package.xml':
            continue
        try:
            document = ET.fromstring(contents)
        except ET.ParseError as error:
            if strict:
                raise RuntimeError('Invalid frozen source package.xml: ' + name) from error
            continue
        for node in document.iter():
            node.tag = node.tag.rsplit('}', 1)[-1]
        version = document.findtext('version/release') or document.findtext('release/version')
        if version:
            result.append({'file': name, 'value': parse_version(version.strip())})
    return result


def extension_version(reader, record, component, php_version, bundled=False):
    path, identity = reader.source(record, 'ext-' + component if not bundled else 'php-src')
    files = reader.files(path)
    if bundled:
        files = {name: text for name, text in files.items() if '/ext/' + component + '/' in '/' + name}
        if not files:
            raise RuntimeError('Selected extension has neither a frozen external source nor a bundled PHP source: ' + component)
    macros = ['PHP_' + component.upper().replace('-', '_') + '_VERSION', *ALIASES.get(component, [])]
    expression = re.compile(r'^\s*#\s*define\s+(' + '|'.join(map(re.escape, macros)) + r')\s+([^\n]+)', re.M)
    definitions = {}
    for name, contents in files.items():
        if not name.endswith('.h'):
            continue
        for match in expression.finditer(contents):
            definitions.setdefault(match[1], []).append({'file': name, 'line': match[0].strip(),
                                                         'expression': match[2].split('//', 1)[0].strip()})

    def resolve(value, visited=()):
        value = re.sub(r'\s*/\*.*?\*/\s*$', '', value).strip()
        if re.fullmatch(r'(?:"mysqlnd "\s*)?PHP_VERSION', value):
            return php_version
        literal = re.fullmatch(r'"([^"\n]+)"', value)
        if literal:
            return parse_version(literal[1])
        if value in definitions and value not in visited:
            resolved = {resolve(item['expression'], (*visited, value)) for item in definitions[value]}
            if len(resolved) == 1:
                return next(iter(resolved))
        raise RuntimeError('Unresolved runtime version macro for ' + component + ': ' + value)

    evidence = [{'file': item['file'], 'line': item['line'], 'value': resolve(item['expression'], (macro,))}
                for macro, records in definitions.items() for item in records]
    if not evidence and component == 'ffi' and bundled:
        for name, contents in files.items():
            if PurePosixPath(name).name != 'ffi.c':
                continue
            for match in re.finditer(r'^\s*PHP_VERSION\s*,[^\n]*', contents, re.M):
                evidence.append({'file': name, 'line': match[0].strip(), 'value': php_version})
    versions = {item['value'] for item in evidence}
    xml = package_xml_versions(files, strict=not evidence)
    if not versions:
        evidence = xml
        versions = {item['value'] for item in xml}
    if len(versions) != 1:
        raise RuntimeError('Missing or conflicting actual source runtime versions for ' + component)
    return next(iter(versions)), dict(identity, runtime_version_evidence=evidence, package_xml_evidence=xml)


def php_source(root, metadata, reader, stream):
    downloads = root / 'downloads'
    full = metadata.get('php_versions', {}).get(stream)
    helper = sibling('php-source.py', 'consumer_php_source')
    if helper.version_stream(full) != stream:
        raise RuntimeError('Pinned PHP release differs from its stream')
    index = helper.load_index(downloads)
    entry = index['sources'].get(full)
    if not isinstance(entry, dict):
        raise RuntimeError('Missing immutable PHP source record for ' + full)
    record = entry.get('source')
    path, identity = reader.source(record, 'php-src')
    if (entry.get('archive_sha1') != identity['archive_sha1'] or entry.get('archive_sha256') != identity['archive_sha256']
            or entry.get('stream') != stream or str(record.get('version', '')).lower() != full.lower()):
        raise RuntimeError('Frozen PHP source identities differ from their index')
    actual = helper.header_version(path)
    if actual.lower() != full.lower() or str(entry.get('header_version', '')).lower() != actual.lower():
        raise RuntimeError('Frozen PHP source version header differs from its index')
    return full, record, identity, index


def candidates(root, stream, package_type=None, packages=''):
    root = Path(root)
    downloads = root / 'downloads'
    metadata = read(downloads / '.spc-meta.json')
    cache = read(downloads / '.cache.json')
    reader = SourceReader(downloads)
    php_version, php_record, php_identity, index = php_source(root, metadata, reader, stream)
    profiles = sibling('payload-profile.py', 'consumer_payload_profile')
    profile = profiles.expected_profile(root, stream)
    extension_metadata = profiles.extension_metadata(root)
    overrides = index.get('extensions', {})
    if overrides and (stream not in overrides or not isinstance(overrides[stream], dict)):
        raise RuntimeError('Missing frozen extension source profile for PHP ' + stream)
    overrides = overrides.get(stream, {})
    result = []
    selected = set(filter(None, packages.split(',')))
    names = {'frankenphp' if name == 'frankenphp' else 'pie-zts' if name == 'pie' else 'php-zts-' + name for name in selected}

    def add(name, version, evidence, component=None):
        if names and name not in names:
            return
        entry = {'php_stream': stream, 'name': name, 'upstream_version': version,
                 'php_version': php_version, 'evidence': evidence}
        if component is not None:
            entry['component'] = component
        result.append(entry)

    for component in profile['shared']:
        if selected and component not in selected:
            continue
        if extension_metadata.get(component, {}).get('addon'):
            continue
        source = overrides.get('ext-' + component, cache.get('ext-' + component, {}).get('source'))
        if component == 'zip' and tuple(map(int, stream.split('.'))) >= (8, 5):
            source = None
        upstream, evidence = extension_version(reader, source or php_record, component, php_version, bundled=source is None)
        add('php-zts-' + component, upstream, evidence, component)
    for sapi in profile['sapis']:
        if selected and sapi not in selected:
            continue
        if sapi == 'frankenphp':
            source = cache.get('frankenphp', {}).get('source')
            _, evidence = reader.source(source, 'frankenphp')
            tag = source.get('version', '')
            upstream = re.sub(r'^[vV](?=\d)', '', tag)
            if VERSION.fullmatch(upstream) is None:
                raise RuntimeError('FrankenPHP source has no frozen release tag')
            add('frankenphp', upstream, dict(evidence, source_tag=tag))
        else:
            add('php-zts-' + sapi, php_version, dict(php_identity, header_version=php_version))
    add('php-zts-devel', php_version, dict(php_identity, header_version=php_version))
    add('php-zts', php_version, dict(php_identity, header_version=php_version))
    if selected and 'pie' not in selected:
        return {'schema': 1, 'candidates': result}
    receipt = read(downloads / 'pie.json')
    phar = downloads / 'pie.phar'
    if phar.is_symlink() or not phar.is_file():
        raise RuntimeError('Missing frozen PIE archive')
    sha256 = digest_file(phar)[1]
    pie_version = receipt.get('version')
    if (receipt.get('schema') != 1 or not isinstance(pie_version, str) or VERSION.fullmatch(pie_version) is None
            or receipt.get('sha256') != sha256 or metadata.get('pie_sha256') != sha256
            or re.sub(r'^[vV](?=\d)', '', str(receipt.get('source_tag', ''))) != pie_version
            or re.sub(r'^[vV](?=\d)', '', str(metadata.get('pie_version', ''))) != pie_version):
        raise RuntimeError('Frozen PIE source version/hash differs from its receipt')
    add('pie-zts', pie_version, {'archive': 'pie.phar', 'archive_sha256': sha256, 'source_tag': receipt.get('source_tag')})
    return {'schema': 1, 'candidates': result}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path('.'))
    parser.add_argument('--stream', required=True)
    parser.add_argument('--type', choices=['rpm', 'deb', 'apk'])
    parser.add_argument('--packages', default='')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    value = candidates(args.root.resolve(), args.stream, args.type, args.packages)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    descriptor, filename = tempfile.mkstemp(prefix='.source-versions-', dir=args.output.parent)
    path = Path(filename)
    try:
        with os.fdopen(descriptor, 'w') as destination:
            destination.write(json.dumps(value, sort_keys=True, indent=2) + '\n')
        path.replace(args.output)
    finally:
        if path.exists():
            path.unlink()


if __name__ == '__main__':
    main()
