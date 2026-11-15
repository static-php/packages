import hashlib
import gzip
import io
import tarfile
from pathlib import Path
import runpy
import tempfile
import unittest
from unittest.mock import Mock


ROOT = Path(__file__).resolve().parents[1]
index = runpy.run_path(str(ROOT / '.github/actions/shared-libraries/forgejo-index.py'))


class ForgejoIndex(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / 'debug.deb'
        self.path.write_bytes(b'identical retained archive')
        self.digest = hashlib.sha256(self.path.read_bytes()).hexdigest()
        self.identity = ('php-zts-zstd-debuginfo', '0.18.0+php83.36+ext-3', 'arm64')
        self.record = {'arch': 'arm64', 'sha256': self.digest}
        self.other = {'arch': 'amd64', 'sha256': 'other-architecture'}
        self.archives = [self.record, self.other]
        self.visible = False
        self.events = []
        self.helper = {'package_metadata': lambda *_: self.identity,
                       'archive_sha256': lambda *_: self.digest,
                       'registry_request': self.request,
                       'remote_package_archives': lambda *args: list(self.archives),
                       'upload_verified_archive': self.upload}

    def request(self, method, url, **kwargs):
        self.events.append((method, url))
        if method == 'delete':
            self.assertEqual(url, 'https://registry.test/api/packages/83/debian/pool/php-zts/main/'
                             'php-zts-zstd-debuginfo/0.18.0%2Bphp83.36%2Bext-3/arm64')
            self.archives.remove(self.record)
            return Mock(status_code=204)
        self.assertEqual(method, 'get')
        body = ''
        if self.visible:
            name, version, arch = self.identity
            body = f'Package: {name}\nVersion: {version}\nArchitecture: {arch}\nSHA256: {self.digest}\n'
        return Mock(text=body)

    def upload(self, host, owner, password, kind, path, **kwargs):
        self.assertEqual((host, owner, kind, path), ('https://registry.test', '83', 'debian', self.path))
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), self.digest)
        self.assertIn(self.other, self.archives)
        self.events.append(('upload', path))
        self.archives.append(self.record)
        self.visible = True

    def verify(self):
        return index['verify_indexes']('https://registry.test', 'secret', {'83': [self.path]}, self.helper)

    def test_current_index_needs_no_archive_read_or_mutation(self):
        self.visible = True
        self.helper['remote_package_archives'] = Mock(side_effect=AssertionError('Unnecessary archive check'))
        self.assertEqual(self.verify(), [])
        self.assertEqual([event[0] for event in self.events], ['get'])

    def test_missing_index_entry_restores_exact_archive_and_preserves_other_architecture(self):
        self.assertEqual(self.verify(), [])
        self.assertEqual([event[0] for event in self.events], ['get', 'delete', 'upload', 'get'])
        self.assertEqual(self.archives, [self.other, self.record])

    def test_different_stored_archive_prevents_deletion(self):
        self.archives[0] = dict(self.record, sha256='different')
        errors = self.verify()
        self.assertIn('identical stored archive', errors[0])
        self.assertEqual([event[0] for event in self.events], ['get'])

    def test_lost_delete_response_still_restores_archive(self):
        def request(method, url, **kwargs):
            response = self.request(method, url, **kwargs)
            if method == 'delete':
                raise TimeoutError('Response lost after deletion')
            return response
        self.helper['registry_request'] = request
        self.assertEqual(self.verify(), [])
        self.assertEqual(self.archives, [self.other, self.record])


class ForgejoApkIndex(ForgejoIndex):
    def setUp(self):
        super().setUp()
        self.path = self.path.with_suffix('.apk')
        self.identity = ('php-zts-zip-debuginfo', '1.22.8.0_p82_p35_p0-r10', 'x86_64')
        raw = io.BytesIO()
        with tarfile.open(fileobj=raw, mode='w') as archive:
            data = b'pkgname = php-zts-zip-debuginfo\n'
            info = tarfile.TarInfo('.PKGINFO')
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
        self.path.write_bytes(gzip.compress(raw.getvalue(), mtime=0))
        self.digest = hashlib.sha256(self.path.read_bytes()).hexdigest()
        self.control = runpy.run_path(str(ROOT / 'bin/freeze-migration-baseline.py'))['apk_control_checksum'](self.path.read_bytes())
        self.record = {'arch': 'x86_64', 'sha256': self.digest}
        self.other = {'arch': 'aarch64', 'sha256': 'other-architecture'}
        self.archives = [self.record, self.other]

    def request(self, method, url, **kwargs):
        self.events.append((method, url))
        if method == 'delete':
            self.assertEqual(url, 'https://registry.test/api/packages/83/alpine/main/php-zts/x86_64/'
                             'php-zts-zip-debuginfo-1.22.8.0_p82_p35_p0-r10.apk')
            self.archives.remove(self.record)
            return Mock(status_code=204)
        self.assertEqual(method, 'get')
        self.assertTrue(url.endswith('/x86_64/APKINDEX.tar.gz'))
        name, version, arch = self.identity
        body = f'P:{name}\nV:{version}\nA:{arch}\nC:{self.control}\n' if self.visible else ''
        raw = io.BytesIO()
        with tarfile.open(fileobj=raw, mode='w') as archive:
            info = tarfile.TarInfo('APKINDEX')
            info.size = len(body.encode())
            archive.addfile(info, io.BytesIO(body.encode()))
        return Mock(content=gzip.compress(raw.getvalue()))

    def upload(self, host, owner, password, kind, path, **kwargs):
        self.assertEqual(kind, 'alpine')
        super().upload(host, owner, password, 'debian', path, **kwargs)

    def verify(self):
        return index['verify_indexes']('https://registry.test', 'secret', {'83': [self.path]}, self.helper, kind='alpine')

    def test_mismatched_control_checksum_never_deletes_an_archive(self):
        self.visible = True
        self.control = 'Q1different'
        errors = self.verify()
        self.assertIn('different checksum', errors[0])
        self.assertEqual([event[0] for event in self.events], ['get'])


if __name__ == '__main__':
    unittest.main()
