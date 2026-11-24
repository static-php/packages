#!/usr/bin/env python3
import gzip
import hashlib
import io
import json
from pathlib import Path
import runpy
import shutil
import subprocess
import tarfile
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
compare = runpy.run_path(str(ROOT / 'bin/package-content.py'))


class PackageContent(unittest.TestCase):
    def archive(self, path, timestamp, body=b'PIE upstream bytes', dependency='php-zts-cli', mode=0o755, bad_digest=False):
        changelog = ('pie-zts (1.5.5+php86-1) ; urgency=medium\n\n  * Package created with FPM.\n\n'
                     ' -- Maintainer <test@example.com>  ' + timestamp + '\n').encode()
        files = {'usr/bin/pie-zts': (body, mode), 'usr/share/doc/pie-zts/changelog.gz': (gzip.compress(changelog), 0o644)}
        control = {'control': (('Package: pie-zts\nVersion: 1.5.5+php86-1\nArchitecture: amd64\nDepends: '+dependency+'\n').encode(), 0o644),
                   'md5sums': (''.join(('0'*32 if bad_digest else hashlib.md5(data).hexdigest())+'  '+name+'\n' for name,(data,_) in files.items()).encode(), 0o644)}
        def tar(entries):
            output = io.BytesIO()
            with tarfile.open(fileobj=output, mode='w:xz') as archive:
                for name,(data,permissions) in entries.items():
                    member = tarfile.TarInfo('./'+name)
                    member.size,member.mode,member.mtime = len(data),permissions,len(timestamp)
                    archive.addfile(member,io.BytesIO(data))
            return output.getvalue()
        result = bytearray(b'!<arch>\n')
        for name,data in [('debian-binary',b'2.0\n'),('control.tar.xz',tar(control)),('data.tar.xz',tar(files))]:
            header = f'{name+"/":<16}{0:<12}{0:<6}{0:<6}{100644:<8}{len(data):<10}`\n'.encode()
            result.extend(header+data+(b'\n' if len(data)%2 else b''))
        path.write_bytes(result)

    @unittest.skipUnless(shutil.which('dpkg-deb'), 'native DEB tools unavailable')
    def test_same_revision_rebuild_keeps_verified_published_deb(self):
        retain = runpy.run_path(str(ROOT / 'bin/retain-published-consumers'))['retain_published']
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            dist = root / 'dist'
            dist.mkdir()
            old, new = root / 'published.deb', dist / 'rebuilt.deb'
            self.archive(old, 'Thu, 12 Nov 2026 11:55:27 +0000')
            self.archive(new, 'Fri, 13 Nov 2026 11:37:50 +0000', body=b'new bytes')
            record = dict(name='pie-zts', version='1.5.5+php86-1', architecture='amd64', path=old.name,
                          sha256=hashlib.sha256(old.read_bytes()).hexdigest())
            manifest = root / 'baseline.json'
            manifest.write_text(json.dumps(dict(type='deb', packages=[record])))
            retain('deb', dist, manifest)
            self.assertEqual(old.read_bytes(), new.read_bytes())
            old.write_bytes(b'wrong frozen archive')
            with self.assertRaisesRegex(RuntimeError, 'checksum/path mismatch'):
                retain('deb', dist, manifest)

    @unittest.skipUnless(shutil.which('dpkg-deb'), 'native DEB tools unavailable')
    def test_orphaned_published_debuginfo_does_not_block_its_new_runtime(self):
        retain = runpy.run_path(str(ROOT / 'bin/retain-published-consumers'))['retain_published']

        def build(path, name, marker):
            tree = path.parent / (name + '-tree')
            (tree / 'DEBIAN').mkdir(parents=True)
            (tree / 'usr/share').mkdir(parents=True)
            (tree / 'DEBIAN/control').write_text(
                f'Package: {name}\nVersion: 1.5.5+php86-1\nArchitecture: amd64\n'
                'Maintainer: test <test@example.com>\nDescription: test\n')
            (tree / 'usr/share' / name).write_text(marker)
            subprocess.run(['dpkg-deb', '--build', str(tree), str(path)], check=True, capture_output=True)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            dist, published = root / 'dist', root / 'published'
            dist.mkdir()
            published.mkdir()
            old_debug = published / 'pie-zts-debuginfo_1.5.5+php86-1_amd64.deb'
            build(old_debug, 'pie-zts-debuginfo', 'old symbols')
            new_debug = dist / old_debug.name
            build(new_debug, 'pie-zts-debuginfo', 'new symbols')
            runtime = dist / 'pie-zts_1.5.5+php86-1_amd64.deb'
            build(runtime, 'pie-zts', 'runtime')
            before_runtime = runtime.read_bytes()
            record = dict(name='pie-zts-debuginfo', version='1.5.5+php86-1', architecture='amd64',
                          path=old_debug.relative_to(root).as_posix(),
                          sha256=hashlib.sha256(old_debug.read_bytes()).hexdigest())
            manifest = root / 'baseline.json'
            manifest.write_text(json.dumps(dict(type='deb', packages=[record])))
            retain('deb', dist, manifest)
            self.assertEqual(old_debug.read_bytes(), new_debug.read_bytes())
            self.assertEqual(before_runtime, runtime.read_bytes())

    @unittest.skipUnless(shutil.which('dpkg-deb'), 'native DEB tools unavailable')
    def test_timestamp_only_rebuild_can_reuse_bytes_but_payload_metadata_and_modes_cannot(self):
        with tempfile.TemporaryDirectory() as temporary:
            old,new = (Path(temporary)/name for name in ('old.deb','new.deb'))
            self.archive(old,'Thu, 12 Nov 2026 11:55:27 +0000')
            self.archive(new,'Fri, 13 Nov 2026 11:37:50 +0000')
            self.assertNotEqual(old.read_bytes(),new.read_bytes())
            self.assertTrue(compare['equivalent'](old,new,'deb'))
            for change in ({'body':b'changed PIE bytes'},{'dependency':'php-zts-devel'},{'mode':0o644}):
                self.archive(new,'Fri, 13 Nov 2026 11:37:50 +0000',**change)
                self.assertFalse(compare['equivalent'](old,new,'deb'))
            self.archive(new,'Fri, 13 Nov 2026 11:37:50 +0000',bad_digest=True)
            with self.assertRaisesRegex(RuntimeError,'checksum mismatch'):
                compare['equivalent'](old,new,'deb')


if __name__ == '__main__':
    unittest.main()
