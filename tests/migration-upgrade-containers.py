#!/usr/bin/env python3
"""Upgrade genuine published static packages through an unchanged signed local repo.

The mirror uses a disposable test signing key established before the old install.
Its archives come from the frozen published baseline, never generated fixtures.
Only the report persists; containers, network, signing keys and mirror are temporary.
This validates package transactions, not the production server's signing identity.
"""

import argparse
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import re
import runpy
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import time
import uuid


HERE = Path(__file__).resolve().parent
RUNTIMES = {'alma8': ('rpm', 'almalinux:8'), 'alma9': ('rpm', 'almalinux:9'),
            'alma10': ('rpm', 'almalinux:10'), 'debian': ('deb', 'debian:11'),
            'alpine': ('apk', 'alpine:3.21')}
WARNING = re.compile(r'PHP Startup:|Unable to (?:load dynamic library|initialize module)|symbol lookup error|already loaded', re.I)
NAME = re.compile(r'^[A-Za-z0-9][A-Za-z0-9+_.-]*$')


def run(command, *, check=True, input=None, cwd=None, env=None):
    process = subprocess.Popen([str(x) for x in command], cwd=cwd, env=env,
                               stdin=subprocess.PIPE if input is not None else None,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               universal_newlines=True)
    try:
        stdout, stderr = process.communicate(input)
    except BaseException:
        process.terminate()
        try:
            process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()
        raise
    result = subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
    if check and result.returncode:
        raise RuntimeError('Command failed: {}\n{}{}'.format(' '.join(map(str, command)), stdout, stderr))
    return result


