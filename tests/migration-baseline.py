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
from threading import Event
from types import SimpleNamespace
import unittest
import urllib.error
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

    def test_partial_extension_downloads_only_runtime_hosts_and_dependency_closure(self):
        rows = [
            ('php-zts-cli', [], []),
            ('php-zts-fpm', ['php-zts-cli'], []),
            ('frankenphp', ['php-zts-embed'], []),
            ('php-zts-embed', ['php-zts-cli'], []),
            ('php-zts-imagick', ['php-zts-lib-imagemagick-abi-1'], []),
            ('php-zts-lib-imagemagick', ['php-zts-runtime-layout'], ['php-zts-lib-imagemagick-abi-1']),
            ('php-zts-runtime-layout', [], []),
            ('php-zts-redis', ['php-zts-cli'], []),
            ('php-zts-grpc', ['php-zts-lib-unrelated-missing-abi-1'], []),
        ]
        inventory, archives, downloaded = [], {}, []
        another_download = Event()
        for name, requires, provides in rows:
            data = apk_fixture(name, '1-r0', depends=requires, provides=provides)
            url = 'https://example.test/' + name + '.apk'
            archives[url] = data
            inventory.append({'name': name, 'version': '1-r0', 'architecture': 'x86_64',
                              'requires': requires, 'provides': provides, 'publication_url': url,
                              'published_checksum': {'algorithm': 'apk-control-sha1', 'value': helper['apk_control_checksum'](data)}})
        def fetch(url):
            downloaded.append(url)
            if len(downloaded) == 1:
                self.assertTrue(another_download.wait(timeout=2), 'Required archives were downloaded sequentially')
            else:
                another_download.set()
            return archives[url]
        with tempfile.TemporaryDirectory() as temporary:
            args = SimpleNamespace(type='apk', php='8.5', arch='x86_64', base_url='https://example.test/',
                                   profile='all', packages='imagick', library_plan=None,
                                   output=Path(temporary), include_debug=False)
            with patch.dict(helper['freeze'].__globals__, {'apk_index': lambda *args: (inventory, {}, []), 'fetch': fetch}):
                helper['freeze'](args)
            result = json.loads((args.output / 'baseline.json').read_text())
        expected = {name for name, _, _ in rows} - {'php-zts-redis', 'php-zts-grpc'}
        self.assertEqual({p['name'] for p in result['packages']}, expected)
        self.assertEqual(set(downloaded), {'https://example.test/' + name + '.apk' for name in expected})
        self.assertEqual(result['published_packages'], inventory)
        self.assertEqual(result['selection']['mode'], 'affected')
        self.assertFalse(result['selection']['complete'])

    def test_library_update_selects_reverse_consumers_without_unrelated_extensions(self):
        packages = [
            {'name': 'php-zts-cli', 'version': '1', 'requires': []},
            {'name': 'php-zts-fpm', 'version': '1', 'requires': ['php-zts-cli']},
            {'name': 'frankenphp', 'version': '1', 'requires': ['php-zts-cli']},
            {'name': 'php-zts-lib-base', 'version': '1', 'provides': ['php-zts-lib-base-abi-1']},
            {'name': 'php-zts-lib-dependent', 'version': '1', 'requires': ['php-zts-lib-base-abi-1'], 'provides': ['php-zts-lib-dependent-abi-1']},
            {'name': 'php-zts-imagick', 'version': '1', 'requires': ['php-zts-lib-dependent-abi-1']},
            {'name': 'php-zts-redis', 'version': '1', 'requires': ['php-zts-cli']},
        ]
        latest = helper['latest_packages']('apk', packages)
        profile = helper['affected_profile'](latest, [], ['base'])
        selected = helper['select_packages']('apk', packages, profile, latest=latest)
        self.assertEqual(set(selected), {p['name'] for p in packages} - {'php-zts-redis'})

    def test_partial_package_aliases_preserve_pie_and_native_extension_names(self):
        packages = [{'name': name, 'version': '1'} for name in
                    ('php-zts-cli', 'php-zts-fpm', 'frankenphp', 'pie-zts', 'php-zts-pdo_mysql', 'php-zts-redis')]
        latest = helper['latest_packages']('apk', packages)
        profile = helper['affected_profile'](latest, ['pie', 'pdo_mysql'], [])
        selected = helper['select_packages']('apk', packages, profile, latest=latest)
        self.assertEqual(set(selected), {p['name'] for p in packages} - {'php-zts-redis'})

    def test_deb_baseline_reads_architecture_independent_dependency_index(self):
        native = b'Package: php-zts-cli\nVersion: 8.3.36-3\nArchitecture: amd64\nDepends: php-zts-runtime-layout\nFilename: cli.deb\nSHA256: cli\n'
        common = b'Package: php-zts-runtime-layout\nVersion: 1-1\nArchitecture: all\nFilename: layout.deb\nSHA256: layout\n'
        base = 'https://example.test/'
        replies = {base + 'dists/php-zts/main/binary-amd64/Packages': native,
                   base + 'dists/php-zts/main/binary-all/Packages': common,
                   base + 'repository.key': b'key'}
        with patch.dict(helper['deb_index'].__globals__, {'fetch': replies.__getitem__}):
            packages, metadata, keys = helper['deb_index'](base, 'amd64')
        selected = helper['select_packages']('deb', packages, {'cli'})
        self.assertEqual(set(selected), {'php-zts-cli', 'php-zts-runtime-layout'})
        self.assertEqual(metadata, {'Packages': native, 'Packages-all': common})
        self.assertEqual(keys, [('repository.asc', b'key')])

    def test_deb_missing_optional_all_index_is_allowed_but_other_errors_fail(self):
        native = b'Package: php-zts-cli\nVersion: 8.3.36-3\nArchitecture: amd64\nFilename: cli.deb\nSHA256: cli\n'
        for status in (404, 503):
            def fetch(url):
                if '/binary-all/' in url:
                    error = urllib.error.HTTPError(url, status, 'fixture', {}, io.BytesIO())
                    error.close()
                    raise error
                return b'key' if url.endswith('repository.key') else native
            with self.subTest(status=status), patch.dict(helper['deb_index'].__globals__, {'fetch': fetch}):
                if status == 503:
                    with self.assertRaises(urllib.error.HTTPError):
                        helper['deb_index']('https://example.test/', 'amd64')
                else:
                    packages, _, _ = helper['deb_index']('https://example.test/', 'amd64')
                    self.assertEqual(len(packages), 1)

    def test_frozen_baseline_preserves_existing_shared_migration_cutoffs(self):
        cutoffs = {'schema': 1, 'php_stream': '8.6', 'architecture': 'x86_64',
                   'formats': {'apk': {'php-zts-amqp': '2.2.0p86.0_rc5-r0'}}}
        for metadata in (None, cutoffs, dict(cutoffs, php_stream='8.5')):
            with self.subTest(metadata=metadata), tempfile.TemporaryDirectory() as temporary:
                data = apk_fixture('php-zts-cli', '8.6.0_rc5-r0')
                if metadata is not None:
                    data += tar_member('usr/share/php-zts/migration/cutoffs.json', json.dumps(metadata).encode())
                package = {'name': 'php-zts-cli', 'version': '8.6.0_rc5-r0', 'architecture': 'x86_64',
                           'publication_url': 'https://example.test/php-zts-cli.apk',
                           'published_checksum': {'algorithm': 'apk-control-sha1', 'value': helper['apk_control_checksum'](data)}}
                args = SimpleNamespace(type='apk', php='8.6', arch='x86_64', base_url='https://example.test/',
                                       profile='all', output=Path(temporary), include_debug=False)
                with patch.dict(helper['freeze'].__globals__, {'apk_index': lambda *args: ([package], {}, []), 'fetch': lambda url: data}):
                    if metadata is not None and metadata['php_stream'] != args.php:
                        with self.assertRaisesRegex(RuntimeError, 'wrong stream or architecture'):
                            helper['freeze'](args)
                        continue
                    helper['freeze'](args)
                result = json.loads((args.output / 'baseline.json').read_text())
                if metadata is None:
                    self.assertNotIn('migration_cutoffs', result)
                else:
                    self.assertEqual(result['migration_cutoffs'], {'8.6': cutoffs['formats']['apk']})

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

    def test_full_user_baseline_excludes_unreferenced_partial_sdk_publication(self):
        cli = {'name': 'php-zts-cli', 'version': '8.3.36-2', 'requires': []}
        library = {'name': 'php-zts-lib-curl', 'version': '8.20.0-1', 'requires': ['php-zts-lib-nghttp3']}
        selected = helper['select_packages']('deb', [cli, library], {'all'})
        self.assertEqual(set(selected), {'php-zts-cli'})
        with self.assertRaisesRegex(RuntimeError, 'Missing or ambiguous'):
            helper['select_packages']('deb', [dict(cli, requires=['php-zts-lib-curl']), library], {'all'})

    def test_full_user_baseline_keeps_required_private_libraries_and_layout(self):
        packages = [{'name': 'php-zts-cli', 'version': '8.3.36-3', 'requires': ['php-zts-lib-zlib-abi-1']},
                    {'name': 'php-zts-lib-zlib', 'version': '1.4-1', 'requires': ['php-zts-runtime-layout'], 'provides': ['php-zts-lib-zlib-abi-1']},
                    {'name': 'php-zts-runtime-layout', 'version': '1-1', 'requires': []}]
        self.assertEqual(set(helper['select_packages']('deb', packages, {'all'})), {p['name'] for p in packages})

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
        self.assertEqual(parse('php-zts-memcache-8.2_82.35+ext-1.el8.x86_64.rpm'),
                         ('php-zts-memcache', '8.2_82.35+ext', '1.el8', 'x86_64', '8.2'))


if __name__ == '__main__':
    unittest.main()
