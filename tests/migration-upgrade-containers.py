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
import html
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
ARCHITECTURE = runpy.run_path(str(HERE / 'container-architecture.py'))
RUNTIMES = {'alma8': ('rpm', 'almalinux:8'), 'alma9': ('rpm', 'almalinux:9'),
            'alma10': ('rpm', 'almalinux:10'), 'debian': ('deb', 'debian:11'),
            'alpine': ('apk', 'alpine:3.24')}
WARNING = re.compile(r'PHP Startup:|Unable to (?:load dynamic library|initialize module)|symbol lookup error|already loaded', re.I)
NAME = re.compile(r'^[A-Za-z0-9][A-Za-z0-9+_.-]*$')
RPM_MIRROR = Path('/spp/repository/generations')


def failure_reason(error):
    lines = str(error).strip().splitlines()
    exceptions = [i for i, line in enumerate(lines) if re.match(r'^[\w.]+(?:Error|Exception):', line)]
    if exceptions:
        lines = lines[exceptions[-1]:]
        lines[0] = lines[0].split(':', 1)[1].strip()
    if lines and lines[0].startswith('Command failed:'):
        command = lines.pop(0).removeprefix('Command failed:').strip().split()
        if not any(line.strip() for line in lines):
            lines = ['Command failed: ' + ' '.join(command[:5] if command[:3] == ['apk', 'version', '-t'] else command[:3])]
        else:
            errors = [i for i, line in enumerate(lines) if re.match(r'^(?:ERROR:|Error:|E:)', line)]
            lines = lines[errors[0]:] if errors else lines[-6:]
    return ' | '.join(line.strip() for line in lines if line.strip())[:800] or 'No error details recorded'


def annotation(level, message):
    escaped = message.replace('%', '%25').replace('\r', '%0D').replace('\n', '%0A')
    print('::' + level + '::' + escaped, flush=True)


def report_summary(args, result):
    cells = result.get('cells', [result])
    passed = sum(cell.get('ok', False) for cell in cells)
    target = f'{args.type.upper()} PHP {args.php} / {args.arch}'
    lines = [f'{target}: {"PASS" if result["ok"] else "FAIL"} ({passed}/{len(cells)} profiles passed)']
    for cell in cells:
        profile = cell.get('profile_id', args.profile)
        if not cell.get('ok'):
            reason = failure_reason(cell['error']) if cell.get('error') else 'Validation did not complete'
            if cell.get('cleanup', {}).get('complete') is False:
                reason += '; cleanup failed: ' + failure_reason('; '.join(cell['cleanup'].get('remaining', [])))
            message = f'{profile} / {cell.get("failed_phase", "validation")}: {reason}'
            lines.append('FAILED ' + message)
            annotation('error', target + ': ' + message)
        if cell.get('baseline_failure'):
            lines.append(f'{profile}: old baseline unavailable; replacement validation '
                         + ('passed' if cell.get('ok') else 'failed') + '. '
                         + failure_reason(cell['baseline_failure']))
    if 'cells' in result and result.get('error'):
        message = 'Validation setup: ' + failure_reason(result['error'])
        lines.append(message)
        annotation('error', target + ': ' + message)
    if result.get('cleanup', {}).get('complete') is False:
        message = 'Cleanup failed: ' + failure_reason('; '.join(result['cleanup'].get('remaining', [])))
        lines.append(message)
        annotation('error', target + ': ' + message)
    lines.append(f'Full diagnostics: {args.report} (included in CI artifacts)')
    summary = '\n'.join(lines)
    print(summary, flush=True)
    if os.environ.get('GITHUB_STEP_SUMMARY'):
        with open(os.environ['GITHUB_STEP_SUMMARY'], 'a') as stream:
            stream.write('<pre>' + html.escape(summary) + '</pre>\n')


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
    if not isinstance(packages, list):
        raise RuntimeError('Frozen baseline package inventory must be a list')
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


def profile_names(profile, require_php=True):
    values = [x.strip() for x in profile.split(',') if x.strip()]
    names = [x if x in ('php-zts', 'frankenphp') or x.startswith(('php-zts-', 'pie-zts')) else 'php-zts-' + x for x in values]
    if any(not NAME.fullmatch(x) for x in names):
        raise RuntimeError('Invalid package profile')
    return sorted(set((['php-zts-cli'] if require_php else []) + names))


def managed(name):
    return name in ('php-zts', 'frankenphp') or name.startswith(('php-zts-', 'pie-zts'))


def resolve_profile(baseline, names):
    return sorted(set(names))


def library_old_profile(baseline, incoming_changes, requested, expand_consumers=True):
    packages = {p['name']: p for p in baseline['packages']}
    roots = {name for name in incoming_changes if name.startswith('php-zts-lib-')
             and not name.endswith('-debuginfo') and name in packages}
    if not roots:
        raise RuntimeError('Library-only validation needs an affected provider in the published shared baseline')
    selected = set(requested) | roots
    if not expand_consumers:
        return sorted(selected)
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


