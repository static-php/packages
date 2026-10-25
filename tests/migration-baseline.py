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
    def alias_fixture(self, *, priority=100, core_capability=None, module_hash=None):
        capability = 'php-zts-shared-runtime-80235-abi20220829-100'
        logical, physical = 'php-zts-igbinary', 'php-zts-igbinary-shared'
        module = b'\x7fELF genuine fixture module bytes'
        metadata = {'schema': 1, 'name': physical, 'logical_name': logical, 'extension': 'igbinary',
                    'php_stream': '8.2', 'architecture': 'x86_64', 'libraries': [],
                    'module_sha256': module_hash or hashlib.sha256(module).hexdigest()}
        records = []
        downloads = {}
        for name, version, content, requires, provides in [
            ('php-zts-cli', '8.2.35-r100', apk_fixture('php-zts-cli', '8.2.35-r100', depends=['!' + logical + '>0'],
                                                    provides=[core_capability or capability]), [], [capability]),
            (logical, '3.2.17~rc1_82-r1', apk_fixture(logical, '3.2.17~rc1_82-r1'), [], []),
            (physical, '3.2.17_rc1_p82-r0', apk_fixture(physical, '3.2.17_rc1_p82-r0', depends=[capability, '!' + logical + '>0'],
                                                        provides=[logical], replaces=[logical], metadata=metadata, module=module,
                                                        priority=priority), [capability], [logical]),
        ]:
            url = f'https://example.test/{name}-{version}.apk'
            records.append({'name': name, 'version': version, 'architecture': 'x86_64', 'requires': requires,
                            'provides': provides, 'publication_url': url,
                            'published_checksum': {'algorithm': 'apk-control-sha1', 'value': helper['apk_control_checksum'](content)}})
            downloads[url] = content
        return records, downloads

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

    def test_apk_alias_effective_inventory_is_native_archive_bound(self):
        records, downloads = self.alias_fixture()
        original = json.loads(json.dumps(records))
        scope = helper['effective_apk_inventory'].__globals__
        with tempfile.TemporaryDirectory(prefix='spp-test-baseline-') as temporary, patch.dict(scope, {'fetch': downloads.__getitem__}):
            root = Path(temporary)
            stage = root / 'stage'
            stage.mkdir()
            effective, replacements, proof = helper['effective_apk_inventory'](records, '8.2', root, stage)
            self.assertEqual(records, original)
            self.assertEqual(replacements, {'php-zts-igbinary': 'php-zts-igbinary-shared'})
            self.assertEqual({package['name'] for package in effective}, {'php-zts-cli', 'php-zts-igbinary-shared'})
            selected = helper['select_packages']('apk', effective, {'cli', 'igbinary'})
            self.assertEqual(set(selected), {'php-zts-cli', 'php-zts-igbinary-shared'})
            receipt = proof['php-zts-igbinary']
            self.assertEqual(receipt['archive_sha256'], hashlib.sha256((root / receipt['path']).read_bytes()).hexdigest())
            self.assertEqual(receipt['core_archive_sha256'], hashlib.sha256((root / receipt['core_path']).read_bytes()).hexdigest())
            self.assertEqual(receipt['version'], '3.2.17_rc1_p82-r0')

    def test_apk_alias_rejects_wrong_core_or_module_metadata(self):
        scope = helper['effective_apk_inventory'].__globals__
        for overrides in [{'core_capability': 'php-zts-shared-runtime-80235-abi20220829-99'}, {'module_hash': '0' * 64}, {'priority': 0}]:
            with self.subTest(overrides=overrides):
                records, downloads = self.alias_fixture(**overrides)
                with tempfile.TemporaryDirectory(prefix='spp-test-baseline-') as temporary, patch.dict(scope, {'fetch': downloads.__getitem__}):
                    root = Path(temporary)
                    with self.assertRaises(RuntimeError):
                        helper['effective_apk_inventory'](records, '8.2', root, root)

    def test_apk_alias_requires_published_control_checksum(self):
        records, downloads = self.alias_fixture()
        records[-1]['published_checksum']['value'] = 'Q1incorrect'
        scope = helper['effective_apk_inventory'].__globals__
        with tempfile.TemporaryDirectory(prefix='spp-test-baseline-') as temporary, patch.dict(scope, {'fetch': downloads.__getitem__}):
            root = Path(temporary)
            with self.assertRaisesRegex(RuntimeError, 'checksum mismatch'):
                helper['effective_apk_inventory'](records, '8.2', root, root)

    def test_old_apk_inventory_without_aliases_is_unchanged(self):
        records, _ = self.alias_fixture()
        records = records[:-1]
        scope = helper['effective_apk_inventory'].__globals__
        with patch.dict(scope, {'fetch': lambda url: self.fail('No alias evidence download expected')}):
            effective, replacements, proof = helper['effective_apk_inventory'](records, '8.2', None, None)
        self.assertIs(effective, records)
        self.assertEqual((replacements, proof), ({}, {}))

    def test_apk_freeze_keeps_historical_inventory_and_alias_receipts(self):
        records, downloads = self.alias_fixture()
        scope = helper['freeze'].__globals__
        with tempfile.TemporaryDirectory(prefix='spp-test-baseline-') as temporary, patch.dict(scope, {
            'fetch': downloads.__getitem__, 'apk_index': lambda *args: (records, {}, [])
        }):
            args = SimpleNamespace(type='apk', arch='x86_64', php='8.2', base_url='https://example.test/',
                                   profile='cli,igbinary', include_debug=False, output=Path(temporary))
            helper['freeze'](args)
            result = json.loads((Path(temporary) / 'baseline.json').read_text())
            self.assertEqual(result['published_packages'], records)
            self.assertEqual(result['logical_replacements']['apk'], {'php-zts-igbinary': 'php-zts-igbinary-shared'})
            self.assertEqual({package['name'] for package in result['packages']}, {'php-zts-cli', 'php-zts-igbinary-shared'})
            self.assertIn('native_metadata', result['effective_published_packages'][-1])
            self.assertTrue((Path(temporary) / result['replacement_candidates']['apk']['php-zts-igbinary']['path']).is_file())


if __name__ == '__main__':
    unittest.main()
