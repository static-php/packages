#!/usr/bin/env python3
"""Install a build in fresh runtime containers using native, local repositories.

Single cell:
  python3 tests/shared-runtime-containers.py --os alma8 --php 8.4 \
      --artifacts /path/to/dist/rpm --manifest /path/to/buildroot/shared-libraries.json
Full matrix (directories are <root>/<os>/<php>/dist/<type>):
  python3 tests/shared-runtime-containers.py --matrix-root /path/to/results

No builder directories are mounted. All Docker resources and scratch files created
here are removed in finally blocks, including on SIGINT/SIGTERM. --report is the
only persistent output. Fixtures test package-manager transactions independently of
the availability of older real packages; they do not replace real payload checks.
"""

import argparse
import gzip
import grp
import hashlib
import http.client
import io
import json
import os
from pathlib import Path
import re
import runpy
import pwd
import shutil
import signal
import socket
import struct
import subprocess
import sys
import tarfile
import tempfile
import time
import uuid


BASES = {
    'alpine': ('apk', 'alpine:3.21'),
    'debian': ('deb', 'debian:11'),
    'alma8': ('rpm', 'almalinux:8'),
    'alma9': ('rpm', 'almalinux:9'),
    'alma10': ('rpm', 'almalinux:10'),
}
FULL_BASES = ('alma8', 'debian', 'alpine')
PHP_VERSIONS = ('8.2', '8.3', '8.4', '8.5', '8.6')
HERE = Path(__file__).resolve().parent
ARCHITECTURE = runpy.run_path(str(HERE / 'container-architecture.py'))
WARNING = re.compile(r'Unable to (?:load dynamic library|initialize module)|PHP Startup:|symbol lookup error|(?:module|extension|Cannot load).*already loaded', re.I)


def run(command, *, check=True, cwd=None, env=None, input=None):
    command = [str(x) for x in command]
    if command[0] == 'docker':
        command[0] = os.environ.get('CONTAINER_ENGINE', 'docker')
    process = subprocess.Popen(command, cwd=cwd, env=env,
                               stdin=subprocess.PIPE if input is not None else None,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               universal_newlines=True)
    try:
        stdout, stderr = process.communicate(input=input)
    except BaseException:
        # Finish the Docker client before removing its registered container;
        # otherwise an interrupted pull can create it after cleanup returns.
        if process.poll() is None:
            process.terminate()
        try:
            process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()
        raise
    result = subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
    if check and result.returncode:
        raise RuntimeError(f"Command failed ({result.returncode}): {' '.join(map(str, command))}\n{result.stdout}{result.stderr}")
    return result


def apk_control(path):
    with gzip.open(path, 'rb') as stream, tarfile.open(fileobj=stream, mode='r|', ignore_zeros=True) as archive:
        for member in archive:
            if member.name == '.PKGINFO':
                return archive.extractfile(member).read().decode()
    raise RuntimeError('APK archive has no .PKGINFO metadata: ' + str(path))


def metadata(kind, path):
    if kind == 'deb':
        return run(['dpkg-deb', '-f', path, 'Package']).stdout.strip()
    if kind == 'rpm':
        return run(['rpm', '-qp', '--nosignature', '--qf', '%{NAME}', path]).stdout.strip()
    info = apk_control(path)
    return re.search(r'^pkgname\s*=\s*(\S+)', info, re.M)[1]


def parse_builtin_provides(kind, output):
    features = set()
    entries = output.split(',') if kind == 'deb' else output.splitlines()
    metadata_names = {'cli', 'cgi', 'fpm', 'embed', 'embedded', 'devel', 'common', 'all'}
    for entry in entries:
        entry = re.sub(r'^Provides:\s*', '', entry.strip(), flags=re.I)
        if not entry or entry.lower().endswith('provides:'):
            continue
        for value in entry.split() if kind == 'apk' else [entry]:
            match = re.match(r'^(php-zts(?:-[a-z0-9_+.-]+)?)(?=[\s=(]|$)', value, re.I)
            if not match or not match[1].lower().startswith('php-zts-'):
                continue
            feature = match[1].lower()[len('php-zts-'):]
            private_runtime = re.fullmatch(r'shared-runtime(?:-\d+-abi\d+(?:-(?:(?:alpha|beta|rc)\d+|dev\d*))?-\d+)?', feature)
            if feature not in metadata_names and not feature.startswith('lib-') and private_runtime is None:
                features.add(feature)
    return sorted(features)


def artifact_builtins(kind, path):
    if kind == 'rpm':
        output = run(['rpm', '-qp', '--nosignature', '--provides', path]).stdout
    elif kind == 'deb':
        output = run(['dpkg-deb', '-f', path, 'Provides']).stdout
    else:
        info = apk_control(path)
        output = '\n'.join(re.findall(r'^provides\s*=\s*(\S+)', info, re.M))
    return parse_builtin_provides(kind, output)


def expected_builtins(kind, cli_installed):
    if cli_installed:
        command = {'rpm': ['rpm', '-q', '--provides', 'php-zts-cli'],
                   'deb': ['dpkg-query', '-W', '-f=${Provides}', 'php-zts-cli'],
                   'apk': ['apk', 'info', '--from', 'installed', '--provides', 'php-zts-cli']}[kind]
        features = parse_builtin_provides(kind, run(command).stdout)
    else:
        contract = Path('/spp/cli-builtins.json')
        if not contract.is_file():
            raise RuntimeError('Standalone SAPI validation requires the matching CLI package builtin metadata')
        data = json.loads(contract.read_text())
        features = data.get('features', []) if data.get('schema') == 1 else []
    if not features:
        raise RuntimeError('CLI package metadata advertises no builtin features')
    return features


def index_repo(kind, directory):
    directory = Path(directory)
    if kind == 'deb':
        content = run(['dpkg-scanpackages', '--multiversion', '.', '/dev/null'], cwd=directory).stdout
        (directory / 'Packages').write_text(content)
        (directory / 'Packages.gz').write_bytes(gzip.compress(content.encode()))
    elif kind == 'apk':
        arch = directory / 'x86_64'
        arch.mkdir(exist_ok=True)
        packages = {}
        for package in sorted(directory.glob('*.apk')):
            fields = dict(re.findall(r'^(pkgname|pkgver)\s*=\s*(\S+)', apk_control(package), re.M))
            if any(not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.+~\-]*', fields.get(field, '')) for field in ('pkgname', 'pkgver')):
                raise RuntimeError('APK archive has invalid package name/version: ' + str(package))
            filename = fields['pkgname'] + '-' + fields['pkgver'] + '.apk'
            if filename in packages:
                raise RuntimeError('Duplicate APK package name/version: ' + filename)
            packages[filename] = package
        for filename, package in packages.items():
            shutil.copy2(package, arch / filename)
        run(['apk', 'index', '--allow-untrusted', '-o', arch / 'APKINDEX.tar.gz',
             *[arch / filename for filename in packages]])
    else:
        run(['createrepo_c', '--update', directory])


