#!/usr/bin/env python3
"""Verify legacy RPM resigning only mutates isolated disposable mirror copies."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import runpy
import shutil
import signal
import struct
import tempfile


HERE = Path(__file__).resolve().parent


def archive_content(path):
    with Path(path).open('rb') as stream:
        if stream.read(4) != b'\xed\xab\xee\xdb':
            raise RuntimeError('Native fixture is not an RPM archive')
        def header(offset):
            stream.seek(offset)
            prefix = stream.read(16)
            if len(prefix) != 16 or prefix[:4] != b'\x8e\xad\xe8\x01':
                raise RuntimeError('Invalid native RPM header')
            entries, size = struct.unpack('>II', prefix[8:])
            length = 16 + entries * 16 + size
            if length > 16 * 1024 * 1024:
                raise RuntimeError('Unexpected native RPM header size')
            stream.seek(offset)
            data = stream.read(length)
            if len(data) != length:
                raise RuntimeError('Truncated native RPM header')
            return data
        signature = header(96)
        offset = 96 + ((len(signature) + 7) // 8) * 8
        main = header(offset)
        digest = hashlib.sha256()
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
        return {'main_header_sha256': hashlib.sha256(main).hexdigest(), 'payload_sha256': digest.hexdigest()}


def inside():
    migration = runpy.run_path(str(HERE / 'migration-upgrade-containers.py'))
    source = Path('/spp/published.rpm')
    original = migration['sha256'](source)
    before = archive_content(source)
    identity = migration['inspect_package']('rpm', source)
    fingerprint = migration['make_keys']('rpm')
    directory = Path('/spp/repository/generations/old')
    directory.mkdir(parents=True)
    failing = directory / 'legacy-attempt.rpm'
    shutil.copy2(source, failing)
    legacy = migration['run'](['rpmsign', '--define', '_gpg_name ' + fingerprint,
                              '--define', '_gpg_path /spp/keys/gpg', '--define', '__gpg /usr/bin/gpg',
                              '--addsign', failing], check=False)
    if not legacy.returncode or 'already contains a legacy signature' not in legacy.stderr:
        raise RuntimeError('Fixture does not reproduce the native legacy-signature rejection')
    failing.unlink()
    shared = directory / 'shared.rpm'
    os.link(source, shared)
    for unsafe in (source, shared):
        try:
            migration['sign_rpm_for_mirror'](unsafe, fingerprint)
        except RuntimeError as error:
            if 'isolated scratch archive' not in str(error):
                raise
        else:
            raise RuntimeError('Native signer accepted a non-isolated archive')
    shared.unlink()
    isolated = directory / 'published.rpm'
    shutil.copy2(source, isolated)
    if migration['sha256'](isolated) != original or isolated.stat().st_nlink != 1:
        raise RuntimeError('Published archive was not copied and verified before signing')
    verified = migration['sign_rpm_for_mirror'](isolated, fingerprint)
    after = migration['inspect_package']('rpm', isolated)
    if (archive_content(isolated) != before
            or any(after[key] != identity[key] for key in ('name', 'version', 'architecture'))
            or migration['sha256'](source) != original):
        raise RuntimeError('Mirror signing changed published content or package identity')
    print('SPP_RPM_SIGNATURE=' + json.dumps({'ok': True, 'rpm_version': migration['run'](['rpm', '--version']).stdout.strip(),
          'legacy_rejection_reproduced': True, 'nonisolated_inputs_rejected': True,
          'original_sha256': original, 'original_unchanged': True, 'identity': {key: identity[key] for key in ('name', 'version', 'architecture')},
          'content': before, 'header_payload_unchanged': True, 'local_signature_check': verified.stdout.strip(),
          'shared_migration_pass': False}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--inside', action='store_true')
    parser.add_argument('--baseline', type=Path)
    parser.add_argument('--image', default='ghcr.io/static-php/packages-builder-rhel-10:latest')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    if args.inside:
        inside()
        return 0
    if args.baseline is None or args.output is None:
        parser.error('--baseline and --output are required')
    migration = runpy.run_path(str(HERE / 'migration-upgrade-containers.py'))
    def interrupted(signum, frame):
        raise KeyboardInterrupt('Interrupted by signal ' + str(signum))
    signal.signal(signal.SIGTERM, interrupted)
    report = {'schema': 1, 'ok': False, 'scope': __doc__, 'shared_migration_pass': False}
    original = migration['sha256'](args.baseline)
    with tempfile.TemporaryDirectory(prefix='spp-native-rpm-signature-') as temporary:
        root = Path(temporary)
        root.chmod(0o755)
        resources = migration['Docker'](root, 'x86_64')
        try:
            if migration['run'](['docker', 'image', 'inspect', args.image], check=False).returncode:
                raise RuntimeError('Native Alma10 fixture image is not already local')
            resources.check_native()
            for name in ['migration-upgrade-containers.py', 'container-architecture.py', Path(__file__).name]:
                shutil.copy2(HERE / name, root / name)
            shutil.copy2(args.baseline, root / 'published.rpm')
            if migration['sha256'](root / 'published.rpm') != original:
                raise RuntimeError('Published archive changed while copying')
            report['helper_sha256'] = {path.name: migration['sha256'](path) for path in root.glob('*.py')}
            container = resources.start(args.image, 'repo')
            checked = resources.exec(container, ['python3', '/spp/' + Path(__file__).name, '--inside'])
            report.update(json.loads(checked.stdout.split('SPP_RPM_SIGNATURE=')[-1]),
                          execution=resources.execution, runtime_evidence=resources.runtime_evidence)
            report['immutable_baseline_unchanged'] = migration['sha256'](args.baseline) == original
            if not report['immutable_baseline_unchanged']:
                raise RuntimeError('Immutable published baseline changed')
        except BaseException as error:
            report.update(ok=False, error=type(error).__name__ + ': ' + str(error))
        finally:
            remaining = resources.close()
            report['cleanup'] = {'complete': not remaining, 'remaining': remaining}
            if remaining:
                report['ok'] = False
    report['cleanup']['temporary_directory_removed'] = not root.exists()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report, indent=2))
    return 0 if report['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
