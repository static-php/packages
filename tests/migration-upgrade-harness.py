"""Regression checks for real-baseline ordinary-upgrade validation."""

import gzip
import io
import json
import os
from pathlib import Path
import runpy
import sys
import subprocess
import tarfile
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch


harness = runpy.run_path(str(Path(__file__).with_name('migration-upgrade-containers.py')))
scope = harness['cell'].__globals__


class PreparedStub:
    def __init__(self, *args, **kwargs):
        self.report = {}
    def prepare(self):
        pass
    def close(self):
        return {'complete': True, 'remaining': []}


class MigrationUpgradeHarness(unittest.TestCase):
    def test_failure_output_preserves_receipt_without_dumping_it(self):
        error = ('Command failed: docker exec test python3 migration.py\n'
                 'Traceback (most recent call last):\n  File "migration.py", line 712\n'
                 'RuntimeError: Replacement unchanged upgrade was not a no-op\n')
        baseline = ('Command failed: docker exec test python3 migration.py\n'
                    'Traceback (most recent call last):\nRuntimeError: Command failed: apk add ' + 'package ' * 200
                    + '\nERROR: unable to select packages:\n  missing-runtime (no such package):\n'
                    '    required by: frankenphp\n')
        result = {'ok': False, 'cells': [
            {'profile_id': 'common', 'ok': False, 'failed_phase': 'fresh replacement validation',
             'error': error, 'baseline_failure': baseline, 'inventory': ['package'] * 2000},
            {'profile_id': 'independent-spx', 'ok': True}], 'cleanup': {'complete': True}}
        with tempfile.TemporaryDirectory() as directory:
            report = Path(directory) / 'receipt.json'
            summary = Path(directory) / 'summary.md'
            arguments = ['migration-upgrade-containers.py', '--type', 'apk', '--php', '8.2', '--arch', 'arm64',
                         '--baseline-dir', '/unused', '--artifacts', '/unused', '--report', str(report)]
            with patch.dict(scope, {'cell': lambda *_: result}), patch.object(sys, 'argv', arguments), \
                    patch.dict(scope['os'].environ, {'GITHUB_STEP_SUMMARY': str(summary)}), \
                    patch('sys.stdout', new_callable=io.StringIO) as output:
                self.assertEqual(harness['main'](), 1)
            rendered = output.getvalue()
            self.assertIn('FAIL (1/2 profiles passed)', rendered)
            self.assertIn('common / fresh replacement validation: Replacement unchanged upgrade was not a no-op', rendered)
            self.assertIn('missing-runtime (no such package)', rendered)
            self.assertNotIn('Traceback', rendered)
            self.assertNotIn('inventory', rendered)
            self.assertLess(len(rendered), 1500)
            self.assertIn('fresh replacement validation', summary.read_text())
            saved = json.loads(report.read_text())
            self.assertEqual(saved['cells'][0]['error'], error)
            self.assertEqual(saved['cells'][0]['baseline_failure'], baseline)
            self.assertEqual(len(saved['cells'][0]['inventory']), 2000)

    def test_native_test_bootstrap_retries_only_metadata_download_failures(self):
        calls = []
        def execute(name, command):
            calls.append(command)
            if len(calls) == 1:
                raise RuntimeError("Failed to download metadata for repo 'baseos': Document is empty")
        harness['prepare'](SimpleNamespace(exec=execute), 'test', 'rpm')
        self.assertEqual([command[:3] for command in calls], [['dnf', 'install', '-y'], ['dnf', 'clean', 'metadata'], ['dnf', 'install', '-y']])
        def broken(*args):
            raise RuntimeError('No match for argument: missing-tool')
        with self.assertRaisesRegex(RuntimeError, 'No match'):
            harness['prepare'](SimpleNamespace(exec=broken), 'test', 'rpm')

    def test_broken_old_index_does_not_block_replacement_validation(self):
        old = [{'name': 'php-zts-cli', 'version': '8.3.36-r3'}]
        def broken_index(*args):
            raise RuntimeError('old CLI: v2 package format error')
        with patch.dict(scope, {'index_generation': broken_index, 'inspect_directory': lambda *args: old}):
            packages, failure = harness['index_baseline']('apk', '8.3', 'arm64', False)
            self.assertEqual(packages, old)
            self.assertIn('v2 package format error', failure)
            self.assertEqual(harness['index_baseline']('apk', '8.3', 'arm64', True), (old, failure))
        with patch.dict(scope, {'index_generation': broken_index, 'inspect_directory': broken_index}):
            packages, failure = harness['index_baseline']('apk', '8.3', 'arm64', True)
            self.assertEqual(packages, [])
            self.assertIn('v2 package format error', failure)

    def test_legacy_apk_upgrade_requires_the_actual_candidate_transaction(self):
        before = {'php-zts-imagick': '3.8.1p85-r1'}
        candidate = {'php-zts-imagick': {'version': '3.8.1.0_p85-r5'}}
        harness['assert_upgrade'](before, {'php-zts-imagick': '3.8.1.0_p85-r5'}, candidate, list(before))
        with self.assertRaisesRegex(RuntimeError, 'held back'):
            harness['assert_upgrade'](before, before, candidate, list(before))
        reason = harness['failure_reason']('Command failed: apk version -t 3.8.1p85-r1 3.8.1.0_p85-r5')
        self.assertIn('3.8.1p85-r1 3.8.1.0_p85-r5', reason)

    def test_old_wrong_architecture_does_not_reject_a_native_replacement(self):
        for new_arch in ('arm64', 'x86_64'):
            with self.subTest(candidate=new_arch), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root / 'published-names.json').write_text('[]')
                old = [{'name': 'php-zts-cli', 'architecture': 'x86_64'}]
                incoming = [{'name': 'php-zts-cli', 'architecture': new_arch}]
                args = harness['options'](SimpleNamespace(type='apk', php='8.2', arch='arm64', runtime='alpine', inside='index'))
                def mapped(path):
                    return root / Path(path).name if str(path).startswith('/spp/') else Path(path)
                with patch.dict(scope, {'Path': mapped, 'inspect_directory': lambda *_: incoming,
                        'make_keys': lambda *_: None, 'index_baseline': lambda *_: (old, None),
                        'index_generation': lambda *_: incoming, 'copy_historical': lambda *_: None}):
                    if new_arch == 'arm64':
                        harness['inside'](args)
                        inventory = json.loads((root / 'inventory.json').read_text())
                        self.assertIn('Published package architecture', inventory['baseline_index_failure'])
                    else:
                        with self.assertRaisesRegex(RuntimeError, 'architecture does not match'):
                            harness['inside'](args)

    def test_partial_candidate_validation_does_not_require_a_parseable_old_apk_version(self):
        old = [{'name': 'php-zts-imagick', 'version': '3.8.1p85-r1'}]
        incoming = [{'name': 'php-zts-imagick', 'version': '3.8.1.0_p85-r5'}]
        def compare(kind, before, after):
            if before == '3.8.1p85-r1' or after == 'invalid-new-version':
                raise RuntimeError('apk version rejected version')
            return False
        with patch.dict(scope, {'newer': compare}):
            self.assertEqual(harness['validate_partial_candidates']('apk', old, incoming), ['php-zts-imagick'])
            incoming[0]['version'] = 'invalid-new-version'
            with self.assertRaisesRegex(RuntimeError, 'rejected version'):
                harness['validate_partial_candidates']('apk', old, incoming)

    def test_failure_reason_keeps_native_error_after_verbose_command_output(self):
        error = ('Command failed: apk add php-zts\n' + 'Installing a package\n' * 2000
                 + 'ERROR: unable to select packages:\n  missing-runtime (no such package)\n')
        reason = harness['failure_reason'](error)
        self.assertIn('missing-runtime (no such package)', reason)
        self.assertNotIn('Installing', reason)

    def test_replacement_noop_check_excludes_tools_installed_by_phpize_smoke(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            packages = {'php-zts-cli': '8.2.35-r2', 'php-zts-devel': '8.2.35-r2'}
            (root / 'inventory.json').write_text(json.dumps({'new': [{'name': name, 'version': version} for name, version in packages.items()]}))
            (root / 'selected.json').write_text(json.dumps(list(packages)))
            args = harness['options'](SimpleNamespace(type='apk', php='8.2', arch='arm64', runtime='alpine', inside='replacement'))
            def mapped(path):
                return root / Path(path).name if str(path).startswith('/spp/') else Path(path)
            def development(*_):
                packages['gcc'] = '14.2.0-r4'
                return {'phpize': 'passed'}
            runtime = {'assert_elf': lambda *_: {}, 'sapi_checks': lambda *_: {}, 'development_checks': development}
            with patch.dict(scope, {'Path': mapped, 'configure_client': lambda *a, **k: None, 'run': lambda *_: None,
                                   'installed': lambda *_: dict(packages), 'ordinary_upgrade': lambda *_: None}), \
                    patch.object(scope['runpy'], 'run_path', return_value=runtime), patch('sys.stdout', new_callable=io.StringIO) as output:
                harness['inside'](args)
                self.assertTrue(json.loads(output.getvalue().split('SPP_MIGRATION_RESULT=')[-1])['ok'])
                with patch.dict(scope, {'ordinary_upgrade': lambda *_: packages.update(gcc='14.2.0-r5')}):
                    harness['inside'](args)
                with patch.dict(scope, {'ordinary_upgrade': lambda *_: packages.update({'php-zts-cli': '8.2.35-r3'})}):
                    with self.assertRaisesRegex(RuntimeError, 'changed managed packages.*php-zts-cli'):
                        harness['inside'](args)

    def test_broken_old_install_uses_clean_replacement_but_candidate_failures_still_block(self):
        for old_broken, candidate_broken, partial, tampered in [
                (True, False, False, False), (True, True, False, False),
                (False, True, False, False), (True, False, True, False), (True, False, 'library', False),
                ('missing', False, True, False),
                (True, True, True, False), (True, True, 'library', False), (True, False, False, True)]:
            with self.subTest(old=old_broken, candidate=candidate_broken, partial=partial, tampered=tampered), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                args = harness['options'](SimpleNamespace(type='apk', php='8.2', arch='arm64', runtime='alpine',
                                                         profile='cli', partial_consumers=partial is True, library_only=partial == 'library'))
                mirror = object.__new__(harness['PreparedMirror'])
                mirror.args, mirror.scratch, mirror.client_image = args, root, 'clean-client'
                mirror.baseline = {'origin': {}, 'packages': [{'name': 'php-zts-cli'}]}
                if partial == 'library':
                    mirror.baseline['packages'].append({'name': 'php-zts-lib-zlib'})
                incoming = list(mirror.baseline['packages'])
                if old_broken == 'missing':
                    mirror.baseline['packages'] = [{'name': 'php-zts-lib-zlib'}]
                mirror.inventory = {'old': mirror.baseline['packages'], 'incoming': incoming}
                mirror.report = {'ledger_sha256': 'test', 'profiles_started': 0}
                generations, executions = [], []
                mirror.select_generation = generations.append
                checks = []
                def bound(_):
                    checks.append(True)
                    if tampered and len(checks) > 1:
                        raise harness['PreparedInputError']('changed candidate')
                mirror.assert_bound = bound
                resources = SimpleNamespace(execution={'native': True, 'emulated': False}, containers=[], runtime_evidence=[])
                mirror.resources = resources
                def start(image, name):
                    self.assertEqual(image, 'clean-client')
                    resources.containers.append(name)
                    resources.runtime_evidence.append({'container': name})
                    return name
                def execute(client, command):
                    if command == ['true']:
                        raise RuntimeError('Broken old container cannot execute commands')
                    phase = command[-1]
                    executions.append((client, phase))
                    if phase == 'old':
                        if old_broken:
                            raise RuntimeError('old package dependency missing')
                        (root / 'client-before.json').write_text(json.dumps({'installed': {}, 'probe': {}, 'module_state': {},
                            'config_sha256': 'config', 'key_sha256': 'key', 'php_ini_sha256': 'ini'}))
                    if phase in ('new', 'replacement') and candidate_broken:
                        raise RuntimeError('candidate dependency missing')
                    return SimpleNamespace(stdout='SPP_MIGRATION_RESULT=' + json.dumps({'ok': True, 'applicable': True}))
                resources.start, resources.exec = start, execute
                with patch.dict(scope, {'run': lambda *a, **k: SimpleNamespace(returncode=0, stderr='')}), patch('sys.stdout', new_callable=io.StringIO):
                    result = mirror.execute(args)
                expected = old_broken and not candidate_broken and not tampered
                self.assertEqual(result['ok'], expected)
                if expected:
                    self.assertIn('baseline packages missing' if old_broken == 'missing' else 'old package dependency missing', result['baseline_failure'])
                    new_client = next(client for client, phase in executions if phase == 'replacement')
                    if old_broken == 'missing':
                        self.assertFalse(any(phase == 'old' for _, phase in executions))
                    else:
                        old_client = next(client for client, phase in executions if phase == 'old')
                        self.assertNotEqual(old_client, new_client)
                if not old_broken or tampered:
                    self.assertFalse(any(phase == 'replacement' for _, phase in executions))
                if tampered:
                    self.assertTrue(result['prepared_inputs_invalid'])

    def test_partial_replacement_checks_the_complete_installed_dependency_closure(self):
        for libraries_only in (False, True):
            with self.subTest(libraries=libraries_only), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                versions = {'php-zts-lib-zlib': '1.3.2-r2', 'php-zts-runtime-layout': '1-r0'}
                if not libraries_only:
                    versions.update({'php-zts-cli': '8.2.35-r2', 'php-zts-curl': '8.2.35-r2'})
                incoming = [{'name': 'php-zts-lib-zlib', 'version': '1.3.2-r2'}]
                (root / 'inventory.json').write_text(json.dumps({'incoming': incoming,
                    'new': [{'name': name, 'version': version} for name, version in versions.items()]}))
                (root / 'selected.json').write_text(json.dumps(['php-zts-lib-zlib'] if libraries_only else ['php-zts-cli', 'php-zts-curl']))
                args = harness['options'](SimpleNamespace(type='apk', php='8.2', arch='arm64', runtime='alpine',
                    inside='replacement', library_only=libraries_only, partial_consumers=not libraries_only))
                def mapped(path):
                    return root / Path(path).name if str(path).startswith('/spp/') else Path(path)
                checked = []
                runtime = {'assert_elf': lambda kind, names: checked.extend(names) or {},
                           'sapi_checks': lambda *_: {}, 'development_checks': lambda *_: {}}
                installed = dict(versions)
                with patch.dict(scope, {'Path': mapped, 'configure_client': lambda *a, **k: None,
                        'run': lambda *_: None, 'installed': lambda *_: dict(installed), 'ordinary_upgrade': lambda *_: None}), \
                        patch.object(scope['runpy'], 'run_path', return_value=runtime), patch('sys.stdout', new_callable=io.StringIO) as output:
                    harness['inside'](args)
                    self.assertTrue(json.loads(output.getvalue().split('SPP_MIGRATION_RESULT=')[-1])['ok'])
                    self.assertEqual(set(checked), set(versions))
                    installed['php-zts-lib-zlib'] = '1.3.2-r1'
                    with self.assertRaisesRegex(RuntimeError, 'candidate version: php-zts-lib-zlib'):
                        harness['inside'](args)

    def test_auto_runs_only_the_frozen_affected_profiles(self):
        baseline = {'selection': {'mode': 'affected', 'complete': False},
                    'packages': [{'name': name} for name in
                                 ('php-zts-cli', 'php-zts-fpm', 'frankenphp', 'php-zts-imagick')]}
        calls = []
        def checked(args, prepared=None):
            calls.append(set(args.profile.split(',')))
            return {'ok': True, 'applicable': True, 'cleanup': {'complete': True}}
        with tempfile.TemporaryDirectory() as directory:
            report = Path(directory) / 'report.json'
            arguments = ['migration-upgrade-containers.py', '--type', 'deb', '--php', '8.5', '--arch', 'x86_64',
                         '--baseline-dir', '/unused', '--artifacts', '/unused', '--profile', 'auto',
                         '--partial-consumers', '--report', str(report)]
            with patch.dict(scope, {'load_baseline': lambda *args: baseline, 'cell': checked, 'PreparedMirror': PreparedStub}), \
                    patch.object(sys, 'argv', arguments), patch('sys.stdout', new_callable=io.StringIO):
                self.assertEqual(harness['main'](), 0)
            self.assertTrue(json.loads(report.read_text())['complete'])
        self.assertEqual(calls, [{'frankenphp', 'php-zts-cli', 'php-zts-fpm', 'php-zts-imagick'},
                                 {'php-zts-cli', 'php-zts-imagick'}])

    def test_affected_profiles_load_selected_instrumentation_under_runtime_hosts(self):
        names = ['php-zts-cli', 'php-zts-fpm', 'frankenphp', 'php-zts-xdebug', 'php-zts-lib-zlib']
        baseline = {'selection': {'mode': 'affected', 'complete': False},
                    'packages': [{'name': name} for name in names]}
        profiles = harness['affected_profiles'](baseline)
        common, instrumentation = profiles['profiles']
        self.assertEqual(common['packages'], ['frankenphp', 'php-zts-cli', 'php-zts-fpm'])
        self.assertEqual(instrumentation['packages'], ['frankenphp', 'php-zts-cli', 'php-zts-fpm', 'php-zts-xdebug'])
        self.assertTrue(harness['all_profiles'](baseline)['candidate_profiles'])
        with self.assertRaisesRegex(RuntimeError, 'changed packages'):
            harness['affected_profiles']({'packages': baseline['packages']})

    def test_new_optional_extension_is_exercised_with_retained_runtime_hosts(self):
        baseline = {'selection': {'mode': 'affected'}, 'packages': [{'name': 'php-zts-cli'}]}
        profiles = harness['candidate_only_profiles'](baseline, {'incoming': [{'name': 'php-zts-new-extension'}]})
        self.assertEqual(profiles, [{'id': 'fresh-php-zts-new-extension', 'fresh_package': 'php-zts-new-extension',
                                    'packages': ['frankenphp', 'php-zts-cli', 'php-zts-fpm', 'php-zts-new-extension']}])

    def test_scoped_library_profiles_keep_other_instrumentation_out(self):
        baseline = {'packages': [
            {'name': 'php-zts-cli'},
            {'name': 'php-zts-lib-base', 'provides': ['php-zts-lib-base-abi-1']},
            {'name': 'php-zts-xdebug', 'requires': ['php-zts-lib-base-abi-1']},
            {'name': 'php-zts-pcov', 'requires': ['php-zts-lib-base-abi-1']},
        ]}
        selected = harness['library_old_profile'](baseline, ['php-zts-lib-base'],
                                                 ['php-zts-cli', 'php-zts-xdebug'], expand_consumers=False)
        self.assertEqual(selected, ['php-zts-cli', 'php-zts-lib-base', 'php-zts-xdebug'])
        self.assertIn('php-zts-pcov', harness['library_old_profile'](baseline, ['php-zts-lib-base'], ['php-zts-cli']))

    def baseline(self, directory):
        root = Path(directory)
        (root / 'artifacts').mkdir()
        package = root / 'artifacts/php-zts-cli.rpm'
        package.write_bytes(b'actual published bytes')
        record = {'schema': 1, 'type': 'rpm', 'php_stream': '8.6', 'architecture': 'x86_64',
                  'origin': {'real_published': True, 'publication_url': 'https://packages.example/x86_64/el8/'},
                  'packages': [{'name': 'php-zts-cli', 'version': '8.6.0~rc4-2.el8',
                                'path': 'artifacts/php-zts-cli.rpm', 'sha256': harness['sha256'](package),
                                'publication_url': 'https://packages.example/php-zts-cli.rpm'}]}
        (root / 'baseline.json').write_text(json.dumps(record))
        return record

    def test_only_frozen_real_published_bytes_are_accepted(self):
        with tempfile.TemporaryDirectory(prefix='spp-migration-input-') as directory:
            record = self.baseline(directory)
            loaded = harness['load_baseline'](directory, 'rpm', '8.6', 'x86_64')
            self.assertEqual(loaded['packages'][0]['name'], 'php-zts-cli')
            Path(loaded['packages'][0]['source']).write_bytes(b'changed after freezing')
            with self.assertRaisesRegex(RuntimeError, 'checksum mismatch'):
                harness['load_baseline'](directory, 'rpm', '8.6', 'x86_64')
            record['origin']['real_published'] = False
            (Path(directory) / 'baseline.json').write_text(json.dumps(record))
            with self.assertRaisesRegex(RuntimeError, 'real published'):
                harness['load_baseline'](directory, 'rpm', '8.6', 'x86_64')

    def test_cross_stream_foreign_arch_and_escaping_archive_are_rejected(self):
        with tempfile.TemporaryDirectory(prefix='spp-migration-input-') as directory:
            record = self.baseline(directory)
            for stream, arch in [('8.5', 'x86_64'), ('8.6', 'arm64')]:
                with self.assertRaisesRegex(RuntimeError, 'stream/architecture'):
                    harness['load_baseline'](directory, 'rpm', stream, arch)
            record['packages'][0]['path'] = '../outside.rpm'
            (Path(directory) / 'baseline.json').write_text(json.dumps(record))
            with self.assertRaisesRegex(RuntimeError, 'escapes'):
                harness['load_baseline'](directory, 'rpm', '8.6', 'x86_64')

    def test_profile_includes_cli_without_explicit_new_library_or_moved_module(self):
        self.assertEqual(harness['profile_names']('fpm,frankenphp,intl'),
                         ['frankenphp', 'php-zts-cli', 'php-zts-fpm', 'php-zts-intl'])
        self.assertEqual(harness['profile_names']('cli,cli'), ['php-zts-cli'])
        with self.assertRaisesRegex(RuntimeError, 'Invalid'):
            harness['profile_names']('cli;echo')

    def test_full_profiles_cover_actual_extensions_and_isolate_runtime_hooks(self):
        baseline = {'selection': {'profile': 'all', 'complete': True},
                    'packages': [{'name': name} for name in ['php-zts-cli', 'php-zts-imagick',
                                  'php-zts-xdebug', 'php-zts-pcov', 'php-zts-lib-openssl', 'php-zts-cli-debuginfo']]}
        plan = harness['all_profiles'](baseline)
        covered = {name for profile in plan['profiles'] for name in profile['packages']}
        self.assertEqual(covered, {'php-zts-cli', 'php-zts-imagick', 'php-zts-xdebug', 'php-zts-pcov'})
        self.assertEqual(plan['profiles'][0]['packages'], ['php-zts-cli', 'php-zts-imagick'])
        self.assertIn({'id': 'disabled-imagick', 'packages': ['php-zts-cli', 'php-zts-imagick'],
                       'disabled_extension': 'imagick'}, plan['profiles'])
        self.assertNotIn('independent-php-zts-imagick', [p['id'] for p in plan['profiles']])

    def test_representative_baseline_uses_candidate_profile_coverage(self):
        self.assertTrue(harness['all_profiles']({'packages': [{'name': 'php-zts-cli'}]})['candidate_profiles'])

    def test_missing_old_profiles_use_the_new_native_inventory(self):
        for library_only in (False, True):
            with self.subTest(libraries=library_only):
                names = ['php-zts-lib-zlib'] if library_only else ['php-zts-cli', 'php-zts-curl', 'php-zts-fpm', 'php-zts-xdebug']
                packages = [{'name': name} for name in names]
                class Shared(PreparedStub):
                    inventory = {'incoming': packages, 'new': packages}
                calls = []
                def check(args, prepared=None):
                    calls.append(args.profile.split(','))
                    return {'ok': True, 'applicable': True}
                args = SimpleNamespace(tls=False, services=False, library_only=library_only, partial_consumers=False)
                baseline = {'packages': [], 'selection': {'mode': 'affected'}}
                with patch.dict(scope, {'PreparedMirror': Shared, 'cell': check}), patch('sys.stdout', new_callable=io.StringIO):
                    result = harness['suite'](args, baseline, {'profiles': [], 'candidate_profiles': True})
                self.assertTrue(result['ok'])
                self.assertEqual({name for selected in calls for name in selected}, set(names))
                self.assertEqual(len(calls), 1 if library_only else 2)

    def test_candidate_only_extensions_get_fresh_install_profiles_without_fake_old_packages(self):
        baseline = {'packages': [{'name': 'php-zts-cli'}, {'name': 'php-zts-imagick'}]}
        inventory = {'incoming': [{'name': name} for name in [
            'php-zts-cli', 'php-zts-imagick', 'php-zts-curl', 'php-zts-fileinfo',
            'php-zts-runtime-layout', 'php-zts-lib-grpc', 'php-zts-grpc',
            'php-zts-grpc-debuginfo', 'php-zts-devel', 'frankenphp']]}
        self.assertEqual(harness['candidate_only_profiles'](baseline, inventory),
                         [{'id': 'fresh-php-zts-grpc', 'packages': ['php-zts-grpc'],
                           'fresh_package': 'php-zts-grpc'}])
        baseline['published_packages'] = [{'name': 'php-zts-grpc'}]
        self.assertEqual(harness['candidate_only_profiles'](baseline, inventory), [])
        baseline.pop('published_packages')
        inventory['published_names'] = ['php-zts-grpc']
        self.assertEqual(harness['candidate_only_profiles'](baseline, inventory), [])

    def test_candidate_only_profile_is_appended_to_shared_mirror_suite_and_counted_separately(self):
        calls = []
        class Shared(PreparedStub):
            inventory = {'incoming': [{'name': 'php-zts-grpc'}]}
        def check(args, prepared=None):
            calls.append((args.profile, args.fresh_package))
            return {'ok': True, 'applicable': bool(args.fresh_package),
                    'coverage': 'fresh_candidate_only' if args.fresh_package else 'published_upgrade'}
        args = SimpleNamespace(tls=False, services=False, library_only=False, partial_consumers=True)
        profiles = {'profiles': [{'id': 'common', 'packages': ['php-zts-cli']}]}
        with patch.dict(scope, {'PreparedMirror': Shared, 'cell': check}), patch('sys.stdout', new_callable=io.StringIO):
            result = harness['suite'](args, {'packages': [{'name': 'php-zts-cli'}]}, profiles)
        self.assertTrue(result['ok'])
        self.assertEqual(calls, [('php-zts-cli', None), ('php-zts-grpc', 'php-zts-grpc')])
        self.assertEqual(result['fresh_install_packages'], ['php-zts-grpc'])
        self.assertEqual(result['cells'][1]['coverage'], 'fresh_candidate_only')
        self.assertEqual(len(profiles['profiles']), 1)


    def test_prepared_mirror_refuses_changed_archive_mode_runtime_or_file_set(self):
        with tempfile.TemporaryDirectory(prefix='spp-prepared-inputs-') as directory:
            root = Path(directory)
            self.baseline(root)
            candidates = root / 'candidates'
            candidates.mkdir()
            package = candidates / 'candidate.rpm'
            package.write_bytes(b'candidate')
            args = harness['options'](SimpleNamespace(type='rpm', php='8.6', arch='x86_64', runtime='alma8',
                                      profile='cli', baseline_dir=root, artifacts=candidates))
            mirror = harness['PreparedMirror'](args)
            try:
                mirror.ledger()
                mirror.assert_bound(args)
                for name, value in [('runtime', 'alma9'), ('php', '8.5'), ('partial_consumers', True), ('allow_emulation', True)]:
                    other = SimpleNamespace(**vars(args))
                    setattr(other, name, value)
                    with self.assertRaises(harness['PreparedInputError']):
                        mirror.assert_bound(other)
                memory = mirror.resources.bounds['client']['memory_mib']
                mirror.resources.bounds['client']['memory_mib'] = memory + 128
                with self.assertRaisesRegex(harness['PreparedInputError'], 'resource bounds changed'):
                    mirror.assert_bound(args)
                mirror.resources.bounds['client']['memory_mib'] = memory
                extra = candidates / 'new.rpm'
                extra.write_bytes(b'new archive')
                with self.assertRaisesRegex(harness['PreparedInputError'], 'archive set changed'):
                    mirror.assert_bound(args)
                extra.unlink()
                package.write_bytes(b'changed!!')
                with self.assertRaisesRegex(harness['PreparedInputError'], 'source bytes/identity changed'):
                    mirror.assert_bound(args)
                with self.assertRaisesRegex(harness['PreparedInputError'], 'changed while copying'):
                    mirror.copy_checked(package, mirror.scratch / 'copied.rpm')
            finally:
                self.assertTrue(mirror.close()['complete'])

    def test_full_suite_prepares_once_and_incomplete_shared_cleanup_fails(self):
        calls = {'prepare': 0, 'close': 0, 'profiles': []}
        class Shared(PreparedStub):
            def prepare(self):
                calls['prepare'] += 1
            def close(self):
                calls['close'] += 1
                return {'complete': False, 'remaining': ['own-image']}
        def check(args, prepared=None):
            self.assertIsInstance(prepared, Shared)
            calls['profiles'].append((args.profile, args.library_artifacts))
            return {'ok': True, 'applicable': True}
        args = SimpleNamespace(tls=False, services=False, library_only=False, partial_consumers=False,
                               library_artifacts=Path('/prepared-third-generation'))
        profiles = {'profiles': [{'id': 'common', 'packages': ['php-zts-cli']},
                                 {'id': 'independent', 'packages': ['php-zts-cli', 'php-zts-imagick']}]}
        with patch.dict(scope, {'PreparedMirror': Shared, 'cell': check}), patch('sys.stdout', new_callable=io.StringIO):
            result = harness['suite'](args, {}, profiles)
        self.assertEqual((calls['prepare'], calls['close']), (1, 1))
        self.assertEqual(len(calls['profiles']), 2)
        self.assertTrue(all(p[1] == args.library_artifacts for p in calls['profiles']))
        self.assertFalse(result['ok'])
        self.assertFalse(result['cleanup']['complete'])

    def test_prepared_mirror_binds_engine_identity_and_execution_mode(self):
        with tempfile.TemporaryDirectory(prefix='spp-prepared-engine-') as directory:
            root = Path(directory)
            self.baseline(root)
            candidates = root / 'candidates'
            candidates.mkdir()
            (candidates / 'candidate.rpm').write_bytes(b'candidate')
            args = harness['options'](SimpleNamespace(type='rpm', php='8.6', arch='arm64', runtime='alma8',
                                      profile='cli', baseline_dir=root, artifacts=candidates, allow_emulation=True))
            baseline = json.loads((root / 'baseline.json').read_text())
            baseline['architecture'] = 'arm64'
            (root / 'baseline.json').write_text(json.dumps(baseline))
            engine = {'id': 'first-engine'}
            def command(arguments, **kwargs):
                value = 'x86_64' if arguments[-1] == '{{.Architecture}}' else engine['id']
                return subprocess.CompletedProcess(arguments, 0, value, '')
            mirror = harness['PreparedMirror'](args)
            try:
                with patch.dict(scope, {'run': command}), patch.dict('os.environ', {'GITHUB_ACTIONS': 'false'}):
                    mirror.resources.check_native()
                    mirror.ledger()
                    mirror.assert_bound(args)
                    self.assertFalse(mirror.report['ledger']['execution']['native'])
                    self.assertTrue(mirror.report['ledger']['execution']['emulated'])
                    engine['id'] = 'replacement-engine'
                    with self.assertRaisesRegex(harness['PreparedInputError'], 'engine/execution mode changed'):
                        mirror.assert_bound(args)
            finally:
                self.assertTrue(mirror.close()['complete'])

    def test_failed_client_cleanup_stops_profile_reuse_until_shared_cleanup(self):
        calls = []
        args = SimpleNamespace(tls=False, services=False, library_only=False, partial_consumers=False)
        profiles = {'profiles': [{'id': 'common', 'packages': ['php-zts-cli']},
                                 {'id': 'next', 'packages': ['php-zts-cli', 'php-zts-imagick']}]}
        def failed_client(args, prepared=None):
            calls.append(args.profile)
            return {'ok': False, 'cleanup': {'complete': False, 'remaining': ['own-client']}}
        with patch.dict(scope, {'PreparedMirror': PreparedStub, 'cell': failed_client}), patch('sys.stdout', new_callable=io.StringIO):
            result = harness['suite'](args, {}, profiles)
        self.assertEqual(calls, ['php-zts-cli'])
        self.assertFalse(result['ok'])
        self.assertTrue(result['cleanup']['complete'])

    def test_interrupt_stops_remaining_full_profile_groups_and_preserves_failure_report(self):
        baseline = {'selection': {'profile': 'all', 'complete': True},
                    'packages': [{'name': 'php-zts-cli'}, {'name': 'php-zts-xdebug'}]}
        calls = []
        def interrupted(args, prepared=None):
            calls.append(args.profile)
            return {'ok': False, 'interrupted': True, 'cleanup': {'complete': True}}
        with tempfile.TemporaryDirectory(prefix='spp-migration-interrupt-') as directory:
            report = Path(directory) / 'report.json'
            arguments = ['migration-upgrade-containers.py', '--type', 'rpm', '--php', '8.6', '--arch', 'x86_64',
                         '--baseline-dir', '/unused', '--artifacts', '/unused', '--profile', 'all', '--report', str(report)]
            with patch.dict(scope, {'load_baseline': lambda *args: baseline, 'cell': interrupted, 'PreparedMirror': PreparedStub}), patch.object(sys, 'argv', arguments), patch('sys.stdout', new_callable=io.StringIO):
                self.assertEqual(harness['main'](), 1)
            self.assertEqual(calls, ['php-zts-cli'])
            self.assertTrue(json.loads(report.read_text())['interrupted'])

    def test_unaffected_profiles_do_not_claim_a_successful_partial_update(self):
        baseline = {'selection': {'profile': 'all', 'complete': True}, 'packages': [{'name': 'php-zts-cli'}]}
        with tempfile.TemporaryDirectory(prefix='spp-migration-unaffected-') as directory:
            report = Path(directory) / 'report.json'
            arguments = ['migration-upgrade-containers.py', '--type', 'rpm', '--php', '8.6', '--arch', 'x86_64',
                         '--baseline-dir', '/unused', '--artifacts', '/unused', '--profile', 'all', '--partial-consumers', '--report', str(report)]
            with patch.dict(scope, {'load_baseline': lambda *args: baseline,
                                   'cell': lambda *args, **kwargs: {'ok': True, 'applicable': False}, 'PreparedMirror': PreparedStub}), patch.object(sys, 'argv', arguments), patch('sys.stdout', new_callable=io.StringIO):
                self.assertEqual(harness['main'](), 1)
            self.assertFalse(json.loads(report.read_text())['applicable'])

    def test_successful_package_manager_exit_cannot_hide_held_packages(self):
        before = {'php-zts-cli': '1-7.el10', 'php-zts-imagick': '1-4.el10', 'unrelated-os-package': '1'}
        candidates = {'php-zts-cli': {'version': '1-8'}, 'php-zts-imagick': {'version': '1-5'}}
        for after in [dict(before), {'php-zts-cli': '1-8', 'php-zts-imagick': '1-4.el10'}]:
            with self.assertRaisesRegex(RuntimeError, 'held back'):
                harness['assert_upgrade'](before, after, candidates, ['php-zts-cli', 'php-zts-imagick'])
        harness['assert_upgrade'](before, {'php-zts-cli': '1-8', 'php-zts-imagick': '1-5'},
                                  candidates, ['php-zts-cli', 'php-zts-imagick'])
        with self.assertRaisesRegex(RuntimeError, 'no migration candidate'):
            harness['assert_upgrade'](before, before, {'php-zts-cli': candidates['php-zts-cli']}, ['php-zts-imagick'])

    def test_previously_installed_unselected_modules_cannot_disappear(self):
        before = {'php-zts-cli': '1-1', 'php-zts-redis': '1-1'}
        with self.assertRaisesRegex(RuntimeError, 'removed installed packages'):
            harness['assert_upgrade'](before, {'php-zts-cli': '1-2'},
                                      {'php-zts-cli': {'version': '1-2'}}, ['php-zts-cli'])

    def test_native_library_profile_has_no_artificial_php_dependency(self):
        names = ['php-zts-lib-zlib', 'php-zts-runtime-layout']
        baseline = {'selection': {'mode': 'affected'}, 'packages': [{'name': name} for name in names]}
        profiles = harness['affected_profiles'](baseline)
        self.assertEqual(profiles['profiles'], [{'id': 'libraries', 'packages': names}])
        self.assertEqual(harness['profile_names'](','.join(names), require_php=False), names)
        self.assertIn('php-zts-cli', harness['profile_names']('curl'))

    def test_library_only_update_advances_library_without_rebuilding_php(self):
        before = {'php-zts-cli': '8.6-9', 'php-zts-imagick': '3.8-4', 'php-zts-lib-openssl': '3.6-1'}
        incoming = {'php-zts-lib-openssl': {'version': '3.6-2'}}
        after = dict(before, **{'php-zts-lib-openssl': '3.6-2'})
        self.assertEqual(harness['assert_library_upgrade'](before, after, incoming), ['php-zts-lib-openssl'])
        with self.assertRaisesRegex(RuntimeError, 'held back library'):
            harness['assert_library_upgrade'](before, before, incoming)
        before['php-zts-runtime-layout'] = '1-1'
        after['php-zts-runtime-layout'] = '2-1'
        incoming['php-zts-runtime-layout'] = {'version': '2-1'}
        self.assertEqual(harness['assert_library_upgrade'](before, after, incoming), ['php-zts-lib-openssl'])
        after['php-zts-imagick'] = '3.8-5'
        with self.assertRaisesRegex(RuntimeError, 'changed a PHP consumer'):
            harness['assert_library_upgrade'](before, after, incoming)

    def test_static_baseline_cannot_claim_a_library_only_update(self):
        with self.assertRaisesRegex(RuntimeError, 'shared baseline'):
            harness['assert_library_upgrade']({'php-zts-cli': '8.6-1'}, {'php-zts-cli': '8.6-1'},
                                              {'php-zts-lib-openssl': {'version': '3.6-2'}})

    def test_library_old_profile_adds_optional_provider_and_its_real_consumers_only(self):
        baseline = {'packages': [
            {'name': 'php-zts-cli', 'requires': [], 'provides': ['php-zts']},
            {'name': 'php-zts-lib-gearman', 'requires': [], 'provides': ['php-zts-lib-gearman-abi-1']},
            {'name': 'php-zts-lib-wrapper', 'requires': ['php-zts-lib-gearman-abi-1'], 'provides': ['php-zts-lib-wrapper-abi-1']},
            {'name': 'php-zts-gearman', 'requires': ['php-zts-cli', 'php-zts-lib-wrapper-abi-1'], 'provides': []},
            {'name': 'php-zts-imagick', 'requires': ['php-zts-cli'], 'provides': []},
        ]}
        self.assertEqual(harness['library_old_profile'](baseline, ['php-zts-lib-gearman'], ['php-zts-cli']),
                         ['php-zts-cli', 'php-zts-gearman', 'php-zts-lib-gearman', 'php-zts-lib-wrapper'])
        with self.assertRaisesRegex(RuntimeError, 'affected provider'):
            harness['library_old_profile'](baseline, ['php-zts-lib-not-published'], ['php-zts-cli'])

    def test_library_layout_obeys_the_same_native_version_and_identity_guards(self):
        with tempfile.TemporaryDirectory(prefix='spp-migration-layout-') as directory:
            old, new = Path(directory) / 'old.rpm', Path(directory) / 'new.rpm'
            old.write_bytes(b'unchanged signed layout archive')
            new.write_bytes(old.read_bytes())
            baseline = [{'name': 'php-zts-runtime-layout', 'version': '1-2', 'path': str(old)}]
            incoming = [{'name': 'php-zts-runtime-layout', 'version': '1-2', 'path': str(new)}]
            harness['validate_library_only_candidates'](incoming)
            harness['validate_partial_candidates']('deb', baseline, incoming)
            new.write_bytes(b'different bytes at the same version')
            with self.assertRaisesRegex(RuntimeError, 'Equal-version partial candidate'):
                harness['validate_partial_candidates']('deb', baseline, incoming)
            incoming[0]['version'] = '2-1'
            with patch.dict(scope, {'newer': lambda *args: True}):
                self.assertEqual(harness['validate_partial_candidates']('deb', baseline, incoming), ['php-zts-runtime-layout'])
            incoming[0]['version'] = '1-1'
            with patch.dict(scope, {'newer': lambda *args: False}):
                with self.assertRaisesRegex(RuntimeError, 'lower than the frozen version'):
                    harness['validate_partial_candidates']('deb', baseline, incoming)
            with self.assertRaisesRegex(RuntimeError, 'only libraries'):
                harness['validate_library_only_candidates']([{'name': 'php-zts-cli'}])

    def test_partial_extension_update_keeps_core_and_other_modules_exact(self):
        before = {'php-zts-cli': '8.6-8', 'php-zts-imagick': '3.8-3', 'php-zts-intl': '8.6-2'}
        incoming = {'php-zts-imagick': {'version': '3.8-4'}}
        after = dict(before, **{'php-zts-imagick': '3.8-4'})
        self.assertEqual(harness['assert_partial_upgrade'](before, after, incoming, ['php-zts-imagick']), ['php-zts-imagick'])
        after['php-zts-cli'] = '8.6-9'
        with self.assertRaisesRegex(RuntimeError, 'changed an unchanged package'):
            harness['assert_partial_upgrade'](before, after, incoming, ['php-zts-imagick'])
        with self.assertRaisesRegex(RuntimeError, 'No affected installed'):
            harness['assert_partial_upgrade'](before, before, incoming, [])

    def test_equal_version_partial_candidate_must_be_the_exact_frozen_archive(self):
        with tempfile.TemporaryDirectory(prefix='spp-migration-partial-') as directory:
            old, new = Path(directory) / 'old.rpm', Path(directory) / 'new.rpm'
            old.write_bytes(b'frozen signed shared extension')
            new.write_bytes(old.read_bytes())
            previous = [{'name': 'php-zts-imagick', 'version': '3.8-3', 'path': str(old)}]
            incoming = [{'name': 'php-zts-imagick', 'version': '3.8-3', 'path': str(new)}]
            self.assertEqual(harness['validate_partial_candidates']('rpm', previous, incoming), [])
            new.write_bytes(b'new bytes at an unchanged version')
            with self.assertRaisesRegex(RuntimeError, 'Equal-version partial candidate differs'):
                harness['validate_partial_candidates']('rpm', previous, incoming)
            incoming[0]['version'] = '3.8-4'
            with patch.dict(scope, {'newer': lambda *args: True}):
                self.assertEqual(harness['validate_partial_candidates']('rpm', previous, incoming), ['php-zts-imagick'])
            with patch.dict(scope, {'newer': lambda *args: False}):
                with self.assertRaisesRegex(RuntimeError, 'lower than the frozen'):
                    harness['validate_partial_candidates']('rpm', previous, incoming)

    def test_disabled_module_rename_user_edit_and_reenable_are_verified(self):
        with tempfile.TemporaryDirectory(prefix='spp-migration-disabled-') as directory:
            root = Path(directory)
            (root / 'conf.d').mkdir()
            ini = root / 'php.ini'
            ini.write_text('; packaged main config\n')
            active = root / 'conf.d/30-imagick.ini'
            active.write_text('extension=imagick-zts-86\n')
            def mapped(value):
                return root / 'absent-tool' if str(value) == '/usr/sbin/phpdismod-zts' else Path(value)
            with patch.dict(scope, {'Path': mapped}):
                state = harness['disable_shared_extension']('imagick', str(ini), {'extensions': ['standard', 'imagick']})
            self.assertFalse(active.exists())
            disabled = Path(state['disabled_ini'])
            self.assertIn('user-edited disabled-module configuration', disabled.read_text())
            harness['assert_disabled_extension'](state, {'extensions': ['standard']})
            with self.assertRaisesRegex(RuntimeError, 're-enabled'):
                harness['assert_disabled_extension'](state, {'extensions': ['standard', 'imagick']})
            active.write_text('extension=imagick-zts-86\n')
            with self.assertRaisesRegex(RuntimeError, 're-enabled'):
                harness['assert_disabled_extension'](state, {'extensions': ['standard']})
            active.unlink()
            disabled.write_text('overwritten user configuration')
            with self.assertRaisesRegex(RuntimeError, 'user-disabled extension'):
                harness['assert_disabled_extension'](state, {'extensions': ['standard']})

    def test_upgrade_commands_never_explicitly_install_new_dependencies_or_override_solver(self):
        expected = {'rpm': [['dnf', 'upgrade', '-y']],
                    'deb': [['apt-get', 'update'], ['apt', 'upgrade', '-y']],
                    'apk': [['apk', 'update', '--cache-max-age', '0'], ['apk', 'upgrade']]}
        for kind in expected:
            calls = []
            with patch.dict(scope, {'run': lambda command, **kwargs: calls.append((command, kwargs))}):
                harness['ordinary_upgrade'](kind)
            self.assertEqual([command for command, _ in calls], expected[kind])
            if kind == 'deb':
                self.assertTrue(calls[-1][1]['input'].strip() == '')

    def test_native_arm64_validation_does_not_accept_emulation(self):
        result = subprocess.CompletedProcess([], 0, 'x86_64\n', '')
        with tempfile.TemporaryDirectory(prefix='spp-migration-native-') as directory:
            resources = harness['Docker'](directory, 'arm64')
            with patch.dict(scope, {'run': lambda *args, **kwargs: result}):
                with self.assertRaisesRegex(RuntimeError, 'native arm64 Docker engine'):
                    resources.check_native()
            self.assertEqual(resources.containers, [])
            self.assertIsNone(resources.network)

    def test_apk_header_reader_handles_signature_and_control_gzip_members(self):
        def member(name, content):
            output = io.BytesIO()
            with tarfile.open(fileobj=output, mode='w') as archive:
                info = tarfile.TarInfo(name)
                info.size = len(content)
                archive.addfile(info, io.BytesIO(content))
            return gzip.compress(output.getvalue())
        with tempfile.TemporaryDirectory(prefix='spp-migration-apk-') as directory:
            path = Path(directory) / 'php.apk'
            path.write_bytes(member('.SIGN.RSA.example.rsa.pub', b'signature') +
                             member('.PKGINFO', b'pkgname = php-zts-cli\npkgver = 8.6.0_rc4-r3\narch = aarch64\n'))
            package = harness['inspect_package']('apk', path)
            self.assertEqual((package['name'], package['version'], package['architecture']),
                             ('php-zts-cli', '8.6.0_rc4-r3', 'arm64'))

    def test_historical_cached_files_remain_available_without_replacing_new_metadata(self):
        with tempfile.TemporaryDirectory(prefix='spp-migration-history-') as directory:
            old, new = Path(directory) / 'old', Path(directory) / 'new'
            for path in (old, new):
                (path / 'repodata').mkdir(parents=True)
            (old / 'old.el10.rpm').write_bytes(b'old package')
            (old / 'repodata/oldhash.xml').write_bytes(b'cached old metadata')
            (old / 'repodata/repomd.xml').write_bytes(b'old root')
            (new / 'repodata/repomd.xml').write_bytes(b'new root')
            harness['copy_historical'](old, new)
            self.assertEqual((new / 'old.el10.rpm').read_bytes(), b'old package')
            self.assertEqual((new / 'repodata/oldhash.xml').read_bytes(), b'cached old metadata')
            self.assertEqual((new / 'repodata/repomd.xml').read_bytes(), b'new root')

    def test_library_generation_reuses_only_finalized_apk_and_deb_archives(self):
        for kind in ('apk', 'deb', 'rpm'):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory(prefix='spp-library-generation-') as directory:
                root = Path(directory)
                current = root / 'current'
                current.mkdir()
                incoming = root / 'incoming'
                incoming.mkdir()
                common = current / ('php-zts-cli.' + kind)
                old_library = current / ('old-zlib.' + kind)
                new_library = incoming / ('new-zlib.' + kind)
                for path in (common, old_library, new_library):
                    path.write_bytes(path.name.encode())
                generation = root / 'library'
                harness['stage_library_generation'](kind,
                    [{'name': 'php-zts-cli', 'path': common}, {'name': 'php-zts-lib-zlib', 'path': old_library}],
                    [{'name': 'php-zts-lib-zlib', 'path': new_library}], generation)
                self.assertEqual((generation / common.name).read_bytes(), common.read_bytes())
                self.assertEqual((generation / common.name).stat().st_ino == common.stat().st_ino, kind in ('apk', 'deb'))
                self.assertFalse((generation / old_library.name).exists())
                self.assertEqual((generation / new_library.name).read_bytes(), new_library.read_bytes())
                self.assertNotEqual((generation / new_library.name).stat().st_ino, new_library.stat().st_ino)

    def test_rpm_mirror_signer_rejects_originals_symlinks_and_shared_inodes(self):
        with tempfile.TemporaryDirectory(prefix='spp-rpm-mirror-sign-') as directory:
            root = Path(directory)
            mirror = root / 'mirror'
            mirror.mkdir()
            original = root / 'original.rpm'
            original.write_bytes(b'immutable published fixture')
            isolated = mirror / 'isolated.rpm'
            isolated.write_bytes(original.read_bytes())
            shared = mirror / 'hardlink.rpm'
            os.link(original, shared)
            symlink = mirror / 'symlink.rpm'
            symlink.symlink_to(isolated)
            calls = []
            with patch.dict(scope, {'RPM_MIRROR': mirror, 'run': lambda command, **kwargs: calls.append(command)}):
                for path in (original, shared, symlink):
                    with self.assertRaisesRegex(RuntimeError, 'isolated scratch archive'):
                        harness['sign_rpm_for_mirror'](path, 'test-key')
                self.assertEqual(calls, [])
                harness['sign_rpm_for_mirror'](isolated, 'test-key')
            self.assertEqual(calls[0], ['rpmsign', '--delsign', isolated])
            self.assertEqual(calls[1][-2:], ['--addsign', isolated])
            self.assertEqual(calls[2], ['rpmkeys', '--checksig', isolated])
            self.assertEqual(original.read_bytes(), b'immutable published fixture')

    def test_tls_and_service_hooks_share_signed_generations_and_fail_closed(self):
        with tempfile.TemporaryDirectory(prefix='spp-migration-hooks-') as directory:
            root = Path(directory)
            for name in ('old', 'new', 'library'):
                (root / 'repository/generations' / name).mkdir(parents=True)
            (root / 'public-key').mkdir()
            (root / 'public-key/repository.asc').write_text('public test key')
            args = SimpleNamespace(type='rpm', php='8.6', arch='arm64', runtime='alma8', tls=True, services=True,
                                   allow_emulation=True)
            calls = []
            def checked(command, **kwargs):
                calls.append(command)
                Path(command[-1]).write_text(json.dumps({'state': 'passed', 'complete': True}))
                return subprocess.CompletedProcess(command, 0, '', '')
            result = {}
            with patch.dict(scope, {'run': checked}):
                harness['lifetime_hooks'](args, root, result)
            self.assertEqual([r['state'] for r in result.values()], ['passed', 'passed'])
            self.assertIn(str(root / 'repository/generations/library'), calls[1])
            for command in calls:
                self.assertIn('--allow-emulation', command)
                self.assertEqual(command[command.index('--arch') + 1], 'arm64')
            def failed(command, **kwargs):
                Path(command[-1]).write_text(json.dumps({'state': 'failed'}))
                return subprocess.CompletedProcess(command, 1, 'native trust failed', '')
            with patch.dict(scope, {'run': failed}):
                with self.assertRaisesRegex(RuntimeError, 'trust checks failed'):
                    harness['lifetime_hooks'](args, root, {})

    def test_a_lifetime_hook_failure_cannot_leave_a_successful_cell_receipt(self):
        with tempfile.TemporaryDirectory(prefix='spp-migration-hook-failure-') as directory:
            root = Path(directory)
            frozen = self.baseline(root)
            candidates = root / 'candidates'
            candidates.mkdir()
            (candidates / 'new.rpm').write_bytes(b'candidate fixture')
            instances = []
            class FakeDocker:
                def __init__(self, directory, arch, allow_emulation=False):
                    self.directory = Path(directory)
                    self.token = 'own-fixture'
                    self.images, self.containers = [], []
                    self.execution = {'native': True, 'emulated': False}
                    self.runtime_evidence = []
                    self.bounds = {'repo': {}, 'client': {}}
                    instances.append(self)
                def check_native(self):
                    return self.execution
                def start(self, image, role):
                    self.containers.append(role)
                    self.runtime_evidence.append({'role': role})
                    return role
                def exec(self, name, command, **kwargs):
                    if '--inside' in command:
                        stage = command[-1]
                        if stage == 'index':
                            (self.directory / 'inventory.json').write_text(json.dumps({'old': [
                                {'name': 'php-zts-cli', 'version': '0:' + frozen['packages'][0]['version']}]}))
                        if stage == 'old':
                            (self.directory / 'client-before.json').write_text(json.dumps({
                                'installed': {}, 'probe': {}, 'module_state': None, 'config_sha256': 'a',
                                'key_sha256': 'b', 'php_ini_sha256': 'c'}))
                        if stage == 'new':
                            return subprocess.CompletedProcess(command, 0, 'SPP_MIGRATION_RESULT={"ok":true}', '')
                    return subprocess.CompletedProcess(command, 0, '', '')
                def close(self):
                    return []
            args = SimpleNamespace(type='rpm', php='8.6', arch='x86_64', runtime='alma8', profile='cli',
                                   baseline_dir=root, artifacts=candidates, tls=True, services=False)
            def failed_hook(*arguments):
                self.assertFalse(any(name.startswith('client-') for name in instances[0].containers))
                raise RuntimeError('native TLS failure')
            with patch.dict(scope, {'Docker': FakeDocker, 'prepare': lambda *args, **kwargs: None,
                                   'run': lambda *args, **kwargs: subprocess.CompletedProcess([], 0, 'sha256:' + 'a' * 64, ''),
                                   'lifetime_hooks': failed_hook}):
                result = harness['cell'](args)
            self.assertFalse(result['ok'])
            self.assertIn('native TLS failure', result['error'])
            self.assertTrue(result['cleanup']['complete'])

    def test_apk_hook_view_preserves_architecture_appending_and_index_bytes(self):
        with tempfile.TemporaryDirectory(prefix='spp-migration-apk-view-') as directory:
            root = Path(directory)
            source = root / 'repository/generations/new'
            source.mkdir(parents=True)
            (source / 'APKINDEX.tar.gz').write_bytes(b'existing signed index')
            (source / 'php.apk').write_bytes(b'unchanged source APK')
            view = harness['hook_repository'](root, 'apk', 'new', 'arm64')
            self.assertEqual((view / 'aarch64/APKINDEX.tar.gz').read_bytes(), b'existing signed index')
            self.assertEqual((view / 'aarch64/php.apk').read_bytes(), b'unchanged source APK')

    def test_cleanup_only_names_registered_task_resources(self):
        with tempfile.TemporaryDirectory(prefix='spp-migration-cleanup-') as directory:
            resources = harness['Docker'](directory, 'x86_64')
            resources.containers = ['own-client', 'own-repo']
            resources.network = 'own-network'
            resources.images = ['new-task-image']
            calls = []
            def fake(command, **kwargs):
                calls.append(command)
                return subprocess.CompletedProcess(command, 0, '', '')
            with patch.dict(scope, {'run': fake}):
                self.assertEqual(resources.close(), [])
            self.assertNotIn('prune', ' '.join(' '.join(x) for x in calls))
            self.assertEqual([c[3] for c in calls if c[:3] == ['docker', 'rm', '-f']], ['own-repo', 'own-client'])
            self.assertIn(['docker', 'network', 'rm', 'own-network'], calls)
            self.assertIn(['docker', 'image', 'rm', 'new-task-image'], calls)


if __name__ == '__main__':
    unittest.main()