def configure_repo(kind, directory):
    directory = Path(directory)
    if kind == 'deb':
        Path('/etc/apt/sources.list.d/spp-runtime.list').write_text(
            f'deb [trusted=yes] file:{directory} ./\n')
        run(['apt-get', '-o', 'Dir::Etc::sourcelist=/etc/apt/sources.list.d/spp-runtime.list',
             '-o', 'Dir::Etc::sourceparts=-', '-o', 'APT::Get::List-Cleanup=0', 'update'])
    elif kind == 'apk':
        lines = [line for line in Path('/etc/apk/repositories').read_text().splitlines()
                 if not line.startswith('/spp/')]
        Path('/etc/apk/repositories').write_text('\n'.join([*lines, str(directory)]) + '\n')
        run(['apk', 'update', '--allow-untrusted'])
    else:
        Path('/etc/yum.repos.d/spp-runtime.repo').write_text(
            '[spp-runtime]\nname=SPP temporary runtime test\n'
            f'baseurl=file://{directory}\nenabled=1\ngpgcheck=0\nmodule_hotfixes=1\nmetadata_expire=0\n')
        run(['dnf', '--disablerepo=*', '--enablerepo=spp-runtime', 'makecache', '--refresh'])


def install(kind, names, *, check=True, upgrade=False):
    command = {'deb': ['apt-get', 'install', '-y', '--no-remove', '--no-install-recommends'],
               'apk': ['apk', 'add', '--allow-untrusted', *(['--upgrade'] if upgrade else [])],
               'rpm': ['dnf', 'upgrade' if upgrade else 'install', '-y', '--setopt=install_weak_deps=False']}[kind]
    return run([*command, *names], check=check, env=dict(os.environ, DEBIAN_FRONTEND='noninteractive'))


def installed(kind, name):
    if kind == 'deb':
        query = run(['dpkg-query', '-W', '-f=${db:Status-Abbrev} ${Version}', name], check=False)
        return query.stdout[4:].strip() if query.returncode == 0 and query.stdout.startswith('ii ') else None
    if kind == 'apk':
        query = run(['apk', 'info', '--from', 'installed', '-e', name], check=False)
        if query.returncode:
            return None
        listing = run(['apk', 'list', '--from', 'installed', '--installed', name]).stdout
        version = re.search(r'^' + re.escape(name) + r'-([^\s]+)\s', listing, re.M)
        if not version:
            raise RuntimeError('Cannot read installed APK version for ' + name + ': ' + listing)
        return version[1]
    query = run(['rpm', '-q', '--qf', '%{VERSION}-%{RELEASE}', name], check=False)
    return query.stdout.strip() if query.returncode == 0 else None


def installed_packages(kind):
    command = {
        'deb': ['dpkg-query', '-W', '-f=${Package}\t${db:Status-Abbrev}\n'],
        'rpm': ['rpm', '-qa', '--qf', '%{NAME}\n'],
        'apk': ['apk', 'info', '--from', 'installed'],
    }[kind]
    output = run(command).stdout
    packages = set()
    for line in output.splitlines():
        if kind == 'deb':
            fields = line.split('\t')
            if len(fields) != 2 or not re.fullmatch(r'[uihrp][ncHUFWti][ R]', fields[1]):
                raise RuntimeError('Malformed installed DEB package inventory: ' + repr(line))
            name, status = fields
        else:
            name, status = line, 'ii '
        pattern = r'[A-Za-z0-9.][A-Za-z0-9+_.-]*' if kind == 'apk' else r'[A-Za-z0-9][A-Za-z0-9+_.-]*'
        if not re.fullmatch(pattern, name):
            raise RuntimeError('Malformed installed package name: ' + repr(name))
        if status[1:] == 'i ':
            packages.add(name)
    if not packages:
        raise RuntimeError('Native installed-package inventory is empty')
    return packages


def payload(kind, names):
    command = {'deb': ['dpkg-query', '-L'], 'apk': ['apk', 'info', '--from', 'installed', '-L'], 'rpm': ['rpm', '-ql']}[kind]
    files = set()
    for name in names:
        for value in run([*command, name]).stdout.splitlines():
            if kind == 'apk' and value and not value.startswith('/'):
                value = '/' + value
            path = Path(value)
            if path.is_file():
                files.add(path)
    return sorted(files)


def dependencies(kind, name):
    if kind == 'deb':
        return run(['dpkg-query', '-W', '-f=${Depends}', name]).stdout
    if kind == 'rpm':
        return run(['rpm', '-qR', name]).stdout
    return run(['apk', 'info', '--from', 'installed', '-R', name]).stdout


