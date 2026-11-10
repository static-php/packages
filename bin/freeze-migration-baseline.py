#!/usr/bin/env python3
"""Freeze genuine published packages for native static-to-shared upgrade tests."""

import argparse
import ast
import base64
from concurrent.futures import ThreadPoolExecutor
import gzip
import hashlib
import io
import json
import lzma
import os
from pathlib import Path
import re
import runpy
import shutil
import subprocess
import tarfile
import tempfile
import time
import urllib.parse
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
import zlib


AGENT = 'APT-HTTP/1.3 (2.6.1)'
DEFAULT_PROFILE = 'cli,cgi,fpm,embed,devel,curl,fileinfo,pdo,pdo_sqlite,sqlite3,pgsql,pdo_pgsql,imagick,intl,frankenphp'


class BaselineDownloadError(RuntimeError):
    pass


def fetch(url):
    request = urllib.request.Request(url, headers={'User-Agent': AGENT})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return response.read()
        except (urllib.error.URLError, TimeoutError) as error:
            retry = not isinstance(error, urllib.error.HTTPError) or error.code in (408, 429, 500, 502, 503, 504, 522, 524)
            if not retry:
                raise
            if attempt == 2:
                raise BaselineDownloadError(f'Baseline download failed after 3 attempts: {url}: {error}') from error
            print(f'Baseline download retry {attempt + 1}/2: {url}: {error}', flush=True)
            time.sleep(attempt + 1)


def decompress(data):
    if data.startswith(b'\x1f\x8b'):
        return gzip.decompress(data)
    if data.startswith(b'\xfd7zXZ'):
        return lzma.decompress(data)
    if data.startswith(b'\x28\xb5\x2f\xfd'):
        return subprocess.check_output(['zstd', '-dc'], input=data)
    return data


def paragraphs(text):
    for block in re.split(r'\n\s*\n', text.strip()):
        fields = {}
        for line in block.splitlines():
            if line[:1].isspace():
                continue
            key, separator, value = line.partition(':')
            if separator:
                fields[key] = value.strip()
        if fields:
            yield fields


def rpm_parser():
    path = Path(__file__).resolve().parents[1] / 'bin/createrepo_static'
    tree = ast.parse(path.read_text())
    functions = [node for node in tree.body if isinstance(node, ast.FunctionDef)
                 and node.name in ('parse_rpm_info', 'php_stream')]
    scope = {'os': os, 're': re}
    exec(compile(ast.Module(body=functions, type_ignores=[]), str(path), 'exec'), scope)
    return scope['parse_rpm_info']


def rpm_index(base, architecture, php):
    repomd = fetch(base + 'repodata/repomd.xml')
    root = ET.fromstring(repomd)
    ns = {'r': 'http://linux.duke.edu/metadata/repo'}
    metadata = {'repomd.xml': repomd}
    primary = None
    for entry in root.findall('r:data', ns):
        href = entry.find('r:location', ns).get('href')
        content = fetch(urllib.parse.urljoin(base, href))
        checksum = entry.find('r:checksum', ns)
        if hashlib.new(checksum.get('type'), content).hexdigest() != checksum.text:
            raise RuntimeError('Published RPM metadata checksum mismatch: ' + href)
        metadata[Path(href).name] = content
        if entry.get('type') == 'primary':
            primary = ET.fromstring(decompress(content))
    if primary is None:
        raise RuntimeError('Published RPM repository has no primary metadata')
    ns = {'c': 'http://linux.duke.edu/metadata/common'}
    parse = rpm_parser()
    packages = []
    for entry in primary.findall('c:package', ns):
        href = entry.find('c:location', ns).get('href')
        parsed = parse(Path(href).name)
        if not parsed or parsed[4] not in (php, 'common', 'libraries'):
            continue
        arch = entry.findtext('c:arch', namespaces=ns)
        if arch not in (architecture, 'noarch'):
            continue
        version = entry.find('c:version', ns)
        checksum = entry.find('c:checksum', ns)
        rpm_ns = {'c': ns['c'], 'r': 'http://linux.duke.edu/metadata/rpm'}
        requires = [item.get('name') for item in entry.findall('c:format/r:requires/r:entry', rpm_ns)]
        provides = [item.get('name') for item in entry.findall('c:format/r:provides/r:entry', rpm_ns)]
        packages.append({'name': entry.findtext('c:name', namespaces=ns),
                         'version': version.get('ver') + '-' + version.get('rel'),
                         'epoch': version.get('epoch', '0'), 'architecture': arch,
                         'requires': requires, 'provides': provides,
                         'publication_url': urllib.parse.urljoin(base, href),
                         'published_checksum': {'algorithm': checksum.get('type'), 'value': checksum.text}})
    return packages, metadata, []


