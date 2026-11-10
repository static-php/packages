#!/usr/bin/env python3
"""Historical validation utilities; the multi-distro compilation entrypoint is retired.

The active glibc workflow builds once on EL8 and packages the same payload as
RPM and DEB. Use shared-runtime-containers.py for packaged runtime validation.
"""

import argparse
from collections import deque
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid


ROOT = Path(__file__).resolve().parents[1]
FORMATS = {'alpine': 'apk', 'debian': 'deb', 'alma8': 'rpm'}
STREAMS = ('8.2', '8.3', '8.4', '8.5', '8.6')
CUSTOM_MASTERS = {
    'ext-apcu': 'https://github.com/krakjoe/apcu.git',
    'ext-ast': 'https://github.com/nikic/php-ast.git',
    'ext-igbinary': 'https://github.com/igbinary/igbinary.git',
    'ext-memcached': 'https://github.com/php-memcached-dev/php-memcached.git',
    'ext-msgpack': 'https://github.com/msgpack/msgpack-php.git',
    'ext-xdebug': 'https://github.com/xdebug/xdebug.git',
    'ext-xlswriter': 'https://github.com/viest/php-ext-xlswriter.git',
}
STATE = {'build', 'buildroot', 'source', 'downloads', 'log', 'logs', 'pkgroot', 'dist', 'locale'}
PRIVATE = {'.git', '.aws', '.agents', '.codex', '.claude', '.docker-home', '.idea', '__pycache__', '.cache'}
PRINT_LOCK = threading.Lock()
ANSI = re.compile(r'\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07]*(?:\x07|\x1b\\)')
DIAGNOSTIC = re.compile(r'\b(?:fatal(?: error)?|error|exception|undefined reference|unresolved|not found|cannot|failed|failure|segmentation fault|permission denied)\b|no rule to make target|make(?:\[\d+\])?: \*\*\*', re.I)
STACK_FRAME = re.compile(r'^\s*(?:#\d+\s|File "[^"]+", line \d+)')


def announce(message):
    with PRINT_LOCK:
        print(message, flush=True)


def copy_sources(source, destination):
    source = Path(source).resolve()
    def ignore(directory, names):
        result = {name for name in names if name in PRIVATE or name.startswith('.env')
                  or name.endswith('.pyc') or name in {'.spc.cache.php', 'auth.json'}}
        if Path(directory) == source:
            result.update(set(names) & STATE)
        return result
    shutil.copytree(source, destination, symlinks=True, ignore=ignore)


def freeze_custom_master_sources(project):
    downloads = project / 'downloads'
    cache = json.loads((downloads / '.cache.json').read_text())
    template = project / 'config/templates/craft.yml.twig'
    content = template.read_text()
    commits = {}
    for name, url in CUSTOM_MASTERS.items():
        entry = cache.get(name, {}).get('source') or {}
        config = entry.get('config') or {}
        revision = entry.get('hash', '')
        relative = Path(entry.get('dirname', ''))
        if entry.get('cache_type') != 'git' or config.get('url') != url or config.get('rev') != 'master' or not re.fullmatch('[a-f0-9]{40}', revision) or relative.is_absolute() or '..' in relative.parts or str(relative) == '.':
            raise StageFailure('freeze source snapshot', 'Missing or mismatched compatibility snapshot for ' + name)
        repository = downloads / relative
        if os.path.commonpath([str(repository.resolve()), str(downloads.resolve())]) != str(downloads.resolve()):
            raise StageFailure('freeze source snapshot', 'Compatibility snapshot escapes downloads: ' + name)
        try:
            head = subprocess.check_output(['git', '-C', str(repository), 'rev-parse', 'HEAD'], universal_newlines=True).strip()
            dirty = subprocess.check_output(['git', '-C', str(repository), 'status', '--porcelain', '--untracked-files=all'], universal_newlines=True).strip()
        except subprocess.CalledProcessError as error:
            raise StageFailure('freeze source snapshot', 'Cannot verify compatibility snapshot: ' + name) from error
        if head != revision or dirty:
            raise StageFailure('freeze source snapshot', 'Dirty or mismatched compatibility snapshot: ' + name)
        pattern = r"(?m)^[ \t]*- '" + re.escape(name + ':master:' + url) + r"'\r?\n"
        replacement = "        - '" + name + ':' + revision + ':' + url + "'\n"
        content, count = re.subn(pattern, replacement, content)
        if count != 1:
            raise StageFailure('freeze source snapshot', 'Expected one compatibility master recipe: ' + name)
        commits[name] = revision
    template.write_text(content)
    return commits


