import contextlib
import gzip
import hashlib
import io
import json
import runpy
import shutil
import subprocess
import tarfile
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]
helper = runpy.run_path(str(ROOT / 'bin/forgejo-helper'))
publisher = runpy.run_path(str(ROOT / '.github/actions/shared-libraries/publish-forgejo.py'))
helper_globals = helper['upload_verified_archive'].__globals__


def apk_bytes(name, version, arch, signed=False, cutoffs=None):
    def member(filename, content):
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode='w') as archive:
            info = tarfile.TarInfo(filename)
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))
        return gzip.compress(stream.getvalue(), mtime=0)
    data = member('.PKGINFO', f'pkgname = {name}\npkgver = {version}\narch = {arch}\n'.encode())
    if cutoffs is not None:
        data += member(helper['MIGRATION_CUTOFF_PATH'], json.dumps(cutoffs).encode())
    return member('.SIGN.RSA.fixture', b'signature') + data if signed else data


def response(status=200, payload=None, data=b''):
    result = Mock(status_code=status)
    result.json.return_value = payload
    result.iter_content.return_value = [data]
    result.__enter__ = Mock(return_value=result)
    result.__exit__ = Mock(return_value=False)
    return result


class ForgejoPublication(unittest.TestCase):
    def setUp(self):
        helper_globals['VERIFIED_ARCHIVE_IDENTITIES'].clear()
        self.temporary = TemporaryDirectory(prefix='forgejo-publication-test-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def archive(self, name='php-zts-lib-ssl', version='3.6.0-r1', arch='x86_64'):
        path = self.root / f'{name}-{version}.{arch}.apk'
        path.write_bytes(apk_bytes(name, version, arch))
        return path

    def test_owner_listing_records_owner_and_reads_every_stream(self):
        listing = Mock(side_effect=lambda host, owner, password, kind: [{'name': 'library', 'version': owner}])
        with patch.dict(helper_globals, {'get_remote_packages': listing}):
            records = helper['get_remote_packages_for_owners']('https://example.com', publisher['STREAM_OWNERS'], 'test', 'alpine')
        self.assertEqual([record['owner'] for record in records], list(publisher['STREAM_OWNERS']))
        self.assertEqual(listing.call_count, 5)

    def test_signed_apk_header_and_sha_are_read_from_file_id(self):
        data = apk_bytes('php-zts-lib-ssl', '3.6.0-r1', 'aarch64', signed=True)
        calls = []
        def get(url, **kwargs):
            calls.append(url)
            return response(payload=[{'id': 45, 'name': 'php-zts-lib-ssl-3.6.0-r1.apk'}]) if url.endswith('/files') else response(data=data)
        with patch.object(helper_globals['requests'], 'get', side_effect=get):
            records = helper['remote_package_archives']('https://example.com', '86', 'test', 'alpine', 'php-zts-lib-ssl', '3.6.0-r1')
        self.assertEqual(records[0]['arch'], 'aarch64')
        self.assertEqual(records[0]['sha256'], hashlib.sha256(data).hexdigest())
        self.assertTrue(calls[-1].endswith('/files/45'))

    def test_registry_and_archive_header_mismatch_fails(self):
        data = apk_bytes('wrong-name', '3.6.0-r1', 'x86_64')
        with patch.object(helper_globals['requests'], 'get', side_effect=[response(payload=[{'id': 45, 'name': 'library.apk'}]), response(data=data)]):
            with self.assertRaisesRegex(RuntimeError, 'header differs'):
                helper['remote_package_archives']('https://example.com', '86', 'test', 'alpine', 'library', '3.6.0-r1')

    def test_verified_file_is_downloaded_once_but_registry_is_read_again(self):
        data = apk_bytes('php-zts-lib-ssl', '3.6.0-r1', 'x86_64')
        digest = hashlib.sha256(data).hexdigest()
        files = [{'id': 45, 'name': 'library.apk', 'sha256': digest}]
        with patch.dict(helper_globals, {'read_consumer_sidecar': lambda *args: None}), patch.object(helper_globals['requests'], 'get', side_effect=[response(payload=files), response(data=data), response(payload=files), response(payload=[])]) as get:
            first = helper['remote_package_archives']('https://example.com', '86', 'test', 'alpine', 'php-zts-lib-ssl', '3.6.0-r1')
            first[0]['native_metadata']['depends'].append('mutated caller result')
            second = helper['remote_package_archives']('https://example.com', '86', 'test', 'alpine', 'php-zts-lib-ssl', '3.6.0-r1')
            self.assertEqual(second[0]['native_metadata']['depends'], [])
            self.assertEqual(helper['remote_package_archives']('https://example.com', '86', 'test', 'alpine', 'php-zts-lib-ssl', '3.6.0-r1'), [])
        self.assertEqual(get.call_count, 4)

    def test_changed_digest_requires_download_and_cannot_reuse_old_identity(self):
        data = apk_bytes('php-zts-lib-ssl', '3.6.0-r1', 'x86_64')
        files = [{'id': 45, 'name': 'library.apk', 'sha256': hashlib.sha256(data).hexdigest()}]
        changed = [dict(files[0], sha256='0' * 64)]
        with patch.dict(helper_globals, {'read_consumer_sidecar': lambda *args: None}), patch.object(helper_globals['requests'], 'get', side_effect=[response(payload=files), response(data=data), response(payload=changed), response(data=data)]) as get:
            helper['remote_package_archives']('https://example.com', '86', 'test', 'alpine', 'php-zts-lib-ssl', '3.6.0-r1')
            with self.assertRaisesRegex(RuntimeError, 'differs from registry file digest'):
                helper['remote_package_archives']('https://example.com', '86', 'test', 'alpine', 'php-zts-lib-ssl', '3.6.0-r1')
        self.assertEqual(get.call_count, 4)

    def test_each_registry_file_endpoint_is_verified_even_with_the_same_digest(self):
        data = apk_bytes('php-zts-lib-ssl', '3.6.0-r1', 'x86_64')
        file = {'id': 45, 'name': 'library.apk', 'sha256': hashlib.sha256(data).hexdigest()}
        replies = [response(payload=[file]), response(data=data), response(payload=[dict(file, id=46)]), response(data=data), response(payload=[file]), response(data=data)]
        with patch.dict(helper_globals, {'read_consumer_sidecar': lambda *args: None}), patch.object(helper_globals['requests'], 'get', side_effect=replies) as get:
            for owner in ('85', '85', '86'):
                helper['remote_package_archives']('https://example.com', owner, 'test', 'alpine', 'php-zts-lib-ssl', '3.6.0-r1')
        self.assertEqual(get.call_count, 6)

    def test_cli_apk_retains_optional_cutoffs_during_download(self):
        cutoffs = {'schema': 1, 'php_stream': '8.6', 'architecture': 'x86_64', 'formats': {'apk': {'php-zts-intl': '8.6.0_rc4-r2'}}}
        for metadata in (None, cutoffs):
            with self.subTest(shared=metadata is not None):
                data = apk_bytes('php-zts-cli', '8.6.0_rc4-r2', 'x86_64', signed=True, cutoffs=metadata)
                with patch.object(helper_globals['requests'], 'get', side_effect=[response(payload=[{'id': 45, 'name': 'php-zts-cli-8.6.0_rc4-r2.apk'}]), response(data=data)]):
                    records = helper['remote_package_archives']('https://example.com', '86', 'test', 'alpine', 'php-zts-cli', '8.6.0_rc4-r2')
                self.assertEqual(records[0]['migration_cutoffs'], metadata)

    def test_cli_cutoff_identity_must_match_header(self):
        cutoffs = {'schema': 1, 'php_stream': '8.5', 'architecture': 'aarch64', 'formats': {'apk': {}}}
        data = apk_bytes('php-zts-cli', '8.6.0_rc4-r2', 'x86_64', cutoffs=cutoffs)
        with patch.object(helper_globals['requests'], 'get', side_effect=[response(payload=[{'id': 45, 'name': 'cli.apk'}]), response(data=data)]):
            with self.assertRaisesRegex(RuntimeError, 'identity differs'):
                helper['remote_package_archives']('https://example.com', '86', 'test', 'alpine', 'php-zts-cli', '8.6.0_rc4-r2')

    def test_cli_cutoff_member_is_bounded_and_schema_checked(self):
        for metadata in ([], {'schema': 1, 'formats': {'apk': {'x': 'x' * helper['MAX_MIGRATION_CUTOFF_BYTES']}}}):
            with self.subTest(metadata_type=type(metadata).__name__):
                path = self.root / 'cutoffs.apk'
                path.write_bytes(apk_bytes('php-zts-cli', '8.6.0_rc4-r2', 'x86_64', cutoffs=metadata))
                with self.assertRaisesRegex(RuntimeError, 'Invalid migration cutoff'):
                    helper['archive_migration_cutoffs'](path, 'alpine')

    @unittest.skipUnless(shutil.which('dpkg-deb'), 'dpkg-deb required')
    def test_deb_architecture_comes_from_header_not_filename(self):
        package = self.root / 'package'
        (package / 'DEBIAN').mkdir(parents=True)
        (package / 'DEBIAN/control').write_text('Package: php-zts-lib-ssl\nVersion: 3.6.0-1\nArchitecture: amd64\nMaintainer: Test <test@example.com>\nDescription: archive identity fixture\n')
        archive = self.root / 'fixture.deb'
        subprocess.run(['dpkg-deb', '--build', '--root-owner-group', str(package), str(archive)], check=True, capture_output=True)
        with patch.object(helper_globals['requests'], 'get', side_effect=[response(payload=[{'id': 65, 'name': 'php-zts-lib-ssl_3.6.0-1_arm64.deb'}]), response(data=archive.read_bytes())]):
            records = helper['remote_package_archives']('https://example.com', '86', 'test', 'debian', 'php-zts-lib-ssl', '3.6.0-1')
        self.assertEqual(records[0]['arch'], 'amd64')
        self.assertEqual(records[0]['sha256'], helper['archive_sha256'](archive))

    @unittest.skipUnless(shutil.which('dpkg-deb'), 'dpkg-deb required')
    def test_cli_deb_optional_cutoffs_are_streamed_without_extraction(self):
        package = self.root / 'package'
        (package / 'DEBIAN').mkdir(parents=True)
        (package / 'DEBIAN/control').write_text('Package: php-zts-cli\nVersion: 8.6.0~rc4-2\nArchitecture: amd64\nMaintainer: Test <test@example.com>\nDescription: cutoff fixture\n')
        archive = self.root / 'cli.deb'
        subprocess.run(['dpkg-deb', '--build', '--root-owner-group', str(package), str(archive)], check=True, capture_output=True)
        self.assertIsNone(helper['archive_migration_cutoffs'](archive, 'debian'))
        cutoffs = {'schema': 1, 'php_stream': '8.6', 'architecture': 'amd64', 'formats': {'deb': {'php-zts-intl': '8.6.0~rc4-2'}}}
        metadata = package / helper['MIGRATION_CUTOFF_PATH']
        metadata.parent.mkdir(parents=True)
        metadata.write_text(json.dumps(cutoffs))
        subprocess.run(['dpkg-deb', '--build', '--root-owner-group', str(package), str(archive)], check=True, capture_output=True)
        self.assertEqual(helper['archive_migration_cutoffs'](archive, 'debian'), cutoffs)

    def test_deb_reader_process_is_reaped_on_bad_tar(self):
        process = Mock(stdout=io.BytesIO(b'invalid tar'))
        process.poll.return_value = None
        with patch.object(helper_globals['subprocess'], 'Popen', return_value=process):
            with self.assertRaises(tarfile.ReadError):
                helper['archive_migration_cutoffs'](self.root / 'bad.deb', 'debian')
        process.terminate.assert_called_once()
        process.wait.assert_called_once()

    def test_equal_version_different_bytes_is_rejected_before_upload(self):
        path = self.archive()
        with patch.dict(helper_globals, {
            'version_is_newer': lambda *args: False,
            'remote_package_archives': lambda *args: [{'arch': 'x86_64', 'sha256': 'different'}],
        }), contextlib.redirect_stdout(io.StringIO()):
            allowed = helper['verify_upload_versions']('https://example.com', '86', 'test', 'alpine', [path], [{'name': 'php-zts-lib-ssl', 'version': '3.6.0-r1'}])
        self.assertEqual(allowed, [])

    def test_idempotent_archive_is_verified_without_upload_or_git_repository_link(self):
        path = self.archive()
        record = {'arch': 'x86_64', 'sha256': helper['archive_sha256'](path)}
        with patch.dict(helper_globals, {
            'get_remote_packages': lambda *args: [],
            'remote_package_archives': lambda *args: [record],
        }), patch.object(helper_globals['requests'], 'put') as upload, patch.object(helper_globals['requests'], 'post', return_value=response(status=409)) as link, contextlib.redirect_stdout(io.StringIO()):
            helper['upload_verified_archive']('https://example.com', '86', 'test', 'alpine', path)
        upload.assert_not_called()
        link.assert_not_called()

    def test_new_archive_is_verified_without_a_git_repository_association(self):
        path = self.archive()
        record = {'arch': 'x86_64', 'sha256': helper['archive_sha256'](path)}
        with patch.dict(helper_globals, {
            'get_remote_packages': lambda *args: [],
            'remote_package_archives': Mock(side_effect=[[], [record]]),
        }), patch.object(helper_globals['requests'], 'put', return_value=response(status=201)) as upload, \
                patch.object(helper_globals['requests'], 'post', return_value=response(status=400)) as link, \
                contextlib.redirect_stdout(io.StringIO()):
            helper['upload_verified_archive']('https://example.com', '86', 'test', 'alpine', path)
        upload.assert_called_once()
        link.assert_not_called()

    def test_second_architecture_cannot_remove_existing_archive(self):
        path = self.archive(arch='aarch64')
        before = {'arch': 'x86_64', 'sha256': 'existing'}
        current = {'arch': 'aarch64', 'sha256': helper['archive_sha256'](path)}
        with patch.dict(helper_globals, {
            'get_remote_packages': lambda *args: [],
            'remote_package_archives': Mock(side_effect=[[before], [current]]),
        }), patch.object(helper_globals['requests'], 'put', return_value=response(status=201)), patch.object(helper_globals['requests'], 'post', return_value=response(status=201)):
            with self.assertRaisesRegex(RuntimeError, 'changed an existing architecture'):
                helper['upload_verified_archive']('https://example.com', '86', 'test', 'alpine', path)

    def test_second_architecture_upload_preserves_both_archive_identities(self):
        path = self.archive(arch='aarch64')
        before = {'arch': 'x86_64', 'sha256': 'existing'}
        current = {'arch': 'aarch64', 'sha256': helper['archive_sha256'](path)}
        with patch.dict(helper_globals, {
            'get_remote_packages': lambda *args: [],
            'remote_package_archives': Mock(side_effect=[[before], [before, current]]),
        }), patch.object(helper_globals['requests'], 'put', return_value=response(status=201)) as upload, patch.object(helper_globals['requests'], 'post', return_value=response(status=201)), contextlib.redirect_stdout(io.StringIO()):
            helper['upload_verified_archive']('https://example.com', '86', 'test', 'alpine', path)
        upload.assert_called_once()

    def test_conflict_response_is_not_success_when_new_architecture_is_missing(self):
        path = self.archive(arch='aarch64')
        old = {'arch': 'x86_64', 'sha256': 'existing'}
        with patch.dict(helper_globals, {
            'get_remote_packages': lambda *args: [],
            'remote_package_archives': lambda *args: [old],
        }), patch.object(helper_globals['requests'], 'put', return_value=response(status=409)), patch.object(helper_globals['requests'], 'post', return_value=response(status=201)):
            with self.assertRaisesRegex(RuntimeError, 'missing after upload'):
                helper['upload_verified_archive']('https://example.com', '86', 'test', 'alpine', path)

    def test_library_and_layout_fan_out_to_existing_stream_owners(self):
        library = self.archive()
        layout = self.archive(name='php-zts-runtime-layout', version='1-r0', arch='noarch')
        cli = self.archive(name='php-zts-cli', version='8.5.12-r1')
        owners = {helper['package_metadata'](cli, 'alpine'): '85'}
        groups = publisher['package_groups'](self.root, 'apk', owners, helper)
        self.assertEqual(set(groups), set(publisher['STREAM_OWNERS']))
        for owner, paths in groups.items():
            self.assertIn(library, paths)
            self.assertIn(layout, paths)
            self.assertEqual(cli in paths, owner == '85')
        scoped = publisher['package_groups'](self.root, 'apk', owners, helper, ['85'])
        self.assertEqual(set(scoped), {'85'})
        self.assertEqual(set(scoped['85']), {library, layout, cli})
        with self.assertRaisesRegex(RuntimeError, 'unselected PHP owner'):
            publisher['package_groups'](self.root, 'apk', owners, helper, ['86'])

    def test_last_owner_preflight_failure_prevents_all_uploads(self):
        path = self.archive()
        upload = Mock()
        helpers = dict(helper, get_remote_packages=lambda *args: [],
                       verify_upload_versions=lambda host, owner, password, kind, paths, remote: [] if owner == '86' else paths,
                       upload_verified_archive=upload)
        groups = {owner: [path] for owner in publisher['STREAM_OWNERS']}
        with self.assertRaisesRegex(RuntimeError, 'owner 86'):
            publisher['publish']('upload', 'https://example.com', 'test', 'alpine', groups, helpers, True)
        upload.assert_not_called()

    def test_initial_and_breaking_update_order(self):
        paths = [self.archive(name=name) for name in ('php-zts-cli', 'php-zts-intl', 'php-zts-lib-ssl', 'php-zts-runtime-layout')]
        groups = {'86': paths}
        def names(bootstrap):
            return [helper['package_metadata'](path, 'alpine')[0] for _, _, path in publisher['publication_order'](groups, helper, 'alpine', bootstrap)]
        self.assertEqual(names(True), ['php-zts-runtime-layout', 'php-zts-lib-ssl', 'php-zts-intl', 'php-zts-cli'])
        self.assertEqual(names(False), ['php-zts-runtime-layout', 'php-zts-intl', 'php-zts-cli', 'php-zts-lib-ssl'])

    def test_archive_mutation_during_preflight_prevents_upload(self):
        path = self.archive()
        def preflight(host, owner, password, kind, paths, remote):
            path.write_bytes(path.read_bytes() + b'changed')
            return paths
        upload = Mock()
        helpers = dict(helper, get_remote_packages=lambda *args: [], verify_upload_versions=preflight, upload_verified_archive=upload)
        with self.assertRaisesRegex(RuntimeError, 'changed during'):
            publisher['publish']('upload', 'https://example.com', 'test', 'alpine', {'86': [path]}, helpers, True)
        upload.assert_not_called()

    def test_numeric_rpm_release_is_understood(self):
        self.assertEqual(helper['get_package_info'](Path('php-zts-cli-8.6.0~rc4-42.x86_64.rpm'), 'rpm')[:3], ('php-zts-cli', '8.6.0~rc4-42', 'x86_64'))


if __name__ == '__main__':
    unittest.main()