def deb_index(base, architecture):
    packages = []
    metadata = {}
    for index_arch in dict.fromkeys((architecture, 'all')):
        relative = 'dists/php-zts/main/binary-' + index_arch + '/Packages'
        try:
            data = fetch(base + relative)
        except urllib.error.HTTPError as error:
            if index_arch == 'all' and index_arch != architecture and error.code == 404:
                error.close()
                continue
            raise
        metadata['Packages' if index_arch == architecture else 'Packages-all'] = data
        for fields in paragraphs(data.decode()):
            if fields.get('Architecture') not in (architecture, 'all'):
                continue
            names = lambda value: re.findall(r'\b(?:(?:lib)?php-zts-[A-Za-z0-9+_.-]+|frankenphp|pie-zts)\b', value)
            packages.append({'name': fields['Package'], 'version': fields['Version'],
                             'architecture': fields['Architecture'],
                             'requires': names(fields.get('Depends', '') + ',' + fields.get('Pre-Depends', '') + ',' + fields.get('Recommends', '')),
                             'provides': names(fields.get('Provides', '')),
                             'publication_url': urllib.parse.urljoin(base, fields['Filename']),
                             'published_checksum': {'algorithm': 'sha256', 'value': fields['SHA256']}})
    key = fetch(base + 'repository.key')
    return packages, metadata, [('repository.asc', key)]


def apk_index(base, architecture):
    data = fetch(base + architecture + '/APKINDEX.tar.gz')
    with gzip.GzipFile(fileobj=io.BytesIO(data)) as stream:
        with tarfile.open(fileobj=stream, mode='r|', ignore_zeros=True) as archive:
            text = next(archive.extractfile(member).read().decode() for member in archive
                        if member.name == 'APKINDEX')
    packages = []
    for fields in paragraphs(text):
        if fields.get('A') not in (architecture, 'noarch'):
            continue
        filename = fields['P'] + '-' + fields['V'] + '.apk'
        dependency_names = lambda value: [re.split(r'[<>=~]', item, 1)[0] for item in value.split() if not item.startswith('!')]
        packages.append({'name': fields['P'], 'version': fields['V'], 'architecture': fields['A'],
                         'requires': dependency_names(fields.get('D', '')),
                         'provides': dependency_names(fields.get('p', '')),
                         'publication_url': base + architecture + '/' + filename,
                         'published_checksum': {'algorithm': 'apk-control-sha1', 'value': fields['C']}})
    key_url = base.split('/alpine/')[0] + '/alpine/key'
    return packages, {'APKINDEX.tar.gz': data}, [('repository.rsa.pub', fetch(key_url))]


def apk_control_checksum(data):
    remaining = data
    while remaining:
        member = zlib.decompressobj(16 + zlib.MAX_WBITS)
        uncompressed = member.decompress(remaining) + member.flush()
        if not member.eof:
            raise RuntimeError('Truncated APK gzip member')
        consumed = len(remaining) - len(member.unused_data)
        with tarfile.open(fileobj=io.BytesIO(uncompressed), mode='r:') as archive:
            if any(item.name == '.PKGINFO' for item in archive):
                return 'Q1' + base64.b64encode(hashlib.sha1(remaining[:consumed]).digest()).decode()
        remaining = member.unused_data
    raise RuntimeError('APK has no control member')


