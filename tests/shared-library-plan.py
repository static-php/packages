import argparse
import copy
import gzip
import hashlib
import io
import json
import runpy
import shutil
import subprocess
import tarfile
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch


root = Path(__file__).resolve().parents[1]
planner = runpy.run_path(str(root / 'bin/shared-library-plan'))
namespace = planner['compare'].__globals__


def library(version='1.0', soname='libfixture.so.1', epoch=1, depends=None):
    return {'version': version, 'source_hash': version, 'abi_epoch': epoch,
            'files': ['lib/' + soname], 'elf': {'lib/' + soname: {'soname': soname, 'needed': []}},
            'depends': depends or [], 'abi_dump': {soname: 'abi/' + soname + '.xml'}}


def manifest(libraries=None):
    return {'schema': 1, 'linkage': 'shared', 'runtime_libdir': '/usr/lib/php-zts/lib',
            'architecture': 'x86_64', 'libc': 'glibc', 'libraries': libraries if libraries is not None else {'fixture': library()}}


class SharedLibraryPlan(unittest.TestCase):
    @unittest.skipUnless(shutil.which('php'), 'PHP manifest round-trip required')
    def test_php_empty_package_version_map_is_normalized_without_losing_versions(self):
        for versions in [{}, {'rpm': '1.0-4', 'deb': '1.0-7'}]:
            with self.subTest(versions=versions):
                value = manifest()
                value['libraries']['fixture']['package_versions'] = versions
                output = subprocess.check_output(['php', '-r',
                    'echo json_encode(json_decode(stream_get_contents(STDIN), true));'],
                    input=json.dumps(value).encode())
                parsed = json.loads(output)
                self.assertEqual(parsed['libraries']['fixture']['package_versions'], versions or [])
                result = planner['validate_manifest'](parsed)
                actual = result['libraries']['fixture']['package_versions']
                self.assertEqual(actual, versions)
                actual.pop('apk', None)

    def test_populated_list_cannot_impersonate_a_package_version_map(self):
        value = manifest()
        value['libraries']['fixture']['package_versions'] = ['1.0-r2']
        with self.assertRaisesRegex(RuntimeError, 'Invalid library package versions'):
            planner['validate_manifest'](value)

    def test_pending_transition_is_available_only_for_intermediate_planning(self):
        value = manifest({'fixture': library(soname='libfixture.so.2'), 'consumer': library(soname='libconsumer.so.1', depends=['fixture'])})
        value['pending_rebuilds'] = {'consumer': {'libfixture.so.1': 'fixture'}}
        for complete in [True, False]:
            with self.subTest(complete=complete), self.assertRaisesRegex(RuntimeError, 'dependent rebuilds'):
                planner['validate_manifest'](value, complete)
        self.assertEqual(planner['validate_manifest'](value, allow_pending=True), value)
        for pending in [[], {'unknown': {'libfixture.so.1': 'fixture'}}, {'consumer': {'libfixture.so.1': 'missing'}}]:
            with self.subTest(pending=pending), self.assertRaisesRegex(RuntimeError, 'Invalid pending'):
                planner['validate_manifest'](dict(value, pending_rebuilds=pending), allow_pending=True)

    def test_epoch_is_published_generation(self):
        previous = manifest({'fixture': library(epoch=7)})
        current = manifest({'fixture': library(version='1.1')})
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / 'abi/libfixture.so.1.xml'
            path.parent.mkdir()
            path.write_text('<abi-corpus/>')
            with patch.dict(namespace, {'dump_abi': Mock(), 'abi_difference': Mock(return_value='compatible')}):
                updated, plan = planner['compare'](previous, current, temporary, temporary)
        self.assertEqual(updated['libraries']['fixture']['abi_epoch'], 7)
        self.assertEqual(updated['libraries']['fixture']['abi_capability'], 'php-zts-lib-fixture-abi-7')
        self.assertEqual(plan['libraries']['fixture']['status'], 'compatible')

    def test_incompatible_changes_rotate_epoch(self):
        previous = manifest({'fixture': library(epoch=4)})
        current = manifest()
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / 'abi/libfixture.so.1.xml'
            path.parent.mkdir()
            path.write_text('<abi-corpus/>')
            with patch.dict(namespace, {'dump_abi': Mock(), 'abi_difference': Mock(return_value='breaking')}):
                updated, _ = planner['compare'](previous, current, temporary, temporary)
        self.assertEqual(updated['libraries']['fixture']['abi_epoch'], 5)
        self.assertEqual(updated['libraries']['fixture']['abi_capabilities'], ['php-zts-lib-fixture-abi-5'])

    def test_removed_soname_is_breaking(self):
        previous = manifest()
        previous['libraries']['fixture']['abi_empty'] = ['libfixture.so.1']
        with patch.dict(namespace, {'dump_abi': Mock(), 'abi_difference': Mock()}):
            updated, result = planner['compare'](previous, manifest({'fixture': library(soname='libfixture.so.2')}), '.', '.')
        self.assertEqual(updated['libraries']['fixture']['abi_epoch'], 2)
        self.assertEqual(result['libraries']['fixture']['status'], 'breaking')

    def test_new_soname_is_compatible(self):
        old = manifest()
        new = copy.deepcopy(old)
        new['libraries']['fixture']['elf']['lib/libextra.so.1'] = {'soname': 'libextra.so.1'}
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / 'abi/libfixture.so.1.xml'
            path.parent.mkdir()
            path.write_text('<abi-corpus/>')
            with patch.dict(namespace, {'dump_abi': Mock(), 'abi_difference': Mock(return_value='compatible')}):
                updated, _ = planner['compare'](old, new, temporary, temporary)
        self.assertEqual(updated['libraries']['fixture']['abi_epoch'], 1)

    def test_trusted_empty_corpus_ignores_crt_representation_changes(self):
        previous = manifest()
        previous['libraries']['fixture']['abi_empty'] = ['libfixture.so.1']
        current = manifest()
        current['libraries']['fixture']['abi_empty'] = ['libfixture.so.1']
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / 'abi/libfixture.so.1.xml'
            path.parent.mkdir()
            path.write_text('<abi-corpus><elf-function-symbols><elf-symbol name="_init" type="func-type"/></elf-function-symbols></abi-corpus>')
            difference = Mock(side_effect=RuntimeError('CRT symbol differences must not classify source ABI'))
            with patch.dict(namespace, {'dump_abi': Mock(), 'abi_difference': difference}):
                updated, result = planner['compare'](previous, current, temporary, temporary)
            difference.assert_not_called()
        self.assertEqual(updated['libraries']['fixture']['abi_epoch'], 1)
        self.assertEqual(result['libraries']['fixture']['status'], 'compatible')

    def test_missing_empty_annotation_uses_normal_abi_comparison(self):
        previous = manifest()
        current = manifest()
        current['libraries']['fixture']['abi_empty'] = ['libfixture.so.1']
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / 'abi/libfixture.so.1.xml'
            path.parent.mkdir()
            path.write_text('<abi-corpus/>')
            difference = Mock(return_value='breaking')
            with patch.dict(namespace, {'dump_abi': Mock(), 'abi_difference': difference}):
                updated, _ = planner['compare'](previous, current, temporary, temporary)
            difference.assert_called_once()
        self.assertEqual(updated['libraries']['fixture']['abi_epoch'], 2)

    def test_bootstrap_is_explicit(self):
        with self.assertRaisesRegex(RuntimeError, '--bootstrap'):
            planner['compare'](planner['empty_manifest'](), manifest(), '.', '.')
        with patch.dict(namespace, {'dump_abi': Mock()}):
            updated, result = planner['compare'](planner['empty_manifest'](), manifest(), '.', '.', True)
        self.assertTrue(result['bootstrap'])
        self.assertEqual(updated['libraries']['fixture']['abi_epoch'], 1)

    def test_missing_debug_baseline_blocks_release(self):
        old = manifest()
        old['libraries']['fixture'].pop('abi_dump')
        with patch.dict(namespace, {'dump_abi': Mock()}), self.assertRaisesRegex(RuntimeError, 'no debug ABI'):
            planner['compare'](old, manifest(), '.', '.')

    def test_foreign_platform_baseline_is_rejected(self):
        old = manifest()
        old['libc'] = 'musl'
        with self.assertRaisesRegex(RuntimeError, 'libc'):
            planner['compare'](old, manifest(), '.', '.')

    def test_ambiguous_and_failed_comparisons_block(self):
        for status in [1, 3, 4, 5, 8, 16, -1]:
            with self.subTest(status=status), patch.dict(namespace, {'run': Mock(return_value=Mock(returncode=status, stderr='error', stdout='changed'))}), self.assertRaises(RuntimeError):
                planner['abi_difference']('old.xml', 'new.xml')

    def test_additions_require_filtered_comparison_to_be_equal(self):
        command = Mock(side_effect=[Mock(returncode=4, stdout='added', stderr=''), Mock(returncode=0)])
        with patch.dict(namespace, {'run': command}):
            self.assertEqual(planner['abi_difference']('old.xml', 'new.xml'), 'compatible')
        self.assertIn('--no-added-syms', command.call_args_list[1][0][0])

    def test_breaking_abidiff_status(self):
        with patch.dict(namespace, {'run': Mock(return_value=Mock(returncode=12))}):
            self.assertEqual(planner['abi_difference']('old.xml', 'new.xml'), 'breaking')

    def test_abi_dump_requires_debug_info(self):
        with TemporaryDirectory() as temporary, patch.dict(namespace, {'run': Mock(return_value=Mock(returncode=1, stderr='no debug'))}) as mocked:
            with self.assertRaisesRegex(RuntimeError, 'debug ABI'):
                planner['dump_abi'](temporary, 'fixture', library())
            self.assertIn('--debug-info-dir', namespace['run'].call_args[0][0])

    def test_reverse_dependencies_include_transitive_and_dlopen_edges(self):
        value = manifest({'fixture': library(), 'middle': library(depends=['fixture']), 'consumer': library(depends=['middle']), 'other': library()})
        value['libraries']['other']['dlopen_dependencies'] = ['consumer']
        self.assertEqual(planner['reverse_closure'](value, ['fixture']), ['consumer', 'fixture', 'middle', 'other'])

    def test_source_changes_ignore_abi_bookkeeping(self):
        old = manifest()
        new = copy.deepcopy(old)
        new['libraries']['fixture']['abi_epoch'] = 2
        new['libraries']['fixture']['package_versions'] = {'deb': '1.0-7'}
        self.assertEqual(planner['source_changes'](old, new), {})
        new['libraries']['fixture']['source_hash'] = 'new-source'
        self.assertEqual(planner['source_changes'](old, new), {'fixture': 'changed'})

    def test_runtime_asset_content_changes_select_the_library(self):
        old = manifest()
        new = copy.deepcopy(old)
        old['libraries']['fixture']['file_hashes'] = {'etc/fixture.conf': 'old-configuration'}
        new['libraries']['fixture']['file_hashes'] = {'etc/fixture.conf': 'new-configuration'}
        self.assertEqual(planner['source_changes'](old, new), {'fixture': 'changed'})

    def test_fingerprint_covers_abi_and_ignores_generation_time(self):
        first = manifest()
        second = copy.deepcopy(first)
        second['generated_at'] = 'tomorrow'
        self.assertEqual(planner['fingerprint'](first), planner['fingerprint'](second))
        second['libraries']['fixture']['abi_epoch'] = 2
        self.assertNotEqual(planner['fingerprint'](first), planner['fingerprint'](second))

    def test_invalid_manifests_fail_closed(self):
        cases = []
        value = manifest()
        value['libraries']['fixture']['files'] = ['../escape']
        cases.append(value)
        value = manifest()
        value['libraries']['fixture']['depends'] = ['missing']
        cases.append(value)
        cases.append(manifest({'fixture': library(), 'duplicate': library()}))
        value = manifest()
        value['libraries']['fixture']['abi_epoch'] = False
        cases.append(value)
        for value in cases:
            with self.subTest(value=value), self.assertRaises(RuntimeError):
                planner['validate_manifest'](value)

    def test_symlink_cannot_escape_baseline(self):
        with TemporaryDirectory() as temporary:
            (Path(temporary) / 'escape').symlink_to('/etc')
            with self.assertRaisesRegex(RuntimeError, 'escapes'):
                planner['rooted'](temporary, 'escape/passwd')

    def test_metadata_tar_rejects_traversal_and_ignores_unrelated_files(self):
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode='w') as archive:
            for name in ['usr/lib/unrelated.so', 'usr/share/php-zts/libraries/fixture.json']:
                entry = tarfile.TarInfo(name)
                entry.size = 2
                archive.addfile(entry, io.BytesIO(b'{}'))
        stream.seek(0)
        self.assertEqual(list(planner['metadata_entries'](stream)), [('usr/share/php-zts/libraries/fixture.json', b'{}')])

    def test_prepare_respects_manual_php_filter_and_keeps_library_only_incremental(self):
        with TemporaryDirectory() as temporary:
            temporary = Path(temporary)
            current = temporary / 'current.json'
            baseline = temporary / 'baseline'
            baseline.mkdir()
            (baseline / 'abi').mkdir()
            (baseline / 'abi/libfixture.so.1.xml').write_text('<abi-corpus/>')
            current.write_text(json.dumps(manifest()))
            published = manifest()
            published['libraries']['fixture']['published_version'] = '1.0-7'
            (baseline / 'shared-libraries.json').write_text(json.dumps(published))
            args = argparse.Namespace(manifest=str(current), baseline=str(baseline), request=None, bootstrap=False,
                                      mode='full', root=str(temporary), libraries=None, php_versions='8.4', packages='curl',
                                      type='deb', manifest_output=str(temporary / 'annotated.json'), output=str(temporary / 'plan.json'))
            result = {'schema': 1, 'baseline_fingerprint': 'old', 'candidate_fingerprint': 'new', 'bootstrap': False,
                      'libraries': {'fixture': {'status': 'compatible'}}}
            with patch.dict(namespace, {'compare': Mock(return_value=(manifest(), result))}):
                planner['prepare'](args)
                plan = json.loads(Path(args.output).read_text())
                self.assertEqual(plan['php_requests']['8.4'], {'full': False, 'packages': ['curl'], 'compile': True,
                                                             'reuse_payload': False, 'compile_full': False, 'compile_packages': ['curl']})
                self.assertEqual(plan['php_requests']['8.5'], {'full': False, 'packages': [], 'compile': False,
                                                             'reuse_payload': False, 'compile_full': False, 'compile_packages': []})
                self.assertEqual(json.loads(Path(args.manifest_output).read_text())['libraries']['fixture']['package_versions']['deb'], '1.0-7')
                args.mode = 'incremental'
                args.packages = None
                planner['prepare'](args)
                plan = json.loads(Path(args.output).read_text())
                self.assertEqual(plan['libraries'], [])
                self.assertFalse(any(item['full'] or item['packages'] for item in plan['php_requests'].values()))

    def test_cohort_rejects_changed_publication_baseline(self):
        with TemporaryDirectory() as temporary:
            temporary = Path(temporary)
            plan = temporary / 'plan.json'
            baseline = temporary / 'baseline.json'
            plan.write_text(json.dumps({'schema': 1, 'baseline_fingerprint': 'outdated'}))
            baseline.write_text(json.dumps(manifest()))
            args = argparse.Namespace(plan=str(plan), baseline_manifest=str(baseline))
            with self.assertRaisesRegex(RuntimeError, 'changed during'):
                planner['cohort'](args)

    def manual_plan(self, names=('fixture', 'other'), selected='fixture', rebuilt='', changed=(), breaking=(), packages=None, dependencies=None):
        with TemporaryDirectory(prefix='shared-library-manual-') as directory:
            temporary = Path(directory)
            baseline = temporary / 'baseline'
            (baseline / 'abi').mkdir(parents=True)
            old = manifest({name: library(soname='lib' + name + '.so.1') for name in names})
            if dependencies is not None:
                old['consumers'] = {stream: sorted(dependencies.get(stream, {})) for stream in planner['STREAMS']}
                old['consumer_dependencies'] = dependencies
                old['consumer_versions'] = {stream: {name: '1.0-1' for name in names} for stream, names in old['consumers'].items()}
            for name, entry in old['libraries'].items():
                entry['published_version'] = '1.0-1'
                (baseline / entry['abi_dump']['lib' + name + '.so.1']).write_text('<abi-corpus/>')
            current = copy.deepcopy(old)
            for name in changed:
                current['libraries'][name]['source_hash'] = 'new-source'
            if 'consumer' in current['libraries']:
                current['libraries']['consumer']['depends'] = ['fixture']
                old['libraries']['consumer']['depends'] = ['fixture']
            (baseline / 'shared-libraries.json').write_text(json.dumps(old))
            (temporary / 'current.json').write_text(json.dumps(current))
            request = {'requests': {stream: {'full': True, 'packages': []} for stream in planner['STREAMS']},
                       'libraries': ['other'], 'library_build_inputs': ['unrelated-tool']}
            (temporary / 'request.json').write_text(json.dumps(request))
            args = argparse.Namespace(manifest=str(temporary / 'current.json'), baseline=str(baseline),
                                      request=str(temporary / 'request.json'), bootstrap=True, mode='full',
                                      root=str(temporary), libraries=selected, rebuilt_libraries=rebuilt,
                                      php_versions='8.4', packages=packages, type='deb',
                                      manifest_output=str(temporary / 'annotated.json'), output=str(temporary / 'plan.json'))
            comparison = {'schema': 1, 'bootstrap': False,
                          'libraries': {name: {'status': 'breaking' if name in breaking else 'compatible'} for name in names}}
            with patch.dict(namespace, {'compare': Mock(return_value=(current, comparison))}):
                planner['prepare'](args)
            return json.loads(Path(args.output).read_text())

    def test_manual_library_bump_ignores_unrelated_snapshot_and_full_default(self):
        plan = self.manual_plan()
        self.assertEqual(plan['libraries'], ['fixture'])
        self.assertFalse(any(value['full'] or value['packages'] for value in plan['php_requests'].values()))

    def test_manual_library_set_includes_only_selected_and_actually_rebuilt_libraries(self):
        plan = self.manual_plan(names=('fixture', 'other', 'consumer', 'headerconsumer'),
                                selected='fixture,other', rebuilt='fixture,other,headerconsumer')
        self.assertEqual(plan['libraries'], ['fixture', 'headerconsumer', 'other'])
        self.assertFalse(plan['abi_break'])

    def test_manual_abi_break_still_rebuilds_all_affected_php_streams(self):
        plan = self.manual_plan(names=('fixture', 'consumer', 'other'), breaking=('fixture',))
        self.assertTrue(plan['abi_break'])
        self.assertEqual(plan['abi_rebuild_roots'], ['fixture'])
        self.assertEqual(plan['libraries'], ['consumer', 'fixture'])
        self.assertTrue(all(value == {'full': True, 'packages': [], 'compile': True, 'reuse_payload': False,
                                     'compile_full': True, 'compile_packages': []} for value in plan['php_requests'].values()))

    def test_abi_break_rebuilds_only_published_consumers_of_affected_libraries(self):
        dependencies = {'8.4': {'php-zts-cli': ['other'], 'php-zts-imagick': ['fixture'], 'php-zts-redis': []},
                        '8.5': {'php-zts-cli': ['other'], 'php-zts-curl': ['consumer']}}
        plan = self.manual_plan(names=('fixture', 'consumer', 'other'), breaking=('fixture',), dependencies=dependencies)
        self.assertEqual(plan['php_requests']['8.4'], {'full': False, 'packages': ['imagick'], 'compile': True,
                         'reuse_payload': False, 'compile_full': False, 'compile_packages': ['imagick']})
        self.assertEqual(plan['php_requests']['8.5']['compile_packages'], ['curl'])
        self.assertFalse(plan['php_requests']['8.6']['compile'])
        self.assertEqual(plan['expected_consumers']['8.4'], ['php-zts-imagick'])
        self.assertEqual(plan['published_consumers']['8.4'], {'php-zts-imagick': '1.0-1'})

    def test_abi_break_in_core_library_requires_core_build_but_only_affected_packages(self):
        dependencies = {'8.4': {'php-zts-cli': ['fixture'], 'php-zts-fpm': ['fixture'], 'frankenphp': ['fixture'], 'php-zts-imagick': ['other']}}
        plan = self.manual_plan(breaking=('fixture',), dependencies=dependencies)
        request = plan['php_requests']['8.4']
        self.assertTrue(request['compile_full'])
        self.assertFalse(request['full'])
        self.assertEqual(request['packages'], ['cli', 'fpm', 'frankenphp'])
        self.assertEqual(request['compile_packages'], [])
        self.assertFalse(plan['php_requests']['8.5']['compile'])

    def test_missing_consumer_dependency_metadata_requires_one_full_stream_build(self):
        value = manifest()
        value['consumers'] = {'8.4': ['php-zts-cli', 'php-zts-imagick'], '8.5': ['php-zts-cli']}
        value['consumer_dependencies'] = {'8.4': {'php-zts-cli': []}, '8.5': {'php-zts-cli': []}}
        requests = planner['abi_consumers'](value, ['fixture'])
        self.assertTrue(requests['8.4']['full'])
        self.assertEqual(requests['8.4']['missing_metadata'], ['php-zts-imagick'])
        self.assertFalse(requests['8.5']['full'])
        self.assertEqual(requests['8.5']['packages'], [])

    def test_manual_bump_rejects_unpublished_unselected_seed_changes(self):
        with self.assertRaisesRegex(RuntimeError, 'outside the requested set: other'):
            self.manual_plan(changed=('other',))

    def test_manual_bump_rejects_unknown_rebuilt_libraries(self):
        with self.assertRaisesRegex(RuntimeError, 'Unknown selected libraries: missing'):
            self.manual_plan(rebuilt='missing')

    def test_manual_library_and_php_package_requests_are_not_silently_combined(self):
        with self.assertRaisesRegex(RuntimeError, 'Select libraries or PHP packages'):
            self.manual_plan(packages='curl')

    def test_peer_baselines_reconcile_an_interrupted_publication(self):
        first = manifest({'fixture': library(version='1.1', epoch=2)})
        second = manifest({'fixture': library(version='1.0', epoch=1)})
        first['libraries']['fixture']['published_version'] = '1.1-1'
        second['libraries']['fixture']['published_version'] = '1.0-1'
        first['consumers'] = {'8.4': ['php-zts-cli', 'php-zts-curl']}
        second['consumers'] = {'8.5': ['php-zts-cli']}
        with TemporaryDirectory() as temporary:
            temporary = Path(temporary)
            for host in ['first', 'second']:
                (temporary / host / 'abi').mkdir(parents=True)
                (temporary / host / 'abi/libfixture.so.1.xml').write_text(host)
            with patch.dict(namespace, {'native_compare': lambda kind, a, b: (a > b) - (a < b)}):
                merged, divergent = planner['merge_baselines']([(first, temporary / 'first'), (second, temporary / 'second')], 'deb', temporary / 'merged')
            self.assertEqual(merged['libraries']['fixture']['abi_epoch'], 2)
            self.assertEqual(divergent, {'fixture'})
            self.assertEqual((temporary / 'merged/abi/libfixture.so.1.xml').read_text(), 'first')
            self.assertEqual(merged['consumers']['8.4'], ['php-zts-cli', 'php-zts-curl'])
            self.assertEqual(merged['consumers']['8.5'], ['php-zts-cli'])

    def test_same_published_version_cannot_have_different_abi_generations(self):
        first = manifest({'fixture': library(epoch=2)})
        second = manifest({'fixture': library(epoch=1)})
        for value in [first, second]:
            value['libraries']['fixture']['published_version'] = '1.0-1'
        with TemporaryDirectory() as temporary, patch.dict(namespace, {'native_compare': Mock(return_value=0)}), self.assertRaisesRegex(RuntimeError, 'conflicting ABI'):
            planner['merge_baselines']([(first, temporary), (second, temporary)], 'deb', temporary)

    def test_cohort_accepts_either_known_peer_baseline(self):
        with TemporaryDirectory() as temporary:
            temporary = Path(temporary)
            plan = temporary / 'plan.json'
            baseline = temporary / 'baseline.json'
            baseline.write_text(json.dumps(manifest()))
            plan.write_text(json.dumps({'schema': 1, 'baseline_fingerprint': 'first',
                                        'baseline_fingerprints': ['first', planner['fingerprint'](manifest())]}))
            args = argparse.Namespace(plan=str(plan), baseline_manifest=str(baseline), artifacts=str(temporary), type='deb', output=str(temporary / 'result.json'))
            planner['cohort'](args)
            self.assertEqual(json.loads(Path(args.output).read_text())['files'], [])

    def test_published_consumer_versions_are_architecture_specific(self):
        packages = [{'name': 'php-zts-cli', 'version': '2'}, {'name': 'php-zts-cli', 'version': '1'},
                    {'name': 'php-zts-cli-debuginfo', 'version': '2'}]
        listing = Mock(return_value=packages)
        archives = Mock(side_effect=lambda host, owner, password, kind, name, version: [{'arch': 'arm64' if version == '2' else 'amd64'}])
        helper = {'get_remote_packages': listing, 'remote_package_archives': archives}
        with patch.dict(namespace, {'native_compare': lambda kind, a, b: (a > b) - (a < b)}):
            self.assertEqual(planner['published_consumers'](helper, 'https://example.com', '84', 'consumer-secret', 'deb', 'amd64'), {'php-zts-cli': '1'})
        listing.assert_called_once_with('https://example.com', '84', 'consumer-secret', 'debian')

    def test_published_consumer_dependency_metadata_is_bound_to_archive_identity(self):
        value = {'schema': 1, 'name': 'php-zts-imagick', 'php_stream': '8.4',
                 'architecture': 'x86_64', 'libraries': ['fixture']}
        archives = [{'arch': 'amd64', 'consumer_metadata': value}]
        helper = {'get_remote_packages': Mock(return_value=[{'name': 'php-zts-imagick', 'version': '1'}]),
                  'remote_package_archives': Mock(return_value=archives)}
        dependencies = {}
        self.assertEqual(planner['published_consumers'](helper, 'https://example.com', '84', '', 'deb', 'amd64',
                                                        dependencies=dependencies), {'php-zts-imagick': '1'})
        self.assertEqual(dependencies, {'8.4': {'php-zts-imagick': ['fixture']}})
        for field, invalid in [('name', 'php-zts-cli'), ('php_stream', '8.5'), ('architecture', 'aarch64'), ('libraries', ['fixture', 'fixture'])]:
            with self.subTest(field=field), self.assertRaisesRegex(RuntimeError, 'does not match its package'):
                planner['consumer_dependencies'](dict(value, **{field: invalid}), 'php-zts-imagick', '8.4', 'amd64')

    def test_debian_hyphenated_package_names_resolve_to_actual_extension_components(self):
        value = manifest()
        value['consumers'] = {'8.4': ['php-zts-pdo-pgsql']}
        value['consumer_dependencies'] = {'8.4': {'php-zts-pdo-pgsql': ['fixture']}}
        self.assertEqual(planner['abi_consumers'](value, ['fixture'])['8.4']['packages'], ['pdo_pgsql'])

    def test_partial_published_library_inventory_remains_readable_for_recovery(self):
        partial = manifest({'fixture': library(depends=['not-uploaded-yet'])})
        with self.assertRaisesRegex(RuntimeError, 'Missing private dependency'):
            planner['validate_manifest'](partial)
        self.assertEqual(planner['validate_manifest'](partial, complete=False), partial)

    def test_library_cohort_checks_native_floor_provides_and_abi_payload(self):
        with TemporaryDirectory() as temporary:
            temporary = Path(temporary)
            artifacts = temporary / 'artifacts'
            artifacts.mkdir()
            abi_path = 'usr/share/php-zts/libraries/abi/fixture/libfixture.so.1.xml'
            abi = b'<abi-corpus><abi-instr><type-decl/></abi-instr></abi-corpus>'
            entry = library()
            entry.update({'abi_capability': 'php-zts-lib-fixture-abi-1', 'abi_dump': {'libfixture.so.1': abi_path},
                          'abi_dump_hashes': {'libfixture.so.1': hashlib.sha256(abi).hexdigest()}, 'package_versions': {'apk': '1.0-r2'}})
            expected = {key: entry[key] for key in ['version', 'source_hash', 'abi_epoch', 'abi_capability', 'abi_dump', 'abi_dump_hashes']}
            plan = {'schema': 1, 'baseline_fingerprint': 'old', 'expected_members': ['php-zts-lib-fixture'],
                    'expected_libraries': {'fixture': expected}, 'published_versions': {'fixture': '1.0-r1'}}
            (temporary / 'plan.json').write_text(json.dumps(plan))
            args = argparse.Namespace(plan=str(temporary / 'plan.json'), baseline_manifest=None, artifacts=str(artifacts), type='apk', output=str(temporary / 'result.json'))

            def archive(provides, floor):
                value = copy.deepcopy(entry)
                value['package_versions']['apk'] = floor
                metadata = 'pkgname = php-zts-lib-fixture\npkgver = 1.0-r2\narch = x86_64\n' + ('provides = ' + provides + '\n' if provides else '')
                with gzip.open(artifacts / 'fixture.apk', 'wb') as compressed, tarfile.open(fileobj=compressed, mode='w|') as output:
                    for name, data in [('.PKGINFO', metadata.encode()), ('usr/share/php-zts/libraries/fixture.json', json.dumps(value).encode()), (abi_path, abi)]:
                        member = tarfile.TarInfo(name)
                        member.size = len(data)
                        output.addfile(member, io.BytesIO(data))

            with patch.dict(namespace, {'native_compare': lambda kind, a, b: (a > b) - (a < b)}):
                archive(entry['abi_capability'], '1.0-r2')
                planner['cohort'](args)
                self.assertEqual(len(json.loads(Path(args.output).read_text())['files']), 1)
                archive('', '1.0-r2')
                with self.assertRaisesRegex(RuntimeError, 'provide its private ABI'):
                    planner['cohort'](args)
                archive(entry['abi_capability'], '1.0-r1')
                with self.assertRaisesRegex(RuntimeError, 'package-version floor'):
                    planner['cohort'](args)

    def test_only_reserved_crt_notype_or_function_exports_are_empty(self):
        for kind, name, expected in [('NOTYPE', '_init', []), ('FUNC', '_fini', []), ('OBJECT', '_init', ['_init']),
                                     ('TLS', '_fini', ['_fini']), ('FUNC', 'public_api', ['public_api']),
                                     ('NOTYPE', 'public_untyped', ['public_untyped'])]:
            output = "Symbol table '.dynsym' contains 1 entry:\n  1: 0000000000001000 0 {} GLOBAL DEFAULT 6 {}\n".format(kind, name)
            with self.subTest(kind=kind, name=name), patch.dict(namespace, {'run': Mock(return_value=Mock(returncode=0, stdout=output))}):
                self.assertEqual(planner['defined_public_symbols']('fixture.so'), expected)