def all_profiles(baseline):
    selection = baseline.get('selection', {})
    if selection.get('profile') != 'all' or selection.get('complete') is not True:
        return {'profiles': [], 'candidate_profiles': True}
    return package_profiles(baseline)


def affected_profiles(baseline):
    if baseline.get('selection', {}).get('mode') != 'affected':
        raise RuntimeError('Affected profiles require a baseline captured for the changed packages')
    names = {p['name'] for p in baseline['packages']}
    if names and all(name.startswith('php-zts-lib-') or name == 'php-zts-runtime-layout' for name in names):
        return {'profiles': [{'id': 'libraries', 'packages': sorted(names)}], 'covered_packages': sorted(names),
                'isolated_runtime_hook_packages': []}
    return package_profiles(baseline)


def package_profiles(baseline):
    names = sorted({p['name'] for p in baseline['packages']
                    if managed(p['name']) and not p['name'].startswith('php-zts-lib-')
                    and not p['name'].endswith(('-debuginfo', '-dbgsym'))})
    if 'php-zts-cli' not in names:
        return {'profiles': [], 'candidate_profiles': True}
    # Keep VM instrumentation profiles independent while testing ordinary modules together.
    isolated = {'php-zts-xdebug', 'php-zts-pcov', 'php-zts-spx', 'php-zts-xhprof'} & set(names)
    profiles = [{'id': 'common', 'packages': [name for name in names if name not in isolated]}]
    hosts = ({'php-zts-cli', 'php-zts-fpm', 'frankenphp'} & set(names)
             if baseline.get('selection', {}).get('mode') == 'affected' else {'php-zts-cli'})
    for name in sorted(isolated):
        profiles.append({'id': 'independent-' + name, 'packages': sorted(hosts | {name})})
    if 'php-zts-imagick' in names:
        profiles.append({'id': 'disabled-imagick', 'packages': ['php-zts-cli', 'php-zts-imagick'],
                         'disabled_extension': 'imagick'})
    if baseline.get('type') == 'apk':
        package = 'php-zts-igbinary' if 'php-zts-igbinary' in names else None
        if package:
            profiles.append({'id': 'disabled-igbinary', 'packages': ['php-zts-cli', package],
                             'disabled_extension': 'igbinary'})
    return {'profiles': profiles, 'covered_packages': names,
            'isolated_runtime_hook_packages': sorted(isolated)}


def candidate_only_profiles(baseline, inventory):
    published = {p['name'] for p in baseline.get('packages', []) + baseline.get('published_packages', [])}
    published.update(inventory.get('published_names', []))
    core = {'php-zts', 'php-zts-cli', 'php-zts-cgi', 'php-zts-fpm', 'php-zts-embed',
            'php-zts-devel', 'php-zts-runtime-layout', 'php-zts-curl', 'php-zts-fileinfo'}
    names = {p['name'] for p in inventory.get('incoming', [])
                    if p['name'].startswith('php-zts-') and p['name'] not in published | core
                    and not p['name'].startswith('php-zts-lib-')
                    and not p['name'].endswith(('-debuginfo', '-dbgsym'))}
    hosts = ({'php-zts-cli', 'php-zts-fpm', 'frankenphp'}
             if baseline.get('selection', {}).get('mode') == 'affected' else set())
    return [{'id': 'fresh-' + name, 'packages': sorted(hosts | {name}), 'fresh_package': name} for name in sorted(names)]


class Docker:
    def __init__(self, scratch, arch, allow_emulation=False):
        self.scratch = Path(scratch)
        self.arch = architecture(arch)
        self.token = 'spp-migration-' + uuid.uuid4().hex[:12]
        self.containers, self.images = [], []
        self.network = None
        self.allow_emulation = allow_emulation
        self.execution = None
        self.runtime_evidence = []
        self.bounds = {role: ARCHITECTURE['resource_limits'](role) for role in ('repo', 'client')}

    def check_native(self):
        identity = ARCHITECTURE['engine_identity'](run, self.arch, self.allow_emulation)
        if self.execution is not None and identity != self.execution:
            raise PreparedInputError('Docker engine/execution mode changed after preparing the mirror')
        self.execution = identity
        return identity

    def start(self, image, role):
        if self.execution is None:
            self.check_native()
        if self.network is None:
            self.network = self.token
            run(['docker', 'network', 'create', self.network])
        if run(['docker', 'image', 'inspect', image], check=False).returncode:
            self.images.append(image)
        name = self.token + '-' + role
        self.containers.append(name)
        limits = self.bounds['repo' if role == 'repo' else 'client']
        command = ['docker', 'run', '-d', '--name', name, *ARCHITECTURE['container_options'](self.execution),
                   *ARCHITECTURE['resource_options'](limits),
                   '--network', self.network, '--mount', 'type=bind,src={},dst=/spp'.format(self.scratch)]
        if role == 'repo':
            command += ['--network-alias', 'repo']
        run(command + [image, 'sh', '-c', 'while :; do sleep 60; done'])
        self.runtime_evidence.append(dict(ARCHITECTURE['container_identity'](run, name, self.execution, limits), role=role))
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
        for attempt in range(3):
            try:
                resources.exec(name, ['dnf', 'install', '-y'] + packages)
                break
            except RuntimeError as error:
                if attempt == 2 or not any(message in str(error) for message in (
                        'Failed to download metadata', 'Cannot download repomd.xml', 'repomd.xml parser error')):
                    raise
                annotation('warning', f'AlmaLinux test-tool metadata fetch failed; retrying ({attempt + 2}/3)')
                resources.exec(name, ['dnf', 'clean', 'metadata'])

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
                   for key, field in [('requires', 'depend'), ('provides', 'provides'), ('replaces', 'replaces'),
                                      ('provider_priority', 'provider_priority')]}
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
    if kind == 'rpm':
        run(['rpmkeys', '--import', keys / 'repository.asc'])
    return fingerprint