def assert_elf(kind, names, manifest=None):
    private = Path('/usr/lib/php-zts/lib')
    physical = Path('/usr/lib64/php-zts/lib') if kind == 'rpm' else private
    roots = {private, physical}
    def is_private(path):
        return any(root in path.parents for root in roots)
    elf_count = private_count = 0
    required_by_owner = {}
    manifest_sonames = set()
    soname_owners = {}
    if manifest:
        data = json.loads(Path(manifest).read_text())
        declared = data.get('runtime_libdir', str(private))
        if declared not in ('/usr/lib/php-zts/lib', '/usr/lib64/php-zts/lib'):
            raise RuntimeError('Unsupported private library path in manifest')
        private = Path(declared)
        roots.add(private)
        libraries = data.get('libraries', {})
        if not isinstance(libraries, dict):
            libraries = {entry['id']: entry for entry in libraries}
        for library_id, library in libraries.items():
            manifest_sonames.update(library.get('sonames', []))
            for elf in library.get('elf', {}).values():
                if elf.get('soname'):
                    manifest_sonames.add(elf['soname'])
                    soname_owners[elf['soname']] = 'php-zts-lib-' + library_id
    paths = {}
    for name in names:
        for path in payload(kind, [name]):
            paths[path] = name
        if kind == 'rpm' and name.startswith('php-zts-lib-'):
            provides = run(['rpm', '-q', '--provides', name]).stdout
            if re.search(r'^\S+\.so(?:\.|\()', provides, re.M):
                raise RuntimeError(f'{name}: private library advertises a system SONAME capability')
    for path, owner in sorted(paths.items()):
        if path.is_symlink() or str(path).startswith('/usr/lib/debug/'):
            continue
        with path.open('rb') as stream:
            if stream.read(4) != b'\x7fELF':
                continue
        dynamic = run(['readelf', '-d', path]).stdout
        needed = re.findall(r'\(NEEDED\).*\[([^]]+)\]', dynamic)
        if not needed and not is_private(path):
            if path.parent in (Path('/usr/bin'), Path('/usr/sbin')):
                raise RuntimeError(f'{path}: statically linked executable in shared-library package build')
            continue
        elf_count += 1
        private_needed = {name for name in needed if any((root / name).exists() for root in roots) or name in manifest_sonames}
        if private_needed or is_private(path):
            match = re.search(r'\(RUNPATH\).*\[([^]]+)\]', dynamic)
            if not match or str(private) not in match[1].split(':'):
                raise RuntimeError(f'{path}: missing private DT_RUNPATH {private}')
            private_count += 1
        for match in re.finditer(r'\((?:RUNPATH|RPATH)\).*\[([^]]+)\]', dynamic):
            if any(x in match[1] for x in ('buildroot', '/build/', '/home/', '/tmp/', '/spp/')):
                raise RuntimeError(f'{path}: build path leaked into runtime search path: {match[1]}')
        loaded = run(['ldd', path], check=False)
        output = loaded.stdout + loaded.stderr
        if re.search(r'^\s*\S+\s*=>\s*not found', output, re.M) or 'Error loading shared library' in output:
            raise RuntimeError(f'{path}: unresolved library\n{output}')
        # musl ldd reports PHP host symbols while examining extension DSOs. Their
        # actual load is checked under each SAPI below, after the PHP host exists.
        if loaded.returncode and not (path.suffix == '.so' and not is_private(path) and 'Error relocating' in output):
            raise RuntimeError(f'{path}: ldd failed\n{output}')
        for soname in private_needed:
            expected = private / soname
            permitted = {str(root / soname) for root in roots}
            if not any(re.search(r'(?:=>\s*)?' + re.escape(value) + r'(?:\s|$)', output) for value in permitted):
                raise RuntimeError(f'{path}: {soname} resolved outside {private}\n{output}')
            provider = soname_owners.get(soname)
            if provider and provider != owner:
                if owner not in required_by_owner:
                    required_by_owner[owner] = dependencies(kind, owner)
                required = required_by_owner[owner]
                base = re.search(r'(?<![A-Za-z0-9+_.-])' + re.escape(provider) + r'(?![A-Za-z0-9+_.-])', required)
                if not base or provider + '-abi-' not in required:
                    raise RuntimeError(f'{owner}: {soname} lacks its private package and ABI dependencies on {provider}')
    if not elf_count:
        raise RuntimeError('Installed package set contains no dynamic ELF objects')
    if any(name.startswith('php-zts-lib-') for name in names) and not private_count:
        raise RuntimeError('No private shared libraries verified')
    return {'elf_objects': elf_count, 'private_runpaths': private_count}


def result_json(output, php):
    if WARNING.search(output):
        raise RuntimeError(f'PHP startup failure: {output}')
    start = output.find('{')
    data = json.loads(output[start:])
    if not data.get('ok') or not data['version'].startswith(php + '.'):
        raise RuntimeError(f'Unexpected PHP runtime: {data}')
    if php == '8.6' and data.get('vm') not in {
        'ZEND_VM_KIND_CALL', 'ZEND_VM_KIND_SWITCH', 'ZEND_VM_KIND_GOTO',
        'ZEND_VM_KIND_HYBRID', 'ZEND_VM_KIND_TAILCALL',
    }:
        raise RuntimeError(f'PHP 8.6 reported an invalid Zend VM kind: {data.get("vm")}')
    return data


def extension_inventory(data, sapi, reference=None, builtins=(), disabled=()):
    expected_sapi = {'CLI': 'cli', 'CGI': 'cgi-fcgi', 'FPM': 'fpm-fcgi', 'FrankenPHP': 'frankenphp'}.get(sapi)
    if expected_sapi and data.get('sapi') != expected_sapi:
        raise RuntimeError(f'{sapi}: expected SAPI {expected_sapi}, got {data.get("sapi")}')
    if not isinstance(data.get('ini_files'), list) or not isinstance(data.get('extensions'), list):
        raise RuntimeError(f'{sapi}: runtime probe omitted its extension/configuration inventory')
    directory = Path(data['extension_dir'])
    disabled = {name.lower() for name in disabled}
    registered = {value.lower() for value in data['extensions']}
    mapped = {Path(path).resolve() for path in data.get('loaded_objects', [])}
    active = {}
    pattern = re.compile(r'''^\s*(zend_extension|extension)\s*=\s*(?:"([^"]+)"|'([^']+)'|([^;\s#]+))\s*(?:[;#].*)?$''', re.I)
    for filename in data['ini_files']:
        path = Path(filename)
        if not path.is_file():
            raise RuntimeError(f'{sapi}: scanned configuration file is missing: {path}')
        for line in path.read_text().splitlines():
            match = pattern.match(line)
            if not match:
                continue
            value = next(value for value in match.groups()[1:] if value is not None)
            if not value.endswith('.so'):
                value += '.so'
            extension = Path(value) if value.startswith('/') else directory / value
            if not extension.is_file():
                raise RuntimeError(f'{sapi}: active extension directive points to missing {extension}')
            active[extension.resolve()] = match[1].lower() == 'zend_extension'
    expected = {}
    for path in directory.glob('*.so'):
        real = path.resolve()
        short = re.sub(r'-(?:zts|nts)[0-9.]*-\d{2,}$', '', path.stem)
        # SPC's only shared module display-name alias; addon switches such as
        # mbregex and swoole hooks do not produce independent extension DSOs.
        module = 'zend opcache' if short.lower() == 'opcache' else short.lower()
        if module in disabled:
            if real in active or module in registered or real in mapped:
                raise RuntimeError(f'{sapi}: intentionally disabled extension {module} became active')
            continue
        if real not in active:
            raise RuntimeError(f'{sapi}: {path} has no active directive in PHP\'s loaded configuration')
        expected[real] = (module, active[real])
    if disabled & registered:
        raise RuntimeError(f'{sapi}: intentionally disabled extensions registered: {sorted(disabled & registered)}')
    server_module = {'CGI': 'cgi-fcgi', 'FPM': 'cgi-fcgi', 'FrankenPHP': 'frankenphp'}.get(sapi)
    if server_module:
        if server_module not in registered:
            raise RuntimeError(f'{sapi}: its server module did not register {server_module}')
        registered.remove(server_module)
    zend = {value.lower() for value in data.get('zend_extensions', [])}
    aliases = {'opcache': 'zend opcache', 'mbregex': 'mbstring',
               'swoole-hook-mysql': 'swoole', 'swoole-hook-pgsql': 'swoole',
               'swoole-hook-sqlite': 'swoole', 'swoole-hook-odbc': 'swoole'}
    expected_modules = {aliases.get(feature, feature) for feature in builtins if feature != 'password-argon2'}
    missing = sorted(expected_modules - registered)
    if missing:
        raise RuntimeError(f'{sapi}: advertised builtin modules missing: {missing}')
    if 'opcache' in builtins and 'zend opcache' not in zend:
        raise RuntimeError(f'{sapi}: advertised builtin Zend OPcache did not register as a Zend extension')
    for feature in ('mbregex', 'password-argon2'):
        if feature in builtins and data.get('features', {}).get(feature) is not True:
            raise RuntimeError(f'{sapi}: advertised builtin feature unavailable: {feature}')
    for path, (module, is_zend) in expected.items():
        if module not in registered or (is_zend and module not in zend):
            raise RuntimeError(f'{sapi}: installed extension {path.name} did not register {module}')
        if path not in mapped:
            raise RuntimeError(f'{sapi}: installed extension {path} is not mapped into the PHP process')
    inventory = {'files': set(expected), 'extensions': registered, 'zend_extensions': zend, 'vm': data.get('vm')}
    if reference is not None:
        if inventory['vm'] != reference.get('vm'):
            raise RuntimeError(f'{sapi}: Zend VM differs from CLI; expected={reference.get("vm")}, actual={inventory["vm"]}')
        for field in ('files', 'extensions', 'zend_extensions'):
            if inventory[field] != reference[field]:
                missing = sorted(str(value) for value in reference[field] - inventory[field])
                extra = sorted(str(value) for value in inventory[field] - reference[field])
                raise RuntimeError(f'{sapi}: {field} differ from CLI; missing={missing}, extra={extra}')
    return inventory


