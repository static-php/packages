#!/usr/bin/env python3

import gzip
import io
import json
from pathlib import Path
import runpy
import shutil
import subprocess
import tarfile
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]
helper = runpy.run_path(str(ROOT / 'bin/forgejo-helper'))
helper_globals = helper['remote_package_archives'].__globals__


def tar_bytes(members):
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode='w') as archive:
        for name, content, kind in members:
            member = tarfile.TarInfo(name)
            member.type = kind
            if kind == tarfile.SYMTYPE:
                member.linkname = 'other.json'
                archive.addfile(member)
            else:
                member.size = len(content)
                archive.addfile(member, io.BytesIO(content))
    return stream.getvalue()


def response(payload=None, content=b''):
    result = Mock(status_code=200)
    result.json.return_value = payload
    result.iter_content.return_value = [content]
    result.__enter__ = Mock(return_value=result)
    result.__exit__ = Mock(return_value=False)
    return result


class ConsumerMetadata(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory(prefix='spp-consumer-metadata-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.name = 'php-zts-imagick'
        self.metadata = {'schema': 1, 'name': self.name, 'php_stream': '8.6',
                         'architecture': 'x86_64', 'libraries': ['imagemagick', 'zlib']}

    def metadata_path(self, name=None):
        return helper['CONSUMER_METADATA_DIRECTORY'] + '/' + (name or self.name) + '.json'

    def apk(self, metadata=None, name=None, arch='x86_64', version='3.8.0p86.0_rc4-r1', signed=True, members=None):
        name = name or self.name
        header = tar_bytes([('.PKGINFO', f'pkgname = {name}\npkgver = {version}\narch = {arch}\n'.encode(), tarfile.REGTYPE)])
        result = gzip.compress(header, mtime=0)
        if members is None:
            members = [] if metadata is None else [(self.metadata_path(name), json.dumps(metadata).encode(), tarfile.REGTYPE)]
        if members:
            result += gzip.compress(tar_bytes(members), mtime=0)
        if signed:
            result = gzip.compress(tar_bytes([('.SIGN.RSA.fixture', b'signature', tarfile.REGTYPE)]), mtime=0) + result
        return result

    def remote(self, content, kind='alpine', name=None, version='3.8.0p86.0_rc4-r1', owner='86', filename=None):
        name = name or self.name
        filename = filename or name + ('.deb' if kind == 'debian' else '.apk')
        with patch.object(helper_globals['requests'], 'get', side_effect=[
                response(payload=[{'id': 45, 'name': filename}]), response(content=content)]):
            return helper['remote_package_archives']('https://example.invalid', owner, 'fixture', kind, name, version)[0]

    def test_signed_multigzip_apk_retains_consumer_metadata(self):
        record = self.remote(self.apk(self.metadata))
        self.assertEqual(record['consumer_metadata'], self.metadata)
        self.assertEqual(record['arch'], 'x86_64')

    def test_empty_library_inventory_is_known_and_absence_is_legacy(self):
        metadata = dict(self.metadata, libraries=[])
        self.assertEqual(self.remote(self.apk(metadata))['consumer_metadata']['libraries'], [])
        self.assertIsNone(self.remote(self.apk())['consumer_metadata'])
        self.assertIsNone(self.remote(self.apk(), owner='unversioned')['consumer_metadata'])

    def test_owner_determines_stream_for_non_php_upstream_versions(self):
        name = 'frankenphp-zts'
        metadata = dict(self.metadata, name=name)
        record = self.remote(self.apk(metadata, name=name, version='1.13.1p86.0_rc4-r1'),
                             name=name, version='1.13.1p86.0_rc4-r1')
        self.assertEqual(record['consumer_metadata']['php_stream'], '8.6')
        for owner in ('85', 'unversioned'):
            with self.subTest(owner=owner), self.assertRaisesRegex(RuntimeError, 'Consumer metadata identity'):
                self.remote(self.apk(self.metadata), owner=owner)

    def test_metadata_identity_must_match_native_header_and_owner(self):
        cases = [{'name': 'php-zts-other'}, {'architecture': 'aarch64'}, {'php_stream': '8.5'},
                 {'schema': 2}, {'schema': True}, {'architecture': 'all'}, {'libraries': None},
                 {'libraries': ['zlib', 'zlib']}, {'libraries': ['../zlib']}, {'libraries': [1]}]
        for changes in cases:
            with self.subTest(changes=changes), self.assertRaisesRegex(RuntimeError, 'Consumer metadata identity'):
                self.remote(self.apk(dict(self.metadata, **changes)))

    def test_architecture_aliases_are_normalized_for_comparison_and_output(self):
        for header, metadata, expected in [('x86_64', 'amd64', 'x86_64'), ('aarch64', 'arm64', 'aarch64'),
                                            ('arm64', 'aarch64', 'aarch64')]:
            with self.subTest(header=header, metadata=metadata):
                value = dict(self.metadata, architecture=metadata)
                record = self.remote(self.apk(value, arch=header))
                self.assertEqual(record['consumer_metadata']['architecture'], expected)

    def test_another_packages_metadata_is_not_the_current_consumer(self):
        members = [(self.metadata_path('php-zts-other'), json.dumps(self.metadata).encode(), tarfile.REGTYPE)]
        self.assertIsNone(self.remote(self.apk(members=members))['consumer_metadata'])

    def test_consumer_archive_member_is_regular_bounded_unique_and_json(self):
        path = self.metadata_path()
        valid = json.dumps(self.metadata).encode()
        cases = [[(path, b'', tarfile.REGTYPE)], [(path, b'{}' * helper['MAX_CONSUMER_METADATA_BYTES'], tarfile.REGTYPE)],
                 [(path, valid, tarfile.SYMTYPE)], [(path, valid, tarfile.REGTYPE)] * 2,
                 [(path, b'invalid JSON', tarfile.REGTYPE)], [(path, b'[]', tarfile.REGTYPE)],
                 [(path, b'null', tarfile.REGTYPE)]]
        for members in cases:
            with self.subTest(size=len(members[0][1]), kind=members[0][2], count=len(members)):
                with self.assertRaisesRegex(RuntimeError, 'Invalid consumer metadata'):
                    self.remote(self.apk(members=members))

    def test_cli_migration_and_consumer_metadata_are_read_together(self):
        name = 'php-zts-cli'
        metadata = dict(self.metadata, name=name, libraries=[])
        cutoffs = {'schema': 1, 'php_stream': '8.6', 'architecture': 'x86_64', 'formats': {'apk': {'php-zts-intl': '8.6.0_rc4-r1'}}}
        members = [(helper['MIGRATION_CUTOFF_PATH'], json.dumps(cutoffs).encode(), tarfile.REGTYPE),
                   (self.metadata_path(name), json.dumps(metadata).encode(), tarfile.REGTYPE)]
        record = self.remote(self.apk(name=name, version='8.6.0_rc4-r2', members=members), name=name, version='8.6.0_rc4-r2')
        self.assertEqual(record['consumer_metadata'], metadata)
        self.assertEqual(record['migration_cutoffs'], cutoffs)

    @unittest.skipUnless(shutil.which('dpkg-deb'), 'dpkg-deb required')
    def test_real_deb_streaming_validates_native_header_not_filename(self):
        package = self.root / 'package'
        (package / 'DEBIAN').mkdir(parents=True)
        (package / 'DEBIAN/control').write_text('Package: php-zts-imagick\nVersion: 3.8.0+php86.0~rc4-1\nArchitecture: amd64\nMaintainer: Test <test@example.com>\nDescription: consumer metadata fixture\n')
        metadata = package / self.metadata_path()
        metadata.parent.mkdir(parents=True)
        metadata.write_text(json.dumps(self.metadata))
        archive = self.root / 'fixture.deb'
        subprocess.run(['dpkg-deb', '--build', '--root-owner-group', str(package), str(archive)], check=True, capture_output=True)
        record = self.remote(archive.read_bytes(), kind='debian', version='3.8.0+php86.0~rc4-1', filename='misleading_arm64.deb')
        self.assertEqual(record['arch'], 'amd64')
        self.assertEqual(record['consumer_metadata'], self.metadata)
        metadata.unlink()
        subprocess.run(['dpkg-deb', '--build', '--root-owner-group', str(package), str(archive)], check=True, capture_output=True)
        record = self.remote(archive.read_bytes(), kind='debian', version='3.8.0+php86.0~rc4-1')
        self.assertIsNone(record['consumer_metadata'])

    def test_deb_stream_process_is_reaped_when_metadata_is_invalid(self):
        data = tar_bytes([(self.metadata_path(), b'[]', tarfile.REGTYPE)])
        process = Mock(stdout=io.BytesIO(data))
        process.poll.return_value = None
        with patch.object(helper_globals['subprocess'], 'Popen', return_value=process):
            with self.assertRaisesRegex(RuntimeError, 'Invalid consumer metadata'):
                helper['archive_embedded_metadata'](self.root / 'fixture.deb', 'debian', self.name)
        self.assertTrue(process.stdout.closed)
        process.terminate.assert_called_once()
        process.wait.assert_called_once_with(timeout=5)

    def test_deb_stream_process_is_killed_and_reaped_if_termination_times_out(self):
        process = Mock(stdout=io.BytesIO(b'invalid tar'))
        process.poll.return_value = None
        process.wait.side_effect = [subprocess.TimeoutExpired('dpkg-deb', 5), -9]
        with patch.object(helper_globals['subprocess'], 'Popen', return_value=process):
            with self.assertRaises(tarfile.ReadError):
                helper['archive_embedded_metadata'](self.root / 'fixture.deb', 'debian', self.name)
        self.assertTrue(process.stdout.closed)
        process.terminate.assert_called_once()
        process.kill.assert_called_once()
        self.assertEqual(process.wait.call_count, 2)

    def test_deb_stream_exit_failure_is_not_accepted(self):
        process = Mock(stdout=io.BytesIO(tar_bytes([])))
        process.wait.return_value = 2
        process.poll.return_value = 2
        with patch.object(helper_globals['subprocess'], 'Popen', return_value=process):
            with self.assertRaisesRegex(RuntimeError, 'Cannot read Debian package metadata archive'):
                helper['archive_embedded_metadata'](self.root / 'fixture.deb', 'debian', self.name)
        process.wait.assert_called_once_with(timeout=60)
        process.terminate.assert_not_called()


if __name__ == '__main__':
    unittest.main()
