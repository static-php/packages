import gzip
import runpy
import shutil
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import requests


root = Path(__file__).resolve().parents[1]
forgejo = runpy.run_path(str(root / 'bin/forgejo-helper'))
rpm = runpy.run_path(str(root / 'bin/check-rpm-upgrades'))
forgejo_globals = forgejo['upload_packages'].__globals__
rpm_globals = rpm['published_packages'].__globals__


class UpgradeGuards(unittest.TestCase):
    def test_forgejo_pagination(self):
        responses = [Mock(json=lambda: [{'name': 'x', 'version': '1'}]), Mock(json=lambda: [{'name': 'x', 'version': '2'}]), Mock(json=lambda: [])]
        with patch.object(requests, 'get', side_effect=responses) as get:
            result = forgejo['get_remote_packages']('https://example.com', '86', 'test', 'debian')
            self.assertEqual(len(result), 2)
            self.assertEqual([call[1]['params']['page'] for call in get.call_args_list], [1, 2, 3])

    def test_forgejo_refuses_batch_before_upload(self):
        files = [root / 'safe.deb', root / 'older.deb']
        packages = [{'name': 'php-zts-xdebug', 'version': 'higher'}]
        with patch.dict(forgejo_globals, {
            'package_metadata': lambda file, kind: ('php-zts-xdebug', 'safe' if file == files[0] else 'older', 'amd64'),
            'version_is_newer': lambda kind, left, right: right == 'older',
            'get_remote_package_archs': lambda *args, **kwargs: {'amd64'},
            'get_remote_packages': lambda *args: packages,
        }), patch.object(Path, 'glob', return_value=files), patch.object(Path, 'is_file', return_value=True), patch.object(requests, 'put') as put:
            with self.assertRaisesRegex(RuntimeError, 'Refusing upload'):
                forgejo['upload_packages']('https://example.com', '86', 'test', 'debian', '*.deb')
            put.assert_not_called()

    def test_forgejo_read_failure_prevents_upload(self):
        with patch.dict(forgejo_globals, {'get_remote_packages': Mock(side_effect=requests.HTTPError('offline'))}), patch.object(requests, 'put') as put:
            with self.assertRaises(requests.HTTPError):
                forgejo['upload_packages']('https://example.com', '86', 'test', 'debian', '*.deb')
            put.assert_not_called()

    def test_forgejo_architecture_isolation(self):
        with patch.dict(forgejo_globals, {
            'package_metadata': lambda *args: ('php-zts-xdebug', 'older', 'arm64'),
            'version_is_newer': lambda *args: True,
            'get_remote_package_archs': lambda *args, **kwargs: {'amd64'},
        }):
            forgejo['verify_upload_versions']('https://example.com', '86', 'test', 'debian', [root / 'test.deb'], [{'name': 'php-zts-xdebug', 'version': 'higher'}])

    def test_rpm_reads_epoch_and_compressed_primary(self):
        repomd = b'<repomd xmlns="http://linux.duke.edu/metadata/repo"><data type="primary"><location href="repodata/primary.xml.gz"/></data></repomd>'
        primary = b'<metadata xmlns="http://linux.duke.edu/metadata/common"><package><name>php-zts-xdebug</name><arch>x86_64</arch><version epoch="1" ver="3.6.0_806.0+ext" rel="1.el10"/></package></metadata>'
        read = Mock(side_effect=[repomd, gzip.compress(primary)])
        result = rpm['published_packages'](read)
        self.assertEqual(result[0]['evr'], '1:3.6.0_806.0+ext-1.el10')

    def test_rpm_ssh_failure_prevents_verification(self):
        with patch.dict(rpm_globals, {'subprocess': Mock(run=lambda *args, **kwargs: Mock(returncode=255, stderr=b'offline'))}):
            with self.assertRaisesRegex(RuntimeError, 'Cannot read'):
                rpm['read_remote_file']('example.com', '/rpm', 'repodata/repomd.xml', True)

    def test_rpm_refuses_lower_version(self):
        local = {'name': 'php-zts-xdebug', 'arch': 'x86_64', 'version': '3.6.0_806.0+ext', 'evr': '0:3.6.0_806.0+ext-1.el10'}
        remote = dict(local, version='3.7.0_806.0+ext', evr='0:3.7.0_806.0+ext-1.el10')
        with patch.dict(rpm_globals, {'subprocess': Mock(run=lambda *args, **kwargs: Mock(returncode=12, stderr=''))}):
            with self.assertRaisesRegex(RuntimeError, 'Refusing upload'):
                rpm['verify_package'](local, [remote], '8.6')

    def test_rpm_stream_isolation(self):
        local = {'name': 'php-zts-xdebug', 'arch': 'x86_64', 'version': '3.6.0_806.0+ext', 'evr': '0:3.6.0_806.0+ext-1.el10'}
        remote = dict(local, version='9.0.0_805.0+ext', evr='0:9.0.0_805.0+ext-1.el10')
        commands = Mock()
        with patch.dict(rpm_globals, {'subprocess': commands}):
            rpm['verify_package'](local, [remote], '8.6')
            commands.run.assert_not_called()

    @unittest.skipUnless(shutil.which('dpkg'), 'dpkg required')
    def test_debian_native_comparison(self):
        self.assertTrue(forgejo['version_is_newer']('debian', '3.7.0+php806.0+ext-1', '3.6.0+php806.0+ext-1'))
        self.assertFalse(forgejo['version_is_newer']('debian', '3.6.0~dev+php86~beta2-1', '3.6.0+php806.0~rc3+ext~alpha1-1'))

    @unittest.skipUnless(shutil.which('apk'), 'apk required')
    def test_apk_native_comparison(self):
        self.assertTrue(forgejo['version_is_newer']('alpine', '3.7.0p806.0_p0-r0', '3.6.0p806.0_p0-r0'))
        self.assertFalse(forgejo['version_is_newer']('alpine', '3.6.0_pre_beta2_p86-r0', '3.6.0p806.0_rc3_p0_alpha1-r0'))

    @unittest.skipUnless(shutil.which('rpmdev-vercmp'), 'rpmdev-vercmp required')
    def test_rpm_native_epoch_comparison(self):
        local = {'name': 'php-zts-xdebug', 'arch': 'x86_64', 'version': '3.6.0_806.0+ext', 'evr': '0:3.6.0_806.0+ext-1.el10'}
        remote = dict(local, evr='1:3.6.0_806.0+ext-1.el10')
        with self.assertRaisesRegex(RuntimeError, 'Refusing upload'):
            rpm['verify_package'](local, [remote], '8.6')


if __name__ == '__main__':
    unittest.main()
