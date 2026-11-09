import contextlib
import hashlib
import io
import json
from pathlib import Path
import runpy
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import requests


ROOT = Path(__file__).resolve().parents[1]
planner = runpy.run_path(str(ROOT / 'bin/shared-library-plan'))
guard = runpy.run_path(str(ROOT / 'bin/check-rpm-upgrades'))
plan_globals = planner['baseline'].__globals__
guard_globals = guard['verify_package'].__globals__


class PublicationBaselines(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory(prefix='publication-baseline-test-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def baseline_args(self, **values):
        args = dict(type='deb', arch='amd64', host='https://fixture.invalid',
                    owner=None, owners=None, password_env='LIB_PASSWORD',
                    consumer_password_env='PASSWORD', base_url=None,
                    remote_directory=None, baseline_directory=[], baseline_url=[],
                    output=str(self.root / 'baseline'))
        return SimpleNamespace(**dict(args, **values))

    def test_explicit_forgejo_baseline_merges_every_requested_owner(self):
        owners = []
        def capture(args):
            owners.append((args.owner, args.password_env))
            value = planner['empty_manifest']()
            value['consumer_versions'] = {'8.6': {'php-zts-cli': args.owner}}
            planner['write_json'](Path(args.output) / 'shared-libraries.json', value)
        with patch.dict(plan_globals, {'baseline_single': capture,
                                      'native_compare': lambda kind, a, b: (int(a) > int(b)) - (int(a) < int(b))}):
            planner['baseline'](self.baseline_args(owners='82,83,84,85,86'))
        result = json.loads((self.root / 'baseline/shared-libraries.json').read_text())
        self.assertEqual(owners, [(str(owner), 'PASSWORD') for owner in range(82, 87)])
        self.assertEqual(result['consumer_versions']['8.6']['php-zts-cli'], '86')
        self.assertEqual(result['architecture'], 'x86_64')

    def test_default_baseline_reads_only_php85_and_shares_dependencies(self):
        owners = []
        def capture(args):
            owners.append(args.owner)
            value = planner['empty_manifest']()
            value.update(consumer_inventory_complete=True, consumer_inventory_stream='8.5')
            value['consumers'] = {'8.5': ['php-zts-cli']}
            value['consumer_dependencies'] = {'8.5': {'php-zts-cli': ['openssl']}}
            value['consumer_versions'] = {'8.5': {'php-zts-cli': '8.5.12-1'}}
            planner['write_json'](Path(args.output) / 'shared-libraries.json', value)
        with patch.dict(plan_globals, {'baseline_single': capture}):
            planner['baseline'](self.baseline_args())
        result = json.loads((self.root / 'baseline/shared-libraries.json').read_text())
        self.assertEqual(owners, ['85'])
        self.assertTrue(result['consumer_inventory_complete'])
        for stream in planner['STREAMS']:
            self.assertEqual(result['consumer_dependencies'][stream], {'php-zts-cli': ['openssl']})
            self.assertEqual(result['consumers'][stream], ['php-zts-cli'])
            if stream != '8.5':
                self.assertEqual(result['consumer_versions'][stream], {})

    def test_no_rebuild_skips_deb_and_apk_consumer_archives(self):
        for kind in ['deb', 'apk']:
            with self.subTest(kind=kind):
                args = self.baseline_args(type=kind, owner='85', skip_consumers=True,
                                          abi_libraries='', output=str(self.root / kind))
                helper = {'get_remote_packages': Mock(return_value=[])}
                with patch.dict(plan_globals, {'published_consumers': Mock(side_effect=AssertionError('consumer scan'))}), \
                        patch.object(runpy, 'run_path', return_value=helper):
                    planner['baseline_single'](args)
                result = json.loads((Path(args.output) / 'shared-libraries.json').read_text())
                self.assertFalse(result['consumer_inventory_complete'])

    def test_php_migration_is_detected_from_native_metadata_without_archives(self):
        for kind in ['rpm', 'deb', 'apk']:
            for provides in [[], ['php-zts-shared-runtime-85']]:
                with self.subTest(kind=kind, provides=provides):
                    index = Mock(return_value=([{'name': 'php-zts-cli', 'provides': provides}], None, None))
                    native = {'rpm_index': index, 'deb_index': index, 'apk_index': index,
                              'latest_packages': lambda kind, packages: {p['name']: p for p in packages}}
                    args = self.baseline_args(type=kind, arch='arm64', php_versions='8.5', base_url='https://fixture/el8')
                    with patch.object(runpy, 'run_path', return_value=native):
                        self.assertEqual(planner['unmigrated_consumers'](args), [] if provides else ['8.5'])
                    self.assertEqual(index.call_args.args[1], 'arm64' if kind == 'deb' else 'aarch64')

    def test_php_migration_probe_does_not_hide_repository_failures(self):
        native = {'apk_index': Mock(side_effect=RuntimeError('repository unavailable'))}
        with patch.object(runpy, 'run_path', return_value=native), self.assertRaisesRegex(RuntimeError, 'repository unavailable'):
            planner['unmigrated_consumers'](self.baseline_args(type='apk', php_versions='8.5'))

    def test_unchanged_selection_does_not_read_unrelated_package_archives(self):
        args = self.baseline_args(type='deb', owner='85', skip_consumers=True, abi_libraries='', libraries='zlib')
        helper = {'get_remote_packages': Mock(return_value=[{'name': 'php-zts-lib-other', 'version': '1-1'}])}
        with patch.object(runpy, 'run_path', return_value=helper), patch.object(requests, 'get', side_effect=AssertionError('unrelated archive')):
            planner['baseline_single'](args)
        self.assertEqual([call.args[-1] for call in helper['get_remote_packages'].call_args_list],
                         ['php-zts-lib-zlib', 'php-zts-runtime-layout'])
        result = json.loads((Path(args.output) / 'shared-libraries.json').read_text())
        self.assertEqual(result['metadata_scope'], ['zlib'])
        self.assertEqual(result['libraries'], {})

    def test_no_rebuild_skips_rpm_consumer_inventory(self):
        args = self.baseline_args(type='rpm', base_url='https://fixture/el8', skip_consumers=True,
                                  abi_libraries='')
        rpm = {'published_packages': Mock(return_value=[])}
        with patch.dict(plan_globals, {'published_rpm_consumer_index': Mock(side_effect=AssertionError('consumer scan'))}), \
                patch.object(runpy, 'run_path', return_value=rpm):
            planner['baseline_single'](args)
        result = json.loads((Path(args.output) / 'shared-libraries.json').read_text())
        self.assertFalse(result['consumer_inventory_complete'])

    def test_historical_rpm_urls_share_a_highest_version_baseline(self):
        sources = []
        def capture(args):
            sources.append(args.base_url)
            value = planner['empty_manifest']()
            value['consumer_versions'] = {'8.6': {'php-zts-cli': args.base_url.rsplit('el', 1)[1]}}
            planner['write_json'](Path(args.output) / 'shared-libraries.json', value)
        args = self.baseline_args(type='rpm', arch='x86_64', base_url='https://fixture/el8',
                                  baseline_url=['https://fixture/el9', 'https://fixture/el10'])
        with patch.dict(plan_globals, {'baseline_single': capture,
                                      'native_compare': lambda kind, a, b: (int(a) > int(b)) - (int(a) < int(b))}):
            planner['baseline'](args)
        result = json.loads((self.root / 'baseline/shared-libraries.json').read_text())
        self.assertEqual(len(sources), 3)
        self.assertEqual(result['consumer_versions']['8.6']['php-zts-cli'], '10')

    def test_confirmed_absent_stream_owner_does_not_discard_existing_baselines(self):
        response = requests.Response()
        response.status_code = 404
        def capture(args):
            if args.owner == '86':
                raise requests.HTTPError(response=response)
            value = planner['empty_manifest']()
            value['consumer_inventory_complete'] = True
            if args.owner == '85':
                value['consumer_inventory_stream'] = '8.5'
            value['consumer_versions'] = {args.owner[0] + '.' + args.owner[1:]: {'php-zts-cli': '1'}}
            planner['write_json'](Path(args.output) / 'shared-libraries.json', value)
        with patch.dict(plan_globals, {'baseline_single': capture}), patch.object(requests, 'get', return_value=Mock(status_code=404)) as lookup:
            planner['baseline'](self.baseline_args(owners='82,83,84,85,86'))
        result = json.loads((self.root / 'baseline/shared-libraries.json').read_text())
        self.assertEqual([value['owner'] for value in result['baseline_origins']], ['82', '83', '84', '85'])
        self.assertEqual(result['skipped_origins'], [{'owner': '86', 'password_env': 'PASSWORD', 'reason': 'owner-not-found'}])
        self.assertTrue(result['consumer_inventory_complete'])
        self.assertEqual(result['consumer_versions']['8.6'], {})
        self.assertEqual(lookup.call_args.args[0], 'https://fixture.invalid/api/v1/users/86')

    def test_missing_archive_of_existing_owner_is_fatal(self):
        response = requests.Response()
        response.status_code = 404
        with patch.dict(plan_globals, {'baseline_single': Mock(side_effect=requests.HTTPError(response=response))}), \
                patch.object(requests, 'get', return_value=Mock(status_code=200)), self.assertRaises(requests.HTTPError):
            planner['baseline'](self.baseline_args())

    def test_authentication_failure_cannot_be_treated_as_an_absent_owner(self):
        response = requests.Response()
        response.status_code = 401
        with patch.dict(plan_globals, {'baseline_single': Mock(side_effect=requests.HTTPError(response=response))}), \
                patch.object(requests, 'get') as lookup, self.assertRaises(requests.HTTPError):
            planner['baseline'](self.baseline_args())
        lookup.assert_not_called()

    def test_owner_abi_divergence_survives_outer_mirror_merge(self):
        value = planner['empty_manifest']()
        value['divergent_libraries'] = ['openssl']
        _, divergent = planner['merge_baselines']([(value, self.root)], 'rpm', self.root)
        self.assertEqual(divergent, {'openssl'})

    def test_snapshot_origin_does_not_change_semantic_fingerprint(self):
        value = planner['empty_manifest']()
        other = dict(value, baseline_origins=[{'base_url': 'https://fixture/el8'}])
        self.assertEqual(planner['fingerprint'](value), planner['fingerprint'](other))

    def test_interrupted_publication_can_only_advance_planned_stream_candidates(self):
        previous = planner['empty_manifest']()
        previous['consumer_versions'] = {'8.2': {'php-zts-cli': 'old82'}, '8.6': {'php-zts-cli': 'old86'}}
        current = dict(previous, consumer_versions={'8.2': {'php-zts-cli': 'new82'}, '8.6': {'php-zts-cli': 'old86'}})
        versions = {('8.2', 'php-zts-cli'): 'new82', ('8.6', 'php-zts-cli'): 'new86'}
        self.assertTrue(planner['candidate_progress'](current, previous, {}, versions))
        current['consumer_versions']['8.2']['php-zts-cli'] = 'unrelated'
        self.assertFalse(planner['candidate_progress'](current, previous, {}, versions))
        deleted = dict(previous, consumer_versions={'8.2': {}, '8.6': {'php-zts-cli': 'old86'}})
        self.assertFalse(planner['candidate_progress'](deleted, previous, {}, {}))

    def test_debian_library_resume_uses_php_independent_candidate_identity(self):
        previous = planner['empty_manifest']()
        expected = {'version': '3.6.0', 'source_hash': 'sealed-source', 'abi_epoch': 1,
                    'abi_capability': 'php-zts-lib-openssl-abi-1', 'abi_dump': {},
                    'abi_dump_hashes': {}, 'abi_empty': []}
        current = planner['empty_manifest']()
        current['libraries']['openssl'] = dict(expected, elf={}, files=[], published_version='3.6.0-1')
        baseline = self.root / 'resumed-baseline.json'
        baseline.write_text(json.dumps(current))
        plan = {'schema': 1, 'baseline_fingerprint': planner['fingerprint'](previous),
                'baseline_states': [previous], 'expected_libraries': {'openssl': expected},
                'expected_members': ['php-zts-lib-openssl']}
        plan_path = self.root / 'plan.json'
        plan_path.write_text(json.dumps(plan))
        artifacts = self.root / 'resume-artifacts'
        artifacts.mkdir()
        (artifacts / 'library.deb').write_bytes(b'sealed archive')
        original = runpy.run_path
        def modules(path):
            return {'package_metadata': lambda *args: ('php-zts-lib-openssl', '3.6.0-1', 'amd64')} if Path(path).name == 'forgejo-helper' else original(path)
        def extract(path, kind, destination):
            data = dict(expected, package_versions={'deb': '3.6.0-1'})
            planner['write_json'](Path(destination) / 'usr/share/php-zts/libraries/openssl.json', data)
            return True
        args = SimpleNamespace(plan=str(plan_path), baseline_manifest=str(baseline),
                               artifacts=str(artifacts), type='deb', output=str(self.root / 'resume.json'))
        with patch.dict(plan_globals, {'extract_metadata': extract, 'package_provides': lambda *args: {expected['abi_capability']},
                                      'native_compare': lambda *args: 0}), patch.object(plan_globals['runpy'], 'run_path', side_effect=modules):
            planner['cohort'](args)
        self.assertEqual(len(json.loads(Path(args.output).read_text())['files']), 1)

    def cutoff_args(self, names):
        baseline = planner['empty_manifest']()
        baseline['consumer_versions'] = {'8.6': names}
        path = self.root / 'published.json'
        path.write_text(json.dumps(baseline))
        return SimpleNamespace(type='rpm', baseline_manifest=[str(path)],
                               artifacts=str(self.root / 'artifacts'), php_version='8.6',
                               output=str(self.root / 'cutoffs.json'))

    def test_missing_published_module_blocks_cli_migration(self):
        args = self.cutoff_args({'php-zts-redis': '0:6.3.0_86.0~rc4-3.el10'})
        with self.assertRaisesRegex(RuntimeError, 'Missing published migration consumers: php-zts-redis'):
            planner['migration_cutoffs'](args)

    def test_already_shared_module_inherits_first_cutoff_without_repackaging(self):
        args = self.cutoff_args({'php-zts-intl': 'new-module'})
        path = Path(args.baseline_manifest[0])
        value = json.loads(path.read_text())
        value['migration_cutoffs'] = {'8.6': {'php-zts-intl': 'first-shared-module'}}
        path.write_text(json.dumps(value))
        with patch.dict(plan_globals, {'native_compare': lambda *args: 1}):
            planner['migration_cutoffs'](args)
        result = json.loads(Path(args.output).read_text())
        self.assertEqual(result['formats']['rpm']['php-zts-intl'], 'first-shared-module')
        self.assertEqual(result['candidate_archives'], {})

    def test_frozen_inventory_uses_all_published_modules_not_only_downloaded_profile(self):
        frozen = {'schema': 1, 'type': 'rpm', 'php_stream': '8.6', 'architecture': 'x86_64',
                  'packages': [{'name': 'php-zts-cli', 'version': '8.6.0~rc4-1.el8', 'epoch': '0', 'architecture': 'x86_64'}],
                  'published_packages': [{'name': 'php-zts-cli', 'version': '8.6.0~rc4-1.el8', 'epoch': '0', 'architecture': 'x86_64'},
                                         {'name': 'php-zts-redis', 'version': '6.3.0_86.0~rc4-3.el10', 'epoch': '0', 'architecture': 'x86_64'},
                                         {'name': 'php-zts-redis', 'version': '6.3.0_85.12-100.el10', 'epoch': '0', 'architecture': 'x86_64'}]}
        path = self.root / 'frozen.json'
        path.write_text(json.dumps(frozen))
        result = planner['cutoff_baseline'](path, 'rpm')
        self.assertEqual(result['consumer_versions']['8.6']['php-zts-redis'], '0:6.3.0_86.0~rc4-3.el10')
        self.assertEqual(result['consumer_versions']['8.5']['php-zts-redis'], '0:6.3.0_85.12-100.el10')

    def test_frozen_rpm_inventory_keeps_every_stream_and_filters_architecture(self):
        frozen = {'schema': 1, 'type': 'rpm', 'php_stream': '8.6', 'architecture': 'x86_64',
                  'packages': [], 'published_packages': [
                      {'name': 'php-zts-cli', 'version': f'{stream}.35-10.el10', 'epoch': '0', 'architecture': 'x86_64'}
                      for stream in planner['STREAMS']] + [
                      {'name': 'php-zts-cli', 'version': '8.2.35-99.el10', 'epoch': '0', 'architecture': 'aarch64'},
                      {'name': 'php-zts-runtime-layout', 'version': '1-1', 'epoch': '0', 'architecture': 'noarch'}]}
        path = self.root / 'frozen.json'
        path.write_text(json.dumps(frozen))
        result = planner['cutoff_baseline'](path, 'rpm')
        for stream in planner['STREAMS']:
            self.assertEqual(result['consumer_versions'][stream], {'php-zts-cli': f'0:{stream}.35-10.el10'})

    def test_frozen_deb_inventory_stays_in_its_owner_stream(self):
        frozen = {'schema': 1, 'type': 'deb', 'php_stream': '8.6', 'architecture': 'amd64',
                  'packages': [], 'published_packages': [
                      {'name': 'php-zts-redis', 'version': '6.3.0+php86.0~rc4-3', 'architecture': 'amd64'}]}
        path = self.root / 'frozen.json'
        path.write_text(json.dumps(frozen))
        result = planner['cutoff_baseline'](path, 'deb')
        self.assertEqual(result['consumer_versions']['8.6']['php-zts-redis'], '6.3.0+php86.0~rc4-3')
        self.assertEqual(result['consumer_versions']['8.5'], {})

    def test_cutoffs_come_from_native_candidate_headers(self):
        args = self.cutoff_args({'php-zts-cli': 'old-core', 'php-zts-intl': 'old-module',
                                 'pie-zts': 'old-tool', 'php-zts-intl-debuginfo': 'old-debug'})
        artifacts = Path(args.artifacts)
        artifacts.mkdir()
        archive = artifacts / 'arbitrary-filename.rpm'
        archive.write_bytes(b'native archive fixture')
        actual = dict(name='php-zts-intl', version='8.6.0~rc4', evr='0:8.6.0~rc4-8', arch='x86_64')
        original = runpy.run_path
        def modules(path):
            return dict(guard, local_package=lambda path: actual) if Path(path).name == 'check-rpm-upgrades' else original(path)
        with patch.dict(plan_globals, {'native_compare': lambda *args: 1}), patch.object(plan_globals['runpy'], 'run_path', side_effect=modules):
            planner['migration_cutoffs'](args)
        result = json.loads(Path(args.output).read_text())
        self.assertEqual(result['formats'], {'rpm': {'php-zts-intl': '0:8.6.0~rc4-8'}})
        self.assertEqual(result['architecture'], 'x86_64')
        self.assertEqual(result['candidate_archives']['php-zts-intl']['sha256'], hashlib.sha256(archive.read_bytes()).hexdigest())

    def test_stream_only_deb_and_apk_tags_enter_migration_cutoffs(self):
        original = runpy.run_path
        for kind, versions in [('deb', ['1.13.6+php86-1', '1.13.6~rc1+php86-1', '1.13.5+php86.0~rc4-2']),
                               ('apk', ['1.13.6p86-r1', '1.13.6_rc1_p86-r1', '1.13.5p86.0_rc4-r2'])]:
            for version in versions:
                with self.subTest(kind=kind, version=version):
                    args = self.cutoff_args({'frankenphp': 'older-release'})
                    args.type = kind
                    artifacts = Path(args.artifacts)
                    artifacts.mkdir(exist_ok=True)
                    archive = artifacts / ('frankenphp.' + kind)
                    archive.write_bytes(b'native archive fixture')
                    def modules(path):
                        value = original(path)
                        if Path(path).name == 'forgejo-helper':
                            value['package_metadata'] = lambda *args: ('frankenphp', version, 'x86_64')
                        return value
                    with patch.dict(plan_globals, {'native_compare': lambda *args: 1}), patch.object(plan_globals['runpy'], 'run_path', side_effect=modules):
                        planner['migration_cutoffs'](args)
                    result = json.loads(Path(args.output).read_text())
                    self.assertEqual(result['formats'], {kind: {'frankenphp': version}})
                    self.assertEqual(result['candidate_archives']['frankenphp']['sha256'], hashlib.sha256(archive.read_bytes()).hexdigest())

    def test_consumer_stream_parsing_keeps_core_and_historical_versions(self):
        for version, expected in [('8.2.35-3', '8.2'), ('1.13.6+php82-1', '8.2'),
                                  ('1.13.6p82-r1', '8.2'), ('3.6.0_alpha1_p86_p0_rc4-r1', '8.6'),
                                  ('1.13.6+php100-1', '10.0'), ('1.13.6_rc1_p100-r1', '10.0')]:
            with self.subTest(version=version):
                self.assertEqual(planner['consumer_version_stream'](version), expected)

    def test_equal_rpm_evr_requires_identical_archive_bytes(self):
        archive = self.root / 'candidate.rpm'
        archive.write_bytes(b'sealed package')
        local = dict(name='php-zts-cli', version='8.6.0~rc4', evr='0:8.6.0~rc4-8', arch='x86_64', path=str(archive))
        remote = dict(local, checksum={'type': 'sha256', 'value': hashlib.sha256(archive.read_bytes()).hexdigest()})
        commands = Mock(run=lambda *args, **kwargs: Mock(returncode=0, stderr=''))
        with patch.dict(guard_globals, {'subprocess': commands}):
            guard['verify_package'](local, [remote], '8.6')
            remote['checksum']['value'] = '0' * 64
            with self.assertRaisesRegex(RuntimeError, 'different bytes'):
                guard['verify_package'](local, [remote], '8.6')

    def test_equal_rpm_without_remote_checksum_is_not_assumed_safe(self):
        local = dict(name='php-zts-cli', version='8.6.0~rc4', evr='0:8.6.0~rc4-8', arch='x86_64')
        with patch.dict(guard_globals, {'subprocess': Mock(run=lambda *args, **kwargs: Mock(returncode=0, stderr=''))}):
            with self.assertRaisesRegex(RuntimeError, 'Cannot prove identical'):
                guard['verify_package'](local, [local], '8.6')

    def test_matching_debug_retry_streams_exact_existing_archive(self):
        data = b'previously signed native debug archive'
        destination = self.root / 'debug'
        destination.mkdir()
        record = {'type': 'rpm', 'name': 'php-zts-lib-fixture-debuginfo', 'package_version': '0:1-7',
                  'architecture': 'x86_64', 'base_url': 'https://fixture/el8',
                  'location': 'php-zts-lib-fixture-debuginfo-1-7.x86_64.rpm',
                  'checksum': {'type': 'sha256', 'value': hashlib.sha256(data).hexdigest()}}
        with patch.dict(plan_globals, {'archive_identity': lambda *args: (record['name'], record['package_version'], record['architecture']),
                                      'native_compare': lambda *args: 0}), patch.object(plan_globals['urllib'].request, 'urlopen', return_value=io.BytesIO(data)):
            copied = planner['stage_debug_archive'](record, destination)
        self.assertEqual(Path(copied['path']).read_bytes(), data)
        self.assertEqual(copied['sha256'], record['checksum']['value'])

    def test_debug_retry_bad_checksum_is_rejected_before_staging(self):
        destination = self.root / 'debug'
        destination.mkdir()
        record = {'type': 'rpm', 'name': 'php-zts-lib-fixture-debuginfo', 'package_version': '0:1-7',
                  'architecture': 'x86_64', 'base_url': 'https://fixture/el8',
                  'location': 'php-zts-lib-fixture-debuginfo-1-7.x86_64.rpm',
                  'checksum': {'type': 'sha256', 'value': '0' * 64}}
        with patch.object(plan_globals['urllib'].request, 'urlopen', return_value=io.BytesIO(b'changed archive')):
            with self.assertRaisesRegex(RuntimeError, 'different bytes'):
                planner['stage_debug_archive'](record, destination)
        self.assertEqual(list(destination.iterdir()), [])

    def test_strict_cohort_rejects_entire_upload_if_one_candidate_is_lower(self):
        directory = self.root / 'artifacts'
        directory.mkdir()
        for name in ['safe.rpm', 'older.rpm']:
            (directory / name).touch()
        allowlist = self.root / 'files'
        def verify(local, *args):
            if local['name'] == 'older':
                raise guard['PackageDowngradeError']('lower candidate')
        with patch.dict(guard_globals, {
            'published_packages': lambda *args: [],
            'local_package': lambda path: dict(name=path.stem, arch='x86_64', evr='1'),
            'verify_package': verify,
        }), patch.object(sys, 'argv', ['check-rpm-upgrades', '--base-url', 'https://fixture/el8',
                                     '--strict-cohort', '--files-from', str(allowlist), str(directory)]), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(guard['main'](), 3)
        self.assertEqual(allowlist.read_bytes(), b'')


if __name__ == '__main__':
    unittest.main()
