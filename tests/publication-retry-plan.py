import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import runpy
import shutil
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

import requests


ROOT = Path(__file__).resolve().parents[1]
planner = runpy.run_path(str(ROOT / 'bin/shared-library-plan'))
namespace = planner['prepare'].__globals__
ABI = b'<abi-corpus architecture="elf-amd-x86_64"/>'
SONAME = 'libfixture.so.1'
ABI_PATH = 'abi/' + SONAME + '.xml'


def library(release=None, archive=None):
    value = {'version': '1.0', 'source_hash': 'unchanged-source', 'abi_epoch': 1,
             'abi_capability': 'php-zts-lib-fixture-abi-1',
             'files': ['lib/' + SONAME],
             'elf': {'lib/' + SONAME: {'soname': SONAME, 'needed': []}},
             'depends': [], 'abi_dump': {SONAME: ABI_PATH},
             'abi_dump_hashes': {SONAME: hashlib.sha256(ABI).hexdigest()},
             'abi_empty': []}
    if release is not None:
        value['published_version'] = '0:1.0-' + str(release)
    if archive is not None:
        value.update(published_archive='archives/fixture-' + str(release) + '.rpm',
                     published_archive_filename='fixture-' + str(release) + '.rpm',
                     published_archive_sha256=hashlib.sha256(archive).hexdigest())
    return value


def manifest(value=None):
    return {'schema': 1, 'linkage': 'shared', 'runtime_libdir': '/usr/lib/php-zts/lib',
            'architecture': 'x86_64', 'libc': 'glibc',
            'libraries': {'fixture': value if value is not None else library()}}


