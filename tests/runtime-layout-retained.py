#!/usr/bin/env python3
"""Reuse a lazily recorded layout from a checksum-verified frozen native baseline."""
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
WORKER = r'''
require $argv[1] . '/vendor/autoload.php';
define('BASE_PATH', $argv[1]);
define('DIST_PATH', $argv[2] . '/dist');
(new ReflectionProperty(staticphp\step\CreatePackages::class, 'packageType'))->setValue(null, 'apk');
$layout = json_decode(file_get_contents($argv[2] . '/layout.json'), true);
(new ReflectionMethod(staticphp\step\CreatePackages::class, 'reuseLayoutArchive'))->invoke(null, $argv[2] . '/metadata/shared-libraries.json', $layout, 'aarch64');
'''


class RetainedLayout(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        for path in ['metadata', 'baseline/artifacts', 'dist/apk']:
            (self.root / path).mkdir(parents=True)
        self.archive = self.root / 'baseline/artifacts/layout.apk'
        with tarfile.open(self.archive, 'w:gz') as archive:
            body = b'pkgname = php-zts-runtime-layout\npkgver = 2-r0\narch = aarch64\n'
            info = tarfile.TarInfo('.PKGINFO')
            info.size = len(body)
            archive.addfile(info, io.BytesIO(body))
        self.layout = {'archive': 'archives/missing.apk', 'package_version': '2-r0',
                       'archive_sha256': hashlib.sha256(self.archive.read_bytes()).hexdigest()}
        (self.root / 'layout.json').write_text(json.dumps(self.layout))
        self.captured = {'type': 'apk', 'architecture': 'arm64', 'php_stream': '8.4', 'packages': [
            {'name': 'php-zts-runtime-layout', 'path': 'artifacts/layout.apk', 'sha256': self.layout['archive_sha256']}]}
        (self.root / 'metadata/shared-libraries.json').write_text('{}')

    def run_reuse(self):
        snapshot = self.root / 'baseline/baseline.json'
        snapshot.write_text(json.dumps(self.captured))
        env = dict(os.environ, SPP_FORGEJO_OWNER='84', SPP_BASELINE_MANIFESTS=json.dumps([str(snapshot)]))
        return subprocess.run(['php', '-r', WORKER, str(ROOT), str(self.root)], env=env, text=True, capture_output=True)

    def test_missing_lazy_archive_uses_exact_frozen_native_bytes(self):
        result = self.run_reuse()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.root / 'dist/apk/php-zts-runtime-layout-2-r0.aarch64.apk').read_bytes(), self.archive.read_bytes())

    def test_tampered_lazy_archive_cannot_fall_back(self):
        target = self.root / 'metadata/archives/missing.apk'
        target.parent.mkdir()
        target.write_bytes(b'tampered')
        result = self.run_reuse()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Invalid retained runtime layout archive', result.stderr)

    def test_tampered_frozen_bytes_are_rejected(self):
        self.archive.write_bytes(b'tampered')
        self.assertNotEqual(self.run_reuse().returncode, 0)

    def test_other_stream_or_architecture_cannot_supply_layout(self):
        for key, value in [('php_stream', '8.5'), ('architecture', 'x86_64')]:
            with self.subTest(key=key):
                previous = self.captured[key]
                self.captured[key] = value
                self.assertNotEqual(self.run_reuse().returncode, 0)
                self.captured[key] = previous

    def test_frozen_archive_cannot_escape_its_baseline_directory(self):
        self.captured['packages'][0]['path'] = '../outside.apk'
        (self.root / 'outside.apk').write_bytes(self.archive.read_bytes())
        self.assertNotEqual(self.run_reuse().returncode, 0)


if __name__ == '__main__':
    unittest.main()