def required_payloads(php, names, kind=None):
    consumers = {name for name in names if not name.startswith('php-zts-lib-') and not name.endswith('-debuginfo')}
    requirements = {
        'php-zts-cli': '/usr/bin/php-zts', 'php-zts-cgi': '/usr/bin/php-cgi-zts',
        'php-zts-fpm': '/usr/sbin/php-fpm-zts', 'frankenphp': '/usr/bin/frankenphp',
        'php-zts-devel': '/usr/bin/phpize-zts',
    }
    for name, filename in requirements.items():
        if name in consumers and not (Path(filename).is_file() and Path(filename).stat().st_mode & 0o111):
            raise RuntimeError(f'{name}: required executable payload missing: {filename}')
    if 'php-zts-devel' in consumers and not (Path('/usr/bin/php-config-zts').is_file() and Path('/usr/bin/php-config-zts').stat().st_mode & 0o111):
        raise RuntimeError('php-zts-devel: required executable payload missing: /usr/bin/php-config-zts')
    cli_consumers = consumers - {'frankenphp', 'php-zts-embed'}
    if cli_consumers and not Path('/usr/bin/php-zts').is_file():
        raise RuntimeError('Consumer installation did not resolve php-zts-cli: ' + ', '.join(sorted(cli_consumers)))
    embeds = []
    if 'php-zts-embed' in consumers:
        directories = ['/usr/lib64'] if kind == 'rpm' else ['/usr/lib'] if kind in ('apk', 'deb') else ['/usr/lib', '/usr/lib64']
        embeds = [Path(directory) / ('libphp-zts-' + php.replace('.', '') + '.so') for directory in directories]
        embeds = [path for path in embeds if path.is_file()]
        if not embeds:
            raise RuntimeError('php-zts-embed: required libphp payload missing for PHP ' + php)
    return embeds


def fcgi_request(port, script):
    def record(kind, data=b''):
        return struct.pack('!BBHHBB', 1, kind, 1, len(data), 0, 0) + data
    def pair(key, value):
        key, value = key.encode(), value.encode()
        length = lambda size: bytes([size]) if size < 128 else struct.pack('!I', size | 0x80000000)
        return length(len(key)) + length(len(value)) + key + value
    with socket.create_connection(('127.0.0.1', port), timeout=5) as connection:
        params = {'SCRIPT_FILENAME': str(script), 'SCRIPT_NAME': '/shared-runtime.php',
                  'REQUEST_METHOD': 'GET', 'SERVER_PROTOCOL': 'HTTP/1.1',
                  'SERVER_NAME': 'localhost', 'SERVER_PORT': str(port), 'REDIRECT_STATUS': '200'}
        connection.sendall(record(1, struct.pack('!HB5x', 1, 0))
                           + record(4, b''.join(pair(k, v) for k, v in params.items()))
                           + record(4) + record(5))
        output = bytearray()
        def exact(length):
            value = bytearray()
            while len(value) < length:
                part = connection.recv(length - len(value))
                if not part:
                    raise RuntimeError('FPM closed an incomplete FastCGI response')
                value.extend(part)
            return bytes(value)
        while True:
            version, kind, request_id, length, padding, _ = struct.unpack('!BBHHBB', exact(8))
            body = exact(length)
            exact(padding)
            if kind in (6, 7):
                output.extend(body)
            if kind == 3:
                return output.decode(errors='replace')