def sign(kind, path):
    if kind == 'apk':
        run(['abuild-sign', '-k', '/spp/keys/spp-migration.rsa', path])
        return
    args = ['gpg', '--homedir', '/spp/keys/gpg', '--batch', '--yes', '--pinentry-mode', 'loopback', '--passphrase', '']
    run(args + ['--armor', '--detach-sign', path])


def sign_rpm_for_mirror(path, fingerprint):
    path = Path(path)
    if (path.is_symlink() or RPM_MIRROR.resolve() not in path.resolve().parents
            or not path.is_file() or path.stat().st_nlink != 1):
        raise RuntimeError('RPM mirror signing requires an isolated scratch archive')
    run(['rpmsign', '--delsign', path])
    run(['rpmsign', '--define', '_gpg_name ' + fingerprint, '--define', '_gpg_path /spp/keys/gpg',
         '--define', '__gpg /usr/bin/gpg', '--addsign', path])
    return run(['rpmkeys', '--checksig', path])


def index_generation(kind, name, stream, arch):
    directory = Path('/spp/repository/generations') / name
    packages = inspect_directory(kind, directory)
    identities = [(p['name'], p['architecture']) for p in packages]
    if len(set(identities)) != len(identities):
        raise RuntimeError('A generation contains duplicate package names/architectures')
    if kind == 'rpm':
        fingerprint = Path('/spp/keys/fingerprint').read_text().strip()
        for package in packages:
            sign_rpm_for_mirror(package['path'], fingerprint)
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


def index_baseline(kind, stream, arch, partial):
    try:
        return index_generation(kind, 'old', stream, arch), None
    except RuntimeError as error:
        try:
            packages = inspect_directory(kind, Path('/spp/repository/generations/old'))
        except RuntimeError:
            packages = []
        return packages, str(error)


def copy_historical(old, new):
    for source in old.rglob('*'):
        if source.is_file():
            destination = new / source.relative_to(old)
            if not destination.exists():
                destination.parent.mkdir(parents=True, exist_ok=True)
                os.link(source, destination)


def stage_library_generation(kind, current, incoming, directory):
    directory = Path(directory)
    directory.mkdir()
    replacements = {package['name'] for package in incoming}
    for package in current:
        if package['name'] in replacements:
            continue
        source = Path(package['path'])
        destination = directory / source.name
        if kind in ('apk', 'deb'):
            os.link(source, destination)
        else:
            shutil.copy2(source, destination)
    for package in incoming:
        source = Path(package['path'])
        shutil.copy2(source, directory / source.name)


def configure_client(kind, stream, arch, runtime, modules=True):
    if kind == 'rpm':
        el = runtime.replace('alma', 'el')
        url = 'http://repo:8080/{}/{}/'.format('aarch64' if arch == 'arm64' else 'x86_64', el)
        Path('/etc/yum.repos.d/spp-migration.repo').write_text(
            '[spp-migration]\nname=SPP signed migration mirror\nbaseurl={}\nenabled=1\n'
            'gpgcheck=1\nrepo_gpgcheck=1\ngpgkey=http://repo:8080/keys/repository.asc\nmetadata_expire=0\n'.format(url))
        run(['rpm', '--import', '/spp/keys/repository.asc'])
        if modules:
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
    run(['apk', 'update', '--cache-max-age', '0'])
    return run(['apk', 'upgrade'])


def assert_upgrade(before, after, candidate, names):
    for name in names:
        if name not in before:
            raise RuntimeError('Requested old package was not actually installed: ' + name)
        if name not in candidate:
            raise RuntimeError('Installed package has no migration candidate: ' + name)
        if after.get(name) != candidate[name]['version'] or after[name] == before[name]:
            raise RuntimeError('Ordinary upgrade held back or failed to advance ' + name)
    removed = {name for name in set(before) - set(after) if managed(name)}
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
    layout = incoming.get('php-zts-runtime-layout')
    if layout and after.get('php-zts-runtime-layout') != layout['version']:
        raise RuntimeError('Ordinary upgrade held back runtime layout')
    consumers = {name: version for name, version in before.items()
                 if managed(name) and not name.startswith('php-zts-lib-') and name != 'php-zts-runtime-layout'}
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