def sha256(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def architecture(value):
    return {'amd64': 'x86_64', 'x86_64': 'x86_64', 'aarch64': 'arm64',
            'arm64': 'arm64', 'noarch': 'all', 'all': 'all'}.get(value, value)


def load_baseline(root, kind, stream, arch):
    root = Path(root).resolve()
    record = json.loads((root / 'baseline.json').read_text())
    if record.get('schema') != 1 or record.get('type') != kind:
        raise RuntimeError('Unsupported or mismatched frozen baseline')
    if record.get('php_stream') != stream or architecture(record.get('architecture')) != architecture(arch):
        raise RuntimeError('Baseline PHP stream/architecture does not match the requested cell')
    origin = record.get('origin', {})
    if not (record.get('real_published') is True or origin.get('real_published') is True):
        raise RuntimeError('Migration requires a real published static baseline, not a fixture')
    if not origin.get('publication_url'):
        raise RuntimeError('Baseline has no published origin URL')
    packages = record.get('packages', [])
    if isinstance(packages, dict):
        packages = [dict(value, name=name) for name, value in packages.items()]
    if not packages:
        raise RuntimeError('Frozen baseline contains no packages')
    checked = []
    for item in packages:
        path = (root / item['path']).resolve()
        if root not in path.parents or not path.is_file():
            raise RuntimeError('Baseline archive escapes its frozen directory')
        if not NAME.fullmatch(item.get('name', '')) or not item.get('version'):
            raise RuntimeError('Baseline archive has no valid package identity')
        if not re.fullmatch('[0-9a-f]{64}', item.get('sha256', '')) or sha256(path) != item['sha256']:
            raise RuntimeError('Frozen published archive checksum mismatch: ' + item['name'])
        if not item.get('publication_url'):
            raise RuntimeError('Frozen archive lacks its publication URL')
        checked.append(dict(item, source=str(path)))
    record['packages'] = checked
    return record


def profile_names(profile):
    values = [x.strip() for x in profile.split(',') if x.strip()]
    names = [x if x in ('php-zts', 'frankenphp') or x.startswith(('php-zts-', 'pie-zts')) else 'php-zts-' + x for x in values]
    if any(not NAME.fullmatch(x) for x in names):
        raise RuntimeError('Invalid package profile')
    return sorted(set(['php-zts-cli'] + names))


def managed(name):
    return name in ('php-zts', 'frankenphp') or name.startswith(('php-zts-', 'pie-zts'))


def apk_alias_config():
    local = HERE / 'apk-runtime-aliases.json'
    return local if local.is_file() else HERE.parent / 'config/apk-runtime-aliases.json'


def apk_aliases():
    data = json.loads(apk_alias_config().read_text())
    if data.get('schema') != 1 or not isinstance(data.get('extensions'), dict):
        raise RuntimeError('Unsupported APK runtime alias configuration')
    result = {}
    for extension, entry in data['extensions'].items():
        suffix = entry.get('suffix', '')
        if not re.fullmatch('[a-z][a-z0-9_]*', extension) or not re.fullmatch('-[a-z][a-z0-9-]*', suffix):
            raise RuntimeError('Invalid APK runtime alias configuration')
        logical = 'php-zts-' + extension
        result[logical] = logical + suffix
    return result


def apk_alias_proofs(stream, packages, aliases=None):
    aliases = apk_aliases() if aliases is None else aliases
    indexed = {p['name']: p for p in packages}
    cli = indexed.get('php-zts-cli', {})
    result = {}
    for logical, physical in aliases.items():
        package = indexed.get(physical)
        if package is None:
            continue
        conflict = '!' + logical + '>0'
        requirements = {value.replace(' ', '') for value in package.get('requires', [])}
        core_requirements = {value.replace(' ', '') for value in cli.get('requires', [])}
        shared = {value for value in package.get('requires', [])
                  if re.fullmatch('php-zts-shared-runtime-' + stream.replace('.', '') + r'-abi\d+-1', value)
                  and value in cli.get('provides', [])}
        if (logical not in package.get('provides', [])
                or any(value.startswith(logical + '=') for value in package.get('provides', []))
                or logical not in package.get('replaces', [])
                or conflict not in requirements or conflict not in core_requirements or not shared):
            raise RuntimeError('APK runtime alias lacks its versionless identity/core-conflict proof: ' + physical)
        result[logical] = {'real_name': physical, 'shared_core_capabilities': sorted(shared),
                           'logical_provide': logical, 'core_conflict': conflict}
    return result


def resolve_profile(baseline, names, aliases=None):
    if baseline.get('type') != 'apk':
        return names
    aliases = apk_aliases() if aliases is None else aliases
    present = {p['name'] for p in baseline['packages']}
    return sorted({aliases[name] if name not in present and aliases.get(name) in present else name for name in names})


def library_old_profile(baseline, incoming_changes, requested):
    packages = {p['name']: p for p in baseline['packages']}
    roots = {name for name in incoming_changes if name.startswith('php-zts-lib-')
             and not name.endswith('-debuginfo') and name in packages}
    if not roots:
        raise RuntimeError('Library-only validation needs an affected provider in the published shared baseline')
    selected = set(requested) | roots
    capabilities = set()
    for name in roots:
        capabilities.update([name] + packages[name].get('provides', []))
    while True:
        added = False
        for name, package in packages.items():
            if name in selected or name.endswith(('-debuginfo', '-dbgsym')):
                continue
            requires = {re.split(r'[\s<>=~]', value, 1)[0] for value in package.get('requires', [])}
            if requires & capabilities:
                selected.add(name)
                if name.startswith('php-zts-lib-'):
                    capabilities.update([name] + package.get('provides', []))
                    added = True
        if not added:
            break
    return sorted(selected)


def all_profiles(baseline, individual=False):
    selection = baseline.get('selection', {})
    if selection.get('profile') != 'all' or selection.get('complete') is not True:
        raise RuntimeError('--profile all requires a complete --profile all frozen baseline, not a representative subset')
    names = sorted({p['name'] for p in baseline['packages']
                    if managed(p['name']) and not p['name'].startswith('php-zts-lib-')
                    and not p['name'].endswith(('-debuginfo', '-dbgsym'))})
    if 'php-zts-cli' not in names:
        raise RuntimeError('Full baseline has no CLI package')
    # Keep VM instrumentation profiles independent while testing ordinary modules together.
    isolated = {'php-zts-xdebug', 'php-zts-pcov', 'php-zts-spx', 'php-zts-xhprof'} & set(names)
    profiles = [{'id': 'common', 'packages': [name for name in names if name not in isolated]}]
    core = {'php-zts', 'php-zts-cli', 'php-zts-cgi', 'php-zts-fpm', 'php-zts-embed', 'php-zts-devel', 'frankenphp'}
    separate = set(names) - core if individual else isolated
    for name in sorted(separate):
        profiles.append({'id': 'independent-' + name, 'packages': sorted({'php-zts-cli', name})})
    if 'php-zts-imagick' in names:
        profiles.append({'id': 'disabled-imagick', 'packages': ['php-zts-cli', 'php-zts-imagick'],
                         'disabled_extension': 'imagick'})
    if baseline.get('type') == 'apk':
        package = next((name for name in ('php-zts-igbinary', 'php-zts-igbinary-shared') if name in names), None)
        if package:
            profiles.append({'id': 'disabled-igbinary', 'packages': ['php-zts-cli', package],
                             'disabled_extension': 'igbinary'})
    return {'profiles': profiles, 'covered_packages': names,
            'isolated_runtime_hook_packages': sorted(isolated),
            'independent_all_extensions': individual}


def candidate_only_profiles(baseline, inventory):
    published = {p['name'] for p in baseline.get('packages', []) + baseline.get('published_packages', [])}
    published.update(inventory.get('published_names', []))
    core = {'php-zts', 'php-zts-cli', 'php-zts-cgi', 'php-zts-fpm', 'php-zts-embed',
            'php-zts-devel', 'php-zts-runtime-layout', 'php-zts-curl', 'php-zts-fileinfo'}
    alias_proofs = inventory.get('aliases', {})
    physical_aliases = {p['real_name'] for p in alias_proofs.values()}
    incoming = {p['name'] for p in inventory.get('incoming', [])}
    names = {p['name'] for p in inventory.get('incoming', [])
                    if p['name'].startswith('php-zts-') and p['name'] not in published | core
                    and p['name'] not in physical_aliases
                    and not p['name'].startswith('php-zts-lib-')
                    and not p['name'].endswith(('-debuginfo', '-dbgsym'))}
    names.update(logical for logical, proof in alias_proofs.items()
                 if logical not in published and proof['real_name'] not in published and proof['real_name'] in incoming)
    return [{'id': 'fresh-' + name, 'packages': [name], 'fresh_package': name} for name in sorted(names)]


class Docker:
    def __init__(self, scratch, arch):
        self.scratch = Path(scratch)
        self.arch = architecture(arch)
        self.token = 'spp-migration-' + uuid.uuid4().hex[:12]
        self.containers, self.images = [], []
        self.network = None

    def check_native(self):
        actual = architecture(run(['docker', 'info', '--format', '{{.Architecture}}']).stdout.strip())
        if actual != self.arch:
            raise RuntimeError('Native {} validation requires a native {} Docker engine; found {}'.format(self.arch, self.arch, actual))

    def start(self, image, role):
        if self.network is None:
            self.network = self.token
            run(['docker', 'network', 'create', self.network])
        if run(['docker', 'image', 'inspect', image], check=False).returncode:
            self.images.append(image)
        name = self.token + '-' + role
        self.containers.append(name)
        command = ['docker', 'run', '-d', '--name', name, '--platform',
                   'linux/amd64' if self.arch == 'x86_64' else 'linux/arm64',
                   '--network', self.network, '--mount', 'type=bind,src={},dst=/spp'.format(self.scratch)]
        if role == 'repo':
            command += ['--network-alias', 'repo']
        run(command + [image, 'sh', '-c', 'while :; do sleep 60; done'])
        return name

    def exec(self, name, command, **kwargs):
        interactive = ['-i'] if kwargs.get('input') is not None else []
        return run(['docker', 'exec'] + interactive + ['-e', 'DEBIAN_FRONTEND=noninteractive', name] + list(command), **kwargs)

    def close(self):
        failures = []
        for name in reversed(self.containers):
            run(['docker', 'exec', name, 'chmod', '-R', 'a+rwX', '/spp'], check=False)
            result = run(['docker', 'rm', '-f', name], check=False)
            if result.returncode and 'No such container' not in result.stderr:
                failures.append(name + ': ' + result.stderr.strip())
        if self.network:
            result = run(['docker', 'network', 'rm', self.network], check=False)
            if result.returncode and 'not found' not in result.stderr:
                failures.append(self.network + ': ' + result.stderr.strip())
        for image in reversed(list(dict.fromkeys(self.images))):
            result = run(['docker', 'image', 'rm', image], check=False)
            if result.returncode and 'No such image' not in result.stderr:
                failures.append(image + ': ' + result.stderr.strip())
        return failures


def prepare(resources, name, kind, helper=False):
    if kind == 'rpm':
        packages = ['python3', 'python3-rpm', 'binutils', 'ca-certificates']
        if helper:
            packages += ['createrepo_c', 'rpm-sign', 'gnupg2', 'python3-pyyaml']
        resources.exec(name, ['dnf', 'install', '-y'] + packages)
    elif kind == 'deb':
        resources.exec(name, ['sh', '-ec', "sed -i 's|deb.debian.org/debian-security|archive.debian.org/debian-security|g' /etc/apt/sources.list; apt-get update"])
        resources.exec(name, ['apt-get', 'install', '-y', '--no-install-recommends', 'python3', 'binutils', 'ca-certificates', 'gnupg'] + (['dpkg-dev', 'apt-utils'] if helper else []))
    else:
        resources.exec(name, ['apk', 'add', '--no-cache', 'python3', 'binutils', 'ca-certificates', 'openssl'] + (['abuild'] if helper else []))


def inspect_package(kind, path):
    path = Path(path)
    details = {}
    if kind == 'rpm':
        value = run(['rpm', '-qp', '--nosignature', '--qf', '%{NAME}\t%{EPOCHNUM}:%{VERSION}-%{RELEASE}\t%{ARCH}', path]).stdout
        name, version, arch = value.split('\t')
    elif kind == 'deb':
        fields = run(['dpkg-deb', '-f', path, 'Package', 'Version', 'Architecture']).stdout
        data = dict(line.split(': ', 1) for line in fields.splitlines() if ': ' in line)
        name, version, arch = data['Package'], data['Version'], data['Architecture']
    else:
        with gzip.open(path, 'rb') as stream, tarfile.open(fileobj=stream, mode='r|', ignore_zeros=True) as archive:
            text = next(archive.extractfile(m).read().decode() for m in archive if m.name == '.PKGINFO')
        data = dict(re.findall(r'^(pkgname|pkgver|arch)\s*=\s*(\S+)', text, re.M))
        name, version, arch = data['pkgname'], data['pkgver'], data['arch']
        details = {key: re.findall(r'^' + field + r'\s*=\s*(.+)$', text, re.M)
                   for key, field in [('requires', 'depend'), ('provides', 'provides'), ('replaces', 'replaces')]}
    return dict(details, name=name, version=version.strip(), architecture=architecture(arch.strip()), path=str(path))


def inspect_directory(kind, directory):
    packages = [inspect_package(kind, p) for p in sorted(Path(directory).glob('*.' + kind))]
    if not packages:
        raise RuntimeError('No package archives in ' + str(directory))
    return packages


def installed(kind):
    if kind == 'rpm':
        text = run(['rpm', '-qa', '--qf', '%{NAME}\t%{EPOCHNUM}:%{VERSION}-%{RELEASE}\n']).stdout
        return dict(line.split('\t', 1) for line in text.splitlines())
    if kind == 'deb':
        text = run(['dpkg-query', '-W', '-f=${Package}\t${db:Status-Abbrev}\t${Version}\n']).stdout
        return {a: c for a, b, c in (line.split('\t', 2) for line in text.splitlines()) if b.startswith('ii ')}
    text = Path('/lib/apk/db/installed').read_text()
    return {dict(line.split(':', 1) for line in block.splitlines() if ':' in line)['P']:
            dict(line.split(':', 1) for line in block.splitlines() if ':' in line)['V']
            for block in text.strip().split('\n\n')}


def newer(kind, old, new):
    if kind == 'rpm':
        import rpm
        def evr(value):
            epoch, value = value.split(':', 1) if ':' in value else ('0', value)
            version, release = value.rsplit('-', 1)
            return epoch, version, release
        return rpm.labelCompare(evr(old), evr(new)) < 0
    if kind == 'deb':
        return run(['dpkg', '--compare-versions', old, 'lt', new], check=False).returncode == 0
    return run(['apk', 'version', '-t', old, new]).stdout.strip() == '<'


def make_keys(kind):
    keys = Path('/spp/keys')
    keys.mkdir(mode=0o700, exist_ok=True)
    if kind == 'apk':
        run(['openssl', 'genrsa', '-out', keys / 'spp-migration.rsa', '2048'])
        run(['openssl', 'rsa', '-in', keys / 'spp-migration.rsa', '-pubout', '-out', keys / 'spp-migration.rsa.pub'])
        return 'spp-migration.rsa.pub'
    home = keys / 'gpg'
    home.mkdir(mode=0o700, exist_ok=True)
    run(['gpg', '--homedir', home, '--batch', '--pinentry-mode', 'loopback', '--passphrase', '',
         '--quick-generate-key', 'SPP disposable migration test', 'rsa2048', 'sign', '0'])
    listing = run(['gpg', '--homedir', home, '--batch', '--with-colons', '--list-keys']).stdout
    fingerprint = next(line.split(':')[9] for line in listing.splitlines() if line.startswith('fpr:'))
    public = run(['gpg', '--homedir', home, '--armor', '--export', fingerprint]).stdout
    (keys / 'repository.asc').write_text(public)
    (keys / 'fingerprint').write_text(fingerprint)
    return fingerprint


def sign(kind, path):
    if kind == 'apk':
        run(['abuild-sign', '-k', '/spp/keys/spp-migration.rsa', path])
        return
    args = ['gpg', '--homedir', '/spp/keys/gpg', '--batch', '--yes', '--pinentry-mode', 'loopback', '--passphrase', '']
    run(args + ['--armor', '--detach-sign', path])


def index_generation(kind, name, stream, arch):
    directory = Path('/spp/repository/generations') / name
    packages = inspect_directory(kind, directory)
    identities = [(p['name'], p['architecture']) for p in packages]
    if len(set(identities)) != len(identities):
        raise RuntimeError('A generation contains duplicate package names/architectures')
    if kind == 'rpm':
        fingerprint = Path('/spp/keys/fingerprint').read_text().strip()
        for package in packages:
            run(['rpmsign', '--define', '_gpg_name ' + fingerprint, '--define', '_gpg_path /spp/keys/gpg',
                 '--define', '__gpg /usr/bin/gpg', '--addsign', package['path']])
        run(['createrepo_c', directory])
        import yaml
        artifacts = ['{}-{}.{}'.format(p['name'], p['version'], 'noarch' if p['architecture'] == 'all' else 'aarch64' if p['architecture'] == 'arm64' else 'x86_64')
                     for p in packages if not p['name'].startswith(('php-zts-lib-', 'php-zts-runtime-layout'))]
        module = {'document': 'modulemd', 'version': 2, 'data': {
            'name': 'php-zts', 'stream': 'static-' + stream, 'version': 1 if name == 'old' else 2,
            'context': 'local', 'arch': 'aarch64' if arch == 'arm64' else 'x86_64',
            'summary': 'SPP migration test', 'description': 'Actual published package migration test',
            'license': {'module': ['MIT']}, 'dependencies': [{'buildrequires': {'platform': ['el8']}}],
            'artifacts': {'rpms': sorted(artifacts)}}}
        modules = directory / 'modules.yaml'
        modules.write_text(yaml.safe_dump(module))
        run(['modifyrepo_c', '--mdtype=modules', modules, directory / 'repodata'])
        sign(kind, directory / 'repodata/repomd.xml')
    elif kind == 'deb':
        text = run(['dpkg-scanpackages', '--multiversion', '.', '/dev/null'], cwd=directory).stdout
        (directory / 'Packages').write_text(text)
        (directory / 'Packages.gz').write_bytes(gzip.compress(text.encode()))
        release = run(['apt-ftparchive', '-o', 'APT::FTPArchive::Release::Origin=SPP migration test', 'release', '.'], cwd=directory).stdout
        (directory / 'Release').write_text(release)
        args = ['gpg', '--homedir', '/spp/keys/gpg', '--batch', '--yes', '--pinentry-mode', 'loopback', '--passphrase', '']
        run(args + ['--clearsign', '--output', directory / 'InRelease', directory / 'Release'])
        sign(kind, directory / 'Release')
        (directory / 'Release.asc').rename(directory / 'Release.gpg')
    else:
        for package in packages:
            canonical = directory / (package['name'] + '-' + package['version'] + '.apk')
            if canonical != Path(package['path']):
                Path(package['path']).rename(canonical)
            package['path'] = str(canonical)
        run(['apk', 'index', '--allow-untrusted', '-o', directory / 'APKINDEX.tar.gz'] + sorted(directory.glob('*.apk')))
        sign(kind, directory / 'APKINDEX.tar.gz')
    return packages


def copy_historical(old, new):
    for source in old.rglob('*'):
        if source.is_file():
            destination = new / source.relative_to(old)
            if not destination.exists():
                destination.parent.mkdir(parents=True, exist_ok=True)
                os.link(source, destination)


def configure_client(kind, stream, arch, runtime):
    if kind == 'rpm':
        el = runtime.replace('alma', 'el')
        url = 'http://repo:8080/{}/{}/'.format('aarch64' if arch == 'arm64' else 'x86_64', el)
        Path('/etc/yum.repos.d/spp-migration.repo').write_text(
            '[spp-migration]\nname=SPP signed migration mirror\nbaseurl={}\nenabled=1\n'
            'gpgcheck=1\nrepo_gpgcheck=1\ngpgkey=http://repo:8080/keys/repository.asc\nmetadata_expire=0\n'.format(url))
        run(['rpm', '--import', '/spp/keys/repository.asc'])
        run(['dnf', 'module', 'enable', '-y', 'php-zts:static-' + stream])
        return '/etc/yum.repos.d/spp-migration.repo', '/spp/keys/repository.asc'
    if kind == 'deb':
        shutil.copy2('/spp/keys/repository.asc', '/etc/apt/trusted.gpg.d/spp-migration.asc')
        Path('/etc/apt/sources.list.d/spp-migration.list').write_text(
            'deb [signed-by=/etc/apt/trusted.gpg.d/spp-migration.asc] http://repo:8080/current/ ./\n')
        run(['apt-get', 'update'])
        return '/etc/apt/sources.list.d/spp-migration.list', '/etc/apt/trusted.gpg.d/spp-migration.asc'
    shutil.copy2('/spp/keys/spp-migration.rsa.pub', '/etc/apk/keys/spp-migration.rsa.pub')
    with open('/etc/apk/repositories', 'a') as output:
        output.write('\nhttp://repo:8080/apk\n')
    run(['apk', 'update'])
    return '/etc/apk/repositories', '/etc/apk/keys/spp-migration.rsa.pub'


def module_state(kind):
    if kind != 'rpm':
        return None
    path = Path('/etc/dnf/modules.d/php-zts.module')
    return path.read_text() if path.exists() else None


def php_probe():
    result = run(['/usr/bin/php-zts', '-r', 'echo json_encode(["version"=>PHP_VERSION,"extensions"=>get_loaded_extensions(),"memory_limit"=>ini_get("memory_limit"),"curl"=>function_exists("curl_init"),"fileinfo"=>function_exists("finfo_open")]);'])
    if WARNING.search(result.stdout + result.stderr):
        raise RuntimeError('PHP startup warnings: ' + result.stderr)
    return json.loads(result.stdout)


def disable_shared_extension(name, ini, probe):
    if not re.fullmatch('[a-z][a-z0-9_]*', name) or name not in {value.lower() for value in probe['extensions']}:
        raise RuntimeError('Requested disabled extension is not loaded in the old profile: ' + name)
    matches = []
    directive = re.compile(r'''^\s*(?:zend_extension|extension)\s*=\s*(?:"([^"]+)"|'([^']+)'|([^;\s#]+))''', re.M)
    for path in (Path(ini).parent / 'conf.d').glob('*.ini'):
        for match in directive.finditer(path.read_text()):
            value = next(v for v in match.groups() if v is not None)
            filename = Path(value).name
            stem = filename[:-3] if filename.endswith('.so') else filename
            short = re.sub(r'-(?:zts|nts)[0-9.]*-\d{2,}$', '', stem)
            if short.lower() == name:
                matches.append(path)
    if len(matches) != 1:
        raise RuntimeError('Requested disabled extension needs one real shared-module INI: ' + name)
    path = matches[0]
    disabled = Path(str(path) + '.disabled')
    tool = Path('/usr/sbin/phpdismod-zts')
    if tool.is_file():
        run([tool, name])
    else:
        path.rename(disabled)
    if path.exists() or not disabled.is_file():
        raise RuntimeError('phpmod did not preserve its .ini.disabled convention')
    with disabled.open('a') as output:
        output.write('\n; SPP user-edited disabled-module configuration\n')
    return {'extension': name, 'enabled_ini': str(path), 'disabled_ini': str(disabled),
            'sha256': sha256(disabled)}


def assert_disabled_extension(state, probe):
    if state is None:
        return
    disabled = Path(state['disabled_ini'])
    if (Path(state['enabled_ini']).exists() or not disabled.is_file()
            or sha256(disabled) != state['sha256']
            or state['extension'] in {value.lower() for value in probe['extensions']}):
        raise RuntimeError('Upgrade changed or re-enabled the user-disabled extension ' + state['extension'])


def ordinary_upgrade(kind):
    if kind == 'rpm':
        return run(['dnf', 'upgrade', '-y'])
    if kind == 'deb':
        run(['apt-get', 'update'])
        return run(['apt', 'upgrade', '-y'], input='\n' * 32)
    run(['apk', 'update'])
    return run(['apk', 'upgrade'])


def assert_upgrade(before, after, candidate, names, replacements=None):
    replacements = replacements or {}
    replaced = set()
    for name in names:
        if name not in before:
            raise RuntimeError('Requested old package was not actually installed: ' + name)
        actual = replacements.get(name, {}).get('real_name', name)
        if actual not in candidate:
            raise RuntimeError('Published package has no migration candidate: ' + name)
        if (after.get(actual) != candidate[actual]['version']
                or (name == actual and after[actual] == before[name])
                or (name != actual and name in after)):
            raise RuntimeError('Ordinary upgrade held back or failed to advance ' + name)
        if actual != name:
            replaced.add(name)
    removed = {name for name in set(before) - set(after) if managed(name) and name not in replaced}
    if removed:
        raise RuntimeError('Ordinary upgrade removed installed packages: ' + ', '.join(sorted(removed)))


def assert_library_upgrade(before, after, incoming):
    updates = {name: package for name, package in incoming.items()
               if name.startswith('php-zts-lib-') and name in before and not name.endswith('-debuginfo')}
    if not updates:
        raise RuntimeError('Library-only validation needs a shared baseline with an affected installed library')
    for name, package in updates.items():
        if after.get(name) != package['version'] or after[name] == before[name]:
            raise RuntimeError('Ordinary upgrade held back library ' + name)
    consumers = {name: version for name, version in before.items()
                 if managed(name) and not name.startswith('php-zts-lib-')}
    for name, version in consumers.items():
        if after.get(name) != version:
            raise RuntimeError('Library-only update changed a PHP consumer: ' + name)
    if any(name not in after for name in before if name.startswith('php-zts-lib-')):
        raise RuntimeError('Library-only update removed an installed private library')
    return sorted(updates)


def assert_partial_upgrade(before, after, incoming, changed, allow_unaffected=False):
    updates = sorted(name for name in changed if name in before and managed(name))
    if not updates and not allow_unaffected:
        raise RuntimeError('No affected installed package in this partial-update profile')
    for name, version in before.items():
        if not managed(name):
            continue
        expected = incoming[name]['version'] if name in updates else version
        if after.get(name) != expected:
            raise RuntimeError('Partial update held back a candidate or changed an unchanged package: ' + name)
    return updates


def validate_library_only_candidates(old, incoming):
    frozen = {p['name']: p for p in old}
    for package in incoming:
        if package['name'].startswith('php-zts-lib-'):
            continue
        previous = frozen.get(package['name'])
        if (package['name'] != 'php-zts-runtime-layout' or previous is None
                or package['version'] != previous['version']
                or sha256(package['path']) != sha256(previous['path'])):
            raise RuntimeError('Library-only candidates may include only libraries and an exact unchanged frozen layout archive')


def validate_partial_candidates(kind, old, incoming):
    frozen = {p['name']: p for p in old}
    changed = []
    for package in incoming:
        if not managed(package['name']):
            raise RuntimeError('Partial candidates must be managed runtime packages')
        previous = frozen.get(package['name'])
        if previous is None:
            changed.append(package['name'])
        elif package['version'] == previous['version']:
            if sha256(package['path']) != sha256(previous['path']):
                raise RuntimeError('Equal-version partial candidate differs from the frozen archive: ' + package['name'])
        elif newer(kind, previous['version'], package['version']):
            changed.append(package['name'])
        else:
            raise RuntimeError('Partial candidate is lower than the frozen version: ' + package['name'])
    return sorted(changed)


def inside(args):
    kind = args.type
    if args.inside != 'index' and (os.environ.get('LD_LIBRARY_PATH') or os.environ.get('LD_PRELOAD')):
        raise RuntimeError('Installed migration checks cannot use developer loader overrides')
    if args.inside == 'index':
        incoming = inspect_directory(kind, Path('/spp/repository/generations/new'))
        partial = args.library_only or args.partial_consumers
        incoming_changes = [p['name'] for p in incoming]
        if partial:
            frozen = inspect_directory(kind, Path('/spp/repository/generations/old'))
            if args.library_only:
                validate_library_only_candidates(frozen, incoming)
            incoming_changes = validate_partial_candidates(kind, frozen, incoming)
        make_keys(kind)
        old = index_generation(kind, 'old', args.php, args.arch)
        if partial:
            incoming_names = {p['name'] for p in incoming}
            for package in old:
                if package['name'] not in incoming_names:
                    source = Path(package['path'])
                    shutil.copy2(source, Path('/spp/repository/generations/new') / source.name)
        new = index_generation(kind, 'new', args.php, args.arch)
        aliases = apk_alias_proofs(args.php, new) if kind == 'apk' else {}
        for item in old + new:
            if item['architecture'] not in (architecture(args.arch), 'all'):
                raise RuntimeError('Package architecture does not match the native cell')
        library = None
        if args.library_artifacts:
            incoming_library = inspect_directory(kind, Path('/spp/library-input'))
            validate_library_only_candidates(new, incoming_library)
            changed_library = validate_partial_candidates(kind, new, incoming_library)
            if not any(name.startswith('php-zts-lib-') for name in changed_library):
                raise RuntimeError('Service lifecycle needs a genuine higher library package revision')
            directory = Path('/spp/repository/generations/library')
            directory.mkdir()
            replacements = {p['name'] for p in incoming_library}
            for package in [p for p in new if p['name'] not in replacements] + incoming_library:
                source = Path(package['path'])
                shutil.copy2(source, directory / source.name)
            library = index_generation(kind, 'library', args.php, args.arch)
            if any(p['architecture'] not in (architecture(args.arch), 'all') for p in library):
                raise RuntimeError('Service library generation has the wrong architecture')
        copy_historical(Path('/spp/repository/generations/old'), Path('/spp/repository/generations/new'))
        if library:
            copy_historical(Path('/spp/repository/generations/new'), Path('/spp/repository/generations/library'))
        published_names = json.loads(Path('/spp/published-names.json').read_text())
        Path('/spp/inventory.json').write_text(json.dumps({'old': old, 'new': new, 'incoming': incoming,
                                                        'incoming_changes': incoming_changes, 'library': library,
                                                        'published_names': published_names, 'aliases': aliases}))
        return
    if args.inside == 'fresh':
        inventory = json.loads(Path('/spp/inventory.json').read_text())
        allowed = {p['fresh_package'] for p in candidate_only_profiles({'packages': inventory['old']}, inventory)}
        if args.fresh_package not in allowed:
            raise RuntimeError('Fresh optional-package profile must name a candidate absent from the published baseline')
        config, key = configure_client(kind, args.php, args.arch, args.runtime)
        command = {'rpm': ['dnf', 'install', '-y'], 'deb': ['apt-get', 'install', '-y'], 'apk': ['apk', 'add']}[kind]
        run(command + [args.fresh_package])
        after = installed(kind)
        candidate = {p['name']: p for p in inventory['new']}
        actual = inventory.get('aliases', {}).get(args.fresh_package, {}).get('real_name', args.fresh_package)
        if (after.get(actual) != candidate[actual]['version']
                or actual != args.fresh_package and args.fresh_package in after):
            raise RuntimeError('Fresh installation selected the wrong optional-package version')
        probe = php_probe()
        if not probe['version'].startswith(args.php + '.') or not probe['curl'] or not probe['fileinfo']:
            raise RuntimeError('Fresh optional-package dependencies selected an incomplete PHP stream')
        if any('php-zts-' + module not in after for module in ('curl', 'fileinfo')):
            raise RuntimeError('Fresh optional-package installation did not resolve default recommended modules')
        list_command = {'rpm': ['rpm', '-ql'], 'deb': ['dpkg-query', '-L'], 'apk': ['apk', 'info', '-L']}[kind]
        files = run(list_command + [actual]).stdout.splitlines()
        extension_dir = Path(run(['/usr/bin/php-zts', '-r', 'echo ini_get("extension_dir");']).stdout).resolve()
        modules = [str(Path('/' + name.lstrip('/')).resolve()) for name in files if name.endswith('.so')
                   and Path('/' + name.lstrip('/')).resolve().parent == extension_dir]
        if not modules:
            raise RuntimeError('Fresh optional extension package owns no installed PHP shared module')
        libraries = sorted(name for name in after if name.startswith('php-zts-lib-'))
        linked = run(['ldd', '/usr/bin/php-zts'], check=False)
        if not libraries or linked.returncode or 'not found' in linked.stdout + linked.stderr or '/php-zts/lib/' not in linked.stdout:
            raise RuntimeError('Fresh optional-package dependencies have an unresolved private library closure')
        state = (sha256(config), sha256(key), module_state(kind))
        runtime = runpy.run_path('/spp/shared-runtime-containers.py')
        smokes = runtime['sapi_checks'](args.php, sorted(after), kind)
        ordinary_upgrade(kind)
        if installed(kind) != after or (sha256(config), sha256(key), module_state(kind)) != state:
            raise RuntimeError('Fresh optional-package unchanged upgrade changed packages/repository/trust/module selection')
        if kind == 'rpm':
            run(['dnf', 'check'])
        elif kind == 'deb' and run(['dpkg', '--audit']).stdout.strip():
            raise RuntimeError('Fresh optional-package installation left an incomplete dpkg transaction')
        print('SPP_MIGRATION_RESULT=' + json.dumps({'ok': True, 'applicable': True,
              'coverage': 'fresh_candidate_only', 'requested_installation': [args.fresh_package],
              'physical_package': actual,
              'installed_versions': {name: version for name, version in after.items() if managed(name)},
              'automatic_libraries': libraries, 'curl_fileinfo_automatic': True,
              'requested_shared_modules': modules,
              'installed_sapi_smokes': smokes, 'second_upgrade_noop': True,
              'repository_and_trust_unchanged': True, 'module_selection_unchanged': True,
              'new_probe': probe}, sort_keys=True))
        return
    state_path = Path('/spp/client-before.json')
    if args.inside == 'old':
        config, key = configure_client(kind, args.php, args.arch, args.runtime)
        names = json.loads(Path('/spp/selected.json').read_text())
        command = {'rpm': ['dnf', 'install', '-y'], 'deb': ['apt-get', 'install', '-y'], 'apk': ['apk', 'add']}[kind]
        run(command + names)
        probe = php_probe()
        if not probe['version'].startswith(args.php + '.'):
            raise RuntimeError('Old installation selected the wrong PHP stream')
        if not probe['curl'] or not probe['fileinfo']:
            raise RuntimeError('Published static CLI lacks expected builtin cURL/Fileinfo')
        if args.library_only or args.partial_consumers:
            closure = run(['ldd', '/usr/bin/php-zts'], check=False)
            if closure.returncode or 'not found' in closure.stdout + closure.stderr or '/php-zts/lib/' not in closure.stdout:
                raise RuntimeError('Partial updates require an already-shared installed PHP baseline')
        php_ini = run(['/usr/bin/php-zts', '-r', 'echo php_ini_loaded_file();']).stdout
        if not php_ini or not Path(php_ini).is_file():
            raise RuntimeError('Installed PHP has no owned php.ini to customize')
        with open(php_ini, 'a') as output:
            output.write('\n; SPP migration configuration preservation\nmemory_limit=321M\n')
        disabled = disable_shared_extension(args.disabled_extension, php_ini, probe) if args.disabled_extension else None
        probe = php_probe()
        assert_disabled_extension(disabled, probe)
        state = {'installed': installed(kind), 'probe': probe, 'config': config, 'config_sha256': sha256(config),
                 'key': key, 'key_sha256': sha256(key), 'module_state': module_state(kind),
                 'php_ini': php_ini, 'php_ini_sha256': sha256(php_ini), 'disabled_extension': disabled}
        state_path.write_text(json.dumps(state))
        return
    before = json.loads(state_path.read_text())
    inventory = json.loads(Path('/spp/inventory.json').read_text())
    candidate = {p['name']: p for p in inventory['new']}
    incoming = {p['name']: p for p in inventory['incoming']}
    changed = inventory['incoming_changes']
    names = [name for name in before['installed'] if managed(name)]
    replacements = {name: proof for name, proof in inventory.get('aliases', {}).items() if name in before['installed']}
    partial = args.library_only or args.partial_consumers
    comparison_names = [name for name in names if name in changed] if partial else names
    if not comparison_names and not args.allow_unaffected_profile:
        raise RuntimeError('No affected installed packages to validate')
    for name in comparison_names:
        actual = replacements.get(name, {}).get('real_name', name)
        if actual not in candidate:
            raise RuntimeError('Published installed package lacks a candidate: ' + name)
        if actual == name and not newer(kind, before['installed'][name], candidate[name]['version']):
            raise RuntimeError('Candidate is not strictly newer: ' + name)
    ordinary_upgrade(kind)
    after = installed(kind)
    if partial:
        actual_updates = assert_partial_upgrade(before['installed'], after, incoming, changed, args.allow_unaffected_profile)
        library_updates = assert_library_upgrade(before['installed'], after, {name: incoming[name] for name in changed}) if args.library_only and actual_updates else []
    else:
        actual_updates = comparison_names
        library_updates = None
        assert_upgrade(before['installed'], after, candidate, names, replacements)
    for field in ('config', 'key', 'php_ini'):
        if sha256(before[field]) != before[field + '_sha256']:
            raise RuntimeError('Upgrade changed existing ' + field)
    if module_state(kind) != before['module_state']:
        raise RuntimeError('Upgrade changed the enabled PHP module stream')
    probe = php_probe()
    assert_disabled_extension(before.get('disabled_extension'), probe)
    if not set(before['probe']['extensions']) <= set(probe['extensions']):
        raise RuntimeError('Upgrade lost enabled PHP extensions')
    if (probe['memory_limit'] != '321M'
            or any(before['probe'][name] and not probe[name] for name in ('curl', 'fileinfo'))):
        raise RuntimeError('Upgrade lost configuration/cURL/Fileinfo')
    for module in ('curl', 'fileinfo'):
        if 'php-zts-' + module not in after:
            raise RuntimeError('Moved module was not installed automatically: ' + module)
    libraries = sorted(name for name in after if name.startswith('php-zts-lib-'))
    if not libraries:
        raise RuntimeError('Shared runtime libraries were not installed automatically')
    linked = run(['ldd', '/usr/bin/php-zts'], check=False)
    if linked.returncode or 'not found' in linked.stdout + linked.stderr or '/php-zts/lib/' not in linked.stdout:
        raise RuntimeError('Installed CLI has an unresolved or non-private library closure')
    ordinary_upgrade(kind)
    if installed(kind) != after:
        raise RuntimeError('Second unchanged ordinary upgrade was not a no-op')
    assert_disabled_extension(before.get('disabled_extension'), php_probe())
    runtime = runpy.run_path('/spp/shared-runtime-containers.py')
    disabled = [before['disabled_extension']['extension']] if before.get('disabled_extension') else []
    sapi_smokes = runtime['sapi_checks'](args.php, sorted(after), kind, disabled=disabled)
    development = runtime['development_checks'](kind, sorted(after)) if 'php-zts-devel' in after else None
    if kind == 'rpm':
        run(['dnf', 'check'])
    elif kind == 'deb':
        audit = run(['dpkg', '--audit'])
        if audit.stdout.strip():
            raise RuntimeError('dpkg reports an incomplete package transaction: ' + audit.stdout)
    result = {'ok': True, 'actual_old_versions': {n: before['installed'][n] for n in names},
              'new_versions': {n: after[replacements.get(n, {}).get('real_name', n)] for n in names},
              'physical_package_replacements': {name: dict(proof, old_version=before['installed'][name],
                  new_version=after[proof['real_name']]) for name, proof in replacements.items()},
              'automatic_libraries': libraries,
              'curl_fileinfo_automatic': True, 'configuration_preserved': True,
              'repository_and_trust_unchanged': True, 'module_selection_unchanged': True,
              'disabled_extension_preserved': before.get('disabled_extension'),
              'enabled_extensions_preserved': True, 'second_upgrade_noop': True,
              'installed_sapi_smokes': sapi_smokes,
              'installed_development_smoke': development,
              'library_only': args.library_only, 'updated_libraries': library_updates,
              'partial_consumers': args.partial_consumers, 'updated_packages': actual_updates,
              'applicable': bool(actual_updates),
              'php_consumer_versions_unchanged': True if args.library_only else None,
              'old_probe': before['probe'], 'new_probe': probe,
              'coverage_limits': ['Managed services require the separate real systemd/OpenRC lifecycle suite.',
                                  'TLS trust and publication failure scenarios require their separate targeted suites.']}
    print('SPP_MIGRATION_RESULT=' + json.dumps(result, sort_keys=True))


def hook_repository(scratch, kind, generation, arch):
    source = Path(scratch) / 'repository/generations' / generation
    if kind != 'apk':
        return source
    root = Path(scratch) / 'hook-repositories' / generation
    target = root / ('aarch64' if arch == 'arm64' else 'x86_64')
    target.mkdir(parents=True, exist_ok=True)
    for path in source.iterdir():
        if path.is_file():
            destination = target / path.name
            if destination.exists():
                if not os.path.samefile(path, destination):
                    raise RuntimeError('Prepared hook repository changed: ' + str(destination))
            else:
                os.link(path, destination)
    return root


def lifetime_hooks(args, scratch, result):
    scratch = Path(scratch)
    key = scratch / 'public-key' / ('spp-migration.rsa.pub' if args.type == 'apk' else 'repository.asc')
    common = ['--type', args.type, '--php-stream', args.php, '--image', RUNTIMES[args.runtime][1], '--key', str(key)]
    repository = hook_repository(scratch, args.type, 'new', args.arch)
    hooks = []
    if args.tls:
        hooks.append(('tls', HERE / 'tls-runtime-containers.py', ['--repo', str(repository)]))
    if args.services:
        hooks.append(('services', HERE / 'runtime-service-containers.py',
                      ['--old-repo', str(hook_repository(scratch, args.type, 'old', args.arch)),
                       '--candidate-repo', str(repository),
                       '--library-repo', str(hook_repository(scratch, args.type, 'library', args.arch))]))
    for name, script, options in hooks:
        report = scratch / (name + '-checks.json')
        checked = run([sys.executable, script, *common, *options, '--output', str(report)], check=False)
        if report.is_file():
            result[name + '_checks'] = json.loads(report.read_text())
        else:
            result[name + '_checks'] = {'state': 'failed', 'error': (checked.stdout + checked.stderr)[-5000:]}
        if (checked.returncode or result[name + '_checks'].get('state') != 'passed'
                or result[name + '_checks'].get('complete') is not True):
            raise RuntimeError(name + ' lifecycle/trust checks failed: ' + (checked.stdout + checked.stderr)[-2500:])


def options(args):
    for name in ('library_only', 'partial_consumers', 'allow_unaffected_profile', 'tls', 'services'):
        if not hasattr(args, name):
            setattr(args, name, False)
    for name in ('disabled_extension', 'library_artifacts', 'fresh_package'):
        if not hasattr(args, name):
            setattr(args, name, None)
    return args


def source_token(path, include_ctime=True):
    value = Path(path).stat()
    result = (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns)
    return result + (value.st_ctime_ns,) if include_ctime else result


def artifact_paths(directory, kind):
    return sorted(Path(directory).resolve().glob('*.' + kind))


class PreparedInputError(RuntimeError):
    pass


class PreparedMirror:
    def __init__(self, args, baseline=None):
        self.args = options(argparse.Namespace(**vars(args)))
        if self.args.services and not self.args.library_artifacts:
            raise RuntimeError('--services requires genuine compatible --library-artifacts')
        self.baseline = baseline if baseline is not None else load_baseline(args.baseline_dir, args.type, args.php, args.arch)
        self.scratch = Path(tempfile.mkdtemp(prefix='spp-migration-upgrade-'))
        self.scratch.chmod(0o755)
        self.resources = Docker(self.scratch, args.arch)
        self.ready = False
        self.closed = False
        self.report = {'native_tool_bootstraps': 0, 'sign_index_runs': 0,
                       'clean_runtime_bootstraps': 0, 'clean_runtime_commits': 0, 'profiles_started': 0}

    def command(self, args):
        value = ['python3', '/spp/migration-upgrade-containers.py', '--type', args.type,
                 '--php', args.php, '--arch', args.arch, '--runtime', args.runtime]
        for name in ('library_only', 'partial_consumers', 'allow_unaffected_profile'):
            if getattr(args, name):
                value.append('--' + name.replace('_', '-'))
        if args.disabled_extension:
            value += ['--disabled-extension', args.disabled_extension]
        if args.fresh_package:
            value += ['--fresh-package', args.fresh_package]
        if self.args.library_artifacts:
            value += ['--library-artifacts', '/spp/library-input']
        return value

    def ledger(self):
        args = self.args
        candidates = artifact_paths(args.artifacts, args.type)
        if not candidates:
            raise RuntimeError('Candidate package directory is empty')
        library = artifact_paths(args.library_artifacts, args.type) if args.library_artifacts else []
        if args.library_artifacts and not library:
            raise RuntimeError('Compatible library package directory is empty')
        records = []
        for item in self.baseline['packages']:
            records.append({'role': 'baseline', 'name': Path(item['source']).name,
                            'path': item['source'], 'sha256': item['sha256']})
        helpers = [Path(__file__), HERE / 'shared-runtime.php', HERE / 'shared-runtime-containers.py']
        if args.type == 'apk':
            helpers.append(apk_alias_config())
        for role, paths in [('candidate', candidates), ('library', library), ('helper', helpers)]:
            for path in paths:
                records.append({'role': role, 'name': path.name, 'path': str(path.resolve()), 'sha256': sha256(path)})
        descriptor = Path(args.baseline_dir).resolve() / 'baseline.json'
        records.append({'role': 'descriptor', 'name': 'baseline.json', 'path': str(descriptor), 'sha256': sha256(descriptor)})
        self.sources = {r['path']: source_token(r['path']) for r in records}
        self.candidates = candidates
        self.library = library
        self.report['ledger'] = {'type': args.type, 'php_stream': args.php, 'architecture': args.arch,
                                 'runtime': args.runtime, 'library_only': args.library_only,
                                 'partial_consumers': args.partial_consumers, 'files': records}
        self.report['ledger_sha256'] = hashlib.sha256(json.dumps(self.report['ledger'], sort_keys=True).encode()).hexdigest()

    def copy_checked(self, source, destination):
        expected = next(r['sha256'] for r in self.report['ledger']['files'] if r['path'] == str(Path(source).resolve()))
        shutil.copy2(source, destination)
        if sha256(destination) != expected:
            raise PreparedInputError('Prepared mirror input changed while copying: ' + str(source))

    def prepare(self):
        if self.ready:
            return
        args, baseline, scratch, resources = self.args, self.baseline, self.scratch, self.resources
        old_by_name = {item['name']: item for item in baseline['packages']}
        resources.check_native()
        self.ledger()
        self.copy_checked(__file__, scratch / 'migration-upgrade-containers.py')
        self.copy_checked(HERE / 'shared-runtime.php', scratch / 'shared-runtime.php')
        self.copy_checked(HERE / 'shared-runtime-containers.py', scratch / 'shared-runtime-containers.py')
        if args.type == 'apk':
            self.copy_checked(apk_alias_config(), scratch / 'apk-runtime-aliases.json')
        (scratch / 'published-names.json').write_text(json.dumps(sorted({p['name'] for p in
            baseline['packages'] + baseline.get('published_packages', [])})))
        repository = scratch / 'repository'
        for generation in ('old', 'new'):
            (repository / 'generations' / generation).mkdir(parents=True)
        for item in baseline['packages']:
            self.copy_checked(item['source'], repository / 'generations/old' / Path(item['source']).name)
        candidates = list(Path(args.artifacts).resolve().glob('*.' + args.type))
        if not candidates:
            raise RuntimeError('Candidate package directory is empty')
        for path in candidates:
            self.copy_checked(path, repository / 'generations/new' / path.name)
        if args.library_artifacts:
            library_input = scratch / 'library-input'
            library_input.mkdir()
            for path in Path(args.library_artifacts).resolve().glob('*.' + args.type):
                self.copy_checked(path, library_input / path.name)
        (repository / 'current').symlink_to('generations/old', target_is_directory=True)
        if args.type == 'rpm':
            parent = repository / ('aarch64' if args.arch == 'arm64' else 'x86_64')
            parent.mkdir()
            (parent / 'el8').symlink_to('../current', target_is_directory=True)
            for el in ('el9', 'el10'):
                (parent / el).symlink_to('el8', target_is_directory=True)
        elif args.type == 'apk':
            parent = repository / 'apk'
            parent.mkdir()
            (parent / ('aarch64' if args.arch == 'arm64' else 'x86_64')).symlink_to('../current', target_is_directory=True)
        kind, image = RUNTIMES[args.runtime]
        helper = resources.start(image, 'repo')
        prepare(resources, helper, kind, helper=True)
        common = ['python3', '/spp/migration-upgrade-containers.py', '--type', kind, '--php', args.php,
                  '--arch', args.arch, '--runtime', args.runtime]
        if args.library_only:
            common.append('--library-only')
        if args.partial_consumers:
            common.append('--partial-consumers')
        if args.allow_unaffected_profile:
            common.append('--allow-unaffected-profile')
        if args.disabled_extension:
            common += ['--disabled-extension', args.disabled_extension]
        if args.library_artifacts:
            common += ['--library-artifacts', '/spp/library-input']
        resources.exec(helper, common + ['--inside', 'index'])
        resources.exec(helper, ['chown', '-R', '{}:{}'.format(os.getuid(), os.getgid()), '/spp/repository'])
        public = scratch / 'public-key'
        public.mkdir()
        keyname = 'spp-migration.rsa.pub' if kind == 'apk' else 'repository.asc'
        resources.exec(helper, ['cp', '/spp/keys/' + keyname, '/spp/public-key/' + keyname])
        resources.exec(helper, ['chmod', '0644', '/spp/public-key/' + keyname])
        native_inventory = json.loads((scratch / 'inventory.json').read_text())
        for package in native_inventory['old']:
            frozen = old_by_name[package['name']]
            native_version = package['version']
            if args.type == 'rpm' and ':' not in frozen['version']:
                native_version = native_version.split(':', 1)[-1]
            if native_version != frozen['version']:
                raise RuntimeError('Frozen archive header differs from recorded published version: ' + package['name'])
        (repository / 'keys').symlink_to('../keys', target_is_directory=True)
        run(['docker', 'exec', '-d', '-w', '/spp/repository', helper, 'python3', '-m', 'http.server', '8080'])
        self.inventory = native_inventory
        self.helper = helper
        self.stock_image = run(['docker', 'inspect', '--format', '{{.Image}}', helper]).stdout.strip()
        if not re.fullmatch('sha256:[0-9a-f]{64}', self.stock_image):
            raise RuntimeError('Cannot pin the native runtime image ID')
        self.report['stock_image_id'] = self.stock_image
        self.report['ledger']['stock_image_id'] = self.stock_image
        self.report['ledger_sha256'] = hashlib.sha256(json.dumps(self.report['ledger'], sort_keys=True).encode()).hexdigest()
        self.report['native_tool_bootstraps'] = 1
        self.report['sign_index_runs'] = 1
        base = resources.start(self.stock_image, 'clean-base')
        prepare(resources, base, kind)
        self.report['clean_runtime_bootstraps'] = 1
        self.client_image = resources.token + ':clean-client'
        resources.images.append(self.client_image)
        run(['docker', 'commit', base, self.client_image])
        self.report['clean_runtime_commits'] = 1
        self.report['clean_client_image_id'] = run(['docker', 'image', 'inspect', '--format', '{{.Id}}', self.client_image]).stdout.strip()
        if not re.fullmatch('sha256:[0-9a-f]{64}', self.report['clean_client_image_id']):
            raise RuntimeError('Cannot pin the clean runtime snapshot image ID')
        self.client_image = self.report['clean_client_image_id']
        removed = run(['docker', 'rm', '-f', base], check=False)
        if removed.returncode:
            raise RuntimeError('Cannot remove clean runtime bootstrap container: ' + removed.stderr)
        if base in resources.containers:
            resources.containers.remove(base)
        # Each client sees sealed generation files; only the current-generation pointer changes.
        self.staged = {str(p): source_token(p, include_ctime=False)
                       for p in (scratch / 'repository/generations').rglob('*') if p.is_file()}
        for name in ('migration-upgrade-containers.py', 'shared-runtime.php', 'shared-runtime-containers.py',
                     'inventory.json', 'published-names.json'):
            self.staged[str(scratch / name)] = source_token(scratch / name, include_ctime=False)
        if args.type == 'apk':
            self.staged[str(scratch / 'apk-runtime-aliases.json')] = source_token(scratch / 'apk-runtime-aliases.json', include_ctime=False)
        self.assert_bound(args)
        self.ready = True

    def assert_bound(self, args):
        for name in ('type', 'php', 'arch', 'runtime', 'library_only', 'partial_consumers'):
            if getattr(args, name) != getattr(self.args, name):
                raise PreparedInputError('Prepared mirror mode/runtime does not match: ' + name)
        for name in ('baseline_dir', 'artifacts', 'library_artifacts'):
            actual, expected = getattr(args, name), getattr(self.args, name)
            if (Path(actual).resolve() if actual else None) != (Path(expected).resolve() if expected else None):
                raise PreparedInputError('Prepared mirror input directory does not match: ' + name)
        if artifact_paths(args.artifacts, args.type) != self.candidates:
            raise PreparedInputError('Prepared mirror candidate archive set changed')
        if args.library_artifacts and artifact_paths(args.library_artifacts, args.type) != self.library:
            raise PreparedInputError('Prepared mirror library archive set changed')
        generation_files = {str(p) for p in (self.scratch / 'repository/generations').rglob('*') if p.is_file()}
        expected_generation = {p for p in getattr(self, 'staged', {}) if '/repository/generations/' in p}
        if expected_generation and generation_files != expected_generation:
            raise PreparedInputError('Prepared signed generation file set changed')
        for path, token in self.sources.items():
            if not Path(path).is_file() or source_token(path) != token:
                raise PreparedInputError('Prepared mirror source bytes/identity changed: ' + path)
        for path, token in getattr(self, 'staged', {}).items():
            if not Path(path).is_file() or source_token(path, include_ctime=False) != token:
                raise PreparedInputError('Prepared signed generation changed: ' + path)

    def select_generation(self, name):
        repository = self.scratch / 'repository'
        replacement = repository / '.next'
        if replacement.is_symlink() or replacement.exists():
            replacement.unlink()
        replacement.symlink_to('generations/' + name, target_is_directory=True)
        replacement.replace(repository / 'current')

    def execute(self, args):
        args = options(args)
        scratch, resources, baseline = self.scratch, self.resources, self.baseline
        repository = scratch / 'repository'
        names = [args.fresh_package] if args.fresh_package else resolve_profile(baseline, profile_names(args.profile))
        result = {'schema': 1, 'ok': False, 'type': args.type, 'php_stream': args.php, 'architecture': args.arch,
                  'runtime': args.runtime, 'baseline_origin': baseline['origin'], 'profile': names,
                  'library_only': args.library_only, 'partial_consumers': args.partial_consumers,
                  'signing_scope': 'Disposable signed local mirror; test key trusted before old installation. Published archive SHA256 verified; production signing/server behavior is not asserted.'}
        client = None
        try:
            self.assert_bound(args)
            allowed = {p['fresh_package'] for p in candidate_only_profiles(baseline, self.inventory)}
            if args.fresh_package and args.fresh_package not in allowed:
                raise RuntimeError('Fresh profile does not name a candidate-only optional package')
            missing = set(names) - {p['name'] for p in baseline['packages']} if not args.fresh_package else set()
            if missing:
                raise RuntimeError('Requested published baseline packages missing: ' + ', '.join(sorted(missing)))
            if args.library_only:
                expanded = library_old_profile(baseline, self.inventory['incoming_changes'], names)
                result['library_profile_expansion'] = sorted(set(expanded) - set(names))
                names = expanded
                result['profile'] = names
            (scratch / 'selected.json').write_text(json.dumps(names))
            before_path = scratch / 'client-before.json'
            if before_path.exists():
                before_path.unlink()
            self.select_generation('new' if args.fresh_package else 'old')
            self.report['profiles_started'] += 1
            result['prepared_mirror'] = {'ledger_sha256': self.report['ledger_sha256'], 'profile_number': self.report['profiles_started']}
            client = resources.start(self.client_image, 'client-' + str(self.report['profiles_started']))
            common = self.command(args)
            if args.fresh_package:
                checked = resources.exec(client, common + ['--inside', 'fresh'])
                result.update(json.loads(checked.stdout.split('SPP_MIGRATION_RESULT=')[-1]))
                return result
            resources.exec(client, common + ['--inside', 'old'])
            before = json.loads((scratch / 'client-before.json').read_text())
            result['old_installation'] = {
                'versions': {name: version for name, version in before['installed'].items() if managed(name)},
                'probe': before['probe'], 'module_state': before['module_state'],
                'repository_sha256': before['config_sha256'], 'key_sha256': before['key_sha256'],
                'custom_php_ini_sha256': before['php_ini_sha256'],
                'disabled_extension': before.get('disabled_extension'),
            }
            self.select_generation('new')
            checked = resources.exec(client, common + ['--inside', 'new'])
            result.update(json.loads(checked.stdout.split('SPP_MIGRATION_RESULT=')[-1]))
            if args.tls or args.services:
                lifetime_hooks(args, scratch, result)
        except BaseException as error:
            result.update(ok=False, error=str(error), interrupted=isinstance(error, (KeyboardInterrupt, SystemExit)),
                          prepared_inputs_invalid=isinstance(error, PreparedInputError))
        finally:
            remaining = []
            if client:
                removed = run(['docker', 'rm', '-f', client], check=False)
                if removed.returncode and 'No such container' not in removed.stderr:
                    remaining.append(client + ': ' + removed.stderr.strip())
                elif client in resources.containers:
                    resources.containers.remove(client)
            result['cleanup'] = {'complete': not remaining, 'remaining': remaining, 'shared_mirror_retained': True}
            if remaining:
                result['ok'] = False
        return result

    def close(self):
        if self.closed:
            return self.report['cleanup']
        remaining = self.resources.close()
        try:
            shutil.rmtree(self.scratch)
        except OSError as error:
            remaining.append(str(self.scratch) + ': ' + str(error))
        self.closed = True
        self.report['cleanup'] = {'complete': not remaining, 'remaining': remaining}
        return self.report['cleanup']


def cell(args, prepared=None):
    if prepared is not None:
        return prepared.execute(args)
    mirror = PreparedMirror(args)
    result = None
    try:
        mirror.prepare()
        result = mirror.execute(args)
    except BaseException as error:
        result = {'schema': 1, 'ok': False, 'error': str(error),
                  'interrupted': isinstance(error, (KeyboardInterrupt, SystemExit))}
    finally:
        cleanup = mirror.close()
        if result is not None:
            result['prepared_mirror'] = mirror.report
            result['cleanup'] = cleanup
            if not cleanup['complete']:
                result['ok'] = False
    return result


def suite(args, baseline, profiles):
    mirror = PreparedMirror(args, baseline=baseline)
    results = []
    result = dict(profiles, schema=1, ok=False, cells=results)
    try:
        mirror.prepare()
        fresh = candidate_only_profiles(baseline, getattr(mirror, 'inventory', {})) if not args.library_only else []
        execution_profiles = list(profiles['profiles']) + fresh
        result['profiles'] = execution_profiles
        result['fresh_install_packages'] = [p['fresh_package'] for p in fresh]
        for profile in execution_profiles:
            options = argparse.Namespace(**vars(args))
            options.profile = ','.join(profile['packages'])
            options.fresh_package = profile.get('fresh_package')
            options.disabled_extension = profile.get('disabled_extension')
            options.tls = args.tls and profile['id'] == 'common'
            options.services = args.services and profile['id'] == 'common'
            options.allow_unaffected_profile = args.library_only or args.partial_consumers
            print('Migration profile: ' + profile['id'], flush=True)
            receipt = cell(options, prepared=mirror)
            receipt['profile_id'] = profile['id']
            results.append(receipt)
            if (receipt.get('interrupted') or receipt.get('prepared_inputs_invalid')
                    or receipt.get('cleanup', {}).get('complete') is False):
                break
        exercised = any(item.get('applicable', item.get('ok', False)) for item in results)
        result.update(ok=len(results) == len(execution_profiles) and all(item['ok'] for item in results) and exercised,
                      applicable=exercised, interrupted=any(item.get('interrupted') for item in results))
    except BaseException as error:
        result.update(ok=False, error=str(error), interrupted=isinstance(error, (KeyboardInterrupt, SystemExit)))
    finally:
        cleanup = mirror.close()
        result['prepared_mirror'] = mirror.report
        result['cleanup'] = cleanup
        if not cleanup['complete']:
            result['ok'] = False
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--type', choices=('rpm', 'deb', 'apk'), required=True)
    parser.add_argument('--php', choices=('8.2', '8.3', '8.4', '8.5', '8.6'), required=True)
    parser.add_argument('--arch', choices=('x86_64', 'arm64'), required=True)
    parser.add_argument('--artifacts', type=Path)
    parser.add_argument('--baseline-dir', type=Path)
    parser.add_argument('--report', type=Path)
    parser.add_argument('--runtime', choices=tuple(RUNTIMES))
    parser.add_argument('--profile', default='cli')
    parser.add_argument('--list-profiles', action='store_true', help='Print complete-baseline common/independent profile groups without Docker')
    parser.add_argument('--individual-profiles', action='store_true', help='Also test every extension in an independent upgrade profile')
    parser.add_argument('--disabled-extension', help='Disable this old shared module using the installed phpmod convention before upgrading')
    parser.add_argument('--tls', action='store_true', help='Check native OpenSSL/cURL and Go TLS trust before removing the signed mirror')
    parser.add_argument('--services', action='store_true', help='Check real service lifecycle across core and compatible library updates')
    parser.add_argument('--library-artifacts', type=Path, help='Genuine higher compatible library package revisions for the service third generation')
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument('--library-only', action='store_true', help='Upgrade only affected libraries under an existing shared PHP installation')
    modes.add_argument('--partial-consumers', action='store_true', help='Upgrade incoming consumer/library candidates while retaining an already-shared baseline')
    parser.add_argument('--allow-unaffected-profile', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--inside', choices=('index', 'old', 'new', 'fresh'), help=argparse.SUPPRESS)
    parser.add_argument('--fresh-package', help=argparse.SUPPRESS)
    args = parser.parse_args()
    args.runtime = args.runtime or {'rpm': 'alma8', 'deb': 'debian', 'apk': 'alpine'}[args.type]
    if RUNTIMES[args.runtime][0] != args.type:
        parser.error('--runtime does not match --type')
    if args.inside:
        inside(args)
        return 0
    if args.list_profiles:
        if not args.baseline_dir:
            parser.error('--list-profiles requires --baseline-dir')
        print(json.dumps(all_profiles(json.loads((args.baseline_dir / 'baseline.json').read_text()), args.individual_profiles), indent=2))
        return 0
    if not args.artifacts or not args.baseline_dir or not args.report:
        parser.error('--artifacts, --baseline-dir and --report are required')
    def interrupted(signum, frame):
        raise KeyboardInterrupt('Interrupted by signal ' + str(signum))
    signal.signal(signal.SIGTERM, interrupted)
    try:
        if args.profile == 'all':
            baseline = load_baseline(args.baseline_dir, args.type, args.php, args.arch)
            profiles = all_profiles(baseline, args.individual_profiles)
            result = suite(args, baseline, profiles)
        else:
            result = cell(args)
    except Exception as error:
        result = {'schema': 1, 'ok': False, 'error': str(error), 'resources_created': False}
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result, indent=2))
    return 0 if result['ok'] else 1


if __name__ == '__main__':
    sys.exit(main())