def library_baseline(root, os_name):
    directory = Path(root).resolve() / os_name
    buildroot = directory / 'buildroot'
    manifest_path = buildroot / 'shared-libraries.json'
    try:
        manifest_bytes = manifest_path.read_bytes()
        manifest = json.loads(manifest_bytes)
    except (OSError, ValueError) as error:
        raise StageFailure('reuse library baseline', 'Cannot read ' + str(manifest_path) + ': ' + str(error)) from error
    kind = FORMATS[os_name]
    runtime = '/usr/lib64/php-zts/lib' if kind == 'rpm' else '/usr/lib/php-zts/lib'
    if not isinstance(manifest, dict) or manifest.get('schema') != 1 or manifest.get('linkage') != 'shared' or manifest.get('runtime_libdir') != runtime or not isinstance(manifest.get('libraries'), dict) or not manifest['libraries']:
        raise StageFailure('reuse library baseline', 'Invalid shared-library manifest for ' + os_name + ': ' + str(manifest_path))
    for identifier, entry in manifest['libraries'].items():
        if not isinstance(entry, dict):
            raise StageFailure('reuse library baseline', 'Invalid library metadata: ' + str(identifier))
        epoch = entry.get('abi_epoch')
        versions = entry.get('package_versions') or {}
        if type(epoch) is not int or epoch < 1 or entry.get('abi_capability') != 'php-zts-lib-' + identifier + '-abi-' + str(epoch) or not isinstance(versions, dict) or not isinstance(versions.get(kind), str) or not versions[kind]:
            raise StageFailure('reuse library baseline', 'Missing prepared ABI or package revision for ' + identifier)
        dumps = entry.get('abi_dump', {})
        if not isinstance(dumps, dict):
            raise StageFailure('reuse library baseline', 'Invalid ABI dump metadata for ' + identifier)
        for relative in dumps.values():
            path = Path(relative) if isinstance(relative, str) else Path('/')
            if path.is_absolute() or '..' in path.parts or not (buildroot / path).is_file():
                raise StageFailure('reuse library baseline', 'Missing or invalid ABI dump for ' + identifier)
    for current, directories, files in os.walk(buildroot):
        for name in directories + files:
            path = Path(current) / name
            if path.is_symlink() and (os.path.isabs(os.readlink(path)) or not path.resolve().is_relative_to(buildroot.resolve())):
                raise StageFailure('reuse library baseline', 'Library baseline symlink escapes its private tree: ' + str(path))
    packages = sorted(path for path in (directory / 'dist' / kind).glob('php-zts-lib-*.' + kind) if path.is_file() and not path.is_symlink())
    if not packages:
        raise StageFailure('reuse library baseline', 'No native php-zts-lib-*.' + kind + ' artifacts in ' + str(directory / 'dist' / kind))
    return {'directory': directory, 'buildroot': buildroot, 'packages': packages,
            'manifest_sha256': hashlib.sha256(manifest_bytes).hexdigest()}


def diagnostic_excerpt(path, offset=0):
    if not path.exists():
        return ''
    context = deque(maxlen=2)
    selected = deque(maxlen=27)
    final = deque(maxlen=8)
    following = 0
    with path.open(errors='replace') as stream:
        stream.seek(offset)
        for number, raw in enumerate(stream):
            line = ANSI.sub('', raw).rstrip('\r\n')
            if len(line) > 400:
                line = line[:397] + '...'
            entry = (number, line)
            if DIAGNOSTIC.search(line) and not STACK_FRAME.match(line):
                selected.extend(context)
                selected.append(entry)
                following = 2
            elif following:
                selected.append(entry)
                following -= 1
            context.append(entry)
            final.append(entry)
    lines = dict([*selected, *final])
    return '\n'.join(lines[number] for number in sorted(lines)).strip()