@unittest.skipUnless(shutil.which('rpmdev-vercmp'), 'native RPM comparator required')
class PublicationRetryPlan(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory(prefix='publication-retry-plan-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def snapshot(self, name, value, archives=None):
        directory = self.root / name
        directory.mkdir()
        (directory / 'shared-libraries.json').write_text(json.dumps(value))
        if value.get('libraries'):
            path = directory / ABI_PATH
            path.parent.mkdir(parents=True)
            path.write_bytes(ABI)
        for relative, contents in (archives or {}).items():
            path = directory / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(contents)
        return directory

    def merge(self, values):
        destination = self.root / 'merged'
        destination.mkdir()
        merged, divergent = planner['merge_baselines'](values, 'rpm', destination)
        return merged, divergent, destination

    def prepare(self, previous=None, peer=None, request=None, libraries=None, current_value=None):
        current = self.snapshot('payload', manifest(current_value))
        baseline = previous or self.snapshot('baseline', manifest(library(1)))
        requested = self.root / 'request.json'
        requested.write_text(json.dumps(request or {'requests': {}}))
        args = argparse.Namespace(type='rpm', manifest=str(current / 'shared-libraries.json'),
                                  root=str(current), baseline=str(baseline),
                                  peer_baseline=str(peer) if peer else None,
                                  mode='incremental', request=str(requested), libraries=libraries,
                                  rebuilt_libraries=None, php_versions=None, packages=None,
                                  manifest_output=str(self.root / 'annotated.json'),
                                  output=str(self.root / 'plan.json'))
        with patch.dict(namespace, {'dump_abi': Mock(),
                                    'abi_difference': Mock(return_value='compatible')}):
            planner['prepare'](args)
        return json.loads(Path(args.output).read_text())

    def test_automatic_metadata_requests_only_select_the_target_sdk(self):
        plan = self.prepare(request={'requests': {}, 'library_package_requests': ['fixture', 'windows-only'],
                                     'variant_package_requests': {'rpm': ['unused-library']}})
        self.assertEqual(plan['libraries'], ['fixture'])
        self.assertEqual(plan['library_package_requests'], ['fixture'])
        self.assertFalse(any(request['compile'] for request in plan['php_requests'].values()))

    def test_explicit_unknown_library_selection_remains_fatal(self):
        with self.assertRaisesRegex(RuntimeError, 'Unknown selected libraries'):
            self.prepare(libraries='windows-only')

    def test_release_only_owner_drift_reuses_archive_without_php_builds(self):
        older = b'old signed archive'
        newer = b'new signed archive with identical ELF payload'
        first = self.snapshot('owner82', manifest(library(1, older)),
                              {'archives/fixture-1.rpm': older})
        second = self.snapshot('owner83', manifest(library(2, newer)),
                               {'archives/fixture-2.rpm': newer})
        plan = self.prepare(first, second)
        self.assertEqual(plan['libraries'], ['fixture'])
        self.assertFalse(plan['abi_break'])
        self.assertEqual(plan['abi_rebuild_roots'], [])
        self.assertTrue(all(not request['compile'] and not request['full']
                            for request in plan['php_requests'].values()))
        retry = plan['publication_retries']['fixture']
        self.assertEqual(retry['published_version'], '0:1.0-2')
        self.assertEqual(retry['published_archive_sha256'], hashlib.sha256(newer).hexdigest())

    def abi_rollout(self, changed_code=False):
        old, new = b'old ABI archive', b'already validated new ABI archive'
        first = self.snapshot('owner82', manifest(library(1, old)), {'archives/fixture-1.rpm': old})
        published = library(2, new)
        published.update(abi_epoch=2, abi_capability='php-zts-lib-fixture-abi-2')
        second = self.snapshot('owner83', manifest(published), {'archives/fixture-2.rpm': new})
        current = library()
        current.update(abi_epoch=2, abi_capability='php-zts-lib-fixture-abi-2')
        if changed_code:
            current['elf']['lib/' + SONAME]['sha256'] = 'changed-runtime-code'
        return self.prepare(first, second, libraries='fixture', current_value=current)

    def test_partial_abi_rollout_reuses_the_exact_validated_provider_archive(self):
        with patch.dict(os.environ, BUMP='false', ITERATION=''):
            plan = self.abi_rollout()
        self.assertTrue(plan['abi_break'])
        self.assertEqual(plan['abi_rebuild_roots'], ['fixture'])
        self.assertEqual(plan['expected_libraries']['fixture']['abi_epoch'], 2)
        self.assertEqual(plan['publication_retries']['fixture']['published_version'], '0:1.0-2')
        self.assertEqual(plan['publication_retries']['fixture']['published_archive_sha256'],
                         hashlib.sha256(b'already validated new ABI archive').hexdigest())

    def test_changed_runtime_bytes_cannot_reuse_an_abi_rollout_archive(self):
        with patch.dict(os.environ, BUMP='false', ITERATION=''):
            self.assertEqual(self.abi_rollout(changed_code=True)['publication_retries'], {})

    def test_empty_runtime_asset_map_spellings_reuse_the_published_archive(self):
        old, new = b'old archive', b'validated archive'
        previous = library(1, old)
        previous['runtime_assets'] = []
        published = library(2, new)
        published['runtime_assets'] = []
        current = library()
        current['runtime_assets'] = {}
        first = self.snapshot('owner84', manifest(previous), {'archives/fixture-1.rpm': old})
        second = self.snapshot('owner85', manifest(published), {'archives/fixture-2.rpm': new})
        plan = self.prepare(first, second, libraries='fixture', current_value=current)
        self.assertEqual(plan['publication_retries']['fixture']['published_version'], '0:1.0-2')

    def test_changed_runtime_assets_cannot_reuse_a_published_archive(self):
        current = library()
        current['runtime_assets'] = {'modules/provider.so': '/usr/lib/php-zts/lib/modules/provider.so'}
        first = self.snapshot('owner84', manifest(library(1, b'old')), {'archives/fixture-1.rpm': b'old'})
        second = self.snapshot('owner85', manifest(library(2, b'new')), {'archives/fixture-2.rpm': b'new'})
        self.assertEqual(self.prepare(first, second, libraries='fixture', current_value=current)['publication_retries'], {})

    def test_explicit_bump_still_repackages_an_abi_rollout_provider(self):
        with patch.dict(os.environ, BUMP='true', ITERATION=''):
            self.assertEqual(self.abi_rollout()['publication_retries'], {})

    def test_explicit_iteration_still_repackages_an_abi_rollout_provider(self):
        with patch.dict(os.environ, BUMP='false', ITERATION='7'):
            self.assertEqual(self.abi_rollout()['publication_retries'], {})

    def test_missing_owner_needs_publication_retry_not_php_compilation(self):
        archive = b'reusable signed library archive'
        first = self.snapshot('owner82', manifest(library(1, archive)),
                              {'archives/fixture-1.rpm': archive})
        second = self.snapshot('owner83', planner['empty_manifest']())
        plan = self.prepare(first, second)
        self.assertFalse(plan['abi_break'])
        self.assertEqual(plan['abi_rebuild_roots'], [])
        self.assertIn('fixture', plan['publication_retries'])
        self.assertTrue(all(not request['compile'] for request in plan['php_requests'].values()))

    def test_metadata_only_package_request_reuses_sealed_payload(self):
        plan = self.prepare(request={'requests': {},
                                     'package_requests': {'8.6': {'packages': ['fpm', 'cli']}}})
        request = plan['php_requests']['8.6']
        self.assertEqual(request['packages'], ['cli', 'fpm'])
        self.assertFalse(request['compile'])
        self.assertTrue(request['reuse_payload'])
        self.assertFalse(request['compile_full'])
        self.assertEqual(request['compile_packages'], [])
        self.assertEqual(plan['libraries'], [])

    def test_library_metadata_selects_packages_without_php_compilation(self):
        plan = self.prepare(request={'requests': {}, 'library_package_requests': ['fixture']})
        self.assertEqual(plan['libraries'], ['fixture'])
        self.assertEqual(plan['library_package_requests'], ['fixture'])
        self.assertEqual(plan['publication_retries'], {})
        self.assertTrue(all(not request['compile'] and not request['full'] for request in plan['php_requests'].values()))

    def test_other_format_overlay_metadata_does_not_repackage_rpm(self):
        plan = self.prepare(request={'requests': {}, 'variant_package_requests': {'deb': ['fixture']}})
        self.assertEqual(plan['libraries'], [])
        self.assertEqual(plan['library_package_requests'], [])

    def test_metadata_change_cannot_reuse_an_outdated_published_archive(self):
        old, new = b'old archive', b'new archive'
        first = self.snapshot('owner82', manifest(library(1, old)), {'archives/fixture-1.rpm': old})
        second = self.snapshot('owner83', manifest(library(2, new)), {'archives/fixture-2.rpm': new})
        plan = self.prepare(first, second, {'requests': {}, 'library_package_requests': ['fixture']})
        self.assertEqual(plan['libraries'], ['fixture'])
        self.assertEqual(plan['publication_retries'], {})
        self.assertTrue(all(not request['compile'] for request in plan['php_requests'].values()))

    def test_full_metadata_request_does_not_become_full_compilation(self):
        plan = self.prepare(request={'requests': {}, 'package_requests': {'8.6': {'full': True}}})
        request = plan['php_requests']['8.6']
        self.assertTrue(request['full'])
        self.assertFalse(request['compile'])
        self.assertTrue(request['reuse_payload'])
        self.assertFalse(request['compile_full'])

    def test_compiled_packages_are_not_expanded_by_packaging_only_union(self):
        plan = self.prepare(request={'requests': {'8.6': {'packages': ['curl']}},
                                     'package_requests': {'8.6': {'packages': ['cli', 'fpm']}}})
        request = plan['php_requests']['8.6']
        self.assertEqual(request['packages'], ['cli', 'curl', 'fpm'])
        self.assertEqual(request['compile_packages'], ['curl'])
        self.assertTrue(request['compile'])
        self.assertFalse(request['compile_full'])
        self.assertFalse(request['reuse_payload'])

    def test_conflicting_same_evr_archive_bytes_are_rejected(self):
        first_bytes, second_bytes = b'first archive', b'rewritten archive'
        first = self.snapshot('owner82', manifest(library(1, first_bytes)),
                              {'archives/fixture-1.rpm': first_bytes})
        second = self.snapshot('owner83', manifest(library(1, second_bytes)),
                               {'archives/fixture-1.rpm': second_bytes})
        with self.assertRaisesRegex(RuntimeError, 'archive|same package version'):
            self.merge([(planner['manifest'](first / 'shared-libraries.json'), first),
                        (planner['manifest'](second / 'shared-libraries.json'), second)])

    def test_equal_runtime_archive_prefers_peer_with_matching_debug_descriptor(self):
        archive = b'identical signed runtime library'
        first_library = library(1, archive)
        first_library['published_debug_required'] = True
        second_library = copy.deepcopy(first_library)
        descriptor = {'type': 'rpm', 'name': 'php-zts-lib-fixture-debuginfo',
                      'package_version': '0:1.0-1', 'architecture': 'x86_64',
                      'location': 'php-zts-lib-fixture-debuginfo-1.0-1.x86_64.rpm'}
        second_library['published_debug_archive'] = descriptor
        first = self.snapshot('owner82', manifest(first_library),
                              {'archives/fixture-1.rpm': archive})
        second = self.snapshot('owner83', manifest(second_library),
                               {'archives/fixture-1.rpm': archive})
        merged, divergent, destination = self.merge([(manifest(first_library), first),
                                                    (manifest(second_library), second)])
        self.assertEqual(merged['libraries']['fixture']['published_debug_archive'], descriptor)
        self.assertEqual(merged['publication_drift'], ['fixture'])
        self.assertEqual(divergent, set())
        self.assertEqual((destination / 'archives/fixture-1.rpm').read_bytes(), archive)

    def test_missing_required_debug_archive_leaves_library_for_sdk_repack(self):
        archive = b'published runtime without matching debug archive'
        previous_library = library(1, archive)
        previous_library['published_debug_required'] = True
        baseline = self.snapshot('baseline', manifest(previous_library),
                                 {'archives/fixture-1.rpm': archive})
        payload = self.snapshot('payload', manifest())
        plan = self.root / 'plan.json'
        retry = {key: previous_library[key] for key in ['published_version', 'published_archive',
                 'published_archive_sha256', 'version', 'source_hash', 'abi_epoch', 'abi_dump_hashes']}
        plan.write_text(json.dumps({'schema': 1, 'type': 'rpm', 'libraries': ['fixture'],
                                   'publication_retries': {'fixture': retry}}))
        args = argparse.Namespace(plan=str(plan), manifest=str(payload / 'shared-libraries.json'),
                                  baseline_manifest=[str(baseline / 'shared-libraries.json')],
                                  destination=str(self.root / 'staged'),
                                  manifest_output=str(self.root / 'staged-manifest.json'),
                                  output=str(self.root / 'staged.json'),
                                  layout_source_hash='fixture-recipe')
        inspect_archive = Mock()
        with patch.dict(namespace, {'archive_identity': inspect_archive}):
            planner['stage_publication_retries'](args)
        result = json.loads(Path(args.output).read_text())
        self.assertEqual(result['remaining_libraries'], ['fixture'])
        self.assertEqual(result['retry_libraries'], [])
        self.assertEqual(result['copied_archives'], [])
        self.assertFalse((Path(args.destination) / 'fixture-1.rpm').exists())
        inspect_archive.assert_not_called()

    def test_missing_runtime_layout_owner_marks_incomplete_and_retains_archive(self):
        archive = b'layout archive'
        value = planner['empty_manifest']()
        value['runtime_layout'] = {'schema': 1, 'package_version': '0:1-1',
                                   'archive': 'archives/layout.rpm',
                                   'archive_sha256': hashlib.sha256(archive).hexdigest(),
                                   'complete': True}
        first = self.snapshot('owner82', value, {'archives/layout.rpm': archive})
        second = self.snapshot('owner83', planner['empty_manifest']())
        merged, divergent, destination = self.merge([(value, first), (planner['empty_manifest'](), second)])
        self.assertFalse(merged['runtime_layout']['complete'])
        self.assertEqual((destination / merged['runtime_layout']['archive']).read_bytes(), archive)
        self.assertEqual(divergent, set())

    def test_complete_runtime_layout_is_retained_once(self):
        archive = b'identical layout archive'
        value = planner['empty_manifest']()
        value['runtime_layout'] = {'schema': 1, 'package_version': '0:1-1',
                                   'archive': 'archives/layout.rpm',
                                   'archive_sha256': hashlib.sha256(archive).hexdigest(),
                                   'complete': True}
        first = self.snapshot('owner82', value, {'archives/layout.rpm': archive})
        second = self.snapshot('owner83', copy.deepcopy(value), {'archives/layout.rpm': archive})
        merged, _, destination = self.merge([(value, first), (copy.deepcopy(value), second)])
        self.assertTrue(merged['runtime_layout']['complete'])
        self.assertEqual((destination / 'archives/layout.rpm').read_bytes(), archive)

    def test_debian_all_architecture_layout_is_discovered_and_retained(self):
        archive = b'architecture-independent layout archive'
        packages = [{'name': 'php-zts-runtime-layout', 'version': '1-1'}]
        helper = {'get_remote_packages': Mock(return_value=packages),
                  'package_metadata': lambda *args: ('php-zts-runtime-layout', '1-1', 'all')}
        listing = Mock()
        listing.json.return_value = [{'id': 1, 'name': 'php-zts-runtime-layout_1-1_all.deb'}]
        download = Mock()
        download.__enter__ = Mock(return_value=download)
        download.__exit__ = Mock(return_value=False)
        download.iter_content.return_value = [archive]
        args = argparse.Namespace(output=str(self.root / 'deb-layout'), type='deb', arch='amd64',
                                  host='https://fixture.invalid', owner='86', password_env=None,
                                  consumer_password_env=None)
        metadata = {'schema': 1, 'version': '1', 'source_hash': 'layout-recipe'}
        with patch.object(namespace['runpy'], 'run_path', return_value=helper), \
             patch.dict(namespace, {'published_consumers': Mock(return_value={}),
                                    'archive_runtime_json': Mock(return_value=metadata)}), \
             patch.object(requests, 'get', side_effect=[listing, download]):
            planner['baseline_single'](args)
        baseline = json.loads((Path(args.output) / 'shared-libraries.json').read_text())
        self.assertEqual(baseline['runtime_layout']['package_version'], '1-1')
        self.assertEqual((Path(args.output) / baseline['runtime_layout']['archive']).read_bytes(), archive)

    def test_unselected_runtime_layout_change_is_not_own_publication_progress(self):
        previous = manifest(library(1))
        previous['runtime_layout'] = {'schema': 1, 'package_version': '0:1-1',
                                      'source_hash': 'previous-layout', 'archive_sha256': 'old'}
        current = copy.deepcopy(previous)
        current['runtime_layout'] = {'schema': 1, 'package_version': '0:1-2',
                                     'source_hash': 'unrelated-layout', 'archive_sha256': 'new'}
        versions = {(None, 'php-zts-lib-fixture'): '0:1.0-2'}
        self.assertFalse(planner['candidate_progress'](current, previous, {}, versions))

    def test_layout_missing_from_peer_mirror_stages_exact_existing_archive(self):
        archive = b'existing layout on the first mirror'
        first_value = manifest(library(1))
        first_value['runtime_layout'] = {'schema': 1, 'package_version': '0:1-1',
                                        'type': 'rpm', 'source_hash': 'fixture-recipe',
                                        'archive': 'archives/layout.rpm',
                                        'archive_sha256': hashlib.sha256(archive).hexdigest(),
                                        'complete': True}
        first = self.snapshot('mirror1', first_value, {'archives/layout.rpm': archive})
        second = self.snapshot('mirror2', manifest(library(1)))
        payload = self.snapshot('payload', manifest())
        plan = self.root / 'plan.json'
        plan.write_text(json.dumps({'schema': 1, 'type': 'rpm', 'libraries': []}))
        args = argparse.Namespace(plan=str(plan), manifest=str(payload / 'shared-libraries.json'),
                                  baseline_manifest=[str(first / 'shared-libraries.json'),
                                                     str(second / 'shared-libraries.json')],
                                  destination=str(self.root / 'staged'),
                                  manifest_output=str(self.root / 'staged-manifest.json'),
                                  output=str(self.root / 'staged.json'),
                                  layout_source_hash='fixture-recipe')
        with patch.dict(namespace, {'archive_identity': Mock(return_value=('php-zts-runtime-layout', '0:1-1', 'noarch'))}):
            planner['stage_publication_retries'](args)
        result = json.loads(Path(args.output).read_text())
        self.assertEqual(len(result['copied_archives']), 1)
        self.assertEqual((Path(args.destination) / 'layout.rpm').read_bytes(), archive)
        self.assertFalse(result['layout_required'])

    def test_changed_layout_recipe_requests_packaging_without_stale_archive_copy(self):
        archive = b'previous layout recipe archive'
        previous = manifest(library(1))
        previous['runtime_layout'] = {'schema': 1, 'package_version': '0:1-1',
                                     'type': 'rpm', 'source_hash': 'previous-recipe',
                                     'archive': 'archives/layout.rpm',
                                     'archive_sha256': hashlib.sha256(archive).hexdigest(),
                                     'complete': False}
        baseline = self.snapshot('baseline', previous, {'archives/layout.rpm': archive})
        payload = self.snapshot('payload', manifest())
        plan = self.root / 'plan.json'
        plan.write_text(json.dumps({'schema': 1, 'type': 'rpm', 'libraries': []}))
        args = argparse.Namespace(plan=str(plan), manifest=str(payload / 'shared-libraries.json'),
                                  baseline_manifest=[str(baseline / 'shared-libraries.json')],
                                  destination=str(self.root / 'staged'),
                                  manifest_output=str(self.root / 'staged-manifest.json'),
                                  output=str(self.root / 'staged.json'),
                                  layout_source_hash='new-recipe')
        inspect_archive = Mock()
        with patch.dict(namespace, {'archive_identity': inspect_archive}):
            planner['stage_publication_retries'](args)
        result = json.loads(Path(args.output).read_text())
        self.assertTrue(result['layout_required'])
        self.assertEqual(result['copied_archives'], [])
        self.assertFalse((Path(args.destination) / 'layout.rpm').exists())
        inspect_archive.assert_not_called()


if __name__ == '__main__':
    unittest.main()