def sapi_checks(php, names, kind=None, disabled=()):
    script = Path('/spp/shared-runtime.php')
    binary = Path('/usr/bin/php-zts')
    embed_paths = required_payloads(php, names, kind)
    data = None
    inventory = None
    sapis = []
    builtins = expected_builtins(kind, binary.is_file()) if binary.is_file() or 'frankenphp' in names else []
    if binary.is_file():
        cli = run([binary, script])
        data = result_json(cli.stdout + cli.stderr, php)
        inventory = extension_inventory(data, 'CLI', builtins=builtins, disabled=disabled)
        sapis.append('cli')
    cgi = Path('/usr/bin/php-cgi-zts')
    if 'php-zts-cgi' in names:
        result = run([cgi, '-f', script], env=dict(os.environ, REDIRECT_STATUS='200', REQUEST_METHOD='GET',
                                                SCRIPT_FILENAME=str(script), SCRIPT_NAME='/shared-runtime.php'))
        extension_inventory(result_json(result.stdout + result.stderr, php), 'CGI', inventory, builtins, disabled)
        sapis.append('cgi')
    processes = []
    try:
        fpm = Path('/usr/sbin/php-fpm-zts')
        if 'php-zts-fpm' in names:
            group = grp.getgrgid(pwd.getpwnam('nobody').pw_gid).gr_name
            config = Path('/spp/fpm-test.conf')
            config.write_text('[global]\ndaemonize=no\nerror_log=/proc/self/fd/2\n[www]\n'
                              f'user=nobody\ngroup={group}\nlisten=127.0.0.1:19000\npm=static\npm.max_children=1\n'
                              'catch_workers_output=yes\nclear_env=no\n')
            log = Path('/spp/fpm-test.log').open('w+')
            process = subprocess.Popen([str(fpm), '-F', '-R', '-y', str(config)], stdout=log, stderr=log)
            processes.append((process, log))
            for attempt in range(50):
                try:
                    extension_inventory(result_json(fcgi_request(19000, script), php), 'FPM', inventory, builtins, disabled)
                    break
                except (ConnectionError, OSError):
                    if process.poll() is not None or attempt == 49:
                        log.seek(0)
                        raise RuntimeError('FPM failed to serve: ' + log.read())
                    time.sleep(.2)
            sapis.append('fpm')
        franken = Path('/usr/bin/frankenphp')
        if 'frankenphp' in names:
            log = Path('/spp/franken-test.log').open('w+')
            process = subprocess.Popen([str(franken), 'php-server', '--listen', '127.0.0.1:18080', '--root', '/spp'], stdout=log, stderr=log)
            processes.append((process, log))
            for attempt in range(100):
                try:
                    connection = http.client.HTTPConnection('127.0.0.1', 18080, timeout=5)
                    connection.request('GET', '/shared-runtime.php')
                    response = connection.getresponse()
                    body = response.read().decode()
                    connection.close()
                    if response.status != 200:
                        raise RuntimeError(f'FrankenPHP HTTP {response.status}: {body}')
                    franken_data = result_json(body, php)
                    extension_inventory(franken_data, 'FrankenPHP', inventory, builtins, disabled)
                    if data is None:
                        data = franken_data
                    break
                except (ConnectionError, OSError):
                    if process.poll() is not None or attempt == 99:
                        log.seek(0)
                        raise RuntimeError('FrankenPHP failed to serve: ' + log.read())
                    time.sleep(.2)
            sapis.append('frankenphp')
        for process, log in processes:
            log.flush()
            log.seek(0)
            text = log.read()
            if WARNING.search(text):
                raise RuntimeError(text)
    finally:
        for process, log in processes:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            log.close()
    embed = []
    for library in embed_paths:
        embed_check(library)
        embed.append(str(library))
    return {'sapis': sapis, 'extensions': sorted(data['extensions']) if data else [], 'builtin_features': builtins, 'embed_libraries': embed}


def embed_check(library):
    probe = '''import ctypes
api = ctypes.CDLL(None)
api.php_embed_init.argtypes = [ctypes.c_int, ctypes.POINTER(ctypes.c_char_p)]
api.php_embed_init.restype = ctypes.c_int
api.zend_eval_string.argtypes = [ctypes.c_char_p, ctypes.c_void_p, ctypes.c_char_p]
api.zend_eval_string.restype = ctypes.c_int
api.php_embed_shutdown.argtypes = []
api.php_embed_shutdown.restype = None
argv = (ctypes.c_char_p * 3)(b'spp-embed', b'-n', None)
status = api.php_embed_init(2, argv)
if status != 0:
    raise RuntimeError('php_embed_init failed: ' + str(status))
try:
    status = api.zend_eval_string(b'echo "SPP_EMBED_OK";', None, b'spp-embed-probe')
    if status != 0:
        raise RuntimeError('zend_eval_string failed: ' + str(status))
finally:
    api.php_embed_shutdown()
'''
    with tempfile.TemporaryDirectory(prefix='spp-embed-ini-') as ini:
        environment = dict(os.environ, LD_PRELOAD=str(library), PHPRC='/dev/null', PHP_INI_SCAN_DIR=ini)
        environment.pop('LD_LIBRARY_PATH', None)
        result = run([sys.executable, '-c', probe], env=environment)
    if result.stdout != 'SPP_EMBED_OK' or result.stderr:
        raise RuntimeError('Installed embed API probe failed for ' + str(library) + ': ' + result.stdout + result.stderr)


def development_checks(kind, names):
    phpize = Path('/usr/bin/phpize-zts')
    config = Path('/usr/bin/php-config-zts')
    if 'php-zts-devel' not in names:
        return {'phpize': 'not packaged'}
    required_payloads('8.4', ['php-zts-devel'], kind)
    if not config.exists():
        raise RuntimeError('phpize package did not provide php-config-zts')
    for option in ('--include-dir', '--includes', '--ldflags', '--libs', '--configure-options'):
        output = run([config, option]).stdout
        if any(path in output for path in ('/build/', 'buildroot', '/home/', '/spp/')):
            raise RuntimeError(f'php-config-zts {option} contains build paths: {output}')
    tools = {'deb': ['apt-get', 'install', '-y', '--no-install-recommends', 'gcc', 'make', 'autoconf', 'libc6-dev'],
             'apk': ['apk', 'add', '--no-cache', 'gcc', 'make', 'autoconf', 'musl-dev'],
             'rpm': ['dnf', 'install', '-y', 'gcc', 'make', 'autoconf', 'glibc-devel']}[kind]
    # Install development tooling only after checking the fresh runtime payload.
    run(tools)
    with tempfile.TemporaryDirectory(prefix='spp-phpize-') as scratch:
        scratch = Path(scratch)
        (scratch / 'config.m4').write_text(
            'PHP_ARG_ENABLE([spp_runtime], [spp runtime], [AS_HELP_STRING([--enable-spp-runtime], [runtime test])], [yes])\n'
            'if test "$PHP_SPP_RUNTIME" != "no"; then\n'
            '  PHP_NEW_EXTENSION([spp_runtime], [spp_runtime.c], [$ext_shared])\nfi\n')
        (scratch / 'spp_runtime.c').write_text(
            '#ifdef HAVE_CONFIG_H\n#include "config.h"\n#endif\n#include "php.h"\n'
            'zend_module_entry spp_runtime_module_entry = { STANDARD_MODULE_HEADER, "spp_runtime", NULL, NULL, NULL, NULL, NULL, NULL, "1.0", STANDARD_MODULE_PROPERTIES };\n'
            'ZEND_GET_MODULE(spp_runtime)\n')
        run([phpize], cwd=scratch)
        run(['./configure', '--with-php-config=' + str(config), '--enable-spp-runtime'], cwd=scratch)
        run(['make', '-j2'], cwd=scratch)
        result = run(['/usr/bin/php-zts', '-n', '-d', 'extension=' + str(scratch / 'modules/spp_runtime.so'),
                      '-r', 'if (!extension_loaded("spp_runtime")) { exit(1); } echo "PHPIZE_OK";'])
        if result.stdout != 'PHPIZE_OK' or WARNING.search(result.stderr):
            raise RuntimeError('Installed phpize extension failed to load: ' + result.stdout + result.stderr)
    return {'phpize': 'built and loaded extension'}


