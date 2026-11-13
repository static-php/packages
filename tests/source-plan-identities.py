#!/usr/bin/env python3
"""Compile and packaging source identities must select independent work."""

import importlib.util
import io
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / '.github/actions/shared-libraries'))
spec = importlib.util.spec_from_file_location('source_plan_split', ROOT / '.github/actions/shared-libraries/source-plan.py')
source = importlib.util.module_from_spec(spec)
spec.loader.exec_module(source)

class SourceIdentities(unittest.TestCase):
    def test_automatic_php_release_update_ignores_builder_changes(self):
        for builder in ['Dockerfile.rhel', 'Dockerfile.alpine']:
            for release_changed, force in [(False, False), (True, False), (True, True)]:
                with self.subTest(builder=builder, release_changed=release_changed, force=force), TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    self.actor_template(root, ['curl', 'zip'])
                    path = root / builder
                    path.write_text('old builder')
                    previous = self.metadata(root)
                    previous['php_versions']['8.6'] = '8.6.0RC5'
                    downloads = root / 'downloads'
                    downloads.mkdir()
                    (downloads / 'php-8.6.0RC5.tar.gz').write_bytes(b'RC5 source')
                    previous['php_source_hashes'] = source.php_archive_inputs(root, previous)
                    current = json.loads(json.dumps(previous))
                    if release_changed:
                        current['php_versions']['8.6'] = '8.6.0RC6'
                        (downloads / 'php-8.6.0RC6.tar.gz').write_bytes(b'RC6 source')
                    state = root / '.download-state'
                    state.mkdir()
                    for destination, value in [(downloads / '.spc-meta.json', current), (state / 'meta.prev.json', previous),
                                               (downloads / '.cache.json', {}), (state / 'lock.prev.json', {})]:
                        destination.write_text(json.dumps(value))
                    path.write_text('new builder')
                    inventory = {'libraries': {}, 'managed_library_artifacts': [], 'library_tool_artifacts': [], 'irrelevant_artifacts': []}
                    arguments = ['source-plan.py', '--root', str(root)] + (['--force-full'] if force else [])
                    output = io.StringIO()
                    with mock.patch('sys.argv', arguments), mock.patch('sys.stdout', output), \
                            mock.patch.object(source.subprocess, 'check_output', return_value=json.dumps(inventory)):
                        source.main()
                    request = json.loads((downloads / 'build-plan.json').read_text())
                    expected = source.STREAMS if force else ['8.6'] if release_changed else []
                    self.assertEqual(request['requests'], {stream: {'full': True, 'packages': []} for stream in expected})
                    self.assertEqual(request['package_requests'], {})
                    self.assertEqual(request['libraries'], [])
                    self.assertEqual(request['library_build_inputs'], [])
                    self.assertIn('any_change=' + str(bool(expected)).lower(), output.getvalue())

    def test_automatic_reconciliation_keeps_php_release_selection_after_builder_change(self):
        for comparison_available in [True, False]:
            with self.subTest(comparison_available=comparison_available), TemporaryDirectory() as temporary:
                root = Path(temporary)
                self.actor_template(root, ['curl', 'zip'])
                builder = root / 'Dockerfile.alpine'
                builder.write_text('old builder')
                previous = self.metadata(root)
                previous['php_versions']['8.6'] = '8.6.0RC5'
                current = json.loads(json.dumps(previous))
                current['php_versions']['8.6'] = '8.6.0RC6'
                original = source.plan({}, {}, previous, current)
                original['source_comparison_available'] = comparison_available
                if not comparison_available:
                    original['requests'] = {stream: {'full': True, 'packages': []} for stream in source.STREAMS}
                downloads = root / 'downloads'
                downloads.mkdir()
                state = root / '.download-state'
                state.mkdir()
                for destination, value in [(downloads / '.spc-meta.json', current), (state / 'meta.prev.json', previous),
                                           (downloads / '.cache.json', {}), (state / 'lock.prev.json', {}),
                                           (downloads / 'build-plan.json', original),
                                           (downloads / '.source-snapshot.json', {'repository': 'org/repo', 'run_id': '123'})]:
                    destination.write_text(json.dumps(value))
                snapshot = {path.name: path.read_bytes() for path in downloads.iterdir()}
                builder.write_text('new builder')
                inventory = {'libraries': {}, 'managed_library_artifacts': [], 'library_tool_artifacts': [], 'irrelevant_artifacts': []}
                output = root / 'stage/effective-source-plan.json'
                restore = mock.Mock()
                arguments = ['source-plan.py', '--root', str(root), '--reconcile', '--output', str(output)]
                with mock.patch('sys.argv', arguments), mock.patch('sys.stdout', io.StringIO()), \
                        mock.patch.object(source.subprocess, 'check_output', return_value=json.dumps(inventory)), \
                        mock.patch.object(source.runpy, 'run_path', return_value={'restore': restore}):
                    source.main()
                effective = json.loads(output.read_text())
                self.assertEqual(effective['requests'], {'8.6': {'full': True, 'packages': []}})
                self.assertEqual(effective['package_requests'], {})
                self.assertEqual(restore.call_count, int(not comparison_available))
                self.assertEqual({path.name: path.read_bytes() for path in downloads.iterdir()}, snapshot)

    def test_helper_changes_cannot_expand_a_pinned_extension_update(self):
        metadata = {'php_versions': {stream: stream + '.1' for stream in source.STREAMS},
                    'shared_components': {'mysqlnd': 'old', 'ssh2': 'old', 'frankenphp': 'old'}}
        original = {'requests': {stream: {'full': False, 'packages': ['ssh2']} for stream in source.STREAMS},
                    'libraries': ['libaom'], 'source_comparison_available': True}
        inventory = {'managed_library_artifacts': ['libaom'], 'libraries': {}}
        current = {name: 'changed helper' for name in metadata['shared_components']}
        result = source.reconcile(original, {}, metadata, inventory)
        self.assertEqual(result['requests'], original['requests'])
        self.assertEqual(result['libraries'], ['libaom'])

    def test_two_changed_libraries_do_not_select_frankenphp_or_php_streams(self):
        previous = {'php_versions': {stream: stream + '.1' for stream in source.STREAMS},
                    'shared_configuration': 'build', 'shared_components': {},
                    'shared_library_recipes': {'libaom': 'same', 'libjxl': 'same'}}
        inventory = {'libraries': {'libaom': {'recipe_hash': 'same'}, 'libjxl': {'recipe_hash': 'same'}},
                     'managed_library_artifacts': ['libaom', 'libjxl'], 'library_tool_artifacts': [], 'irrelevant_artifacts': []}
        old = {'libaom': 'old-aom', 'libjxl': 'old-jxl', 'frankenphp': 'unchanged-frankenphp'}
        new = dict(old, libaom='new-aom', libjxl='new-jxl')
        original = source.plan(old, new, previous, previous, library_ids={'libaom', 'libjxl'})
        self.assertEqual(original['libraries'], ['libaom', 'libjxl'])
        self.assertEqual(original['requests'], {})
        self.assertEqual(original['package_requests'], {})
        effective = source.reconcile(original, new, previous, inventory)
        self.assertEqual(effective['requests'], {})
        self.assertEqual(effective['package_requests'], {})
        self.assertEqual(effective['library_package_requests'], [])

    def test_missing_download_state_does_not_invent_library_packaging_changes(self):
        inventory = {'libraries': {'attr': {'recipe_hash': 'same-recipe'}},
                     'managed_library_artifacts': ['attr'], 'library_tool_artifacts': [], 'irrelevant_artifacts': []}
        def plan(previous):
            return source.plan({}, {}, previous, {})
        self.assertEqual(plan({})['library_package_requests'], [])
        self.assertEqual(plan({})['variant_package_requests'], {})
        self.assertEqual(plan({'shared_library_package_configuration': 'older-packaging'})['library_package_requests'], [])
        self.assertEqual(plan({'shared_variant_configuration': {'rpm': 'older-profile'}})['variant_package_requests'], {})

    def test_legacy_snapshot_packaging_hints_are_ignored(self):
        inventory = {'libraries': {'attr': {'recipe_hash': 'same-recipe'}},
                     'managed_library_artifacts': ['attr'], 'library_tool_artifacts': [], 'irrelevant_artifacts': []}
        metadata = {'shared_configuration': 'build', 'shared_package_configuration': 'package',
                    'shared_library_package_configuration': 'library-package', 'shared_variant_configuration': {'rpm': 'profile'}}
        original = {'library_package_requests': ['attr'], 'variant_package_requests': {'rpm': ['openssl']}}
        def reconcile():
            return source.reconcile(original, {}, metadata, inventory)
        self.assertEqual(reconcile()['library_package_requests'], [])
        self.assertEqual(reconcile()['variant_package_requests'], {})
        original.update(library_package_baseline='older-package', variant_package_baselines={'rpm': 'older-profile'})
        self.assertEqual(reconcile()['library_package_requests'], [])
        self.assertEqual(reconcile()['variant_package_requests'], {})

    def actor_template(self, root, shared):
        path = root / 'config/templates/craft.yml.twig'
        path.parent.mkdir(parents=True)
        path.write_text("{% set base_sapi_str = 'cli,embed' %}\n{% set base_extensions_str = 'ctype' %}\n"
                        "{% set base_shared_str = '" + ','.join(shared) + "' %}\n")

    def definitions(self, root, contents):
        path = root / ('vendor/crazywhalecc/static-php-cli/' + 'config/pkg/ext/builtin-extensions.yml')
        path.parent.mkdir(parents=True)
        path.write_text(contents)
        return path

    def metadata(self, root):
        return {'php_versions': {stream: stream + '.1' for stream in source.STREAMS}}

    def request(self, root, old):
        return source.plan({}, {}, old, old)

    def test_package_metadata_changes_do_not_request_work(self):
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
            self.assertEqual(current['package_requests'], {})
            self.assertEqual(current['payload_inputs'], old_inputs)
            self.assertEqual(current['libraries'], [])
            self.assertEqual(current['library_build_inputs'], [])

    def test_extension_recipe_changes_do_not_select_sources(self):
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
            self.assertEqual(after['payload_inputs'], before['payload_inputs'])
            self.assertEqual(after['requests'], {})

    def test_library_recipe_and_spc_pin_alone_do_not_recompile_php(self):
        with TemporaryDirectory(prefix='source-plan-identity-') as temporary:
            root = Path(temporary)
            path = root / ('vendor/crazywhalecc/static-php-cli/' + 'src/Package/Library/zlib.php')
            path.parent.mkdir(parents=True)
            path.write_text('old library recipe')
            lock = root / 'composer.lock'
            lock.write_text(json.dumps({'packages': [{'name': 'crazywhalecc/static-php-cli', 'version': 'dev-old'}]}))
            previous = self.metadata(root)
            path.write_text('new library recipe')
            lock.write_text(json.dumps({'packages': [{'name': 'crazywhalecc/static-php-cli', 'version': 'dev-new'}]}))
            self.assertEqual(self.request(root, previous)['requests'], {})

    def test_only_php_source_changes_require_a_core_build(self):
        with TemporaryDirectory(prefix='source-plan-identity-') as temporary:
            root = Path(temporary)
            path = root / 'config/templates/craft.yml.twig'
            path.parent.mkdir(parents=True)
            path.write_text('-O2')
            previous = self.metadata(root)
            path.write_text('-O3')
            self.assertEqual(self.request(root, previous)['requests'], {})
            changed = dict(previous, php_source_hashes={'8.6': {'php-8.6.1.tar.gz': 'different bytes'}})
            request = source.plan({}, {}, previous, changed)
            self.assertEqual(set(request['requests']), {'8.6'})

    def test_recipe_only_update_does_not_select_library_sources(self):
        metadata = {'php_versions': {stream: stream + '.1' for stream in source.STREAMS},
                    'shared_configuration': 'same', 'shared_library_recipes': {'zlib': 'old', 'curl': 'same'}}
        inventory = {'managed_library_artifacts': ['zlib', 'curl'], 'library_tool_artifacts': [],
                     'irrelevant_artifacts': [], 'libraries': {'zlib': {'recipe_hash': 'new'}, 'curl': {'recipe_hash': 'same'}}}
        request = source.plan({}, {}, metadata, metadata, library_ids={'zlib', 'curl'})
        self.assertEqual(request['requests'], {})
        self.assertEqual(request['libraries'], [])

    def test_unused_memcache_and_pecl_zip_sources_do_not_change_new_php(self):
        metadata = {'php_versions': {stream: stream + '.1' for stream in source.STREAMS}, 'shared_configuration': 'same'}
        for name in ['memcache', 'zip']:
            old = {'ext-' + name: {'source': {'hash': 'old'}}}
            new = {'ext-' + name: {'source': {'hash': 'new'}}}
            before = source.plan(old, old, metadata, metadata)
            after = source.plan(old, new, metadata, metadata)
            self.assertEqual(set(after['requests']), {'8.2', '8.3', '8.4'})
            for stream in ['8.5', '8.6']:
                self.assertEqual(before['payload_inputs'][stream], after['payload_inputs'][stream])

    def test_unused_packaging_recipes_cannot_break_source_planning(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.actor_template(root, ['ssh2'])
            downloads = root / 'downloads'
            downloads.mkdir()
            original = {'source_comparison_available': True, 'requests': {'8.6': {'packages': ['ssh2']}},
                        'libraries': ['libaom'], 'package_requests': {'8.5': {'full': True, 'packages': []}},
                        'library_package_requests': ['zlib'], 'variant_package_requests': {'deb': ['openssl']}}
            for filename, value in [('.spc-meta.json', self.metadata(root)), ('.cache.json', {}), ('build-plan.json', original)]:
                (downloads / filename).write_text(json.dumps(value))
            recipe = root / '.github/actions/shared-libraries/openssl-variants.py'
            recipe.parent.mkdir(parents=True)
            recipe.write_text('invalid syntax that source planning must not parse')
            inventory = {'managed_library_artifacts': ['libaom'], 'libraries': {}}
            output = root / 'effective.json'
            with mock.patch('sys.argv', ['source-plan.py', '--root', str(root), '--reconcile', '--output', str(output)]), \
                    mock.patch('sys.stdout', io.StringIO()), \
                    mock.patch.object(source.subprocess, 'check_output', return_value=json.dumps(inventory)):
                source.main()
            result = json.loads(output.read_text())
            self.assertEqual(result['requests'], {'8.6': {'full': False, 'packages': ['ssh2']}})
            self.assertEqual(result['libraries'], ['libaom'])
            self.assertEqual(result['package_requests'], {})
            self.assertEqual(result['library_package_requests'], [])
            self.assertEqual(result['variant_package_requests'], {})

    def test_current_checkout_reconciles_old_snapshot_without_mutating_sources(self):
        with TemporaryDirectory(prefix='source-plan-identity-') as temporary:
            root = Path(temporary)
            template = root / 'config/templates/craft.yml.twig'
            template.parent.mkdir(parents=True)
            template.write_text('-O2')
            metadata = self.metadata(root)
            metadata['shared_library_package_configuration'] = 'old-library-recipe'
            metadata['shared_variant_configuration'] = {}
            original = {'requests': {}, 'package_requests': {}, 'configuration': 'source-only'}
            inventory = {'managed_library_artifacts': [], 'library_tool_artifacts': [], 'irrelevant_artifacts': [], 'libraries': {}}
            template.write_text('-O3')
            request = source.reconcile(original, {}, metadata, inventory)
            self.assertEqual(request['requests'], {})
            self.assertEqual(original['requests'], {})

    def test_never_selected_vendor_extension_recipe_does_not_request_work(self):
        with TemporaryDirectory(prefix='source-plan-identity-') as temporary:
            root = Path(temporary)
            template = root / 'config/templates/craft.yml.twig'
            template.parent.mkdir(parents=True)
            template.write_text("{% set base_sapi_str = 'cli,embed' %}\n{% set base_shared_str = 'rar' %}\n{% set base_extensions_str = 'ctype' %}\n")
            recipe = root / ('vendor/crazywhalecc/static-php-cli/' + 'src/Package/Extension/imap.php')
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
        request = source.plan(artifacts, artifacts, previous, current)
        self.assertEqual(request['requests'], {'8.6': {'full': False, 'packages': ['apcu']}})
        before = source.plan(artifacts, artifacts, previous, previous)
        for stream in ['8.2', '8.3', '8.4', '8.5']:
            self.assertEqual(request['payload_inputs'][stream], before['payload_inputs'][stream])
        current['extension_sources']['8.6']['ext-apcu']['hash'] = 'old-master'
        current['extension_sources']['8.6']['ext-apcu']['time'] = 999
        self.assertEqual(source.plan(artifacts, artifacts, previous, current)['requests'], {})

    def test_retired_apk_alias_configuration_has_no_effect(self):
        with TemporaryDirectory(prefix='source-plan-apk-alias-') as temporary:
            root = Path(temporary)
            self.actor_template(root, ['amqp', 'igbinary'])
            path = root / 'config/apk-runtime-aliases.json'
            path.write_text(json.dumps({'schema': 1, 'extensions': {'amqp': {'suffix': '-shared'}, 'igbinary': {'suffix': '-shared'}}}))
            previous = self.metadata(root)
            before = self.request(root, previous)
            path.write_text(path.read_text().replace('-shared', '-runtime'))
            after = source.plan({}, {}, previous, previous)
            self.assertEqual(after['requests'], {})
            self.assertEqual(after['package_requests'], {})
            self.assertEqual(after['libraries'], [])
            self.assertEqual(after['payload_inputs'], before['payload_inputs'])
            self.assertEqual(after['format_package_requests'], {})

if __name__ == '__main__':
    unittest.main()