def bounded_detail(text):
    lines = [ANSI.sub('', line)[:400] for line in text.splitlines()]
    if len(lines) > 120:
        lines = lines[:100] + ['... bounded diagnostic summary ...'] + lines[-19:]
    return '\n'.join(lines)


def failure_summary(console, spc_logs=None):
    sources = [('Stage console', console, 0)]
    for path, previous in (spc_logs or {}).items():
        if not path.is_file():
            continue
        current = path.stat()
        if (current.st_size, current.st_mtime_ns) == previous:
            continue
        offset = previous[0] if current.st_size > previous[0] else 0
        sources.append((str(path.relative_to(path.parent.parent)), path, offset))
    sections = []
    for label, path, offset in sources:
        excerpt = diagnostic_excerpt(path, offset)
        if excerpt:
            sections.append(label + ':\n' + excerpt)
    return bounded_detail('\n\n'.join(sections))


class StageFailure(RuntimeError):
    def __init__(self, stage, detail):
        super().__init__(stage + ': ' + detail)
        self.stage = stage
        self.detail = detail


@contextmanager
def abi_slot(semaphore, cancelled):
    while not cancelled.is_set():
        if not semaphore.acquire(timeout=0.1):
            continue
        try:
            if cancelled.is_set():
                raise StageFailure('prepare library ABI', 'Interrupted while waiting for an ABI slot')
            yield
        finally:
            semaphore.release()
        return
    raise StageFailure('prepare library ABI', 'Interrupted while waiting for an ABI slot')


