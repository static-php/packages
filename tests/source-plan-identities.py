#!/usr/bin/env python3
"""Compile and packaging source identities must select independent work."""

import importlib.util
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / '.github/actions/shared-libraries'))
spec = importlib.util.spec_from_file_location('source_plan_split', ROOT / '.github/actions/shared-libraries/source-plan.py')
source = importlib.util.module_from_spec(spec)
spec.loader.exec_module(source)


class SourceIdentities(unittest.TestCase):
    def test_test_package_destination_keeps_existing_compiled_cache_identity(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            launcher = root / 'bin/spp'
            launcher.parent.mkdir()
            launcher.write_text("const DIST_PATH = BASE_PATH . '/dist';\n")
            previous = source.identity(root)
            launcher.write_text("define('DIST_PATH', getenv('SPP_DIST_PATH') ?: BASE_PATH . '/dist');\n")
            self.assertEqual(source.identity(root), previous)
            launcher.write_text(launcher.read_text() + "putenv('CFLAGS=-O0');\n")
            self.assertNotEqual(source.identity(root), previous)

    def actor_template(self, root, shared):
        path = root / 'config/templates/craft.yml.twig'
        path.parent.mkdir(parents=True)
        path.write_text("{% set base_sapi_str = 'cli,embed' %}\n{% set base_extensions_str = 'ctype' %}\n"
                        "{% set base_shared_str = '" + ','.join(shared) + "' %}\n")

    def definitions(self, root, contents):
        path = root / (source.SPC + 'config/pkg/ext/builtin-extensions.yml')
        path.parent.mkdir(parents=True)
        path.write_text(contents)
        return path

    def metadata(self, root):
        return {'php_versions': {stream: stream + '.1' for stream in source.STREAMS},
                'shared_configuration': source.identity(root),
                'shared_package_configuration': source.package_identity(root),
                'shared_components': source.component_identities(root)}

    def request(self, root, old):
        return source.plan({}, {}, old, old, configuration=source.identity(root),
                           package_configuration=source.package_identity(root), components=source.component_identities(root))

    def test_package_metadata_changes_preserve_compiled_payload_key(self):
        with TemporaryDirectory(prefix='source-plan-identity-') as temporary:
            root = Path(temporary)
            path = root / 'src/step/CreatePackages.php'
            path.parent.mkdir(parents=True)
            path.write_text('old package metadata')
            previous = self.metadata(root)
            old_inputs = self.request(root, previous)['payload_inputs']
            path.write_text('new dependency relationships')
            current = self.request(root, previous)
            self.assertEqual(current['requests'], {})
            self.assertEqual(set(current['package_requests']), set(source.STREAMS))
            self.assertTrue(all(item['full'] for item in current['package_requests'].values()))
            self.assertEqual(current['payload_inputs'], old_inputs)
            self.assertEqual(current['libraries'], [])
            self.assertEqual(current['library_build_inputs'], [])

    def test_one_extension_recipe_change_keeps_php_core_seed(self):
        with TemporaryDirectory(prefix='source-plan-identity-') as temporary:
            root = Path(temporary)
            path = root / 'config/pkg/ext/ext-rar.yml'
            path.parent.mkdir(parents=True)
            path.write_text('old rar patch')
            previous = self.metadata(root)
            before = self.request(root, previous)
            path.write_text('new rar patch')
            after = self.request(root, previous)
            self.assertEqual(after['payload_core_inputs'], before['payload_core_inputs'])
            self.assertNotEqual(after['payload_inputs'], before['payload_inputs'])
            self.assertTrue(all(item == {'full': False, 'packages': ['rar']} for item in after['requests'].values()))

    def test_library_recipe_and_spc_pin_alone_do_not_recompile_php(self):
        with TemporaryDirectory(prefix='source-plan-identity-') as temporary:
            root = Path(temporary)
            path = root / (source.SPC + 'src/Package/Library/zlib.php')
            path.parent.mkdir(parents=True)
            path.write_text('old library recipe')
            lock = root / 'composer.lock'
            lock.write_text(json.dumps({'packages': [{'name': 'crazywhalecc/static-php-cli', 'version': 'dev-old'}]}))
            previous = self.metadata(root)
            path.write_text('new library recipe')
            lock.write_text(json.dumps({'packages': [{'name': 'crazywhalecc/static-php-cli', 'version': 'dev-new'}]}))
            self.assertEqual(self.request(root, previous)['requests'], {})

    def test_core_flags_and_php_archive_replacement_require_core_build(self):
        with TemporaryDirectory(prefix='source-plan-identity-') as temporary:
            root = Path(temporary)
            path = root / 'config/templates/craft.yml.twig'
            path.parent.mkdir(parents=True)
            path.write_text('-O2')
            previous = self.metadata(root)
            path.write_text('-O3')
            self.assertTrue(all(item['full'] for item in self.request(root, previous)['requests'].values()))
            changed = dict(previous, php_source_hashes={'8.6': {'php-8.6.1.tar.gz': 'different bytes'}})
            request = source.plan({}, {}, previous, changed, configuration=previous['shared_configuration'])
            self.assertEqual(set(request['requests']), {'8.6'})

    def test_recipe_only_update_requests_only_that_library(self):
        metadata = {'php_versions': {stream: stream + '.1' for stream in source.STREAMS},
                    'shared_configuration': 'same', 'shared_library_recipes': {'zlib': 'old', 'curl': 'same'}}
        inventory = {'managed_library_artifacts': ['zlib', 'curl'], 'library_tool_artifacts': [],
                     'irrelevant_artifacts': [], 'libraries': {'zlib': {'recipe_hash': 'new'}, 'curl': {'recipe_hash': 'same'}}}
        request = source.plan({}, {}, metadata, metadata, configuration='same', library_ids={'zlib', 'curl'}, build_inputs=inventory)
        self.assertEqual(request['requests'], {})
        self.assertEqual(request['libraries'], ['zlib'])

    def test_linked_core_patch_and_helper_change_core_identity(self):
        with TemporaryDirectory(prefix='source-plan-identity-') as temporary:
            root = Path(temporary)
            recipe = root / (source.SPC + 'src/Package/Target/php/unix.php')
            patch = root / (source.SPC + 'src/globals/patch/php-fix.patch')
            recipe.parent.mkdir(parents=True)
            patch.parent.mkdir(parents=True)
            recipe.write_text("<?php patchFile('php-fix.patch');")
            patch.write_text('old patch')
            previous = self.metadata(root)
            patch.write_text('new patch')
            self.assertTrue(all(item['full'] for item in self.request(root, previous)['requests'].values()))
            helper = root / (source.SPC + 'src/StaticPHP/Util/SourcePatcher.php')
            helper.parent.mkdir(parents=True)
            previous = self.metadata(root)
            helper.write_text('changed patch application')
            self.assertTrue(all(item['full'] for item in self.request(root, previous)['requests'].values()))

    def test_linked_shared_extension_patch_is_partial_but_static_recipe_is_full(self):
        with TemporaryDirectory(prefix='source-plan-identity-') as temporary:
            root = Path(temporary)
            recipe = root / (source.SPC + 'src/Package/Extension/rar.php')
            patch = root / (source.SPC + 'src/globals/patch/rar-fix.patch')
            recipe.parent.mkdir(parents=True)
            patch.parent.mkdir(parents=True)
            recipe.write_text("<?php patchFile('rar-fix.patch');")
            patch.write_text('old patch')
            previous = self.metadata(root)
            patch.write_text('new patch')
            self.assertTrue(all(item == {'full': False, 'packages': ['rar']} for item in self.request(root, previous)['requests'].values()))
            static = recipe.with_name('ctype.php')
            static.write_text('old ctype recipe')
            previous = self.metadata(root)
            static.write_text('new ctype recipe')
            request = self.request(root, previous)
            self.assertTrue(all(item['full'] for item in request['requests'].values()))
            self.assertNotIn('ctype', source.component_identities(root))

    def test_unused_memcache_and_pecl_zip_sources_do_not_change_new_php(self):
        metadata = {'php_versions': {stream: stream + '.1' for stream in source.STREAMS}, 'shared_configuration': 'same'}
        for name in ['memcache', 'zip']:
            old = {'ext-' + name: {'source': {'hash': 'old'}}}
            new = {'ext-' + name: {'source': {'hash': 'new'}}}
            before = source.plan(old, old, metadata, metadata, configuration='same')
            after = source.plan(old, new, metadata, metadata, configuration='same')
            self.assertEqual(set(after['requests']), {'8.2', '8.3', '8.4'})
            for stream in ['8.5', '8.6']:
                self.assertEqual(before['payload_inputs'][stream], after['payload_inputs'][stream])

    def test_library_metadata_and_one_openssl_profile_never_request_compilation(self):
        with TemporaryDirectory(prefix='source-plan-identity-') as temporary:
            root = Path(temporary)
            library = root / 'src/package/library.php'
            library.parent.mkdir(parents=True)
            library.write_text('old library package metadata')
            variants = root / '.github/actions/shared-libraries/openssl-variants.py'
            variants.parent.mkdir(parents=True)
            variants.write_text("PROFILES = {'rpm': '/etc/pki/tls', 'deb': '/etc/ssl'}\ndef build(): pass\n")
            previous = self.metadata(root)
            previous['shared_library_package_configuration'] = source.library_package_identity(root)
            previous['shared_variant_configuration'] = source.variant_identities(root)
            library.write_text('new library package dependencies')
            variants.write_text("PROFILES = {'rpm': '/etc/pki/tls', 'deb': '/etc/ssl/changed'}\ndef build(): pass\n")
            inventory = {'managed_library_artifacts': ['zlib'], 'library_tool_artifacts': [], 'irrelevant_artifacts': [], 'libraries': {'zlib': {'recipe_hash': 'same'}}}
            previous['shared_library_recipes'] = {'zlib': 'same'}
            request = source.plan({}, {}, previous, previous, configuration=source.identity(root),
                                  package_configuration=source.package_identity(root), components=source.component_identities(root),
                                  build_inputs=inventory, library_package_configuration=source.library_package_identity(root), variants=source.variant_identities(root))
            self.assertEqual(request['requests'], {})
            self.assertEqual(request['package_requests'], {})
            self.assertEqual(request['libraries'], [])
            self.assertEqual(request['library_package_requests'], ['zlib'])
            self.assertEqual(request['variant_package_requests'], {'deb': ['openssl']})

    def test_current_checkout_reconciles_old_snapshot_without_mutating_sources(self):
        with TemporaryDirectory(prefix='source-plan-identity-') as temporary:
            root = Path(temporary)
            template = root / 'config/templates/craft.yml.twig'
            template.parent.mkdir(parents=True)
            template.write_text('-O2')
            metadata = self.metadata(root)
            metadata['shared_library_package_configuration'] = source.library_package_identity(root)
            metadata['shared_variant_configuration'] = {}
            original = {'requests': {}, 'package_requests': {}, 'configuration': source.identity(root)}
            inventory = {'managed_library_artifacts': [], 'library_tool_artifacts': [], 'irrelevant_artifacts': [], 'libraries': {}}
            template.write_text('-O3')
            request = source.reconcile(original, {}, metadata, source.identity(root), source.package_identity(root),
                                       source.component_identities(root), inventory, source.library_package_identity(root), {})
            self.assertTrue(all(item['full'] for item in request['requests'].values()))
            self.assertEqual(original['requests'], {})
            self.assertEqual(request['snapshot_configuration'], original['configuration'])

    def test_rendered_craft_changes_between_streams_do_not_change_recipe_identity(self):
        with TemporaryDirectory(prefix='source-plan-identity-') as temporary:
            root = Path(temporary)
            previous = source.identity(root)
            (root / 'craft.yml').write_text('php-version: 8.2.35\n')
            self.assertEqual(source.identity(root), previous)
            (root / 'craft.yml').write_text('php-version: 8.6.0RC4\n')
            self.assertEqual(source.identity(root), previous)

    def test_never_selected_vendor_extension_recipe_does_not_request_work(self):
        with TemporaryDirectory(prefix='source-plan-identity-') as temporary:
            root = Path(temporary)
            template = root / 'config/templates/craft.yml.twig'
            template.parent.mkdir(parents=True)
            template.write_text("{% set base_sapi_str = 'cli,embed' %}\n{% set base_shared_str = 'rar' %}\n{% set base_extensions_str = 'ctype' %}\n")
            recipe = root / (source.SPC + 'src/Package/Extension/imap.php')
            recipe.parent.mkdir(parents=True)
            recipe.write_text('old unused imap recipe')
            before = self.metadata(root)
            recipe.write_text('new unused imap recipe')
            self.assertEqual(self.request(root, before)['requests'], {})

    def test_prerelease_git_sources_only_rebuild_their_php_stream(self):
        stable = {'lock_type': 'source', 'hash': 'stable', 'time': 1}
        master = {'lock_type': 'source', 'hash': 'old-master', 'time': 1}
        previous = {'php_versions': {stream: stream + '.1' for stream in source.STREAMS}, 'shared_configuration': 'same',
                    'extension_sources': {stream: {'ext-apcu': master if stream == '8.6' else stable} for stream in source.STREAMS}}
        current = json.loads(json.dumps(previous))
        current['extension_sources']['8.6']['ext-apcu']['hash'] = 'new-master'
        artifacts = {'ext-apcu': {'source': stable}}
        request = source.plan(artifacts, artifacts, previous, current, configuration='same')
        self.assertEqual(request['requests'], {'8.6': {'full': False, 'packages': ['apcu']}})
        before = source.plan(artifacts, artifacts, previous, previous, configuration='same')
        for stream in ['8.2', '8.3', '8.4', '8.5']:
            self.assertEqual(request['payload_inputs'][stream], before['payload_inputs'][stream])
        current['extension_sources']['8.6']['ext-apcu']['hash'] = 'old-master'
        current['extension_sources']['8.6']['ext-apcu']['time'] = 999
        self.assertEqual(source.plan(artifacts, artifacts, previous, current, configuration='same')['requests'], {})

    def test_module_only_helper_and_hook_rebuild_only_the_seven_configured_modules(self):
        actors = {'mysqli', 'mysqlnd', 'shmop', 'soap', 'sqlite3', 'xsl', 'xhprof'}
        self.assertEqual(source.module_only_components(ROOT), actors)
        with TemporaryDirectory(prefix='source-plan-module-actors-') as temporary:
            root = Path(temporary)
            self.actor_template(root, sorted(actors | {'curl', 'grpc'}))
            self.definitions(root, ''.join('ext-' + name + ':\n  php-extension:\n    build-with-php: true\n' for name in sorted(actors)))
            helper = root / 'src/util/ModuleOnlyBuild.php'
            helper.parent.mkdir(parents=True)
            helper.write_text('old selected module linker')
            hook = root / 'src/hook/BuildSelectedPhpModules.php'
            hook.parent.mkdir(parents=True)
            hook.write_text('old selected module make invocation')
            for path in [helper, hook]:
                previous = self.metadata(root)
                before = self.request(root, previous)
                path.write_text(path.read_text() + '\nchanged selected compiler inputs')
                after = self.request(root, previous)
                self.assertEqual(after['payload_core_inputs'], before['payload_core_inputs'])
                self.assertEqual(after['package_requests'], {})
                self.assertTrue(all(entry == {'full': False, 'packages': sorted(actors)} for entry in after['requests'].values()))

    def test_bundled_source_repair_hashes_only_actual_bundled_stream_components(self):
        with TemporaryDirectory(prefix='source-plan-source-repair-') as temporary:
            root = Path(temporary)
            self.actor_template(root, ['curl', 'grpc', 'zip'])
            self.definitions(root, 'ext-curl:\n  type: php-extension\next-grpc:\n  artifact:\n    source:\n      type: pie\n'
                                   'ext-zip:\n  artifact:\n    source:\n      type: pecl\n')
            helper = root / '.github/actions/shared-libraries/rehydrate-php-extensions.py'
            helper.parent.mkdir(parents=True)
            helper.write_text('old selected source repair')
            previous = self.metadata(root)
            before = self.request(root, previous)
            helper.write_text('new selected source repair')
            after = self.request(root, previous)
            self.assertEqual(after['payload_core_inputs'], before['payload_core_inputs'])
            self.assertEqual(after['package_requests'], {})
            for stream in source.STREAMS:
                self.assertEqual(after['requests'][stream], {'full': False, 'packages': ['curl'] if stream < '8.5' else ['curl', 'zip']})

    def test_one_preparation_switch_branch_rebuilds_only_its_extension(self):
        with TemporaryDirectory(prefix='source-plan-prepare-hook-') as temporary:
            root = Path(temporary)
            self.actor_template(root, ['decimal', 'grpc', 'maxminddb'])
            hook = root / 'src/hook/PrepareReusedExtensionSources.php'
            hook.parent.mkdir(parents=True)
            hook.write_text((ROOT / 'src/hook/PrepareReusedExtensionSources.php').read_text())
            previous = self.metadata(root)
            before = self.request(root, previous)
            hook.write_text(hook.read_text().replace("'@@build_lib_path@@'", "'@@repaired_build_lib_path@@'"))
            after = self.request(root, previous)
            self.assertEqual(after['payload_core_inputs'], before['payload_core_inputs'])
            self.assertTrue(all(entry == {'full': False, 'packages': ['grpc']} for entry in after['requests'].values()))

    def test_individual_extension_hooks_never_invalidate_the_core(self):
        with TemporaryDirectory(prefix='source-plan-individual-hook-') as temporary:
            root = Path(temporary)
            self.actor_template(root, ['ev', 'ds', 'rar'])
            hook = root / 'src/hook/Ev.php'
            hook.parent.mkdir(parents=True)
            hook.write_text('old ev patch')
            previous = self.metadata(root)
            before = self.request(root, previous)
            hook.write_text('new ev patch')
            after = self.request(root, previous)
            self.assertEqual(after['payload_core_inputs'], before['payload_core_inputs'])
            self.assertTrue(all(entry == {'full': False, 'packages': ['ev']} for entry in after['requests'].values()))

    def test_shared_registry_flag_edits_do_not_invalidate_neighboring_core_recipe(self):
        with TemporaryDirectory(prefix='source-plan-registry-block-') as temporary:
            root = Path(temporary)
            self.actor_template(root, ['curl'])
            registry = self.definitions(root, 'ext-ctype:\n  php-extension:\n    arg-type: enable\n'
                                             'ext-curl:\n  php-extension:\n    arg-type: with\n')
            previous = self.metadata(root)
            before = self.request(root, previous)
            registry.write_text(registry.read_text().replace('arg-type: with', 'arg-type: with-path'))
            after = self.request(root, previous)
            self.assertEqual(after['payload_core_inputs'], before['payload_core_inputs'])
            self.assertTrue(all(entry == {'full': False, 'packages': ['curl']} for entry in after['requests'].values()))
            previous = self.metadata(root)
            registry.write_text(registry.read_text().replace('arg-type: enable', 'arg-type: custom'))
            self.assertTrue(all(entry['full'] for entry in self.request(root, previous)['requests'].values()))

    def test_shared_preservation_helpers_and_addon_flags_bind_real_parent_modules(self):
        with TemporaryDirectory(prefix='source-plan-addon-actors-') as temporary:
            root = Path(temporary)
            self.actor_template(root, ['swoole', 'swoole-hook-mysql'])
            registry = self.definitions(root, 'ext-swoole:\n  php-extension:\n    arg-type: custom\n'
                                             'ext-swoole-hook-mysql:\n  php-extension:\n    arg-type: none\n    display-name: swoole\n')
            helper = root / 'src/util/ConsumerSnapshot.php'
            helper.parent.mkdir(parents=True)
            helper.write_text('old shared consumer preservation')
            previous = self.metadata(root)
            before = self.request(root, previous)
            helper.write_text('new shared consumer preservation')
            after = self.request(root, previous)
            self.assertEqual(after['payload_core_inputs'], before['payload_core_inputs'])
            self.assertEqual(after['package_requests'], {})
            self.assertTrue(all(entry == {'full': False, 'packages': ['swoole']} for entry in after['requests'].values()))
            self.assertNotIn('swoole-hook-mysql', source.component_identities(root))
            previous = self.metadata(root)
            registry.write_text(registry.read_text().replace('    display-name: swoole', '    display-name: swoole\n    extra-build-flag: true'))
            self.assertTrue(all(entry == {'full': False, 'packages': ['swoole']} for entry in self.request(root, previous)['requests'].values()))

    def test_retired_apk_alias_configuration_has_no_effect(self):
        with TemporaryDirectory(prefix='source-plan-apk-alias-') as temporary:
            root = Path(temporary)
            self.actor_template(root, ['amqp', 'igbinary'])
            path = root / 'config/apk-runtime-aliases.json'
            path.write_text(json.dumps({'schema': 1, 'extensions': {'amqp': {'suffix': '-shared'}, 'igbinary': {'suffix': '-shared'}}}))
            previous = self.metadata(root)
            previous['shared_format_package_configuration'] = source.format_package_identities(root)
            before = self.request(root, previous)
            path.write_text(path.read_text().replace('-shared', '-runtime'))
            after = source.plan({}, {}, previous, previous, configuration=source.identity(root),
                                package_configuration=source.package_identity(root), components=source.component_identities(root),
                                format_package_configuration=source.format_package_identities(root))
            self.assertEqual(after['requests'], {})
            self.assertEqual(after['package_requests'], {})
            self.assertEqual(after['libraries'], [])
            self.assertEqual(after['payload_inputs'], before['payload_inputs'])
            self.assertEqual(after['format_package_requests'], {})


if __name__ == '__main__':
    unittest.main()