def newer(kind, candidate, previous):
    if kind == 'rpm':
        import rpm
        return rpm.labelCompare((candidate.get('epoch', '0'), *candidate['version'].rsplit('-', 1)),
                                (previous.get('epoch', '0'), *previous['version'].rsplit('-', 1))) > 0
    if kind == 'deb':
        result = subprocess.run(['dpkg', '--compare-versions', candidate['version'], 'gt', previous['version']])
        if result.returncode not in (0, 1):
            raise RuntimeError('Native DEB version comparison failed')
        return result.returncode == 0
    versions = [candidate['version'], previous['version']]
    valid = []
    for version in versions:
        result = subprocess.run(['apk', 'version', '--check', version], capture_output=True)
        if result.returncode not in (0, 1):
            raise RuntimeError('Cannot validate published APK version: ' + version)
        valid.append(result.returncode == 0)
    if not all(valid):
        return valid[0] and not valid[1]
    compared = subprocess.check_output(['apk', 'version', '-t', *versions], text=True).strip()
    if compared not in ('<', '=', '>'):
        raise RuntimeError('Native APK version comparison failed')
    return compared == '>'


def latest_packages(kind, inventory):
    latest = {}
    for package in inventory:
        previous = latest.get(package['name'])
        if previous is None or newer(kind, package, previous):
            latest[package['name']] = package
    return latest


def affected_profile(latest, packages, libraries, tls=False):
    names = {'php-zts-cli', 'php-zts-fpm', 'frankenphp'} if packages else set()
    if tls:
        names.update({'php-zts-cli', 'php-zts-curl', 'frankenphp'})
    normalize = lambda name: name.removeprefix('php-zts-').replace('_', '-')
    requested = {normalize('pie-zts' if p == 'pie' else p) for p in packages}
    names.update(name for name in latest if normalize(name) in requested)
    affected = {'php-zts-lib-' + name for name in libraries}
    while affected:
        names.update(affected)
        capabilities = {capability for name in affected for capability in
                        [name, *latest.get(name, {}).get('provides', [])]}
        affected = {name for name, package in latest.items() if name not in names
                    and not name.endswith(('-debuginfo', '-dbgsym'))
                    and capabilities.intersection(package.get('requires', []))}
    if any(not name.startswith('php-zts-lib-') and name != 'php-zts-runtime-layout' for name in names):
        names.update({'php-zts-cli', 'php-zts-fpm', 'frankenphp'})
    return {normalize(name) for name in names}


def select_packages(kind, inventory, profile, include_debug=False, latest=None, defects=None):
    latest = latest_packages(kind, inventory) if latest is None else latest
    def wanted(name):
        if 'all' not in profile:
            return name.removeprefix('php-zts-').replace('_', '-') in profile
        managed = name.startswith('php-zts-') or name in ('php-zts', 'frankenphp', 'pie-zts')
        # Private SDK uploads may precede the consumers that require them.
        if not include_debug and (name.startswith('php-zts-lib-') or name == 'php-zts-runtime-layout'):
            return False
        return managed and (include_debug or not name.endswith(('-debuginfo', '-dbgsym')))
    selected = {name: package for name, package in latest.items() if wanted(name)}
    providers = {}
    for package in latest.values():
        for capability in [package['name'], *package.get('provides', [])]:
            providers.setdefault(capability, {})[package['name']] = package
    pending = list(selected.values())
    while pending:
        package = pending.pop()
        for dependency in package.get('requires', []):
            if not dependency.startswith(('php-zts-', 'libphp-zts-', 'frankenphp', 'pie-zts')):
                continue
            choices = list(providers.get(dependency, {}).values())
            if len(choices) != 1:
                message = 'Missing or ambiguous published managed dependency: ' + dependency
                if defects is None:
                    raise RuntimeError(message)
                defects.append({'package': package['name'], 'dependency': dependency, 'error': message})
                continue
            provider = choices[0]
            if provider['name'] not in selected:
                selected[provider['name']] = provider
                pending.append(provider)
    return selected


def download_package(package, kind, output, stage):
    filename = Path(urllib.parse.urlparse(package['publication_url']).path).name
    destination = output / 'artifacts' / filename
    destination.parent.mkdir(exist_ok=True)
    data = destination.read_bytes() if destination.exists() else fetch(package['publication_url'])
    expected = package['published_checksum']
    checksum = apk_control_checksum(data) if kind == 'apk' else hashlib.new(expected['algorithm'], data).hexdigest()
    if checksum != expected['value']:
        raise RuntimeError('Published package checksum mismatch: ' + package['publication_url'])
    if not destination.exists():
        temporary_file = stage / filename
        temporary_file.write_bytes(data)
        shutil.copy2(temporary_file, destination)
        temporary_file.unlink()
    return dict(package, path=str(destination.relative_to(output)), sha256=hashlib.sha256(data).hexdigest())