class Target:
    def __init__(self, args, os_name, cancelled, abi_slots=None):
        self.args = args
        self.os = os_name
        self.kind = FORMATS[os_name]
        self.image = args.image_prefix + os_name + '-debug'
        self.cancelled = cancelled
        self.abi_slots = abi_slots if abi_slots is not None else threading.Semaphore(1)
        self.prefix = 'spp-matrix-' + os_name + '-' + uuid.uuid4().hex[:10]
        self.containers = []
        self.stages = []
        self.directory = None
        self.project = None
        self.spc = None

    def stage(self, name, command, *, container=None):
        if self.cancelled.is_set():
            raise StageFailure(name, 'Interrupted')
        announce(f'{self.os}: {name}')
        log = self.directory / ('stage-' + str(len(self.stages)) + '.log')
        started = time.monotonic()
        status = {'stage': name, 'ok': False}
        self.stages.append(status)
        spc_logs = {}
        if container and self.project:
            for filename in ('spc.output.log', 'spc.shell.log'):
                path = self.project / 'log' / filename
                if path.is_file():
                    path.unlink()
                spc_logs[path] = (0, 0)
        with log.open('w') as output:
            process = subprocess.Popen([str(x) for x in command], stdout=output, stderr=subprocess.STDOUT)
            try:
                while process.poll() is None:
                    if self.cancelled.wait(1):
                        process.terminate()
                        if container:
                            subprocess.run(['docker', 'rm', '-f', container], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                        try:
                            process.wait(timeout=30)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            process.wait()
                        raise StageFailure(name, 'Interrupted')
                    elapsed = int(time.monotonic() - started)
                    if elapsed and elapsed % 30 == 0:
                        announce(f'{self.os}: {name} still running ({elapsed}s)')
                if process.returncode:
                    raise StageFailure(name, failure_summary(log, spc_logs) or 'Exit code ' + str(process.returncode))
                status['ok'] = True
            finally:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=30)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
                status['seconds'] = round(time.monotonic() - started, 1)

    def builder(self, name, arguments):
        container = self.prefix + '-' + str(len(self.containers))
        self.containers.append(container)
        environment = {
            'HOME': '/build/.validation/home', 'TMPDIR': '/build/.validation/tmp',
            'XDG_CACHE_HOME': '/build/.validation/cache',
            'COMPOSER_CACHE_DIR': '/build/.validation/cache/composer',
            'GOCACHE': '/build/.validation/cache/go-build',
            'GOMODCACHE': '/build/.validation/cache/go-mod',
            'GOPATH': '/build/.validation/cache/go',
            'PKG_ROOT_PATH': '/build/.validation/pkgroot',
            'SPP_SPC_ROOT': '/spc', 'SPP_SPC_BINARY': '/spc/bin/spc',
            'SPC_REGISTRIES': '/build/spc.registry.yml',
            'SPC_LOGS_DIR': '/build/log',
            'SPC_CONCURRENCY': str(self.args.build_concurrency), 'CI': '1',
            'PYTHONDONTWRITEBYTECODE': '1',
        }
        command = ['docker', 'run', '--rm', '--name', container, '--platform', 'linux/amd64',
                   '--user', str(os.getuid()) + ':' + str(os.getgid()), '--workdir', '/build',
                   '--mount', f'type=bind,src={self.project},dst=/build',
                   '--mount', f'type=bind,src={self.spc},dst=/spc']
        for key, value in environment.items():
            command += ['--env', key + '=' + value]
        if os.environ.get('GITHUB_TOKEN'):
            command += ['--env', 'GITHUB_TOKEN']
        self.stage(name, [*command, self.image, *arguments], container=container)

    def cleanup(self):
        errors = []
        for container in reversed(self.containers):
            result = subprocess.run(['docker', 'rm', '-f', container], stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, universal_newlines=True)
            if result.returncode and 'No such container' not in result.stderr:
                errors.append(container + ': ' + result.stderr.strip())
        if self.directory:
            try:
                # Go's module cache makes directories read-only by default.
                # Only adjust directories inside this target's disposable tree.
                self.directory.chmod(self.directory.stat().st_mode | 0o700)
                for current, directories, _ in os.walk(self.directory, followlinks=False):
                    for name in directories:
                        path = Path(current) / name
                        if not path.is_symlink():
                            path.chmod(path.stat().st_mode | 0o700)
                shutil.rmtree(self.directory)
            except OSError as error:
                errors.append(str(self.directory) + ': ' + str(error))
        return errors

    def execute(self):
        reuse_root = getattr(self.args, 'library_matrix_root', None)
        result = {'os': self.os, 'image': self.image, 'libraries': False, 'library_mode': 'reused' if reuse_root else 'built', 'cells': [], 'stages': self.stages}
        self.directory = Path(tempfile.mkdtemp(prefix=self.prefix + '-'))
        try:
            if self.cancelled.is_set():
                raise StageFailure('prepare', 'Interrupted')
            reused = library_baseline(reuse_root, self.os) if reuse_root else None
            if reused:
                result['library_baseline'] = str(reused['directory'])
                result['library_manifest_sha256'] = reused['manifest_sha256']
            self.project = self.directory / 'project'
            self.spc = self.directory / 'spc'
            announce(f'{self.os}: copying isolated project, SPC and pinned downloads')
            copy_sources(self.args.project, self.project)
            copy_sources(self.args.spc, self.spc)
            # A source-mode SPC CLI uses its checkout's Composer dependencies.
            if not (self.spc / 'vendor/autoload.php').is_file():
                raise StageFailure('copy sources', 'SPC checkout must have vendor/autoload.php')
            shutil.copytree(self.args.downloads, self.project / 'downloads', symlinks=False)
            if '8.6' in self.args.php_versions:
                result['custom_master_commits'] = freeze_custom_master_sources(self.project)
            metadata = json.loads((self.project / 'downloads/.spc-meta.json').read_text())
            result['php_sources'] = {stream: metadata['php_versions'][stream] for stream in sorted(set(self.args.php_versions) | {'8.4'})}
            for relative in ('home', 'tmp', 'cache', 'empty-baseline'):
                (self.project / '.validation' / relative).mkdir(parents=True, exist_ok=True)
            self.builder('prepare builder tools', ['python3', '-c',
                         'import pathlib, shutil; source=pathlib.Path("/opt/spc-pkgroot"); '
                         'destination=pathlib.Path("/build/.validation/pkgroot"); '
                         'shutil.copytree(str(source), str(destination), symlinks=True) if source.is_dir() else destination.mkdir()'])
            baseline = self.directory / 'library-buildroot'
            packages = self.directory / 'library-packages'
            packages.mkdir()
            if reused:
                announce(f'{self.os}: copying reused library baseline (no library build or ABI preparation)')
                shutil.copytree(reused['buildroot'], baseline, symlinks=True)
                library_packages = reused['packages']
            else:
                self.builder('build libraries', ['php', 'bin/spp', 'build', '--libs-only', '--phpv=8.4', '--type=' + self.kind, '--debuginfo'])
                with abi_slot(self.abi_slots, self.cancelled):
                    self.builder('prepare library ABI', ['python3', 'bin/shared-library-plan', 'prepare',
                                 '--type', self.kind, '--manifest', '/build/buildroot/shared-libraries.json',
                                 '--root', '/build/buildroot', '--baseline', '/build/.validation/empty-baseline',
                                 '--mode', 'full', '--manifest-output', '/build/buildroot/shared-libraries.json',
                                 '--output', '/build/.validation/library-plan.json'])
                self.builder('package libraries', ['php', 'bin/spp', 'package', '--libs-only', '--phpv=8.4',
                             '--type=' + self.kind, '--iteration=1', '--debuginfo'])
                shutil.copytree(self.project / 'buildroot', baseline, symlinks=True)
                library_packages = (self.project / 'dist' / self.kind).glob('*.' + self.kind)
            for package in library_packages:
                shutil.copy2(package, packages / package.name)
            if not list(packages.iterdir()):
                raise StageFailure('package libraries', 'No library package artifacts were produced')
            result['libraries'] = True
            for php in self.args.php_versions:
                cell = {'php': php, 'ok': False}
                result['cells'].append(cell)
                try:
                    if self.cancelled.is_set():
                        raise StageFailure('PHP ' + php, 'Interrupted')
                    for directory in ('buildroot', 'build', 'dist', 'pkgroot'):
                        path = self.project / directory
                        if path.exists():
                            shutil.rmtree(path)
                    shutil.copytree(baseline, self.project / 'buildroot', symlinks=True)
                    dist = self.project / 'dist' / self.kind
                    shutil.copytree(packages, dist)
                    self.builder('build/package PHP ' + php,
                                 ['php', 'bin/spp', 'all', '--phpv=' + php, '--type=' + self.kind,
                                  '--iteration=1', '--debuginfo'])
                    cell['built'] = True
                    runtime_report = self.directory / ('runtime-' + php + '.json')
                    self.stage('fresh install/runtime PHP ' + php,
                               [sys.executable, ROOT / 'tests/shared-runtime-containers.py', '--os', self.os,
                                '--php', php, '--artifacts', dist, '--manifest', self.project / 'buildroot/shared-libraries.json',
                                '--report', runtime_report, *([] if php == self.args.php_versions[0] else ['--skip-fixtures'])])
                    validation = json.loads(runtime_report.read_text())['cells']
                    if len(validation) != 1 or not validation[0].get('ok'):
                        raise StageFailure('fresh install/runtime PHP ' + php, 'Runtime report did not verify one successful cell')
                    cell.update({'ok': True, 'runtime': validation[0]})
                except StageFailure as error:
                    cell.update({'stage': error.stage, 'error': error.detail})
                    announce(f'{self.os} PHP {php}: FAIL {error.stage}')
        except Exception as error:
            result['error'] = str(error)
            covered = {cell['php'] for cell in result['cells']}
            for php in self.args.php_versions:
                if php not in covered:
                    result['cells'].append({'php': php, 'ok': False, 'stage': getattr(error, 'stage', 'prepare'),
                                            'error': 'Library/build preparation failed: ' + str(error)})
        finally:
            errors = self.cleanup()
            if errors:
                result['cleanup_errors'] = errors
        result['ok'] = result['libraries'] and all(cell['ok'] for cell in result['cells']) and not result.get('cleanup_errors')
        return result


