import gzip
import hashlib
import io
import json
from pathlib import Path
import runpy
import tarfile
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]
helper = runpy.run_path(str(ROOT / 'bin/forgejo-helper'))
planner = runpy.run_path(str(ROOT / 'bin/shared-library-plan'))
namespace = helper['remote_package_archives'].__globals__


def response(status=200, content=b'', payload=None):
    value = Mock(status_code=status)
    value.json.return_value = payload
    value.iter_content.return_value = [content]
    value.__enter__ = Mock(return_value=value)
    value.__exit__ = Mock(return_value=False)
    return value


class ConsumerIndexes(unittest.TestCase):
    def setUp(self):
        self.consumer = {'schema': 1, 'name': 'php-zts-imagick', 'php_stream': '8.6',
                         'architecture': 'x86_64', 'libraries': ['imagemagick', 'zlib']}
        self.digest = 'a' * 64
        self.record = {'schema': 1, 'package_type': 'alpine', 'name': 'php-zts-imagick',
                       'version': '3.8.0p86.0_rc4-r2', 'arch': 'x86_64', 'sha256': self.digest,
                       'consumer_metadata': self.consumer, 'migration_cutoffs': None}

    def read_remote(self, files, sidecars):
        replies = [response(payload=files)] + [response(content=json.dumps(value).encode()) for value in sidecars]
        with patch.object(namespace['requests'], 'get', side_effect=replies) as get:
            result = helper['remote_package_archives']('https://fixture.invalid', '86', 'fixture', 'alpine',
                                                       self.record['name'], self.record['version'])
        return result, get

    def test_digest_bound_sidecar_reuses_consumer_closure_without_archive_download(self):
        records, get = self.read_remote([{'id': 41, 'name': 'imagick.apk', 'sha256': self.digest}], [self.record])
        self.assertEqual(records[0]['consumer_metadata'], self.consumer)
        self.assertEqual(records[0]['file_id'], 41)
        self.assertEqual(get.call_count, 2)
        self.assertIn('/generic/php-zts-consumer-metadata/' + self.digest + '/', get.call_args.args[0])

    def test_same_apk_filename_keeps_both_architectures_and_file_ids(self):
        arm = dict(self.record, arch='aarch64', sha256='b' * 64,
                   consumer_metadata=dict(self.consumer, architecture='aarch64'))
        records, _ = self.read_remote([{'id': 41, 'name': 'imagick.apk', 'sha256': self.digest},
                                       {'id': 42, 'name': 'imagick.apk', 'sha256': arm['sha256']}], [self.record, arm])
        self.assertEqual({(item['arch'], item['file_id']) for item in records}, {('x86_64', 41), ('aarch64', 42)})

    def test_mismatched_archive_digest_cannot_supply_dependency_metadata(self):
        with self.assertRaisesRegex(RuntimeError, 'archive digest'):
            self.read_remote([{'id': 41, 'name': 'imagick.apk', 'sha256': self.digest}], [dict(self.record, sha256='b' * 64)])

    def test_wrong_owner_cannot_supply_dependency_metadata(self):
        with self.assertRaisesRegex(RuntimeError, 'identity'):
            self.read_remote([{'id': 41, 'name': 'imagick.apk', 'sha256': self.digest}],
                             [dict(self.record, consumer_metadata=dict(self.consumer, php_stream='8.5'))])

    def test_no_api_digest_does_not_trust_an_unbound_sidecar(self):
        with patch.object(namespace['requests'], 'get') as get:
            self.assertIsNone(helper['read_consumer_sidecar']('https://fixture.invalid', '86', 'fixture', 'alpine',
                                                             self.record['name'], self.record['version'], None))
        get.assert_not_called()

    def test_generic_conflict_is_verified_after_upload(self):
        with TemporaryDirectory(prefix='consumer-index-test-') as temporary:
            archive = Path(temporary) / 'imagick.apk'
            stream = io.BytesIO()
            content = ('pkgname = php-zts-imagick\npkgver = 3.8.0p86.0_rc4-r2\narch = x86_64\n').encode()
            with tarfile.open(fileobj=stream, mode='w') as data:
                member = tarfile.TarInfo('.PKGINFO')
                member.size = len(content)
                data.addfile(member, io.BytesIO(content))
            archive.write_bytes(gzip.compress(stream.getvalue()))
            candidate = dict(self.record, sha256=hashlib.sha256(archive.read_bytes()).hexdigest())
            bad = dict(candidate, consumer_metadata=dict(self.consumer, libraries=['zlib']))
            with patch.dict(namespace, {'package_metadata': Mock(return_value=(candidate['name'], candidate['version'], candidate['arch'])),
                                        'archive_embedded_metadata': Mock(return_value={'consumer_metadata': self.consumer, 'migration_cutoffs': None}),
                                        'read_consumer_sidecar': Mock(side_effect=[None, bad])}), \
                    patch.object(namespace['requests'], 'put', return_value=response(status=409)):
                with self.assertRaisesRegex(RuntimeError, 'differs after upload'):
                    helper['publish_consumer_sidecar']('https://fixture.invalid', '86', 'fixture', 'alpine', archive)

    def rpm_index(self, record=None):
        package = {'name': 'php-zts-imagick', 'evr': '0:3.8.0_86.0~rc4-2', 'arch': 'x86_64',
                   'location': 'php-zts-imagick.rpm', 'checksum': {'type': 'sha256', 'value': self.digest}}
        record = record or dict(self.record, version=package['evr'])
        content = gzip.compress(json.dumps({'schema': 1, 'type': 'rpm', 'architecture': 'x86_64',
                                           'packages': {'php-zts-imagick.rpm': record}}).encode())
        metadata = ('<repomd xmlns="http://linux.duke.edu/metadata/repo"><data type="php-zts-consumers">'
                    '<location href="repodata/consumers.json.gz"/><checksum type="sha256">' +
                    hashlib.sha256(content).hexdigest() + '</checksum></data></repomd>').encode()
        return package, {'repodata/repomd.xml': metadata, 'repodata/consumers.json.gz': content}

    def test_rpm_index_is_bound_to_repodata_and_actual_primary_archive(self):
        package, files = self.rpm_index()
        result = planner['published_rpm_consumer_index'](files.__getitem__, [package], 'x86_64')
        self.assertEqual(result[(package['name'], package['evr'], 'x86_64')]['consumer_metadata'], self.consumer)

    def test_rpm_index_from_other_generation_is_rejected(self):
        package, files = self.rpm_index(dict(self.record, version='0:3.8.0_86.0~rc4-2', sha256='b' * 64))
        with self.assertRaisesRegex(RuntimeError, 'archive digest'):
            planner['published_rpm_consumer_index'](files.__getitem__, [package], 'x86_64')

    def test_rpm_index_tampering_is_rejected(self):
        package, files = self.rpm_index()
        files['repodata/consumers.json.gz'] += b'changed'
        with self.assertRaisesRegex(RuntimeError, 'checksum'):
            planner['published_rpm_consumer_index'](files.__getitem__, [package], 'x86_64')

    def test_legacy_rpm_repository_retains_archive_fallback(self):
        metadata = b'<repomd xmlns="http://linux.duke.edu/metadata/repo"/>'
        self.assertEqual(planner['published_rpm_consumer_index'](lambda _: metadata, [], 'x86_64'), {})


if __name__ == '__main__':
    unittest.main()