def validate_library_only_candidates(incoming):
    if any(not package['name'].startswith('php-zts-lib-') and package['name'] != 'php-zts-runtime-layout'
           for package in incoming):
        raise RuntimeError('Library-only candidates may include only libraries and their runtime layout')


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
        else:
            try:
                increased = newer(kind, previous['version'], package['version'])
            except RuntimeError:
                if kind != 'apk':
                    raise
                newer(kind, package['version'], package['version'])
                increased = True
            if not increased:
                raise RuntimeError('Partial candidate is lower than the frozen version: ' + package['name'])
            changed.append(package['name'])
    return sorted(changed)


def inside_libraries(args, names):
    kind = args.type
    state_path = Path('/spp/client-before.json')
    runtime = runpy.run_path('/spp/shared-runtime-containers.py')
    if args.inside == 'old':
        config, key = configure_client(kind, args.php, args.arch, args.runtime, modules=False)
        command = {'rpm': ['dnf', 'install', '-y'], 'deb': ['apt-get', 'install', '-y'], 'apk': ['apk', 'add']}[kind]
        run(command + names)
        state_path.write_text(json.dumps({'installed': installed(kind), 'probe': runtime['assert_elf'](kind, names),
            'config': config, 'config_sha256': sha256(config), 'key': key, 'key_sha256': sha256(key),
            'module_state': module_state(kind), 'php_ini_sha256': None}))
        return
    before = json.loads(state_path.read_text())
    inventory = json.loads(Path('/spp/inventory.json').read_text())
    incoming = {package['name']: package for package in inventory['incoming']}
    changed = inventory['incoming_changes']
    ordinary_upgrade(kind)
    after = installed(kind)
    updates = assert_partial_upgrade(before['installed'], after, incoming, changed, allow_unaffected=True)
    libraries = assert_library_upgrade(before['installed'], after, {name: incoming[name] for name in changed}) if updates else []
    probe = runtime['assert_elf'](kind, names)
    for field in ('config', 'key'):
        if sha256(before[field]) != before[field + '_sha256']:
            raise RuntimeError('Library upgrade changed existing ' + field)
    if module_state(kind) != before['module_state']:
        raise RuntimeError('Library upgrade changed the module selection')
    ordinary_upgrade(kind)
    if installed(kind) != after:
        raise RuntimeError('Second unchanged library upgrade was not a no-op')
    if kind == 'rpm':
        run(['dnf', 'check'])
    elif kind == 'deb' and run(['dpkg', '--audit']).stdout.strip():
        raise RuntimeError('Library upgrade left an incomplete dpkg transaction')
    print('SPP_MIGRATION_RESULT=' + json.dumps({'ok': True, 'applicable': True, 'library_only': True,
        'coverage': 'published_library_upgrade_without_php_consumers',
        'actual_old_versions': {name: version for name, version in before['installed'].items() if managed(name)},
        'new_versions': {name: version for name, version in after.items() if managed(name)},
        'updated_libraries': libraries, 'updated_packages': updates, 'old_probe': before['probe'], 'new_probe': probe,
        'repository_and_trust_unchanged': True, 'module_selection_unchanged': True, 'second_upgrade_noop': True}, sort_keys=True))


