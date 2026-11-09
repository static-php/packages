#!/usr/bin/env python3

import gzip
import io
import json
from pathlib import Path
import runpy
import tarfile
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
HELPER = runpy.run_path(str(ROOT / 'bin/apk-reuse-published'))


def archive(path, revision=0, payload=b'working ELF', dependency='musl', script=b'exit 0', stamp=1, debug=False, cutoffs=None):
    name = 'php-zts-cli' + ('-debuginfo' if debug else '')
    metadata = f'pkgname = {name}\npkgver = 8.3.36-r{revision}\narch = x86_64\nbuilddate = {stamp}\ndatahash = ignored-{stamp}\n'
    metadata += f'depend = {dependency}\nsize = {len(payload) + len(json.dumps(cutoffs))}\n'
    if debug:
        metadata += f'depend = php-zts-cli=8.3.36-r{revision}\n'
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode='w') as target:
        entries = [('.PKGINFO', metadata.encode()), ('.post-install', script), ('usr/bin/php-zts', payload)]
        if cutoffs is not None:
            entries.append(('usr/share/php-zts/migration/cutoffs.json', json.dumps(cutoffs).encode()))
        for name, data in entries:
            entry = tarfile.TarInfo(name)
            entry.mode = 0o755
            entry.mtime = stamp
            entry.size = len(data)
            target.addfile(entry, io.BytesIO(data))
    path.write_bytes(gzip.compress(buffer.getvalue(), mtime=stamp))


class ReusePublished(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.old, self.new = self.root / 'old.apk', self.root / 'new.apk'
        self.manifest = self.root / 'baseline.json'
        self.native = patch('subprocess.run')
        self.verification = self.native.start()
        self.addCleanup(self.native.stop)
        self.verification.return_value.returncode = 0

    def baseline(self, debug=False):
        self.manifest.write_text(json.dumps({'type': 'apk', 'packages': [{
            'name': 'php-zts-cli' + ('-debuginfo' if debug else ''), 'architecture': 'x86_64',
            'version': '8.3.36-r3', 'path': self.old.name, 'sha256': HELPER['sha256'](self.old)}]}))

    def select(self):
        return HELPER['select'](self.new, [self.manifest])

    def test_unchanged_payload_reuses_original_archive_despite_build_times(self):
        archive(self.old, revision=3)
        archive(self.new, stamp=999)
        self.baseline()
        self.assertEqual(self.select(), {'revision': 3, 'reuse': str(self.old)})

    def test_changed_payload_dependencies_or_scripts_advance_only_once(self):
        archive(self.old, revision=3)
        self.baseline()
        for changes in [{'payload': b'fixed ELF'}, {'dependency': 'musl>=1.2'}, {'script': b'fixed script'}]:
            with self.subTest(changes=changes):
                archive(self.new, **changes)
                self.assertEqual(self.select(), {'revision': 4})

    def test_debug_reuses_matching_exact_runtime_identity(self):
        archive(self.old, revision=3, debug=True)
        archive(self.new, revision=3, debug=True, stamp=999)
        self.baseline(debug=True)
        self.assertEqual(self.select(), {'revision': 3, 'reuse': str(self.old)})
        archive(self.new, revision=4, debug=True)
        self.assertEqual(self.select(), {'revision': 4})

    def test_invalid_old_archive_is_replaced_even_when_its_payload_matches(self):
        archive(self.old, revision=3)
        archive(self.new)
        self.baseline()
        self.verification.return_value.returncode = 1
        self.assertEqual(self.select(), {'revision': 4})

    def test_malformed_old_archive_cannot_veto_a_valid_candidate(self):
        self.old.write_bytes(b'broken old gzip/tar')
        archive(self.new)
        self.baseline()
        self.assertEqual(self.select(), {'revision': 4})

    def test_missing_old_debug_archive_advances_just_its_runtime_pair(self):
        archive(self.old, revision=3)
        archive(self.new)
        self.baseline()
        data = json.loads(self.manifest.read_text())
        data['published_packages'] = [{'name': 'php-zts-cli-debuginfo', 'architecture': 'x86_64',
            'version': '8.3.36-r3', 'publication_url': 'https://example.test/missing-debug.apk',
            'published_checksum': {'algorithm': 'apk-control-sha1', 'value': 'Q1old'}}]
        self.manifest.write_text(json.dumps(data))
        def missing(*_):
            raise RuntimeError('Old archive unavailable')
        with patch.object(HELPER['select'].__globals__['runpy'], 'run_path',
                          return_value={'download_package': missing, 'BaselineDownloadError': RuntimeError}):
            self.assertEqual(HELPER['select'](self.new, [self.manifest], with_debug=True), {'revision': 4})

    def test_missing_old_runtime_uses_published_version_without_requiring_old_installation(self):
        archive(self.new)
        self.manifest.write_text(json.dumps({'type': 'apk', 'architecture': 'x86_64',
            'consumer_versions': {'8.3': {'php-zts-cli': '8.3.36-r3'}}}))
        self.assertEqual(self.select(), {'revision': 4})

    def test_metadata_does_not_force_bump_when_verified_archive_is_available(self):
        archive(self.old, revision=3)
        archive(self.new)
        self.baseline()
        data = json.loads(self.manifest.read_text())
        data.update(architecture='x86_64', consumer_versions={'8.3': {'php-zts-cli': '8.3.36-r3'}})
        self.manifest.write_text(json.dumps(data))
        self.assertEqual(self.select(), {'revision': 3, 'reuse': str(self.old)})

    def test_retry_inventory_does_not_bump_cli_but_changed_migration_constraints_do(self):
        rules = {'schema': 1, 'formats': {'apk': {'php-zts-amqp': '2.2.0.0_p83-r1'}}}
        archive(self.old, revision=3, cutoffs=dict(rules, candidate_archives={'old': 'hash'}, published_versions={'old': 'version'}))
        archive(self.new, cutoffs=dict(rules, candidate_archives={}, published_versions={'new': 'different inventory'}))
        self.baseline()
        self.assertEqual(self.select(), {'revision': 3, 'reuse': str(self.old)})
        rules['formats']['apk']['php-zts-amqp'] = '2.2.0.0_p83-r2'
        archive(self.new, cutoffs=rules)
        self.assertEqual(self.select(), {'revision': 4})

    def test_tampered_baseline_archive_is_rejected(self):
        archive(self.old, revision=3)
        archive(self.new)
        self.baseline()
        self.old.write_bytes(b'tampered')
        with self.assertRaisesRegex(RuntimeError, 'checksum/path mismatch'):
            self.select()

    def test_other_architecture_does_not_raise_revision(self):
        archive(self.old, revision=3)
        archive(self.new)
        self.baseline()
        data = json.loads(self.manifest.read_text())
        data['packages'][0]['architecture'] = 'aarch64'
        self.manifest.write_text(json.dumps(data))
        self.assertEqual(self.select(), {'revision': 0})


if __name__ == '__main__':
    unittest.main()