def tar_gz(files, *, control=False):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode='w', format=tarfile.PAX_FORMAT) as archive:
        for name, path in files.items():
            if isinstance(path, bytes):
                entry = tarfile.TarInfo(name)
                entry.size = len(path)
                entry.mode = 0o644
                archive.addfile(entry, io.BytesIO(path))
            else:
                entry = archive.gettarinfo(path, arcname=name)
                entry.uid = entry.gid = 0
                entry.uname = entry.gname = 'root'
                entry.mtime = 0
                if entry.isfile():
                    content = path.read_bytes()
                    entry.pax_headers['APK-TOOLS.checksum.SHA1'] = hashlib.sha1(content).hexdigest()
                    archive.addfile(entry, io.BytesIO(content))
                else:
                    archive.addfile(entry)
        content_end = archive.offset
    # APKv2 control members have no end-of-tar records: the gzip members
    # concatenate into one logical tar stream, whose data segment ends it.
    return gzip.compress(buffer.getvalue()[:content_end] if control else buffer.getvalue())


def fixture_package(kind, directory, name, version, files, depends=(), provides=()):
    directory.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='spp-pkg-') as scratch:
        scratch = Path(scratch)
        tree = scratch / 'root'
        tree.mkdir()
        for destination, source in files.items():
            target = tree / destination.lstrip('/')
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
        if kind == 'deb':
            control = tree / 'DEBIAN'
            control.mkdir()
            fields = [f'Package: {name}', f'Version: {version}-1', 'Architecture: amd64',
                      'Maintainer: SPP local runtime test <nobody@localhost>', 'Description: Temporary SPP runtime upgrade fixture']
            if depends:
                fields.append('Depends: ' + ', '.join(depends))
            if provides:
                fields.append('Provides: ' + ', '.join(provides))
            (control / 'control').write_text('\n'.join(fields) + '\n')
            run(['dpkg-deb', '--build', '--root-owner-group', tree, directory / f'{name}_{version}-1_amd64.deb'])
        elif kind == 'apk':
            data = tar_gz({str(path.relative_to(tree)): path for path in sorted(tree.rglob('*'))})
            info = [f'pkgname = {name}', f'pkgver = {version}-r0', 'pkgdesc = SPP local upgrade fixture',
                    'url = https://localhost/', 'arch = x86_64', 'license = MIT', 'origin = spp-runtime-fixture',
                    'builddate = 1', f'size = {sum(path.stat().st_size for path in tree.rglob("*") if path.is_file())}',
                    f'datahash = {hashlib.sha256(data).hexdigest()}']
            info += ['depend = ' + x.replace(' (>= ', '>=').replace(')', '') for x in depends]
            info += ['provides = ' + x for x in provides]
            control = tar_gz({'.PKGINFO': ('\n'.join(info) + '\n').encode()}, control=True)
            (directory / f'{name}-{version}-r0.apk').write_bytes(control + data)
        else:
            top = scratch / 'rpm'
            for part in ('BUILD', 'BUILDROOT', 'RPMS', 'SOURCES', 'SPECS', 'SRPMS'):
                (top / part).mkdir(parents=True)
            spec = top / 'SPECS' / 'fixture.spec'
            fields = [f'Name: {name}', f'Version: {version}', 'Release: 1', 'Summary: SPP local upgrade fixture',
                      'License: MIT', 'BuildArch: x86_64', 'AutoReqProv: no', '%global debug_package %{nil}',
                      '%global _build_id_links none']
            fields += ['Requires: ' + x.replace(' (>= ', ' >= ').replace(')', '') for x in depends]
            fields += ['Provides: ' + x for x in provides]
            fields += ['%description', 'Temporary SPP runtime package fixture.', '%install',
                       f'cp -a {tree}/. %{{buildroot}}/', '%files', *files.keys()]
            spec.write_text('\n'.join(fields) + '\n')
            run(['rpmbuild', '--define', f'_topdir {top}', '--define', f'_tmppath {scratch}', '-bb', spec])
            for package in (top / 'RPMS').rglob('*.rpm'):
                shutil.copy2(package, directory / package.name)


def build_fixtures(kind):
    root = Path('/spp/fixtures')
    root.mkdir()
    private = '/usr/lib64/php-zts/lib' if kind == 'rpm' else '/usr/lib/php-zts/lib'
    source = root / 'fixture.c'
    source.write_text('int spp_fixture(void) { return SPP_VALUE; }\n')
    main = root / 'main.c'
    main.write_text('#include <stdio.h>\nextern int spp_fixture(void);\nint main(void) { printf("%d\\n", spp_fixture()); return 0; }\n')
    binaries = {}
    libraries = {}
    for abi in (1, 2):
        library = root / f'libspprtfixture.so.{abi}'
        run(['gcc', '-shared', '-fPIC', f'-DSPP_VALUE={abi}', source, f'-Wl,-soname,{library.name}',
             f'-Wl,--enable-new-dtags,-rpath,{private}', '-o', library])
        executable = root / f'consumer{abi}'
        run(['gcc', main, library, f'-Wl,--enable-new-dtags,-rpath,{private}', '-o', executable])
        libraries[abi] = {private + '/' + library.name: library}
        binaries[abi] = executable
    static = root / 'static-consumer'
    run(['gcc', '-DSPP_VALUE=1', main, source, '-o', static])
    consumers = ('php-zts-runtime-fixture82', 'php-zts-runtime-fixture86')
    library_name = 'php-zts-lib-runtime-fixture'
    for stage, lib_version, abi, revisions in (
        ('static', None, None, (1, 1)),
        ('shared', '1.0.0', 1, (2, 2)),
        ('compatible', '1.1.0', 1, (2, 2)),
        ('incomplete', '2.0.0', 2, (3, 2)),
        ('complete', '2.0.0', 2, (3, 3)),
    ):
        directory = root / stage
        if abi:
            fixture_package(kind, directory, library_name, lib_version, libraries[abi],
                            provides=[f'{library_name}-abi-{abi}'])
        for name, revision in zip(consumers, revisions):
            consumer_abi = 2 if revision == 3 else 1
            deps = [f'{library_name} (>= {"2.0.0" if consumer_abi == 2 else "1.0.0"})', f'{library_name}-abi-{consumer_abi}'] if revision > 1 else []
            fixture_package(kind, directory, name, f'{revision}.0.0',
                            {'/usr/bin/' + name: binaries[consumer_abi] if revision > 1 else static}, depends=deps)
        index_repo(kind, directory)