def inside(args):
    kind = args.type
    if args.inside != 'index' and (os.environ.get('LD_LIBRARY_PATH') or os.environ.get('LD_PRELOAD')):
        raise RuntimeError('Installed migration checks cannot use developer loader overrides')
    if args.inside == 'index':
        incoming = inspect_directory(kind, Path('/spp/repository/generations/new'))
        partial = args.library_only or args.partial_consumers
        incoming_changes = [p['name'] for p in incoming]
        if partial:
            try:
                frozen = inspect_directory(kind, Path('/spp/repository/generations/old'))
            except RuntimeError:
                frozen = []
            if args.library_only:
                validate_library_only_candidates(incoming)
            incoming_changes = validate_partial_candidates(kind, frozen, incoming)
        make_keys(kind)
        old, baseline_index_failure = index_baseline(kind, args.php, args.arch, partial)
        if partial:
            incoming_names = {p['name'] for p in incoming}
            for package in old:
                if package['name'] not in incoming_names:
                    source = Path(package['path'])
                    shutil.copy2(source, Path('/spp/repository/generations/new') / source.name)
        new = index_generation(kind, 'new', args.php, args.arch)
        for item in new:
            if item['architecture'] not in (architecture(args.arch), 'all'):
                raise RuntimeError('Package architecture does not match the native cell')
        if any(item['architecture'] not in (architecture(args.arch), 'all') for item in old):
            baseline_index_failure = 'Published package architecture does not match the native cell'
        library = None
        if args.library_artifacts:
            incoming_library = inspect_directory(kind, Path('/spp/library-input'))
            validate_library_only_candidates(incoming_library)
            changed_library = validate_partial_candidates(kind, new, incoming_library)
            if not any(name.startswith('php-zts-lib-') for name in changed_library):
                raise RuntimeError('Service lifecycle needs a genuine higher library package revision')
            directory = Path('/spp/repository/generations/library')
            stage_library_generation(kind, new, incoming_library, directory)
            library = index_generation(kind, 'library', args.php, args.arch)
            if any(p['architecture'] not in (architecture(args.arch), 'all') for p in library):
                raise RuntimeError('Service library generation has the wrong architecture')
        copy_historical(Path('/spp/repository/generations/old'), Path('/spp/repository/generations/new'))
        if library:
            copy_historical(Path('/spp/repository/generations/new'), Path('/spp/repository/generations/library'))
        published_names = json.loads(Path('/spp/published-names.json').read_text())
        Path('/spp/inventory.json').write_text(json.dumps({'old': old, 'new': new, 'incoming': incoming,
                                                        'incoming_changes': incoming_changes, 'library': library,
                                                        'baseline_index_failure': baseline_index_failure,
                                                        'published_names': published_names}))
        return
    if args.inside == 'replacement':
        inventory = json.loads(Path('/spp/inventory.json').read_text())
        candidate = {p['name']: p for p in inventory['new']}
        names = json.loads(Path('/spp/selected.json').read_text())
        if not names or set(names) - set(candidate):
            raise RuntimeError('Replacement repository does not contain the selected packages')
        libraries_only = args.library_only and all(name.startswith('php-zts-lib-') or name == 'php-zts-runtime-layout' for name in names)
        configure_client(kind, args.php, args.arch, args.runtime, modules=not libraries_only)
        command = {'rpm': ['dnf', 'install', '-y'], 'deb': ['apt-get', 'install', '-y'], 'apk': ['apk', 'add']}[kind]
        run(command + names)
        after = installed(kind)
        for name in set(names) | {name for name in after if managed(name)}:
            if name not in candidate or after.get(name) != candidate[name]['version']:
                raise RuntimeError('Replacement installation did not select the candidate version: ' + name)
        runtime = runpy.run_path('/spp/shared-runtime-containers.py')
        managed_names = sorted(name for name in after if managed(name))
        elf = runtime['assert_elf'](kind, managed_names)
        has_php = any(not name.startswith('php-zts-lib-') and name != 'php-zts-runtime-layout' for name in managed_names)
        smokes = runtime['sapi_checks'](args.php, managed_names, kind) if has_php else {}
        development = runtime['development_checks'](kind, managed_names)
        before_upgrade = installed(kind)
        ordinary_upgrade(kind)
        after_upgrade = installed(kind)
        changes = {name: [before_upgrade.get(name), after_upgrade.get(name)]
                   for name in before_upgrade.keys() | after_upgrade.keys()
                   if managed(name) and before_upgrade.get(name) != after_upgrade.get(name)}
        if changes:
            raise RuntimeError('Replacement upgrade changed managed packages: ' + json.dumps(changes, sort_keys=True))
        print('SPP_MIGRATION_RESULT=' + json.dumps({'ok': True, 'applicable': True,
              'coverage': 'fresh_replacement_after_broken_baseline',
              'installed_versions': {name: after[name] for name in managed_names},
              'installed_elf_checks': elf, 'installed_sapi_smokes': smokes,
              'installed_development_smoke': development, 'second_upgrade_noop': True}, sort_keys=True))
        return
    if args.inside == 'fresh':
        inventory = json.loads(Path('/spp/inventory.json').read_text())
        allowed = {p['fresh_package'] for p in candidate_only_profiles({'packages': inventory['old']}, inventory)}
        if args.fresh_package not in allowed:
            raise RuntimeError('Fresh optional-package profile must name a candidate absent from the published baseline')
        config, key = configure_client(kind, args.php, args.arch, args.runtime)
        command = {'rpm': ['dnf', 'install', '-y'], 'deb': ['apt-get', 'install', '-y'], 'apk': ['apk', 'add']}[kind]
        names = json.loads(Path('/spp/selected.json').read_text())
        run(command + names)
        after = installed(kind)
        candidate = {p['name']: p for p in inventory['new']}
        actual = args.fresh_package
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
        first_upgrade = installed(kind)
        managed_before = {name: version for name, version in after.items() if managed(name)}
        managed_after = {name: version for name, version in first_upgrade.items() if managed(name)}
        if managed_after != managed_before or (sha256(config), sha256(key), module_state(kind)) != state:
            raise RuntimeError('Fresh optional-package unchanged upgrade changed packages/repository/trust/module selection')
        ordinary_upgrade(kind)
        if installed(kind) != first_upgrade or (sha256(config), sha256(key), module_state(kind)) != state:
            raise RuntimeError('Fresh optional-package second unchanged upgrade was not a no-op')
        if kind == 'rpm':
            run(['dnf', 'check'])
        elif kind == 'deb' and run(['dpkg', '--audit']).stdout.strip():
            raise RuntimeError('Fresh optional-package installation left an incomplete dpkg transaction')
        print('SPP_MIGRATION_RESULT=' + json.dumps({'ok': True, 'applicable': True,
              'coverage': 'fresh_candidate_only', 'requested_installation': names,
              'physical_package': actual,
              'installed_versions': {name: version for name, version in after.items() if managed(name)},
              'automatic_libraries': libraries, 'curl_fileinfo_automatic': True,
              'requested_shared_modules': modules,
              'installed_sapi_smokes': smokes, 'second_upgrade_noop': True,
              'repository_and_trust_unchanged': True, 'module_selection_unchanged': True,
              'new_probe': probe}, sort_keys=True))
        return
    names = json.loads(Path('/spp/selected.json').read_text())
    if args.library_only and names and all(name.startswith('php-zts-lib-') or name == 'php-zts-runtime-layout' for name in names):
        return inside_libraries(args, names)
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
    partial = args.library_only or args.partial_consumers
    comparison_names = [name for name in names if name in changed] if partial else names
    if not comparison_names and not args.allow_unaffected_profile:
        raise RuntimeError('No affected installed packages to validate')
    for name in comparison_names:
        if name not in candidate:
            raise RuntimeError('Published installed package lacks a candidate: ' + name)
    # The ordinary solver transaction below proves version ordering. In particular,
    # APK 3 can upgrade installed legacy tags which `apk version -t` rejects.
    ordinary_upgrade(kind)
    after = installed(kind)
    if partial:
        actual_updates = assert_partial_upgrade(before['installed'], after, incoming, changed, args.allow_unaffected_profile)
        library_updates = assert_library_upgrade(before['installed'], after, {name: incoming[name] for name in changed}) if args.library_only and actual_updates else []
    else:
        actual_updates = comparison_names
        library_updates = None
        assert_upgrade(before['installed'], after, candidate, names)
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
              'new_versions': {n: after[n] for n in names},
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
    common = ['--type', args.type, '--php-stream', args.php, '--arch', args.arch,
              '--image', RUNTIMES[args.runtime][1], '--key', str(key)]
    if getattr(args, 'allow_emulation', False):
        common.append('--allow-emulation')
    repository = hook_repository(scratch, args.type, 'new', args.arch)
    hooks = []
    if args.tls:
        hooks.append(('tls', HERE / 'tls-runtime-containers.py', ['--repo', str(repository)]))
    if args.services and result.get('baseline_failure'):
        result['services_checks'] = {'state': 'unavailable', 'reason': 'Published baseline is broken; fresh SAPI checks passed'}
    elif args.services:
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
    for name in ('library_only', 'partial_consumers', 'allow_unaffected_profile', 'tls', 'services', 'allow_emulation'):
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
        self.resources = Docker(self.scratch, args.arch, allow_emulation=self.args.allow_emulation)
        self.resources.bounds = {role: ARCHITECTURE['resource_limits'](role, args.runtime) for role in ('repo', 'client')}
        self.ready = False
        self.closed = False
        self.report = {'native_tool_bootstraps': 0, 'sign_index_runs': 0,
                       'clean_runtime_bootstraps': 0, 'clean_runtime_commits': 0, 'profiles_started': 0,
                       'runtime_evidence': self.resources.runtime_evidence}

    def bootstrap_memory_peak(self, name, role):
        if self.args.runtime != 'alma8':
            return
        checked = self.resources.exec(name, ['cat', '/sys/fs/cgroup/memory.peak'], check=False)
        if checked.stdout.strip().isdigit():
            self.report.setdefault('bootstrap_memory_peak_bytes', {})[role] = int(checked.stdout.strip())

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
        helpers = [Path(__file__), HERE / 'container-architecture.py', HERE / 'shared-runtime.php', HERE / 'shared-runtime-containers.py']
        if args.tls:
            helpers.append(HERE / 'tls-runtime-containers.py')
        if args.tls or args.services:
            helpers.append(HERE / 'runtime-service-containers.py')
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
                                 'partial_consumers': args.partial_consumers,
                                 'allow_emulation': args.allow_emulation,
                                 'execution': self.resources.execution,
                                 'container_limits': json.loads(json.dumps(self.resources.bounds)), 'files': records}
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
        self.report.update(execution=resources.execution, native=resources.execution['native'], emulated=resources.execution['emulated'])
        self.ledger()
        self.copy_checked(__file__, scratch / 'migration-upgrade-containers.py')
        self.copy_checked(HERE / 'container-architecture.py', scratch / 'container-architecture.py')
        self.copy_checked(HERE / 'shared-runtime.php', scratch / 'shared-runtime.php')
        self.copy_checked(HERE / 'shared-runtime-containers.py', scratch / 'shared-runtime-containers.py')
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
        self.bootstrap_memory_peak(helper, 'repository')
        if kind == 'rpm':
            resources.exec(helper, ['dnf', 'clean', 'all'])
            self.report['repository_dnf_cache_removed'] = True
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
            frozen = old_by_name.get(package['name'])
            native_version = package['version']
            if frozen is not None and args.type == 'rpm' and ':' not in frozen['version']:
                native_version = native_version.split(':', 1)[-1]
            if frozen is None or native_version != frozen['version']:
                native_inventory['baseline_index_failure'] = 'Frozen archive header differs from recorded published version: ' + package['name']
        (scratch / 'inventory.json').write_text(json.dumps(native_inventory))
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
        self.bootstrap_memory_peak(base, 'clean_client')
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
        for name in ('migration-upgrade-containers.py', 'container-architecture.py', 'shared-runtime.php', 'shared-runtime-containers.py',
                     'inventory.json', 'published-names.json'):
            self.staged[str(scratch / name)] = source_token(scratch / name, include_ctime=False)
        self.assert_bound(args)
        self.ready = True

    def assert_bound(self, args):
        for name in ('type', 'php', 'arch', 'runtime', 'library_only', 'partial_consumers', 'allow_emulation'):
            if getattr(args, name) != getattr(self.args, name):
                raise PreparedInputError('Prepared mirror mode/runtime does not match: ' + name)
        if self.resources.execution is not None:
            if self.resources.check_native() != self.report['ledger']['execution']:
                raise PreparedInputError('Prepared mirror Docker engine/execution mode changed')
        if self.resources.bounds != self.report['ledger']['container_limits']:
            raise PreparedInputError('Prepared mirror container resource bounds changed')
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
        names = resolve_profile(baseline, profile_names(args.profile, require_php=not args.library_only))
        result = {'schema': 1, 'ok': False, 'type': args.type, 'php_stream': args.php, 'architecture': args.arch,
                  'runtime': args.runtime, 'baseline_origin': baseline['origin'], 'profile': names,
                  'library_only': args.library_only, 'partial_consumers': args.partial_consumers,
                  'execution': resources.execution, 'native': resources.execution['native'],
                  'emulated': resources.execution['emulated'],
                  'signing_scope': 'Disposable signed local mirror; test key trusted before old installation. Published archive SHA256 verified; production signing/server behavior is not asserted.'}
        client = None
        phase = 'prepare profile'
        try:
            self.assert_bound(args)
            allowed = {p['fresh_package'] for p in candidate_only_profiles(baseline, self.inventory)}
            if args.fresh_package and args.fresh_package not in allowed:
                raise RuntimeError('Fresh profile does not name a candidate-only optional package')
            missing = set(names) - {p['name'] for p in baseline['packages']} if not args.fresh_package else set()
            if args.library_only:
                libraries = [package['name'] for package in self.inventory['incoming'] if package['name'].startswith('php-zts-lib-')
                             and not package['name'].endswith('-debuginfo')]
                try:
                    expanded = library_old_profile(baseline, libraries, names,
                        expand_consumers=baseline.get('selection', {}).get('mode') != 'affected')
                except RuntimeError:
                    expanded = sorted(set(names) | set(libraries))
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
            result['runtime_evidence'] = resources.runtime_evidence[-1]
            common = self.command(args)
            if args.fresh_package:
                phase = 'fresh installation'
                checked = resources.exec(client, common + ['--inside', 'fresh'])
                result.update(json.loads(checked.stdout.split('SPP_MIGRATION_RESULT=')[-1]))
                return result
            try:
                phase = 'published baseline installation/load'
                if missing:
                    raise RuntimeError('Requested published baseline packages missing: ' + ', '.join(sorted(missing)))
                if self.inventory.get('baseline_index_failure'):
                    raise RuntimeError(self.inventory['baseline_index_failure'])
                resources.exec(client, common + ['--inside', 'old'])
            except RuntimeError as error:
                self.assert_bound(args)
                result['baseline_failure'] = str(error)
                annotation('warning', 'Published baseline unavailable; trying a fresh replacement. ' + failure_reason(error))
                phase = 'fresh replacement validation'
                removed = run(['docker', 'rm', '-f', client], check=False)
                if removed.returncode and 'No such container' not in removed.stderr:
                    raise RuntimeError('Cannot remove failed baseline client: ' + removed.stderr)
                if client in resources.containers:
                    resources.containers.remove(client)
                client = None
                self.select_generation('new')
                client = resources.start(self.client_image, 'replacement-' + str(self.report['profiles_started']))
                result['runtime_evidence'] = resources.runtime_evidence[-1]
                checked = resources.exec(client, common + ['--inside', 'replacement'])
                result.update(json.loads(checked.stdout.split('SPP_MIGRATION_RESULT=')[-1]))
            else:
                phase = 'ordinary upgrade validation'
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
                phase = 'TLS/service validation'
                self.assert_bound(args)
                removed = run(['docker', 'rm', '-f', client], check=False)
                if removed.returncode and 'No such container' not in removed.stderr:
                    raise RuntimeError('Cannot remove upgrade client before lifecycle checks: ' + removed.stderr)
                if client in resources.containers:
                    resources.containers.remove(client)
                client = None
                lifetime_hooks(args, scratch, result)
        except BaseException as error:
            result.update(ok=False, error=str(error), failed_phase=phase, interrupted=isinstance(error, (KeyboardInterrupt, SystemExit)),
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
            result.update({name: mirror.report[name] for name in ('execution', 'native', 'emulated') if name in mirror.report})
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
        candidate_profiles = profiles.get('candidate_profiles', False)
        if candidate_profiles:
            replacement = dict(baseline, packages=mirror.inventory['new'])
            profiles = affected_profiles(replacement) if args.library_only else package_profiles(replacement)
            if profiles.get('candidate_profiles'):
                raise RuntimeError('Replacement repository has no CLI package')
            result.update(profiles)
        fresh = candidate_only_profiles(baseline, getattr(mirror, 'inventory', {})) if not args.library_only and not candidate_profiles else []
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
            if getattr(args, 'report', None) is not None:
                progress = dict(result, complete=False, state='running', prepared_mirror=mirror.report)
                progress['profiles_completed'] = len(results)
                args.report.parent.mkdir(parents=True, exist_ok=True)
                temporary_report = args.report.with_suffix(args.report.suffix + '.next')
                try:
                    temporary_report.write_text(json.dumps(progress, indent=2) + '\n')
                    temporary_report.replace(args.report)
                finally:
                    if temporary_report.exists():
                        temporary_report.unlink()
            print('Migration result: ' + profile['id'] + (' PASS' if receipt['ok'] else ' FAIL'), flush=True)
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
        result.update({name: mirror.report[name] for name in ('execution', 'native', 'emulated') if name in mirror.report})
        result['cleanup'] = cleanup
        if not cleanup['complete']:
            result['ok'] = False
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--type', choices=('rpm', 'deb', 'apk'), required=True)
    parser.add_argument('--php', choices=('8.2', '8.3', '8.4', '8.5', '8.6'), required=True)
    parser.add_argument('--arch', choices=('x86_64', 'arm64'), required=True)
    parser.add_argument('--allow-emulation', action='store_true', help='Allow local cross-architecture QEMU validation and label its receipts as emulated; CI remains native only')
    parser.add_argument('--artifacts', type=Path)
    parser.add_argument('--baseline-dir', type=Path)
    parser.add_argument('--report', type=Path)
    parser.add_argument('--runtime', choices=tuple(RUNTIMES))
    parser.add_argument('--profile', default='cli')
    parser.add_argument('--list-profiles', action='store_true', help='Print complete-baseline common/independent profile groups without Docker')
    parser.add_argument('--disabled-extension', help='Disable this old shared module using the installed phpmod convention before upgrading')
    parser.add_argument('--tls', action='store_true', help='Check native OpenSSL/cURL and Go TLS trust before removing the signed mirror')
    parser.add_argument('--services', action='store_true', help='Check real service lifecycle across core and compatible library updates')
    parser.add_argument('--library-artifacts', type=Path, help='Genuine higher compatible library package revisions for the service third generation')
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument('--library-only', action='store_true', help='Upgrade only affected libraries under an existing shared PHP installation')
    modes.add_argument('--partial-consumers', action='store_true', help='Upgrade incoming consumer/library candidates while retaining an already-shared baseline')
    parser.add_argument('--allow-unaffected-profile', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--inside', choices=('index', 'old', 'new', 'fresh', 'replacement'), help=argparse.SUPPRESS)
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
        print(json.dumps(all_profiles(json.loads((args.baseline_dir / 'baseline.json').read_text())), indent=2))
        return 0
    if not args.artifacts or not args.baseline_dir or not args.report:
        parser.error('--artifacts, --baseline-dir and --report are required')
    def interrupted(signum, frame):
        raise KeyboardInterrupt('Interrupted by signal ' + str(signum))
    signal.signal(signal.SIGTERM, interrupted)
    try:
        if args.profile in ('all', 'auto'):
            baseline = load_baseline(args.baseline_dir, args.type, args.php, args.arch)
            profiles = (affected_profiles(baseline) if args.profile == 'auto' and
                        baseline.get('selection', {}).get('mode') == 'affected'
                        else all_profiles(baseline))
            result = suite(args, baseline, profiles)
        else:
            result = cell(args)
    except Exception as error:
        result = {'schema': 1, 'ok': False, 'error': str(error), 'resources_created': False}
    result.update(complete=result['ok'], state='passed' if result['ok'] else 'interrupted' if result.get('interrupted') else 'failed')
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(result, indent=2) + '\n')
    report_summary(args, result)
    return 0 if result['ok'] else 1


if __name__ == '__main__':
    sys.exit(main())
