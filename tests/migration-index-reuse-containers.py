#!/usr/bin/env python3
"""Verify archive hardlinks survive native APK/DEB index generation and signing."""

import argparse
import json
from pathlib import Path
import runpy
import shutil
import signal
import tempfile


HERE = Path(__file__).resolve().parent


def inside(kind):
    migration = runpy.run_path(str(HERE / 'migration-upgrade-containers.py'))
    fixtures = runpy.run_path(str(HERE / 'shared-runtime-containers.py'))
    root = Path('/spp')
    current = root / 'repository/generations/new'
    incoming = root / 'library-input'
    payload = root / 'fixture-payload'
    payload.write_text('Native index reuse fixture; no executable payload.\n')
    for directory, name, version in [(current, 'php-zts-cli', '8.2.35'),
                                      (current, 'php-zts-lib-zlib', '1.0.0'),
                                      (incoming, 'php-zts-lib-zlib', '1.0.1')]:
        fixtures['fixture_package'](kind, directory, name, version, {'/usr/share/' + name: payload})
    migration['make_keys'](kind)
    packages = migration['index_generation'](kind, 'new', '8.2', 'x86_64')
    before = {package['name']: {'sha256': migration['sha256'](package['path']),
                              'inode': Path(package['path']).stat().st_ino} for package in packages}
    updates = migration['inspect_directory'](kind, incoming)
    library = root / 'repository/generations/library'
    migration['stage_library_generation'](kind, packages, updates, library)
    indexed = migration['index_generation'](kind, 'library', '8.2', 'x86_64')
    for package in packages:
        if migration['sha256'](package['path']) != before[package['name']]['sha256']:
            raise RuntimeError('Index/signing mutated an original finalized archive')
    common = next(package for package in indexed if package['name'] == 'php-zts-cli')
    if (Path(common['path']).stat().st_ino != before['php-zts-cli']['inode']
            or migration['sha256'](common['path']) != before['php-zts-cli']['sha256']):
        raise RuntimeError('Finalized common archive was copied or modified')
    update = next(package for package in indexed if package['name'] == 'php-zts-lib-zlib')
    original = Path(updates[0]['path'])
    if (Path(update['path']).stat().st_ino == original.stat().st_ino
            or migration['sha256'](update['path']) != migration['sha256'](original)):
        raise RuntimeError('Incoming library was not isolated by a fresh copy')
    if kind == 'apk':
        repository = migration['hook_repository'](root, kind, 'library', 'x86_64')
        migration['run'](['apk', '--no-network', '--repositories-file', '/dev/null', '--keys-dir',
                          '/spp/keys', '--repository', str(repository), 'add', 'php-zts-cli', 'php-zts-lib-zlib'])
        version = migration['run'](['apk', '--version']).stdout.splitlines()[0]
    else:
        migration['run'](['gpg', '--homedir', '/spp/keys/gpg', '--verify', str(library / 'InRelease')])
        version = migration['run'](['dpkg-scanpackages', '--version']).stdout.splitlines()[0]
    print('SPP_INDEX_REUSE=' + json.dumps({'ok': True, 'type': kind, 'native_index_tool': version,
          'common_archive_hardlinked': True, 'common_archive_sha256': before['php-zts-cli']['sha256'],
          'all_finalized_archives_unchanged': True, 'incoming_library_copied': True,
          'signed_generation_verified': True, 'shared_migration_pass': False}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--inside', choices=('apk', 'deb'))
    parser.add_argument('--type', choices=('apk', 'deb', 'all'), default='all')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    if args.inside:
        inside(args.inside)
        return 0
    if args.output is None:
        parser.error('--output is required')
    migration = runpy.run_path(str(HERE / 'migration-upgrade-containers.py'))
    def interrupted(signum, frame):
        raise KeyboardInterrupt('Interrupted by signal ' + str(signum))
    signal.signal(signal.SIGTERM, interrupted)
    report = {'schema': 1, 'ok': False, 'scope': __doc__, 'cells': [], 'shared_migration_pass': False}
    for kind, image in [('apk', 'alpine:3.24'), ('deb', 'debian:11')]:
        if args.type not in ('all', kind):
            continue
        cell = {'type': kind, 'ok': False}
        report['cells'].append(cell)
        with tempfile.TemporaryDirectory(prefix='spp-native-index-reuse-') as temporary:
            root = Path(temporary)
            root.chmod(0o755)
            resources = migration['Docker'](root, 'x86_64')
            try:
                if migration['run'](['docker', 'image', 'inspect', image], check=False).returncode:
                    raise RuntimeError('Required native fixture image is not already local: ' + image)
                resources.check_native()
                for name in ['migration-upgrade-containers.py', 'container-architecture.py',
                             'shared-runtime-containers.py', Path(__file__).name]:
                    shutil.copy2(HERE / name, root / name)
                cell['helper_sha256'] = {path.name: migration['sha256'](path) for path in root.glob('*.py')}
                container = resources.start(image, 'repo')
                migration['prepare'](resources, container, kind, helper=True)
                checked = resources.exec(container, ['python3', '/spp/' + Path(__file__).name, '--inside', kind])
                cell.update(json.loads(checked.stdout.split('SPP_INDEX_REUSE=')[-1]),
                            execution=resources.execution, runtime_evidence=resources.runtime_evidence)
            except BaseException as error:
                cell.update(ok=False, error=type(error).__name__ + ': ' + str(error),
                            interrupted=isinstance(error, (KeyboardInterrupt, SystemExit)))
            finally:
                remaining = resources.close()
                cell['cleanup'] = {'complete': not remaining, 'remaining': remaining}
                if remaining:
                    cell['ok'] = False
        cell['cleanup']['temporary_directory_removed'] = not root.exists()
        if cell.get('interrupted'):
            break
    report['ok'] = all(cell['ok'] for cell in report['cells'])
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report, indent=2))
    return 0 if report['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
