"""Regression checks for genuine published baseline capture."""

import base64
import gzip
import hashlib
import io
import json
from pathlib import Path
import runpy
import tarfile
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch


helper = runpy.run_path(str(Path(__file__).resolve().parents[1] / 'bin/freeze-migration-baseline.py'))


def tar_member(name, content):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode='w') as archive:
        item = tarfile.TarInfo(name)
        item.size = len(content)
        archive.addfile(item, io.BytesIO(content))
    return gzip.compress(output.getvalue(), mtime=0)


def apk_fixture(name, version, *, depends=(), provides=(), replaces=(), metadata=None, module=None, priority=None):
    control = f'pkgname = {name}\npkgver = {version}\narch = x86_64\n'
    for field, values in [('depend', depends), ('provides', provides), ('replaces', replaces)]:
        control += ''.join(f'{field} = {value}\n' for value in values)
    if priority is not None:
        control += f'provider_priority = {priority}\n'
    data = tar_member('.PKGINFO', control.encode())
    if metadata is not None:
        data += tar_member('usr/share/php-zts/consumers/' + name + '.json', json.dumps(metadata).encode())
    if module is not None:
        data += tar_member('usr/lib/php-zts/modules/igbinary-zts-82.so', module)
    return data


class BaselineCapture(unittest.TestCase):

    def test_deleted_apk_versions_are_not_selected_from_stale_index(self):
        stale = {'name': 'php-zts-igbinary', 'version': '3.2.17~rc1_84-r1'}
        current = {'name': 'php-zts-igbinary', 'version': '3.2.17_rc1_p84-r0'}
        cli = {'name': 'php-zts-cli', 'version': '8.4.27-r1'}
        calls = []
        def packages(*args):
            calls.append(args)
            return [current, cli]
        scope = helper['registered_apk_packages'].__globals__
        with patch.dict(scope, {'runpy': SimpleNamespace(run_path=lambda path: {'get_remote_packages': packages})}):
            available = helper['registered_apk_packages']([stale, current, cli], '84')
        self.assertEqual(available, [current, cli])
        self.assertEqual(calls[0][:2], ('https://git.henderkes.com', '84'))
        with patch.dict(scope, {'runpy': SimpleNamespace(run_path=lambda path: {'get_remote_packages': lambda *args: (_ for _ in ()).throw(RuntimeError('registry unavailable'))})}):
            with self.assertRaisesRegex(RuntimeError, 'registry unavailable'):
                helper['registered_apk_packages']([stale, current, cli], '84')

    def test_apk_checksum_is_control_member_not_signature_or_file_hash(self):
        signature = tar_member('.SIGN.RSA.example.rsa.pub', b'signature')
        control = tar_member('.PKGINFO', b'pkgname = php-zts-cli\npkgver = 8.6.0_rc4-r3\n')
        data = tar_member('usr/bin/php-zts', b'original static executable')
        expected = 'Q1' + base64.b64encode(hashlib.sha1(control).digest()).decode()
        self.assertEqual(helper['apk_control_checksum'](signature + control + data), expected)
        self.assertEqual(helper['apk_control_checksum'](control + data), expected)
        changed = tar_member('usr/bin/php-zts', b'another payload')
        self.assertEqual(helper['apk_control_checksum'](signature + control + changed), expected)
        with self.assertRaises(RuntimeError):
            helper['apk_control_checksum'](control[:-7])

    def test_managed_closure_includes_private_library_and_layout(self):
        packages = [
            {'name': 'php-zts-cli', 'version': '8.6.0-4',
             'requires': ['php-zts-lib-zlib-abi-1', 'libc.so.6'], 'provides': ['php-zts-cli']},
            {'name': 'php-zts-lib-zlib', 'version': '1.3.2-1',
             'requires': ['php-zts-runtime-layout'], 'provides': ['php-zts-lib-zlib-abi-1']},
            {'name': 'php-zts-runtime-layout', 'version': '1-1',
             'requires': [], 'provides': ['php-zts-runtime-layout']},
            {'name': 'php-zts-imagick', 'version': '3.8.1-1', 'requires': [], 'provides': []},
        ]
        selected = helper['select_packages']('rpm', packages, {'cli'})
        self.assertEqual(set(selected), {'php-zts-cli', 'php-zts-lib-zlib', 'php-zts-runtime-layout'})

    def test_missing_or_ambiguous_managed_dependency_fails(self):
        cli = {'name': 'php-zts-cli', 'version': '8.6.0-4', 'requires': ['php-zts-lib-ssl-abi-1']}
        with self.assertRaisesRegex(RuntimeError, 'Missing or ambiguous'):
            helper['select_packages']('rpm', [cli], {'cli'})
        providers = [dict(name='php-zts-lib-' + name, version='1-1', provides=['php-zts-lib-ssl-abi-1'])
                     for name in ['openssl', 'another']]
        with self.assertRaisesRegex(RuntimeError, 'Missing or ambiguous'):
            helper['select_packages']('rpm', [cli, *providers], {'cli'})

    def test_frankenphp_closure_includes_embed_soname_provider(self):
        packages = [
            {'name': 'frankenphp', 'version': '1.13.4-1', 'requires': ['libphp-zts-82.so']},
            {'name': 'php-zts-embed', 'version': '8.2.35-2',
             'provides': ['libphp-zts-82.so', 'libphp-zts-82.so()(64bit)'], 'requires': ['php-zts-cli']},
            {'name': 'php-zts-cli', 'version': '8.2.35-2'},
        ]
        self.assertEqual(set(helper['select_packages']('rpm', packages, {'frankenphp'})),
                         {'frankenphp', 'php-zts-embed', 'php-zts-cli'})
        with self.assertRaisesRegex(RuntimeError, 'Missing or ambiguous'):
            helper['select_packages']('rpm', [packages[0]], {'frankenphp'})

    def test_latest_selection_uses_native_comparator(self):
        packages = [{'name': 'php-zts-cli', 'version': version} for version in ['8.6.0~rc4-3', '8.6.0-1']]
        scope = helper['select_packages'].__globals__
        with patch.dict(scope, {'newer': lambda kind, candidate, previous: candidate['version'] == '8.6.0-1'}):
            selected = helper['select_packages']('deb', packages, {'cli'})
        self.assertEqual(selected['php-zts-cli']['version'], '8.6.0-1')

    def test_rpm_parser_accepts_plain_release_without_executing_repository_writer(self):
        parse = helper['rpm_parser']()
        self.assertEqual(parse('php-zts-cli-8.6.0~rc4-42.x86_64.rpm'),
                         ('php-zts-cli', '8.6.0~rc4', '42', 'x86_64', '8.6'))


if __name__ == '__main__':
    unittest.main()
