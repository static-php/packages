#!/usr/bin/env python3
"""Prepare a real one-library package revision for service lifecycle validation."""
import argparse
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tarfile
import tempfile

ROOT = Path(__file__).resolve().parents[3]


def run(arguments, **kwargs):
    return subprocess.check_output([str(value) for value in arguments], text=True, **kwargs).strip()


def identity(path, kind):
    if kind == 'rpm':
        return run(['rpm', '-qp', '--nosignature', '--qf', '%{NAME}|%{VERSION}-%{RELEASE}', path]).split('|')
    if kind == 'deb':
        return [run(['dpkg-deb', '-f', path, field]) for field in ['Package', 'Version']]
    with gzip.open(path, 'rb') as stream, tarfile.open(fileobj=stream, mode='r|', ignore_zeros=True) as archive:
        for item in archive:
            if item.name == '.PKGINFO':
                fields = dict(re.findall(r'^(pkgname|pkgver)\s*=\s*(\S+)', archive.extractfile(item).read().decode(), re.M))
                return fields['pkgname'], fields['pkgver']
    raise RuntimeError('APK has no native package identity')


def metadata(path, kind, output):
    if kind == 'deb':
        subprocess.run(['dpkg-deb', '--extract', path, output], check=True)
    elif kind == 'rpm':
        with subprocess.Popen(['rpm2cpio', path], stdout=subprocess.PIPE) as producer:
            result = subprocess.run(['cpio', '-idm', '--quiet', '--no-absolute-filenames'], stdin=producer.stdout, cwd=output)
            producer.stdout.close()
            if producer.wait() or result.returncode:
                raise RuntimeError('Cannot extract runtime layout metadata')
    else:
        with gzip.open(path, 'rb') as stream, tarfile.open(fileobj=stream, mode='r|', ignore_zeros=True) as archive:
            for item in archive:
                if item.name.lstrip('./') == 'usr/share/php-zts/runtime-layout.json':
                    return json.load(archive.extractfile(item))
        raise RuntimeError('Runtime layout APK has no metadata')
    return json.loads((output / 'usr/share/php-zts/runtime-layout.json').read_text())


def archive_for(name, kind, candidates, baseline):
    for path in candidates.glob('*.' + kind):
        if identity(path, kind)[0] == name:
            return path.resolve()
    record = json.loads((baseline / 'baseline.json').read_text())
    for item in record['packages']:
        if item['name'] == name:
            return (baseline / item['path']).resolve()
    raise RuntimeError('Runtime validation requires the genuine current ' + name + ' archive')



def stage_library_sdk(source, destination):
    manifest = json.loads((source / 'shared-libraries.json').read_text())
    if manifest.get('pending_rebuilds'):
        raise RuntimeError('Cannot prepare a runtime revision from an unfinished SDK')
    entry = manifest['libraries']['zlib']
    paths = set(entry['files'] if isinstance(entry['files'], list) else entry['files'].keys())
    paths.update(entry.get('license_files', []))
    assets = entry.get('runtime_assets', {})
    paths.update(assets if isinstance(assets, dict) else [item['source'] for item in assets])
    destination.mkdir()
    for relative in sorted(paths):
        if not isinstance(relative, str) or Path(relative).is_absolute() or '..' in Path(relative).parts:
            raise RuntimeError('Invalid SDK ownership path')
        original = source / relative
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if original.is_symlink():
            target.symlink_to(os.readlink(original))
        else:
            shutil.copy2(original, target)
        fallback = source / 'debug' / (relative + '.debug')
        if fallback.is_file():
            debug = destination / 'debug' / (relative + '.debug')
            debug.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(fallback, debug)
    manifest['libraries'] = {'zlib': entry}
    (destination / 'shared-libraries.json').write_text(json.dumps(manifest) + '\n')
    record = json.loads((source / 'build-payload.json').read_text())
    (destination / 'provenance').mkdir()
    (destination / 'provenance/source-sealed-payload.json').write_text(json.dumps(record) + '\n')
    return record