def published_inventory(args):
    architecture = ('aarch64' if args.arch == 'arm64' else 'x86_64')
    if args.type == 'deb':
        architecture = 'arm64' if args.arch == 'arm64' else 'amd64'
    owner = args.php.replace('.', '')
    if args.type == 'rpm':
        base = (args.base_url or f'https://rpm.henderkes.com/{architecture}/{args.el}/').rstrip('/') + '/'
        inventory, metadata, keys = rpm_index(base, architecture, args.php)
    else:
        suffix = 'debian/' if args.type == 'deb' else 'alpine/main/php-zts/'
        base = (args.base_url or f'https://git.henderkes.com/api/packages/{owner}/' + suffix).rstrip('/') + '/'
        inventory, metadata, keys = (deb_index if args.type == 'deb' else apk_index)(base, architecture)
        # Capture the repository that real package managers see. Missing archives
        # are old-baseline failures; scanning the registry cannot repair its index.
    return base, inventory, metadata, keys


def snapshot_inventory(args):
    base, inventory, metadata, keys = published_inventory(args)
    return {'schema': 1, 'type': args.type, 'php_stream': args.php, 'architecture': args.arch,
            'base': base, 'inventory': inventory,
            'metadata': {name: base64.b64encode(content).decode() for name, content in metadata.items()},
            'keys': [(name, base64.b64encode(content).decode()) for name, content in keys]}


def unavailable_baseline(args, error):
    architecture = 'aarch64' if args.arch == 'arm64' else 'x86_64'
    base = (getattr(args, 'base_url', None) or
            (f'https://rpm.henderkes.com/{architecture}/{getattr(args, "el", "el8")}/' if args.type == 'rpm' else
             f'https://git.henderkes.com/api/packages/{args.php.replace(".", "")}/' +
             ('debian/' if args.type == 'deb' else 'alpine/main/php-zts/')))
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    result = {'schema': 1, 'type': args.type, 'php_stream': args.php, 'architecture': args.arch,
              'origin': {'publication_url': base, 'real_published': True}, 'packages': [], 'published_packages': [],
              'keys': [], 'defects': [{'error': str(error)}],
              'selection': {'profile': args.profile, 'complete': False,
                            'mode': 'affected' if getattr(args, 'packages', '') or getattr(args, 'library_plan', None) else 'full'},
              'repository': {'publication_url': base, 'metadata': []}}
    (output / 'baseline.json').write_text(json.dumps(result, indent=2) + '\n')
    print('::warning::Published baseline unavailable; validating the replacement: ' + str(error), flush=True)


