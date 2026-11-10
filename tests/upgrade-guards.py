import contextlib
import gzip
import hashlib
import os
import struct
import subprocess
import io
import runpy
import shutil
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, mock_open, patch

import requests


root = Path(__file__).resolve().parents[1]
forgejo = runpy.run_path(str(root / 'bin/forgejo-helper'))
rpm = runpy.run_path(str(root / 'bin/check-rpm-upgrades'))
forgejo_globals = forgejo['upload_packages'].__globals__
rpm_globals = rpm['published_packages'].__globals__


class UpgradeGuards(unittest.TestCase):
    def setUp(self):
        client = patch.dict(forgejo_globals, {'registry_session': lambda: requests})
        client.start()
        self.addCleanup(client.stop)

    def test_rpm_signature_reuse_preserves_exact_remote_bytes_and_rejects_content_changes(self):
        def archive(signature, body=b'header payload'):
            header = b'\x8e\xad\xe8\x01' + bytes(4)
            lead = b'\xed\xab\xee\xdb' + bytes(92)
            signature = header + struct.pack('>II', 0, len(signature)) + signature
            signature += bytes((-len(signature)) % 8)
            return lead + signature + header + struct.pack('>II', 0, 0) + body
        original, signed = archive(b'old signature'), archive(b'new signature with different size')
        self.assertEqual(rpm['rpm_content'](original), rpm['rpm_content'](signed))
        with TemporaryDirectory() as directory:
            path = Path(directory) / 'package.rpm'
            path.write_bytes(original)
            local = dict(name='php-zts-fixture', arch='aarch64', evr='0:1.0-1', path=str(path))
            remote = dict(local, location='package.rpm', checksum=dict(type='sha256', value=hashlib.sha256(signed).hexdigest()))
            with patch.dict(rpm_globals, local_package=lambda _: local):
                self.assertTrue(rpm['reuse_published_signature'](path, [remote], lambda _: signed))
                self.assertEqual(path.read_bytes(), signed)
                rpm['same_archive'](local, remote)
                for changed in [archive(b'new signature', b'changed header or payload'), signed[:-1]]:
                    path.write_bytes(changed)
                    with self.assertRaisesRegex(RuntimeError, 'content differs'):
                        rpm['reuse_published_signature'](path, [remote], lambda _: signed)
                    self.assertEqual(path.read_bytes(), changed)
                path.write_bytes(original)
                with self.assertRaisesRegex(RuntimeError, 'checksum mismatch'):
                    rpm['reuse_published_signature'](path, [remote], lambda _: original)
                self.assertFalse(rpm['reuse_published_signature'](path, [dict(remote, arch='x86_64')], lambda _: signed))
        for invalid in [b'', signed[:100], signed[:104] + struct.pack('>II', 2**32 - 1, 0) + signed[112:]]:
            with self.assertRaisesRegex(RuntimeError, 'RPM'):
                rpm['rpm_content'](invalid)

    @unittest.skipUnless(all(shutil.which(tool) for tool in ('fpm', 'rpm', 'rpmsign', 'gpg')), 'native RPM signing tools required')
    def test_real_rpm_signature_is_reused_without_changing_the_built_package(self):
        with TemporaryDirectory() as directory:
            directory = Path(directory)
            payload = directory / 'payload'
            payload.mkdir()
            (payload / 'fixture').write_text('validated package bytes\n')
            path = directory / 'package.rpm'
            def run(*args, **kwargs):
                try:
                    return subprocess.check_output(args, stderr=subprocess.STDOUT, timeout=120, **kwargs)
                except subprocess.CalledProcessError as error:
                    raise RuntimeError(error.output.decode(errors='replace')) from error
            run('fpm', '-s', 'dir', '-t', 'rpm', '-n', 'php-zts-signature-fixture', '-v', '1.0', '--iteration', '1',
                '--package', str(path), '-C', str(payload), '.')
            unsigned = path.read_bytes()
            home = directory / 'gpg'
            home.mkdir(mode=0o700)
            environment = dict(os.environ, GNUPGHOME=str(home))
            run('gpg', '--batch', '--pinentry-mode', 'loopback', '--passphrase', '', '--quick-generate-key',
                'SPP signature fixture', 'rsa2048', 'sign', '0', env=environment)
            fingerprint = next(line.split(':')[9] for line in run('gpg', '--with-colons', '--list-secret-keys', env=environment).decode().splitlines() if line.startswith('fpr:'))
            run('rpmsign', '--define', '_gpg_name ' + fingerprint, '--define', '_gpg_path ' + str(home),
                '--define', '__gpg ' + shutil.which('gpg'), '--addsign', str(path), env=environment)
            signed = path.read_bytes()
            self.assertNotEqual(unsigned, signed)
            self.assertEqual(rpm['rpm_content'](unsigned), rpm['rpm_content'](signed))
            local = rpm['local_package'](path)
            remote = dict(local, location='package.rpm', checksum=dict(type='sha256', value=hashlib.sha256(signed).hexdigest()))
            path.write_bytes(unsigned)
            with self.assertRaisesRegex(RuntimeError, 'different bytes'):
                rpm['same_archive'](local, remote)
            self.assertTrue(rpm['reuse_published_signature'](path, [remote], lambda _: signed))
            rpm['same_archive'](local, remote)
            self.assertEqual(path.read_bytes(), signed)
            secret = run('gpg', '--batch', '--armor', '--export-secret-keys', fingerprint, env=environment).decode()
            workspace = directory / 'workspace'
            archives = workspace / 'dist/rpm'
            archives.mkdir(parents=True)
            candidate = archives / 'package.rpm'
            candidate.write_bytes(unsigned)
            metadata = directory / 'remote'
            (metadata / 'repodata').mkdir(parents=True)
            (metadata / 'package.rpm').write_bytes(signed)
            (metadata / 'repodata/repomd.xml').write_text('<repomd xmlns="http://linux.duke.edu/metadata/repo"><data type="primary"><location href="repodata/primary.xml.gz"/></data></repomd>')
            primary = '<metadata xmlns="http://linux.duke.edu/metadata/common"><package><name>{name}</name><arch>{arch}</arch><version epoch="0" ver="1.0" rel="1"/><location href="package.rpm"/><checksum type="sha256">{checksum}</checksum></package></metadata>'.format(checksum=hashlib.sha256(signed).hexdigest(), **local)
            (metadata / 'repodata/primary.xml.gz').write_bytes(gzip.compress(primary.encode()))
            tools = directory / 'tools'
            tools.mkdir()
            ssh = tools / 'ssh'
            ssh.write_text('#!' + sys.executable + '\nimport pathlib,shlex,sys\nparts=shlex.split(sys.argv[-1]);p=pathlib.Path(parts[parts.index("cat")+1].rstrip(";"));sys.stdout.buffer.write(p.read_bytes())\n')
            ssh.chmod(0o755)
            signing_environment = dict(environment, PATH=str(tools) + os.pathsep + environment['PATH'],
                RPM_HOST='fixture', RPM_DIRECTORY=str(metadata), RPM_SIGNING_KEY=secret, RPM_SIGNING_PASSWORD='')
            script = root / '.github/actions/shared-libraries/sign-rpm.sh'
            run('bash', str(script), cwd=workspace, env=signing_environment)
            self.assertEqual(candidate.read_bytes(), signed)
            candidate.write_bytes(unsigned[:-1] + bytes([unsigned[-1] ^ 1]))
            rejected = subprocess.run(['bash', str(script)], cwd=workspace, env=signing_environment,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            self.assertNotEqual(rejected.returncode, 0)
            self.assertIn(b'content differs', rejected.stderr)
            run('gpgconf', '--kill', 'gpg-agent', env=environment)

    def test_rpm_metadata_retries_connection_resets_without_hiding_failures(self):
        response = contextlib.nullcontext(io.BytesIO(b'verified metadata'))
        with patch.object(rpm_globals['urllib'].request, 'urlopen', side_effect=[ConnectionResetError('reset'), response]) as request, \
                patch.object(rpm_globals['time'], 'sleep'):
            self.assertEqual(rpm['read_http_file']('https://example.com', 'repodata/repomd.xml'), b'verified metadata')
            self.assertEqual(request.call_count, 2)
        with patch.object(rpm_globals['urllib'].request, 'urlopen', side_effect=ConnectionResetError('offline')) as request, \
                patch.object(rpm_globals['time'], 'sleep'), self.assertRaises(ConnectionResetError):
            rpm['read_http_file']('https://example.com', 'repodata/repomd.xml', missing_ok=True)
        self.assertEqual(request.call_count, 3)

    def test_rpm_metadata_retries_origin_errors_and_still_fails_closed(self):
        url = 'https://example.com/repodata/repomd.xml'
        for status in (429, 500, 520, 522, 525, 526):
            with self.subTest(status=status):
                error = rpm_globals['urllib'].error.HTTPError(url, status, 'unavailable', {}, io.BytesIO())
                response = contextlib.nullcontext(io.BytesIO(b'verified metadata'))
                with patch.object(rpm_globals['urllib'].request, 'urlopen', side_effect=[error, response]) as request, \
                        patch.object(rpm_globals['time'], 'sleep'):
                    self.assertEqual(rpm['read_http_file']('https://example.com', 'repodata/repomd.xml'), b'verified metadata')
                    self.assertEqual(request.call_count, 2)
                with patch.object(rpm_globals['urllib'].request, 'urlopen', side_effect=error) as request, \
                        patch.object(rpm_globals['time'], 'sleep'), self.assertRaisesRegex(RuntimeError, url + ': HTTP ' + str(status)):
                    rpm['read_http_file']('https://example.com', 'repodata/repomd.xml', missing_ok=True)
                self.assertEqual(request.call_count, 3)
                error.close()

    def test_rpm_library_ordering_is_php_independent(self):
        local = {'name': 'php-zts-lib-openssl', 'arch': 'x86_64', 'version': '3.0.1', 'evr': '0:3.0.1-1.el10'}
        remote = dict(local, version='3.1.0', evr='0:3.1.0-1.el10')
        commands = Mock(run=lambda *args, **kwargs: Mock(returncode=12, stderr=''))
        with patch.dict(rpm_globals, {'subprocess': commands}), self.assertRaisesRegex(RuntimeError, 'Refusing upload'):
            rpm['verify_package'](local, [remote], '8.6')

    def test_forgejo_preflight_does_not_upload(self):
        with patch.dict(forgejo_globals, {'get_remote_candidates': Mock(return_value=[]), 'verify_upload_versions': Mock(return_value=[])}), patch.object(Path, 'glob', return_value=[root / 'library.deb']), patch.object(Path, 'is_file', return_value=True), patch.object(requests, 'put') as upload, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(forgejo['check_packages']('https://example.com', 'libs', 'test', 'debian', '*.deb'), 3)
            upload.assert_not_called()

    def test_forgejo_pagination(self):
        for query in [None, 'php-zts-lib-']:
            with self.subTest(query=query):
                responses = [Mock(json=lambda: [{'name': 'x', 'version': '1'}]), Mock(json=lambda: [{'name': 'x', 'version': '2'}]), Mock(json=lambda: [])]
                with patch.object(requests, 'get', side_effect=responses) as get:
                    result = forgejo['get_remote_packages']('https://example.com', '86', 'test', 'debian', query)
                    self.assertEqual(len(result), 2)
                    self.assertEqual([call[1]['params']['page'] for call in get.call_args_list], [1, 2, 3])
                    self.assertTrue(all(call[1]['auth'] == ('86', 'test') for call in get.call_args_list))
                    self.assertTrue(all(call[1]['params'].get('q') == query for call in get.call_args_list))

    def test_public_forgejo_reads_omit_empty_credentials(self):
        for password in ('', None):
            with self.subTest(password=password):
                package = {'name': 'php-zts-cli', 'version': '82.35'}
                responses = [Mock(json=lambda: [package]), Mock(json=lambda: []), Mock(status_code=200, json=lambda: [])]
                with patch.object(requests, 'get', side_effect=responses) as get:
                    self.assertEqual(forgejo['get_remote_packages']('https://example.com', '82', password, 'debian'), [package])
                    self.assertEqual(forgejo['get_remote_package_files']('https://example.com', '82', password, 'debian', 'php-zts-cli', '82.35'), [])
                    self.assertEqual(get.call_count, 3)
                    self.assertTrue(all(call[1]['auth'] is None for call in get.call_args_list))

    def test_forgejo_skips_lower_package(self):
        files = [root / 'safe.deb', root / 'older.deb', root / 'older-debuginfo.deb']
        metadata = {
            files[0]: ('php-zts-safe', 'safe', 'amd64'),
            files[1]: ('php-zts-xdebug', 'older', 'amd64'),
            files[2]: ('php-zts-xdebug-debuginfo', 'older', 'amd64'),
        }
        packages = [{'name': 'php-zts-xdebug-debuginfo', 'version': 'higher'}]
        with patch.dict(forgejo_globals, {
            'package_metadata': lambda file, kind: metadata[file],
            'version_is_newer': lambda kind, left, right: right == 'older',
            'get_remote_package_archs': lambda *args, **kwargs: {'amd64'},
            'get_remote_packages': lambda *args: packages,
            'get_package_info': lambda file, kind: (*metadata[file], file.stem),
        }), patch.object(Path, 'glob', return_value=files), patch.object(Path, 'is_file', return_value=True), patch('builtins.open', mock_open()) as opened, patch.object(requests, 'put', return_value=Mock(status_code=201)) as put, patch.object(requests, 'post', return_value=Mock(status_code=201)), contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(forgejo['upload_packages']('https://example.com', '86', 'test', 'debian', '*.deb'), 3)
            put.assert_called_once()
            opened.assert_called_once_with(files[0], 'rb')
            self.assertIs(put.call_args[1]['files']['file'], opened.return_value)
        self.assertIn('::warning::Skipping upload of older.deb:', output.getvalue())
        self.assertIn('::error::Skipped 2 package files', output.getvalue())

    def test_forgejo_read_failure_prevents_upload(self):
        with patch.dict(forgejo_globals, {'get_remote_candidates': Mock(side_effect=requests.HTTPError('offline'))}), patch.object(requests, 'put') as put:
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
        primary = b'<metadata xmlns="http://linux.duke.edu/metadata/common"><package><name>php-zts-xdebug</name><arch>x86_64</arch><version epoch="1" ver="3.6.0_86.0+ext" rel="1.el10"/></package></metadata>'
        read = Mock(side_effect=[repomd, gzip.compress(primary)])
        result = rpm['published_packages'](read)
        self.assertEqual(result[0]['evr'], '1:3.6.0_86.0+ext-1.el10')

    def test_rpm_ssh_failure_prevents_verification(self):
        with patch.dict(rpm_globals, {'subprocess': Mock(run=lambda *args, **kwargs: Mock(returncode=255, stderr=b'offline'))}):
            with self.assertRaisesRegex(RuntimeError, 'Cannot read'):
                rpm['read_remote_file']('example.com', '/rpm', 'repodata/repomd.xml', True)

    def test_rpm_refreshes_index_when_publication_replaces_primary_metadata(self):
        template = '<repomd xmlns="http://linux.duke.edu/metadata/repo"><data type="primary"><location href="repodata/{}.xml.gz"/></data></repomd>'
        old, new = (template.format(name).encode() for name in ('old', 'new'))
        primary = b'<metadata xmlns="http://linux.duke.edu/metadata/common"><package><name>php-zts-cli</name><arch>x86_64</arch><version ver="8.6.0~rc5" rel="6"/></package></metadata>'
        read = Mock(side_effect=[old, None, new, gzip.compress(primary)])
        with patch.object(rpm_globals['time'], 'sleep'):
            result = rpm['published_packages'](read)
        self.assertEqual(result[0]['evr'], '0:8.6.0~rc5-6')
        self.assertEqual([call.args[0] for call in read.call_args_list],
                         ['repodata/repomd.xml', 'repodata/old.xml.gz', 'repodata/repomd.xml', 'repodata/new.xml.gz'])
        with patch.object(rpm_globals['time'], 'sleep'), self.assertRaisesRegex(RuntimeError, 'after 3 reads'):
            rpm['published_packages'](Mock(side_effect=[old, None] * 3))

    def test_rpm_refuses_lower_version(self):
        local = {'name': 'php-zts-xdebug', 'arch': 'x86_64', 'version': '3.6.0_86.0+ext', 'evr': '0:3.6.0_86.0+ext-1.el10'}
        remote = dict(local, version='3.7.0_86.0+ext', evr='0:3.7.0_86.0+ext-1.el10')
        with TemporaryDirectory() as directory:
            directory = Path(directory)
            metadata = {
                'safe.rpm': dict(local, name='php-zts-safe'),
                'older.rpm': local,
                'older-debuginfo.rpm': dict(local, name='php-zts-xdebug-debuginfo'),
            }
            for name in metadata:
                (directory / name).touch()
            manifest = directory / 'upload-files'
            with patch.dict(rpm_globals, {
                'local_package': lambda path: metadata[path.name],
                'published_packages': lambda read: [remote],
                'subprocess': Mock(run=lambda *args, **kwargs: Mock(returncode=12, stderr='')),
            }), patch.object(sys, 'argv', ['check-rpm-upgrades', '--host', 'example.com', '--remote-directory', '/rpm', '--php-version', '8.6', '--files-from', str(manifest), str(directory)]), contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(rpm['main'](), 3)
            self.assertIn('::warning::Skipping upload of older.rpm:', output.getvalue())
            self.assertIn('::warning::Skipping upload of older-debuginfo.rpm:', output.getvalue())
            self.assertEqual(manifest.read_bytes(), b'safe.rpm\0')

    def test_rpm_stream_isolation(self):
        local = {'name': 'php-zts-xdebug', 'arch': 'x86_64', 'version': '3.6.0_86.0+ext', 'evr': '0:3.6.0_86.0+ext-1.el10'}
        remote = dict(local, version='9.0.0_85.0+ext', evr='0:9.0.0_85.0+ext-1.el10')
        commands = Mock()
        with patch.dict(rpm_globals, {'subprocess': commands}):
            rpm['verify_package'](local, [remote], '8.6')
            commands.run.assert_not_called()

    @unittest.skipUnless(shutil.which('dpkg'), 'dpkg required')
    def test_debian_native_comparison(self):
        self.assertTrue(forgejo['version_is_newer']('debian', '3.7.0+php86.0+ext-1', '3.6.0+php86.0+ext-1'))
        self.assertFalse(forgejo['version_is_newer']('debian', '3.6.0~dev+php86~beta2-1', '3.6.0+php86.0~rc3+ext~alpha1-1'))
        published = '1.13.1+php82.35+ext-1'
        self.assertTrue(forgejo['version_is_newer']('debian', published, '1.13.1+php82.35-2'))
        self.assertTrue(forgejo['version_is_newer']('debian', '1.13.2+php82.35-1', published))
        self.assertTrue(forgejo['version_is_newer']('debian', '1.13.1+php82.36-1', published))

    @unittest.skipUnless(shutil.which('apk'), 'apk required')
    def test_apk_native_comparison(self):
        self.assertTrue(forgejo['version_is_newer']('alpine', '3.7.0p86.0_p0-r0', '3.6.0p86.0_p0-r0'))
        self.assertFalse(forgejo['version_is_newer']('alpine', '3.6.0_pre_beta2_p86-r0', '3.6.0p86.0_rc3_p0_alpha1-r0'))
        published = '1.13.1p82.35_p0-r0'
        self.assertTrue(forgejo['version_is_newer']('alpine', published, '1.13.1p82.35-r1'))
        self.assertTrue(forgejo['version_is_newer']('alpine', '1.13.2p82.35-r0', published))
        self.assertTrue(forgejo['version_is_newer']('alpine', '1.13.1p82.36-r0', published))

    @unittest.skipUnless(shutil.which('rpmdev-vercmp'), 'rpmdev-vercmp required')
    def test_rpm_suffix_removal(self):
        remote = {'name': 'frankenphp', 'arch': 'aarch64', 'version': '1.13.1_82.35+ext', 'evr': '0:1.13.1_82.35+ext-1.el10'}
        local = dict(remote, version='1.13.1_82.35', evr='0:1.13.1_82.35-2.el10')
        with self.assertRaisesRegex(RuntimeError, 'Refusing upload'):
            rpm['verify_package'](local, [remote], '8.2')
        for version in ['1.13.2_82.35', '1.13.1_82.36']:
            with self.subTest(version=version):
                rpm['verify_package'](dict(remote, version=version, evr=f'0:{version}-1.el10'), [remote], '8.2')

    @unittest.skipUnless(shutil.which('rpmdev-vercmp'), 'rpmdev-vercmp required')
    def test_rpm_native_epoch_comparison(self):
        local = {'name': 'php-zts-xdebug', 'arch': 'x86_64', 'version': '3.6.0_86.0+ext', 'evr': '0:3.6.0_86.0+ext-1.el10'}
        remote = dict(local, evr='1:3.6.0_86.0+ext-1.el10')
        with self.assertRaisesRegex(RuntimeError, 'Refusing upload'):
            rpm['verify_package'](local, [remote], '8.6')


if __name__ == '__main__':
    unittest.main()
