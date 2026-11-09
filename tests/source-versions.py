#!/usr/bin/env python3
"""Offline preflight must use the exact source runtime version before compiling."""

import hashlib
import importlib.util
import io
import json
from pathlib import Path
import shutil
import subprocess
from tempfile import TemporaryDirectory
import tarfile
import unittest
import zipfile


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('source_versions', ROOT / '.github/actions/shared-libraries/source-versions.py')
versions = importlib.util.module_from_spec(spec)
spec.loader.exec_module(versions)


class SourceVersions(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory(prefix='spp-source-versions-test-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.downloads = self.root / 'downloads'
        self.downloads.mkdir()
        self.stream, self.php = '8.6', '8.6.0RC4'
        self.cache = {}
        self.index = {'schema': 1, 'sources': {}}
        self.metadata = {'php_versions': {self.stream: self.php}, 'pie_version': '1.5.3'}
        self.template = self.root / 'config/templates/craft.yml.twig'
        self.template.parent.mkdir(parents=True)
        self.profile([])
        self.php_source()
        self.archive('frankenphp', {'frankenphp/README.md': 'Frozen source'}, version='v1.13.3')
        phar = self.downloads / 'pie.phar'
        phar.write_bytes(b'frozen PIE fixture')
        checksum = hashlib.sha256(phar.read_bytes()).hexdigest()
        self.metadata['pie_sha256'] = checksum
        (self.downloads / 'pie.json').write_text(json.dumps({'schema': 1, 'version': '1.5.3', 'sha256': checksum, 'source_tag': '1.5.3'}))
        self.save()

    def profile(self, shared, sapis='cli,embed,frankenphp'):
        self.template.write_text("{% set base_sapi_str = '" + sapis + "' %}\n{% set base_shared_str = '" + ','.join(shared) + "' %}\n{% set base_extensions_str = 'ctype' %}\n")

    def archive(self, artifact, files, version='1.0.0', kind='tar', filename=None):
        path = self.downloads / (filename or artifact + ('.zip' if kind == 'zip' else '.tar.gz'))
        if kind == 'zip':
            with zipfile.ZipFile(path, 'w') as archive:
                for name, content in files.items():
                    archive.writestr(name, content)
        else:
            with tarfile.open(path, 'w:gz') as archive:
                for name, content in files.items():
                    content = content.encode()
                    member = tarfile.TarInfo(name)
                    member.size = len(content)
                    archive.addfile(member, io.BytesIO(content))
        sha1, sha256 = versions.digest_file(path)
        record = {'lock_type': 'source', 'cache_type': 'archive', 'filename': path.name,
                  'hash': sha1, 'version': version, 'config': {'type': 'fixture'}}
        self.cache[artifact] = {'source': record}
        return record, sha256

    def php_source(self, extra=None):
        files = {'php-' + self.php + '/main/php_version.h': '#define PHP_VERSION "' + self.php + '"\n'}
        files.update({('php-' + self.php + '/' + name): value for name, value in (extra or {}).items()})
        record, checksum = self.archive('php-src', files, version=self.php, filename='php-' + self.php + '.tar.gz')
        self.index['sources'][self.php] = {'stream': self.stream, 'source': record, 'archive_sha1': record['hash'],
                                          'archive_sha256': checksum, 'header_version': self.php}

    def save(self):
        for name, value in [('.spc-meta.json', self.metadata), ('.cache.json', self.cache), ('.php-sources.json', self.index)]:
            (self.downloads / name).write_text(json.dumps(value))

    def result(self):
        self.save()
        return {item['name']: item for item in versions.candidates(self.root, self.stream)['candidates']}

    def test_full_candidate_list_includes_modules_sapis_franken_pie_and_php_metadata(self):
        self.profile(['grpc', 'protobuf'])
        self.archive('ext-grpc', {'grpc/version.h': '#define PHP_GRPC_VERSION "1.83.0"\n'}, version='v1.84.1', kind='zip')
        self.archive('ext-protobuf', {'protobuf/protobuf.h': '#define PHP_PROTOBUF_VERSION "5.36.0"\n'}, version='5.36.2')
        result = self.result()
        self.assertEqual(set(result), {'php-zts-grpc', 'php-zts-protobuf', 'php-zts-cli', 'php-zts-embed',
                                      'frankenphp', 'php-zts-devel', 'php-zts', 'pie-zts'})
        self.assertEqual(result['php-zts-grpc']['upstream_version'], '1.83.0')
        self.assertEqual(result['php-zts-protobuf']['upstream_version'], '5.36.0')
        self.assertEqual(result['frankenphp']['upstream_version'], '1.13.3')
        self.assertEqual(result['pie-zts']['upstream_version'], '1.5.3')
        self.assertEqual(result['php-zts-grpc']['php_version'], self.php)
        self.assertEqual(len(result['php-zts-grpc']['evidence']['archive_sha256']), 64)

    def test_runtime_headers_override_stale_package_xml_and_release_labels(self):
        self.profile(['xhprof', 'mysqlnd_ed25519', 'mysqlnd_parsec', 'lz4', 'snappy'])
        for component, macro, actual in [('xhprof', 'XHPROF_VERSION', '2.3.9'),
                                         ('mysqlnd_ed25519', 'PHP_MARIADB_AUTH_PLUGIN_VERSION', '1.0.1'),
                                         ('mysqlnd_parsec', 'PHP_MARIADB_AUTH_PLUGIN_VERSION', '1.0.2'),
                                         ('lz4', 'LZ4_EXT_VERSION', '0.8.0'), ('snappy', 'SNAPPY_EXT_VERSION', '0.2.3')]:
            self.archive('ext-' + component, {component + '/php_' + component + '.h': '#define ' + macro + ' "' + actual + '"\n',
                                              component + '/package.xml': '<package><version><release>99.0.0</release></version></package>'}, version='99.0.0')
        result = self.result()
        for component, actual in [('xhprof', '2.3.9'), ('mysqlnd_ed25519', '1.0.1'), ('mysqlnd_parsec', '1.0.2'),
                                  ('lz4', '0.8.0'), ('snappy', '0.2.3')]:
            self.assertEqual(result['php-zts-' + component]['upstream_version'], actual)

    def test_bundled_php_aliases_and_new_zip_use_actual_pinned_php_archive(self):
        self.profile(['curl', 'ffi', 'mysqlnd', 'zip'])
        self.php_source({'ext/curl/curl_private.h': '#define PHP_CURL_VERSION PHP_VERSION\n',
                         'ext/ffi/ffi.c': '\tPHP_VERSION, /* Version */\n',
                         'ext/mysqlnd/mysqlnd.h': '#define PHP_MYSQLND_VERSION "mysqlnd " PHP_VERSION\n',
                         'ext/zip/php_zip.h': '#define PHP_ZIP_VERSION "1.22.8"\n'})
        self.archive('ext-zip', {'zip/php_zip.h': '#define PHP_ZIP_VERSION "99.0.0"\n'}, version='99.0.0')
        result = self.result()
        for component in ['curl', 'ffi', 'mysqlnd']:
            self.assertEqual(result['php-zts-' + component]['upstream_version'], self.php)
        self.assertEqual(result['php-zts-zip']['upstream_version'], '1.22.8')
        self.assertEqual(result['php-zts-zip']['evidence']['archive'], 'php-' + self.php + '.tar.gz')

    def test_xdebug_runtime_header_overrides_stale_release_manifest(self):
        self.profile(['xdebug'])
        self.archive('ext-xdebug', {'xdebug/php_xdebug.h': '#define XDEBUG_VERSION "3.6.0alpha2-dev"\n',
                                    'xdebug/package.xml': '<package><version><release>3.6.0alpha1</release></version></package>'},
                     version='3.6.0alpha1')
        self.assertEqual(self.result()['php-zts-xdebug']['upstream_version'], '3.6.0alpha2-dev')

    def test_apk_keeps_original_package_names_and_sources(self):
        self.profile(['amqp', 'igbinary', 'grpc'])
        for component, version in [('amqp', '2.2.0'), ('igbinary', '3.2.17RC1'), ('grpc', '1.84.1')]:
            self.archive('ext-' + component, {component + '/version.h': '#define PHP_' + component.upper() + '_VERSION "' + version + '"\n'})
        path = self.root / 'config/apk-runtime-aliases.json'
        path.write_text(json.dumps({'schema': 1, 'extensions': {'amqp': {'suffix': '-shared'}, 'igbinary': {'suffix': '-shared'}}}))
        self.save()
        logical = {entry['name']: entry for entry in versions.candidates(self.root, self.stream)['candidates']}
        apk = {entry['name']: entry for entry in versions.candidates(self.root, self.stream, 'apk')['candidates']}
        for component in ['amqp', 'igbinary']:
            old, new = logical['php-zts-' + component], apk['php-zts-' + component]
            self.assertNotIn('logical_name', new)
            self.assertEqual(new['component'], component)
            self.assertEqual(new['upstream_version'], old['upstream_version'])
            self.assertEqual(new['evidence'], old['evidence'])
        self.assertIn('php-zts-grpc', apk)
        self.assertEqual(logical, apk)
        self.assertEqual(logical, {entry['name']: entry for entry in versions.candidates(self.root, self.stream, 'rpm')['candidates']})

    def test_runtime_macro_aliases_resolve_without_using_release_template(self):
        self.stream, self.php = '8.5', '8.5.12'
        self.metadata['php_versions'] = {self.stream: self.php}
        self.php_source()
        self.profile(['swoole'])
        self.archive('ext-swoole', {'swoole/include/php_swoole.h': '#define PHP_SWOOLE_VERSION SWOOLE_VERSION\n',
                                   'swoole/include/swoole_version.h': '#define SWOOLE_VERSION "6.2.4"\n',
                                   'swoole/tools/templates/version.tpl.h': '#define SWOOLE_VERSION "<?=$next->getVersion() ?>"\n'})
        self.assertEqual(self.result()['php-zts-swoole']['upstream_version'], '6.2.4')

    def test_selected_preflight_does_not_require_unrelated_source_archives(self):
        self.profile(['grpc', 'protobuf'])
        self.archive('ext-grpc', {'grpc/version.h': '#define PHP_GRPC_VERSION "1.83.0"\n'})
        self.save()
        (self.downloads / 'frankenphp.tar.gz').unlink()
        (self.downloads / 'pie.phar').unlink()
        result = versions.candidates(self.root, self.stream, packages='grpc')['candidates']
        self.assertEqual([(item['name'], item['upstream_version']) for item in result], [('php-zts-grpc', '1.83.0')])

    def test_unknown_external_extension_does_not_fall_back_to_php_release(self):
        self.profile(['missing'])
        with self.assertRaisesRegex(RuntimeError, 'neither a frozen external source nor a bundled PHP source'):
            self.result()

    def test_package_xml_is_only_fallback_when_no_runtime_version_macro_exists(self):
        self.profile(['imagick'])
        self.archive('ext-imagick', {'imagick/package.xml': '<package xmlns="urn:fixture"><version><release>3.8.1</release></version></package>'})
        self.assertEqual(self.result()['php-zts-imagick']['upstream_version'], '3.8.1')

    def test_runtime_header_is_authoritative_when_package_xml_is_invalid(self):
        self.profile(['xhprof'])
        self.archive('ext-xhprof', {'xhprof/php_xhprof.h': '#define XHPROF_VERSION "2.3.10"\n',
                                  'xhprof/package.xml': '<not-package-xml'})
        self.assertEqual(self.result()['php-zts-xhprof']['upstream_version'], '2.3.10')

    def test_nonpackaged_addons_are_excluded_from_source_requirements(self):
        self.profile(['swoole-hook-odbc'])
        metadata = self.root / 'config/pkg/ext/ext-swoole.yml'
        metadata.parent.mkdir(parents=True)
        metadata.write_text('ext-swoole-hook-odbc:\n  display-name: swoole\n  php-extension:\n    arg-type: none\n')
        self.assertNotIn('php-zts-swoole-hook-odbc', self.result())

    def test_archive_tampering_is_rejected_before_version_read(self):
        self.profile(['grpc'])
        record, _ = self.archive('ext-grpc', {'grpc/version.h': '#define PHP_GRPC_VERSION "1.84.1"\n'})
        (self.downloads / record['filename']).write_bytes(b'changed source')
        with self.assertRaisesRegex(RuntimeError, 'SHA1 differs'):
            self.result()

    def test_frozen_php_index_checks_archive_checksum_and_real_header(self):
        self.index['sources'][self.php]['archive_sha256'] = '0' * 64
        with self.assertRaisesRegex(RuntimeError, 'identities differ'):
            self.result()
        self.php_source()
        self.index['sources'][self.php]['header_version'] = '8.6.0RC3'
        with self.assertRaisesRegex(RuntimeError, 'version header differs'):
            self.result()

    def test_frozen_pie_hash_and_metadata_are_required(self):
        (self.downloads / 'pie.phar').write_bytes(b'changed PIE')
        with self.assertRaisesRegex(RuntimeError, 'PIE source version/hash differs'):
            self.result()

    def test_cli_writes_complete_candidates_without_mutating_snapshot(self):
        before = {path.name: path.read_bytes() for path in self.downloads.iterdir() if path.is_file()}
        output = self.root / 'source-versions.json'
        subprocess.run(['python3', str(ROOT / '.github/actions/shared-libraries/source-versions.py'), '--root', str(self.root),
                        '--stream', self.stream, '--output', str(output)], check=True)
        value = json.loads(output.read_text())
        self.assertEqual(value['schema'], 1)
        self.assertIn('pie-zts', [item['name'] for item in value['candidates']])
        self.assertEqual({path.name: path.read_bytes() for path in self.downloads.iterdir() if path.is_file()}, before)

    @unittest.skipUnless(shutil.which('git'), 'Git required for source lock fixture')
    def test_per_stream_frozen_git_override_is_read_only_and_not_archive_label(self):
        self.profile(['apcu'])
        self.archive('ext-apcu', {'apcu/php_apcu.h': '#define PHP_APCU_VERSION "5.1.28"\n'}, version='5.1.28')
        path = self.downloads / 'ext-apcu'
        path.mkdir()
        (path / 'php_apcu.h').write_text('#define PHP_APCU_VERSION "5.1.29-dev"\n')
        commands = [['init', '-q'], ['add', '.'], ['-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid', 'commit', '-qm', 'fixture']]
        for command in commands:
            subprocess.run(['git', '-C', str(path), *command], check=True)
        head = subprocess.check_output(['git', '-C', str(path), 'rev-parse', 'HEAD'], text=True).strip()
        self.index['extensions'] = {self.stream: {'ext-apcu': {'lock_type': 'source', 'cache_type': 'git', 'dirname': path.name,
                                                            'hash': head, 'version': 'dev-master+fixture', 'config': {'type': 'git'}}}}
        before = (self.downloads / 'ext-apcu' / 'php_apcu.h').read_bytes()
        result = self.result()['php-zts-apcu']
        self.assertEqual(result['upstream_version'], '5.1.29-dev')
        self.assertEqual(result['evidence']['git_commit'], head)
        self.assertEqual((path / 'php_apcu.h').read_bytes(), before)
        (path / 'php_apcu.h').write_text('#define PHP_APCU_VERSION "99.0.0"\n')
        with self.assertRaisesRegex(RuntimeError, 'Git tree differs'):
            self.result()
        (path / 'php_apcu.h').write_bytes(before)
        (path / 'untracked.h').write_text('#define PHP_APCU_VERSION "99.0.0"\n')
        with self.assertRaisesRegex(RuntimeError, 'Git tree differs'):
            self.result()


if __name__ == '__main__':
    unittest.main()