def fixture_checks(kind):
    consumers = ['php-zts-runtime-fixture82', 'php-zts-runtime-fixture86']
    library = 'php-zts-lib-runtime-fixture'
    for stage in ('static', 'shared', 'compatible', 'incomplete', 'complete'):
        configure_repo(kind, '/spp/fixtures/' + stage)
        before = {name: installed(kind, name) for name in consumers}
        if stage == 'static':
            install(kind, consumers)
            if installed(kind, library):
                raise RuntimeError('Static fixture unexpectedly installed shared dependency')
        elif stage == 'shared':
            install(kind, consumers, upgrade=True)
            if not installed(kind, library):
                raise RuntimeError('Static-to-shared migration did not resolve its library')
        elif stage == 'compatible':
            install(kind, [library], upgrade=True)
            if '1.1.0' not in (installed(kind, library) or ''):
                raise RuntimeError('Compatible library update was not installed')
            if before != {name: installed(kind, name) for name in consumers}:
                raise RuntimeError('Compatible library update changed consumer packages')
        elif stage == 'incomplete':
            version = '2.0.0-r0' if kind == 'apk' else '2.0.0-1'
            install(kind, [library + '=' + version] if kind in ('deb', 'apk') else [library + '-' + version], check=False)
            if '2.0.0' in (installed(kind, library) or ''):
                raise RuntimeError('Package manager installed an incomplete ABI transition')
            if before != {name: installed(kind, name) for name in consumers}:
                raise RuntimeError('Incomplete ABI transition changed or removed consumers')
        else:
            # A single transaction includes every installed reverse dependency.
            transaction = install(kind, [library, *consumers], upgrade=True)
            if not all('3.0.0' in (installed(kind, name) or '') for name in consumers):
                raise RuntimeError('Complete ABI transition did not upgrade both consumers: '
                                   + repr({name: installed(kind, name) for name in [library, *consumers]})
                                   + '\n' + transaction.stdout + transaction.stderr)
        for name in consumers:
            expected = '2' if stage == 'complete' else '1'
            if run(['/usr/bin/' + name]).stdout.strip() != expected:
                raise RuntimeError(f'{stage}: installed fixture consumer {name} cannot run')
    return {'static_to_shared': True, 'compatible_library_only': True,
            'incomplete_abi_rejected': True, 'complete_abi_cohort': True}


def inside(args):
    kind = args.type
    if args.inside == 'prepare':
        if list(Path('/spp/repo').glob('*.' + kind)):
            index_repo(kind, '/spp/repo')
        if not args.skip_fixtures:
            build_fixtures(kind)
        files = sorted(Path('/spp/repo').glob('*.' + kind))
        names = [metadata(kind, path) for path in files]
        if len(names) != len(set(names)):
            raise RuntimeError('Artifacts contain multiple versions of the same package; provide one build cell')
        Path('/spp/package-names.json').write_text(json.dumps(names))
        for name, path in zip(names, files):
            if name == 'php-zts-cli':
                Path('/spp/cli-builtins.json').write_text(json.dumps({'schema': 1, 'features': artifact_builtins(kind, path)}))
        return
    if os.environ.get('LD_LIBRARY_PATH'):
        raise RuntimeError('LD_LIBRARY_PATH must be unset')
    if args.inside == 'fixtures':
        result = fixture_checks(kind)
    else:
        names = json.loads(Path('/spp/package-names.json').read_text())
        if args.package:
            names = [args.package]
        configure_repo(kind, '/spp/repo')
        install(kind, names)
        # Include dependencies in the ELF audit, not just the explicitly requested package.
        installed_names = installed_packages(kind)
        all_names = [name for name in json.loads(Path('/spp/package-names.json').read_text()) if name in installed_names]
        result = assert_elf(kind, all_names, '/spp/manifest.json' if Path('/spp/manifest.json').exists() else None)
        result.update(sapi_checks(args.php, all_names, kind))
        result.update(development_checks(kind, all_names))
        if args.inside == 'individual':
            if args.package:
                remove = {'deb': ['apt-get', 'remove', '-y'], 'apk': ['apk', 'del'], 'rpm': ['dnf', 'remove', '-y']}[kind]
                run([*remove, args.package])
                if installed(kind, args.package):
                    raise RuntimeError(f'{args.package}: package removal failed')
                install(kind, [args.package])
    print('SPP_RESULT=' + json.dumps(result, sort_keys=True), flush=True)


class DockerRun:
    def __init__(self, directory, identity=None):
        self.directory = directory
        self.identity = identity
        self.prefix = 'spp-runtime-' + uuid.uuid4().hex[:12]
        self.containers = []
        self.images = []
        self.checked_images = set()

    def container(self, image, suffix):
        if self.identity is None:
            self.identity = ARCHITECTURE['engine_identity'](run)
        if image not in self.checked_images and image not in self.images:
            self.checked_images.add(image)
            if run(['docker', 'image', 'inspect', image], check=False).returncode:
                self.images.append(image)
        name = self.prefix + '-' + suffix
        self.containers.append(name)
        run(['docker', 'run', '-d', '--name', name, *ARCHITECTURE['container_options'](self.identity),
             '--mount', f'type=bind,src={self.directory},dst=/spp', image, 'sh', '-c', 'while :; do sleep 60; done'])
        return name

    def exec(self, container, command):
        return run(['docker', 'exec', container, *command])

    def prepare(self, image, kind, helper, fixtures=True):
        name = self.container(image, 'helper' if helper else 'base')
        if kind == 'deb':
            command = ['sh', '-ec', "sed -i 's|deb.debian.org/debian-security|archive.debian.org/debian-security|g' /etc/apt/sources.list && apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends python3 binutils ca-certificates" + (' dpkg-dev' if helper else '') + (' gcc libc6-dev' if helper and fixtures else '')]
        elif kind == 'apk':
            command = ['apk', 'add', '--no-cache', 'python3', 'binutils', 'ca-certificates', *(['gcc', 'musl-dev'] if helper and fixtures else [])]
        else:
            command = ['dnf', 'install', '-y', '--setopt=install_weak_deps=False', 'python3', 'binutils', 'ca-certificates', *(['createrepo_c'] if helper else []), *(['rpm-build', 'gcc', 'glibc-devel'] if helper and fixtures else [])]
        self.exec(name, command)
        if helper:
            self.exec(name, ['python3', '/spp/shared-runtime-containers.py', '--inside', 'prepare', '--type', kind,
                             *([] if fixtures else ['--skip-fixtures'])])
            return None
        tag = self.prefix + ':runtime'
        self.images.append(tag)
        run(['docker', 'commit', name, tag])
        return tag

    def close(self):
        remaining = []
        for container in reversed(self.containers):
            # The helper creates root-owned repository trees in this task's bind
            # mount. Make them removable by the invoking host user before rm.
            run(['docker', 'exec', container, 'chmod', '-R', 'a+rwX', '/spp'], check=False)
            result = run(['docker', 'rm', '-f', container], check=False)
            if result.returncode and 'No such container' not in result.stderr:
                remaining.append(container + ': ' + result.stderr.strip())
        for image in reversed(self.images):
            result = run(['docker', 'image', 'rm', '-f', image], check=False)
            if result.returncode and 'No such image' not in result.stderr:
                remaining.append(image + ': ' + result.stderr.strip())
        return remaining


