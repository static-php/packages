#!/usr/bin/env python3

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import urllib.error
from unittest import mock


sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('freeze_pie', ROOT / 'bin/freeze-pie.py')
freezer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(freezer)


class FrozenPie(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='spp-frozen-pie-test-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.downloads = self.root / 'downloads'
        self.downloads.mkdir()
        self.snapshot = {'php_versions': {'8.6': '8.6.0RC4'}, 'pie_version': 'v1.5.3'}
        (self.downloads / '.spc-meta.json').write_text(json.dumps(self.snapshot))
        self.content = b'<?php throw new RuntimeException("Packaging must not execute PIE");'
        self.sha = hashlib.sha256(self.content).hexdigest()
        self.release = {'tag_name': '1.5.3', 'assets': [{
            'name': 'pie.phar', 'browser_download_url': 'https://github.com/php/pie/releases/download/1.5.3/pie.phar',
            'digest': 'sha256:' + self.sha}]}

    def fetch(self, url, authenticated=False):
        if authenticated:
            self.assertEqual(url, 'https://api.github.com/repos/php/pie/releases/tags/v1.5.3')
            return json.dumps(self.release).encode()
        self.assertEqual(url, self.release['assets'][0]['browser_download_url'])
        return self.content

    def freeze(self, detected='1.5.3'):
        with mock.patch.object(freezer, 'fetch', side_effect=self.fetch), mock.patch.object(freezer, 'probe', return_value=detected):
            return freezer.freeze(self.downloads)

    def package(self, seal=False):
        code = r'''
require $argv[1] . '/vendor/autoload.php';
define('BASE_PATH', $argv[2]);
define('BUILD_ROOT_PATH', BASE_PATH . '/buildroot');
try {
    if ($argv[3] === 'seal') {
        \staticphp\util\BuildPayload::record(BUILD_ROOT_PATH, ['extra-env' => ['SPC_LIBC' => 'glibc'], 'sapi' => []], '8.6.0RC4', 'x86_64');
        \staticphp\util\BuildPayload::seal(BUILD_ROOT_PATH);
    }
    echo json_encode(['version' => (new \staticphp\package\pie())->getVersion()]);
} catch (Throwable $error) {
    echo json_encode(['error' => $error->getMessage()]);
    exit(1);
}
'''
        result = subprocess.run(['php', '-r', code, str(ROOT), str(self.root), 'seal' if seal else 'package'],
                                capture_output=True, text=True)
        return result.returncode, json.loads(result.stdout)

    def test_source_snapshot_freezes_exact_tag_bytes_and_version(self):
        record = self.freeze()
        self.assertEqual(record['version'], '1.5.3')
        self.assertEqual(record['sha256'], self.sha)
        self.assertEqual((self.downloads / 'pie.phar').read_bytes(), self.content)
        snapshot = json.loads((self.downloads / '.spc-meta.json').read_text())
        self.assertEqual(snapshot['php_versions'], self.snapshot['php_versions'])
        self.assertEqual(snapshot['pie_sha256'], self.sha)
        self.assertEqual(snapshot['pie_version'], '1.5.3')
        self.assertEqual(list(self.downloads.glob('spp-pie-freeze-*')), [])

    def test_archive_version_mismatch_does_not_replace_snapshot(self):
        with self.assertRaisesRegex(RuntimeError, 'version differs'):
            self.freeze(detected='1.5.4')
        self.assertFalse((self.downloads / 'pie.phar').exists())
        self.assertEqual(json.loads((self.downloads / '.spc-meta.json').read_text()), self.snapshot)
        self.assertEqual(list(self.downloads.glob('spp-pie-freeze-*')), [])

    def test_unchanged_archive_uses_published_digest_before_downloading(self):
        self.freeze()
        with mock.patch.object(freezer, 'fetch', return_value=json.dumps(self.release).encode()) as fetch, \
                mock.patch.object(freezer, 'probe', return_value='1.5.3'):
            self.assertEqual(freezer.freeze(self.downloads)['sha256'], self.sha)
        self.assertEqual(fetch.call_count, 1)
        self.assertTrue(fetch.call_args.kwargs['authenticated'])

    def test_corrupt_cached_archive_is_downloaded_again(self):
        (self.downloads / 'pie.phar').write_bytes(b'partial download')
        self.assertEqual(self.freeze()['sha256'], self.sha)

    def test_normalized_version_still_resolves_its_exact_prefixed_tag(self):
        self.snapshot['pie_version'] = '1.5.3'
        (self.downloads / '.spc-meta.json').write_text(json.dumps(self.snapshot))
        def fetch(url, authenticated=False):
            if authenticated and url.endswith('/tags/1.5.3'):
                raise urllib.error.HTTPError(url, 404, 'Not found', {}, None)
            return self.fetch(url, authenticated)
        with mock.patch.object(freezer, 'fetch', side_effect=fetch), mock.patch.object(freezer, 'probe', return_value='1.5.3'):
            self.assertEqual(freezer.freeze(self.downloads)['version'], '1.5.3')

    def test_source_archive_digest_is_checked(self):
        self.release['assets'][0]['digest'] = 'sha256:' + '0' * 64
        with self.assertRaisesRegex(RuntimeError, 'published SHA256'):
            self.freeze()
        self.assertFalse((self.downloads / 'pie.phar').exists())

    def test_packaging_uses_frozen_metadata_without_executing_phar(self):
        self.freeze()
        status, result = self.package()
        self.assertEqual(status, 0, result)
        self.assertEqual(result['version'], '1.5.3')

    def test_packaging_rejects_missing_hash_in_old_snapshot(self):
        (self.downloads / 'pie.phar').write_bytes(self.content)
        status, result = self.package()
        self.assertEqual(status, 1)
        self.assertIn('must be frozen', result['error'])

    def test_packaging_rejects_changed_snapshot_bytes(self):
        self.freeze()
        (self.downloads / 'pie.phar').write_bytes(self.content + b'changed')
        status, result = self.package()
        self.assertEqual(status, 1)
        self.assertIn('SHA256 mismatch', result['error'])

    def test_packaging_accepts_sealed_producer_asset_without_downloads(self):
        directory = self.root / 'buildroot/packaging/pie'
        directory.mkdir(parents=True)
        (directory / 'pie.phar').write_bytes(self.content)
        (directory / 'metadata.json').write_text(json.dumps({'schema': 1, 'version': '1.5.3', 'sha256': self.sha}))
        status, result = self.package(seal=True)
        self.assertEqual(status, 0, result)
        self.assertEqual(result['version'], '1.5.3')


if __name__ == '__main__':
    unittest.main()
