#!/usr/bin/env python3
import gzip
import hashlib
import io
from pathlib import Path
import runpy
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