def cell(os_name, php, artifacts, manifest=None, individual=True, fixtures=True, fixtures_only=False, architecture=None):
    kind, image = BASES[os_name]
    artifacts = Path(artifacts).resolve() if artifacts else None
    if not fixtures_only and (not artifacts or not artifacts.is_dir() or not list(artifacts.glob('*.' + kind))):
        raise RuntimeError(f'{os_name} PHP {php}: no {kind} packages in {artifacts}')
    identity = ARCHITECTURE['engine_identity'](run, architecture)
    directory = Path(tempfile.mkdtemp(prefix='spp-runtime-'))
    directory.chmod(0o755)
    resources = DockerRun(directory, identity)
    result = {'os': os_name, 'php': php, 'base_image': image, **resources.identity}
    try:
        (directory / 'repo').mkdir()
        for package in artifacts.glob('*.' + kind) if artifacts else []:
            shutil.copy2(package, directory / 'repo' / package.name)
        for filename in ('shared-runtime-containers.py', 'shared-runtime.php', 'container-architecture.py'):
            shutil.copy2(HERE / filename, directory / filename)
        if manifest:
            shutil.copy2(manifest, directory / 'manifest.json')
        resources.prepare(image, kind, helper=True, fixtures=fixtures or fixtures_only)
        runtime = resources.prepare(image, kind, helper=False)
        if fixtures_only:
            container = resources.container(runtime, 'fixtures')
            checked = resources.exec(container, ['python3', '/spp/shared-runtime-containers.py', '--inside', 'fixtures', '--type', kind])
            result['upgrade_transactions'] = json.loads(checked.stdout.split('SPP_RESULT=')[-1])
            result['fixtures_only'] = True
            result.pop('php')
            result['ok'] = True
            return result
        names = json.loads((directory / 'package-names.json').read_text())
        consumers = [name for name in names if not name.startswith('php-zts-lib-') and not name.endswith('-debuginfo')]
        if individual:
            result['individual_installs'] = []
            for number, name in enumerate(consumers):
                print(f'{os_name} PHP {php}: independent install {name}', flush=True)
                container = resources.container(runtime, 'individual-' + str(number))
                resources.exec(container, ['python3', '/spp/shared-runtime-containers.py', '--inside', 'individual',
                                          '--type', kind, '--php', php, '--package', name])
                run(['docker', 'rm', '-f', container])
                resources.containers.remove(container)
                result['individual_installs'].append(name)
        print(f'{os_name} PHP {php}: full installation and runtime checks', flush=True)
        container = resources.container(runtime, 'full')
        full = resources.exec(container, ['python3', '/spp/shared-runtime-containers.py', '--inside', 'runtime', '--type', kind, '--php', php])
        result.update(json.loads(full.stdout.split('SPP_RESULT=')[-1]))
        if fixtures:
            print(f'{os_name} PHP {php}: native upgrade transactions', flush=True)
            container = resources.container(runtime, 'fixtures')
            checked = resources.exec(container, ['python3', '/spp/shared-runtime-containers.py', '--inside', 'fixtures', '--type', kind])
            result['upgrade_transactions'] = json.loads(checked.stdout.split('SPP_RESULT=')[-1])
        result['ok'] = True
        return result
    finally:
        remaining = resources.close()
        try:
            shutil.rmtree(directory)
        except OSError as error:
            remaining.append(str(directory) + ': ' + str(error))
        if remaining:
            raise RuntimeError('Runtime validation cleanup failed; remaining resources:\n' + '\n'.join(remaining))


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--os', choices=BASES)
    parser.add_argument('--php', choices=PHP_VERSIONS)
    parser.add_argument('--arch', choices=('x86_64', 'arm64'))
    parser.add_argument('--type', choices=('rpm', 'deb', 'apk'))
    parser.add_argument('--artifacts', type=Path)
    parser.add_argument('--manifest', type=Path)
    parser.add_argument('--matrix-root', type=Path)
    parser.add_argument('--report', type=Path)
    parser.add_argument('--skip-individual', action='store_true', help='Diagnostic shortcut; not a complete validation')
    parser.add_argument('--skip-fixtures', action='store_true', help='Skip transaction fixtures, e.g. after the first PHP stream on this distro')
    parser.add_argument('--fixtures-only', action='store_true', help='Exercise native package upgrade fixtures; --os defaults to all five runtime baselines')
    parser.add_argument('--inside', choices=('prepare', 'individual', 'runtime', 'fixtures'), help=argparse.SUPPRESS)
    parser.add_argument('--package', help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.inside:
        inside(args)
        return 0
    if not args.fixtures_only and not args.matrix_root and not (args.os and args.php and args.artifacts):
        parser.error('Supply --matrix-root or --os, --php, and --artifacts')
    if args.type and args.os and args.type != BASES[args.os][0]:
        parser.error('--type disagrees with --os')
    run(['docker', 'version'])
    def interrupted(signum, frame):
        raise KeyboardInterrupt(f'Interrupted by signal {signum}')
    signal.signal(signal.SIGTERM, interrupted)
    results = []
    def validate(os_name, php, *parameters, **options):
        try:
            results.append(cell(os_name, php, *parameters, architecture=args.arch, **options))
        except Exception as error:
            results.append({'os': os_name, 'php': php, 'ok': False, 'error': str(error)})
            print(str(error), file=sys.stderr, flush=True)
    try:
        if args.fixtures_only:
            for os_name in [args.os] if args.os else FULL_BASES:
                validate(os_name, args.php or '8.4', None, fixtures_only=True)
        elif args.matrix_root:
            for os_name in FULL_BASES:
                kind, _ = BASES[os_name]
                for php in PHP_VERSIONS:
                    location = args.matrix_root / os_name / php
                    manifest = location / 'buildroot/shared-libraries.json'
                    validate(os_name, php, location / 'dist' / kind,
                             manifest if manifest.exists() else None,
                             not args.skip_individual,
                             not args.skip_fixtures and php == PHP_VERSIONS[0])
        else:
            compatibility_smoke = args.os not in FULL_BASES
            validate(args.os, args.php, args.artifacts, args.manifest,
                     not args.skip_individual and not compatibility_smoke,
                     not args.skip_fixtures and not compatibility_smoke)
    finally:
        if args.report:
            args.report.write_text(json.dumps({'cells': results}, indent=2) + '\n')
    print(json.dumps({'cells': results}, indent=2))
    return 0 if all(result['ok'] for result in results) else 1


if __name__ == '__main__':
    sys.exit(main())