def write_report(destination, results, run_id, requested):
    start = '<!-- shared-matrix:' + run_id + ' -->'
    end = '<!-- /shared-matrix:' + run_id + ' -->'
    lines = [start, '## Real package matrix ' + run_id, '',
             'Each PHP result requires a build, packaging, independent consumer installations and fresh runtime checks. Library baselines are built and prepared in this run unless explicitly marked Reused.', '',
             f'Requested: {requested} PHP/distro combinations. Completed reports: {sum(len(target["cells"]) for target in results)}.', '',
             '| Distro | PHP | Libraries | PHP build/packages | Fresh install/runtime |', '|---|---|---|---|---|']
    for target in sorted(results, key=lambda entry: entry['os']):
        for cell in target['cells']:
            lines.append('| ' + target['os'] + ' | ' + cell['php'] + ' | '
                         + (('Reused' if target.get('library_mode') == 'reused' else 'Pass') if target['libraries'] else 'Fail') + ' | '
                         + ('Pass' if cell.get('built', cell['ok']) else 'Fail') + ' | '
                         + ('Pass' if cell['ok'] else 'Fail: ' + cell.get('stage', 'unknown').replace('|', '/')) + ' |')
    for target in results:
        if target.get('library_mode') == 'reused':
            lines += ['', 'Reused library baseline for ' + target['os'] + ': `' + target.get('library_baseline', 'invalid input') + '`; manifest SHA256 `' + target.get('library_manifest_sha256', 'unavailable') + '`. Libraries and ABI data were not rebuilt in this run.']
        if target.get('custom_master_commits'):
            lines += ['', 'Source snapshot for ' + target['os'] + ': PHP releases ' + ', '.join(stream + '=' + version for stream, version in sorted(target['php_sources'].items())) + '.',
                      'Compatibility master commits: ' + ', '.join(name + '=' + revision for name, revision in sorted(target['custom_master_commits'].items())) + '. Xdebug retains the fixed `3.6.0alpha1` tag.']
        failed = [cell for cell in target['cells'] if not cell['ok']]
        # Library failures share one cause across all PHP streams.
        if not target['libraries'] and failed:
            failed = failed[:1]
        for cell in failed:
            detail = bounded_detail(cell.get('error', 'No failure detail')).replace('```', "'''")
            lines += ['', f'Failure: {target["os"]} PHP {cell["php"]}, {cell.get("stage", "prepare")}.', '', '```text', detail, '```']
        for failure in target.get('cleanup_errors', []):
            lines += ['', 'Cleanup failed; remaining resource: ' + failure]
    lines += ['', 'Each distro cleans its raw logs, build outputs, copied downloads and temporary containers; any cleanup failures are listed above.', end]
    block = '\n'.join(lines) + '\n'
    previous = destination.read_text() if destination.exists() else '# Shared library validation\n'
    if start in previous:
        before, rest = previous.split(start, 1)
        _, after = rest.split(end, 1)
        content = before + block.rstrip('\n') + after
    else:
        content = previous.rstrip() + '\n\n' + block
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(content)


def csv(value, allowed, label):
    values = value.split(',')
    if not values or any(item not in allowed for item in values) or len(set(values)) != len(values):
        raise argparse.ArgumentTypeError(label + ' must be a comma-separated subset of ' + ','.join(allowed))
    return values


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.error('The legacy multi-distro build matrix is retired. Use the single-EL8 producer in '
                 '.github/workflows/build-rpm-modular-packages.yml; validate its sealed package artifacts '
                 'with tests/shared-runtime-containers.py and tests/migration-upgrade-containers.py.')


if __name__ == '__main__':
    sys.exit(main())