@unittest.skipUnless(all(shutil.which(tool) for tool in ['gcc', 'abidw', 'abidiff']), 'GCC and libabigail required')
class NativeAbiComparison(unittest.TestCase):
    def build(self, root, source, debug=True, extra_flags=()):
        (root / 'lib').mkdir(parents=True)
        (root / 'source.c').write_text(source)
        command = ['gcc', '-shared', '-fPIC', '-Wl,-soname,libfixture.so.1']
        if debug:
            command.append('-g')
        command.extend(extra_flags)
        subprocess.check_call(command + ['-o', str(root / 'lib/libfixture.so.1'), str(root / 'source.c')])

    def test_native_compatible_addition_preserves_generation(self):
        with TemporaryDirectory(prefix='shared-library-abi-') as temporary:
            old_root, new_root = Path(temporary) / 'old', Path(temporary) / 'new'
            self.build(old_root, 'int fixture(void) { return 1; }')
            self.build(new_root, 'int fixture(void) { return 2; } int added(void) { return 3; }')
            old, new = manifest(), manifest({'fixture': library(version='1.1')})
            planner['dump_abi'](old_root, 'fixture', old['libraries']['fixture'])
            updated, plan = planner['compare'](old, new, old_root, new_root)
            self.assertEqual(updated['libraries']['fixture']['abi_epoch'], 1)
            self.assertEqual(plan['libraries']['fixture']['status'], 'compatible')

    def test_native_removed_symbol_rotates_generation(self):
        with TemporaryDirectory(prefix='shared-library-abi-') as temporary:
            old_root, new_root = Path(temporary) / 'old', Path(temporary) / 'new'
            self.build(old_root, 'int fixture(void) { return 1; }')
            self.build(new_root, 'int replacement(void) { return 3; }')
            old, new = manifest(), manifest({'fixture': library(version='2.0')})
            planner['dump_abi'](old_root, 'fixture', old['libraries']['fixture'])
            updated, plan = planner['compare'](old, new, old_root, new_root)
            self.assertEqual(updated['libraries']['fixture']['abi_epoch'], 2)
            self.assertEqual(plan['libraries']['fixture']['status'], 'breaking')

    def test_missing_debug_information_blocks_dump(self):
        with TemporaryDirectory(prefix='shared-library-abi-') as temporary:
            temporary = Path(temporary)
            self.build(temporary, 'int fixture(void) { return 1; }', debug=False)
            with self.assertRaisesRegex(RuntimeError, 'type information'):
                planner['dump_abi'](temporary, 'fixture', library())

    def test_data_only_c_library_retains_type_information(self):
        with TemporaryDirectory(prefix='shared-library-data-abi-') as temporary:
            temporary = Path(temporary)
            self.build(temporary, 'const struct { double alignment; unsigned char bytes[16]; } fixture_data = { 0, { 1, 2, 3 } };')
            entry = library()
            planner['dump_abi'](temporary, 'fixture', entry)
            self.assertIn('libfixture.so.1', entry['abi_dump_hashes'])

    def test_empty_compatibility_stub_has_an_empty_public_abi(self):
        with TemporaryDirectory(prefix='shared-library-empty-abi-') as temporary:
            temporary = Path(temporary)
            self.build(temporary, '/* All interfaces use the C++ standard library. */')
            entry = library()
            planner['dump_abi'](temporary, 'fixture', entry)
            self.assertEqual(entry['abi_empty'], ['libfixture.so.1'])

    def test_empty_stub_without_crt_has_an_empty_public_abi(self):
        with TemporaryDirectory(prefix='shared-library-no-crt-abi-') as temporary:
            temporary = Path(temporary)
            self.build(temporary, '/* Empty stub without startup objects. */', extra_flags=['-nostartfiles'])
            entry = library()
            planner['dump_abi'](temporary, 'fixture', entry)
            self.assertEqual(entry['abi_empty'], ['libfixture.so.1'])

    @unittest.skipUnless(shutil.which('strip'), 'strip required')
    def test_stripped_real_api_is_rejected(self):
        with TemporaryDirectory(prefix='shared-library-stripped-abi-') as temporary:
            temporary = Path(temporary)
            self.build(temporary, 'int fixture(void) { return 1; }')
            subprocess.check_call(['strip', '--strip-debug', str(temporary / 'lib/libfixture.so.1')])
            with self.assertRaisesRegex(RuntimeError, 'type information'):
                planner['dump_abi'](temporary, 'fixture', library())

    def test_non_crt_notype_export_is_rejected(self):
        with TemporaryDirectory(prefix='shared-library-untyped-abi-') as temporary:
            temporary = Path(temporary)
            self.build(temporary, '__asm__(".globl public_untyped\\npublic_untyped:\\n.byte 0");', extra_flags=['-nostartfiles'])
            with self.assertRaisesRegex(RuntimeError, 'type information'):
                planner['dump_abi'](temporary, 'fixture', library())

    def test_empty_to_real_api_is_compatible_and_reverse_is_breaking(self):
        with TemporaryDirectory(prefix='shared-library-empty-transition-') as temporary:
            old_root, new_root = Path(temporary) / 'old', Path(temporary) / 'new'
            self.build(old_root, '/* Empty compatibility stub. */')
            self.build(new_root, 'int fixture(void) { return 1; }')
            old, new = manifest(), manifest({'fixture': library(version='1.1')})
            planner['dump_abi'](old_root, 'fixture', old['libraries']['fixture'])
            updated, result = planner['compare'](old, new, old_root, new_root)
            self.assertEqual(result['libraries']['fixture']['status'], 'compatible')
            self.assertEqual(updated['libraries']['fixture']['abi_epoch'], 1)
            restored, result = planner['compare'](updated, old, new_root, old_root)
            self.assertEqual(result['libraries']['fixture']['status'], 'breaking')
            self.assertEqual(restored['libraries']['fixture']['abi_epoch'], 2)

    def test_empty_crt_startup_changes_keep_the_generation(self):
        with TemporaryDirectory(prefix='shared-library-empty-crt-') as temporary:
            old_root, new_root = Path(temporary) / 'old', Path(temporary) / 'new'
            self.build(old_root, '/* Empty compatibility stub with native CRT. */')
            self.build(new_root, '/* Empty compatibility stub without CRT. */', extra_flags=['-nostartfiles'])
            old, new = manifest(), manifest({'fixture': library(version='1.1')})
            planner['dump_abi'](old_root, 'fixture', old['libraries']['fixture'])
            updated, result = planner['compare'](old, new, old_root, new_root)
            self.assertEqual(result['libraries']['fixture']['status'], 'compatible')
            self.assertEqual(updated['libraries']['fixture']['abi_epoch'], 1)
            restored, result = planner['compare'](updated, old, new_root, old_root)
            self.assertEqual(result['libraries']['fixture']['status'], 'compatible')
            self.assertEqual(restored['libraries']['fixture']['abi_epoch'], 1)


if __name__ == '__main__':
    unittest.main()