def prepare(args):
    sdk = args.payload.resolve()
    candidates = args.candidates.resolve()
    baseline = args.baseline_dir.resolve()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    zlib = archive_for('php-zts-lib-zlib', args.type, candidates, baseline)
    _, old_version = identity(zlib, args.type)
    release = old_version.rsplit('-r' if args.type == 'apk' else '-', 1)[-1]
    match = re.match(r'([0-9]+)', release)
    if match is None:
        raise RuntimeError('Cannot allocate a native test revision above ' + old_version)
    iteration = int(match[1]) + 1
    layout = archive_for('php-zts-runtime-layout', args.type, candidates, baseline)
    with tempfile.TemporaryDirectory(prefix='spp-runtime-revision-') as temporary:
        task = Path(temporary)
        project = task / 'project'
        project.mkdir()
        for name in ['bin', 'src', 'config', 'composer.json', 'composer.lock', 'spc.registry.yml', 'craft.yml']:
            source = ROOT / name
            if source.is_dir():
                shutil.copytree(source, project / name, symlinks=True)
            elif source.is_file():
                shutil.copy2(source, project / name)
        (project / 'vendor').symlink_to(ROOT / 'vendor', target_is_directory=True)
        source_record = stage_library_sdk(sdk, project / 'buildroot')
        (project / 'craft.yml').write_text(json.dumps(source_record['configuration']) + '\n')
        subprocess.run(['php', 'bin/build-payload', 'record', '--kind', 'sdk', '--root', 'buildroot',
                        '--config', 'craft.yml', '--php-version', source_record['source_version'],
                        '--arch', source_record['architecture']], cwd=project, check=True)
        subprocess.run(['php', 'bin/build-payload', 'seal', '--root', 'buildroot'], cwd=project, check=True)
        unpacked = task / 'layout'
        unpacked.mkdir()
        layout_record = metadata(layout, args.type, unpacked)
        layout_root = task / 'baseline'
        (layout_root / 'archives').mkdir(parents=True)
        shutil.copy2(layout, layout_root / 'archives' / layout.name)
        layout_record.update(package_version=identity(layout, args.type)[1], complete=True,
                             archive='archives/' + layout.name, archive_sha256=hashlib.sha256(layout.read_bytes()).hexdigest())
        manifest = layout_root / 'shared-libraries.json'
        manifest.write_text(json.dumps({'schema': 1, 'linkage': 'shared', 'libraries': {}, 'runtime_layout': layout_record}) + '\n')
        environment = dict(os.environ, SPP_BASELINE_MANIFESTS=json.dumps([str(manifest)]))
        for name in ['SPP_SHARED_LIBRARY_PLAN', 'SPP_MIGRATION_CUTOFFS', 'SPP_DIST_PATH']:
            environment.pop(name, None)
        subprocess.run(['php', 'bin/spp', 'package', '--libs-only', '--libraries=zlib', '--type=' + args.type,
                        '--iteration=' + str(iteration)], cwd=project, env=environment, check=True)
        packages = list((project / 'dist' / args.type).glob('*.' + args.type))
        new = [path for path in packages if identity(path, args.type)[0] == 'php-zts-lib-zlib']
        if len(new) != 1 or any(identity(path, args.type)[0] != 'php-zts-lib-zlib' for path in packages):
            raise RuntimeError('Service fixture unexpectedly repackaged another runtime component')
        archive = output / new[0].name
        shutil.copy2(new[0], archive)
        result = {'schema': 1, 'type': args.type, 'library': 'zlib', 'source_archive': zlib.name,
                  'source_version': old_version, 'candidate_version': identity(archive, args.type)[1],
                  'iteration': iteration, 'archive': archive.name, 'sha256': hashlib.sha256(archive.read_bytes()).hexdigest(),
                  'production_packaging_api': True, 'compilation_performed': False}
        (output / 'runtime-check-revision.json').write_text(json.dumps(result, indent=2) + '\n')
        print(json.dumps(result))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--type', choices=['rpm', 'deb', 'apk'], required=True)
    parser.add_argument('--payload', type=Path, required=True)
    parser.add_argument('--candidates', type=Path, required=True)
    parser.add_argument('--baseline-dir', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    prepare(parser.parse_args())


if __name__ == '__main__':
    main()