def freeze(args):
    snapshot = getattr(args, 'inventory', None)
    if snapshot:
        captured = json.loads(snapshot.read_text())
        if (captured.get('schema'), captured.get('type'), captured.get('php_stream'), captured.get('architecture')) != (1, args.type, args.php, args.arch):
            raise RuntimeError('Published inventory snapshot does not match this target')
        if captured.get('error'):
            return unavailable_baseline(args, captured['error'])
        base, inventory = captured['base'], captured['inventory']
        metadata = {name: base64.b64decode(content) for name, content in captured['metadata'].items()}
        keys = [(name, base64.b64decode(content)) for name, content in captured['keys']]
    else:
        try:
            base, inventory, metadata, keys = published_inventory(args)
        except (RuntimeError, ValueError, ET.ParseError, urllib.error.URLError, TimeoutError) as error:
            return unavailable_baseline(args, error)
    profile = {name.strip().removeprefix('php-zts-').replace('_', '-') for name in args.profile.split(',') if name.strip()}
    requested = {name.strip() for name in getattr(args, 'packages', '').split(',') if name.strip()}
    library_plan = getattr(args, 'library_plan', None)
    libraries = json.loads(library_plan.read_text())['libraries'] if library_plan else []
    latest = latest_packages(args.type, inventory)
    if requested or library_plan:
        if library_plan and not libraries:
            raise RuntimeError('Library-only baseline requires planned library updates')
        profile = affected_profile(latest, requested, libraries, tls=getattr(args, 'tls', False))
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='spp-freeze-baseline-') as temporary:
        stage = Path(temporary)
        defects = []
        selected = select_packages(args.type, inventory, profile, args.include_debug, latest=latest, defects=defects)
        for defect in defects:
            print('::warning::Published baseline defect: ' + defect['package'] + ': ' + defect['error'])
        if 'php-zts-cli' not in selected and not library_plan:
            return unavailable_baseline(args, 'Published baseline has no matching CLI package')
        for name, content in metadata.items():
            target = output / 'metadata' / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        key_paths = []
        for name, content in keys:
            target = output / 'keys' / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
            key_paths.append(str(target.relative_to(output)))
        (output / 'artifacts').mkdir(exist_ok=True)
        def download_old(package):
            try:
                return download_package(package, args.type, output, stage)
            except (BaselineDownloadError, urllib.error.URLError, TimeoutError) as error:
                defects.append({'package': package['name'], 'error': str(error)})
                print('::warning::Published archive unavailable: ' + package['name'] + ': ' + str(error), flush=True)
                return None
        with ThreadPoolExecutor(max_workers=2) as downloads:
            packages = [package for package in downloads.map(download_old, selected.values()) if package is not None]
        retained = {package['path'] for package in packages}
        for path in (output / 'artifacts').iterdir():
            if str(path.relative_to(output)) not in retained:
                path.unlink()
        result = {'schema': 1, 'type': args.type, 'php_stream': args.php, 'architecture': args.arch,
                  'origin': {'publication_url': base, 'real_published': True},
                  'packages': packages, 'published_packages': inventory, 'keys': key_paths, 'defects': defects,
                  'selection': {'profile': ','.join(sorted(profile)), 'complete': 'all' in profile and not defects,
                                'mode': 'affected' if requested or library_plan else 'full',
                                'include_debug': args.include_debug,
                                'scope': 'Latest managed packages for this PHP stream and architecture, with managed dependency closure'},
                  'repository': {'publication_url': base, 'metadata': ['metadata/' + name for name in metadata]}}
        cli = next((package for package in packages if package['name'] == 'php-zts-cli'), None)
        planner = runpy.run_path(str(Path(__file__).resolve().parent / 'shared-library-plan'))
        cutoffs = planner['archive_migration_cutoffs'](output / cli['path'], args.type) if cli else None
        if cutoffs is not None:
            normalize_arch = lambda value: {'amd64': 'x86_64', 'arm64': 'aarch64'}.get(value, value)
            if (cutoffs.get('php_stream') != args.php
                    or normalize_arch(cutoffs.get('architecture')) != normalize_arch(args.arch)):
                raise RuntimeError('Published CLI migration metadata has the wrong stream or architecture')
            result['migration_cutoffs'] = {args.php: cutoffs['formats'].get(args.type, {})}
        (output / 'baseline.json').write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps({'baseline': str(output / 'baseline.json'), 'packages': len(packages),
                      'published_inventory': len(inventory)}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--type', choices=('rpm', 'deb', 'apk'), required=True)
    parser.add_argument('--php', choices=('8.2', '8.3', '8.4', '8.5', '8.6'), required=True)
    parser.add_argument('--arch', choices=('x86_64', 'arm64'), required=True)
    parser.add_argument('--el', choices=('el8',), default='el8')
    parser.add_argument('--base-url')
    parser.add_argument('--inventory', type=Path, help='Use the run-pinned published metadata without reading the live indexes')
    parser.add_argument('--profile', default=DEFAULT_PROFILE)
    parser.add_argument('--packages', default='', help='Limit the baseline to these changed consumers and runtime hosts')
    parser.add_argument('--library-plan', type=Path, help='Limit a library-only baseline to affected published consumers and dependencies')
    parser.add_argument('--include-debug', action='store_true', help='Include debug packages with --profile=all')
    parser.add_argument('--tls', action='store_true', help='Include published TLS validation hosts and their dependencies')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    try:
        freeze(args)
    except Exception as error:
        message = f'{args.type.upper()} PHP {args.php} / {args.arch}: baseline capture failed: {error}'
        print('::error::' + message.replace('%', '%25').replace('\r', '%0D').replace('\n', '%0A'))
        raise SystemExit(1)


if __name__ == '__main__':
    main()
