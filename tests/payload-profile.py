#!/usr/bin/env python3
"""Cache coverage must describe built outputs, never leftovers in an SDK."""

import copy
import json
from pathlib import Path
import runpy
import subprocess
from tempfile import TemporaryDirectory
import unittest


helper = runpy.run_path(str(Path(__file__).resolve().parents[1] / '.github/actions/shared-libraries/payload-profile.py'))


class PayloadProfile(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory(prefix='spp-payload-profile-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        template = self.root / 'config/templates/craft.yml.twig'
        template.parent.mkdir(parents=True)
        template.write_text("{% set base_sapi_str = 'cli,cgi,fpm,embed,frankenphp' %}\n"
                            "{% set base_shared_str = 'imagick,redis,memcache' %}\n"
                            "{% set base_extensions_str = 'ctype,openssl' %}\n")
        self.record = {'schema': 1, 'kind': 'consumer', 'state': 'sealed', 'php_version': '8.6.0RC4',
                       'configuration': {'sapi': 'cli,cgi,fpm,embed,frankenphp',
                                         'shared-extensions': 'imagick,redis', 'extensions': 'ctype,openssl',
                                         'build-options': {'allow-shared-ext-failure': True},
                                         'extra-env': {'SPC_CMD_VAR_PHP_MAKE_EXTRA_LDFLAGS': '-Wl,--build-id=sha1 -release zts-86'}},
                       'packaging_assets': {name: {} for name in ['bin/phpize', 'bin/php-config', 'include/php/Zend/zend_modules.h']},
                       'files': {name: {'sha256': 'fixture', 'mode': 0o755} for name in
                                 ['bin/php', 'bin/php-cgi', 'bin/php-fpm', 'bin/frankenphp',
                                  'lib/libphp-zts-86.so', 'modules/imagick-zts-86.so', 'modules/redis-zts-86.so']}}

    def test_complete_profile_and_actual_outputs_are_cacheable(self):
        result = helper['cache_export_coverage'](self.record, {}, self.root, '8.6')
        self.assertTrue(result['full'])
        self.assertEqual(result['modules'], ['imagick', 'redis'])
        self.assertEqual(len(result['profile_fingerprint']), 64)

    def test_template_version_filter_drops_memcache_only_for_new_streams(self):
        self.assertIn('memcache', helper['expected_profile'](self.root, '8.4')['shared'])
        self.assertNotIn('memcache', helper['expected_profile'](self.root, '8.5')['shared'])

    def test_filtered_cli_build_cannot_use_complete_cache_key(self):
        self.record['configuration']['sapi'] = 'cli'
        self.record['configuration']['shared-extensions'] = ''
        with self.assertRaisesRegex(RuntimeError, 'Filtered PHP payload'):
            helper['cache_export_coverage'](self.record, {}, self.root, '8.6')

    def test_old_unselected_sapis_modules_embed_and_assets_are_excluded(self):
        self.record['configuration']['sapi'] = 'cli'
        self.record['configuration']['shared-extensions'] = 'imagick'
        allowed = helper['php_file_allowed']
        self.assertTrue(allowed('bin/php', self.record))
        self.assertTrue(allowed('bin/php-config', self.record))
        self.assertTrue(allowed('include/php/Zend/zend_modules.h', self.record))
        self.assertTrue(allowed('modules/imagick-zts-86.so', self.record))
        for name in ['bin/php-cgi', 'bin/php-fpm', 'bin/frankenphp', 'lib/libphp-zts-86.so',
                     'modules/redis-zts-86.so', 'modules/imagick-zts-85.so', 'packaging/frankenphp/package/Caddyfile']:
            self.assertFalse(allowed(name, self.record), name)

    def test_skipped_extension_does_not_resurrect_old_sdk_module(self):
        self.record['skipped_shared_extensions'] = {'schema': 1, 'php_version_id': 80600, 'allow_shared_ext_failure': True,
                                                     'skipped': [{'extension': 'redis'}]}
        self.record['files'].pop('modules/redis-zts-86.so')
        result = helper['cache_export_coverage'](self.record, {}, self.root, '8.6')
        self.assertEqual(result['modules'], ['imagick'])
        self.assertFalse(helper['php_file_allowed']('modules/redis-zts-86.so', self.record))

    def test_frozen_pie_packaging_assets_survive_php_cache_without_core_ownership(self):
        for name in ['packaging/pie/pie.phar', 'packaging/pie/metadata.json']:
            self.assertTrue(helper['php_file_allowed'](name, self.record))
        for name in ['packaging/pie/old.phar', 'packaging/other/pie.phar']:
            self.assertFalse(helper['php_file_allowed'](name, self.record))
        self.assertTrue(helper['php_file_allowed']('packaging/php-module-build/ext/xsl/config.m4', self.record))

    def test_spx_ui_is_cached_only_with_its_module(self):
        name = 'packaging/spx/web-ui/index.html'
        self.assertFalse(helper['php_file_allowed'](name, self.record))
        self.record['configuration']['shared-extensions'] += ',spx'
        self.assertTrue(helper['php_file_allowed'](name, self.record))
        self.record['skipped_shared_extensions'] = {'schema': 1, 'php_version_id': 80600,
            'allow_shared_ext_failure': True, 'skipped': [{'extension': 'spx'}]}
        self.assertFalse(helper['php_file_allowed'](name, self.record))

    def test_honest_incomplete_checkpoint_cannot_occupy_complete_cache_key(self):
        self.record['build_context'] = {'kind': 'full-consumer-build', 'coverage': 'incomplete'}
        with self.assertRaisesRegex(RuntimeError, 'incomplete PHP build checkpoint'):
            helper['cache_export_coverage'](self.record, {}, self.root, '8.6')

    def test_missing_unskipped_module_is_not_a_complete_payload(self):
        self.record['files'].pop('modules/redis-zts-86.so')
        with self.assertRaisesRegex(RuntimeError, 'missing its built module: redis'):
            helper['cache_export_coverage'](self.record, {}, self.root, '8.6')

    def test_configured_but_missing_sapi_or_embed_fails(self):
        for name in ['bin/php-fpm', 'lib/libphp-zts-86.so']:
            record = copy.deepcopy(self.record)
            record['files'].pop(name)
            with self.assertRaisesRegex(RuntimeError, 'missing its built'):
                helper['cache_export_coverage'](record, {}, self.root, '8.6')

    def test_partial_update_requires_verified_full_seed_coverage(self):
        self.record['build_context'] = {'kind': 'partial-consumer-build'}
        for seed in [{}, {'state': 'verified'}, {'state': 'verified', 'coverage': {'full': False}}]:
            with self.assertRaisesRegex(RuntimeError, 'verified complete seed coverage'):
                helper['cache_export_coverage'](self.record, seed, self.root, '8.6')
        result = helper['cache_export_coverage'](self.record, {'state': 'verified', 'coverage': {'full': True}}, self.root, '8.6')
        self.assertTrue(result['full'])

    def test_unsupported_template_does_not_silently_trust_filtered_record(self):
        (self.root / 'config/templates/craft.yml.twig').write_text('sapi: {{ selected }}')
        with self.assertRaisesRegex(RuntimeError, 'Cannot resolve the complete'):
            helper['cache_export_coverage'](self.record, {}, self.root, '8.6')

    def test_wrong_stream_or_sdk_record_is_not_a_full_php_cache(self):
        with self.assertRaisesRegex(RuntimeError, 'different profile stream'):
            helper['cache_export_coverage'](self.record, {}, self.root, '8.5')
        self.record['kind'] = 'sdk'
        with self.assertRaisesRegex(RuntimeError, 'consumer configuration'):
            helper['cache_export_coverage'](self.record, {}, self.root, '8.6')

    def test_unknown_sapi_cannot_masquerade_as_complete_coverage(self):
        self.record['configuration']['sapi'] += ',unmapped'
        with self.assertRaisesRegex(RuntimeError, 'Unsupported SAPI'):
            helper['coverage'](self.record)

    def test_addon_and_dependency_skips_use_registry_metadata(self):
        metadata = self.root / 'config/pkg/ext/extensions.yml'
        metadata.parent.mkdir(parents=True)
        metadata.write_text('ext-addon:\n  php-extension:\n    arg-type: none\n    display-name: imagick\n'
                            'ext-dependent:\n  depends:\n    - ext-redis\n')
        expected = helper['expected_profile'](self.root, '8.6')
        expected['shared'] += ['addon', 'dependent']
        self.record['configuration']['shared-extensions'] += ',addon,dependent'
        self.record['skipped_shared_extensions'] = {'schema': 1, 'php_version_id': 80600, 'allow_shared_ext_failure': True,
                                                     'skipped': [{'extension': 'redis'}]}
        self.record['files'].pop('modules/redis-zts-86.so')
        result = helper['verify_expected_profile'](self.record, expected, self.root)
        self.assertEqual(result['addons'], ['addon'])
        self.assertEqual(result['skipped'], ['dependent', 'redis'])
        self.assertEqual(result['modules'], ['imagick'])
        self.assertFalse(helper['php_file_allowed']('modules/addon-zts-86.so', self.record, self.root))
        self.assertFalse(helper['php_file_allowed']('modules/dependent-zts-86.so', self.record, self.root))

    def test_optional_registry_suggestions_do_not_drop_built_modules(self):
        project = Path(__file__).resolve().parents[1]
        metadata = helper['extension_metadata'](project)
        for name in ['snappy', 'zstd']:
            self.assertNotIn('apcu', metadata[name]['depends'])
        self.assertNotIn('sockets', metadata['event']['depends'])
        self.assertNotIn('igbinary', metadata['memcached']['depends'])
        registry = self.root / 'config/pkg/ext/suggested.yml'
        registry.parent.mkdir(parents=True)
        registry.write_text('ext-imagick:\n  depends:\n    - imagemagick\n  suggests:\n    - ext-redis\n'
                            'ext-dependent:\n  depends@unix:\n    - ext-redis\n  depends@windows:\n    - ext-imagick\n')
        self.record['skipped_shared_extensions'] = {'schema': 1, 'php_version_id': 80600,
                                                     'allow_shared_ext_failure': True, 'skipped': [{'extension': 'redis'}]}
        self.record['files'].pop('modules/redis-zts-86.so')
        result = helper['cache_export_coverage'](self.record, {}, self.root, '8.6')
        self.assertEqual(result['modules'], ['imagick'])
        metadata = helper['extension_metadata'](self.root)
        self.assertEqual(metadata['dependent']['depends'], ['redis'])
        self.assertEqual(metadata['imagick']['depends'], [])
        self.assertEqual(helper['extension_metadata'](self.root, include_suggests=True)['imagick']['depends'], ['redis'])

    def test_skip_profiles_require_matching_php_and_enabled_tolerance(self):
        manifest = {'schema': 1, 'php_version_id': 80500, 'allow_shared_ext_failure': True,
                    'skipped': [{'extension': 'redis'}]}
        self.record['skipped_shared_extensions'] = manifest
        with self.assertRaisesRegex(RuntimeError, 'different PHP source version'):
            helper['coverage'](self.record)
        manifest['php_version_id'] = 80600
        self.record['configuration']['build-options']['allow-shared-ext-failure'] = False
        with self.assertRaisesRegex(RuntimeError, 'unauthorized skip profile'):
            helper['coverage'](self.record)

    def test_complete_cache_requires_development_assets_used_for_core_seed(self):
        self.record['packaging_assets'].pop('bin/phpize')
        with self.assertRaisesRegex(RuntimeError, 'development asset: bin/phpize'):
            helper['cache_export_coverage'](self.record, {}, self.root, '8.6')

    def test_actual_project_template_yields_complete_profile(self):
        project = Path(__file__).resolve().parents[1]
        expected = helper['expected_profile'](project, '8.6')
        self.assertEqual(set(expected['sapis']), {'cli', 'cgi', 'fpm', 'embed', 'frankenphp'})
        self.assertIn('curl', expected['shared'])
        self.assertIn('openssl', expected['extensions'])
        self.assertNotIn('memcache', expected['shared'])
        self.assertNotIn('swoole', expected['shared'])
        self.assertIn('swoole', helper['expected_profile'](project, '8.5')['shared'])

    def test_complete_prerelease_profile_matches_the_rendered_build_configuration(self):
        project = Path(__file__).resolve().parents[1]
        for kind in ['rpm', 'deb', 'apk']:
            with self.subTest(kind=kind):
                configuration = json.loads(subprocess.check_output(
                    ['php', str(project / 'tests/glibc-build-config.php'), '--type=' + kind, '--json'], text=True))
                expected = helper['expected_profile'](project, '8.6')
                for key, field in [('sapis', 'sapi'), ('shared', 'shared-extensions'), ('extensions', 'extensions')]:
                    self.assertEqual(set(expected[key]), set(helper['names'](configuration.get(field))))


if __name__ == '__main__':
    unittest.main()
