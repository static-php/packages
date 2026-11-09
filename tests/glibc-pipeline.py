#!/usr/bin/env python3
"""Integrity and job selection checks for the single EL8 producer pipeline."""
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from threading import Barrier
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
HELPERS = ROOT / '.github/actions/shared-libraries'


def module(name):
    spec = importlib.util.spec_from_file_location(name, HELPERS / (name + '.py'))
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


workflow = module('workflow')
glibc = module('glibc')
archives = module('payload-archives')
runtime = module('runtime-checks')


class Pipeline(unittest.TestCase):
    def test_explicit_iteration_cannot_silently_disable_bumping(self):
        with patch.dict(os.environ, ITERATION='5', BUMP='true'):
            with self.assertRaisesRegex(RuntimeError, 'Choose bump=true or an explicit iteration'):
                glibc.revision_flags()
            result = subprocess.run(['bash', str(HELPERS / 'run.sh'), 'package'], capture_output=True, text=True)
            self.assertEqual(result.returncode, 1)
            self.assertIn('Choose bump=true or an explicit iteration', result.stderr)
        for iteration, bump, expected in [('', 'true', ['--bump']), ('5', 'false', ['--iteration=5']), ('', 'false', [])]:
            with patch.dict(os.environ, ITERATION=iteration, BUMP=bump):
                self.assertEqual(glibc.revision_flags(), expected)

    def test_complete_repackage_can_recover_a_broken_shared_baseline(self):
        baseline = {'packages': [{'name': 'php-zts-cli', 'requires': ['php-zts-lib-openssl']}]}
        self.assertEqual(workflow.upgrade_mode(baseline, {'php_requests': {'8.5': {'full': True}}}, '8.5'), 'full')
        self.assertEqual(workflow.upgrade_mode(baseline, {'php_requests': {'8.5': {'packages': ['curl']}}}, '8.5'), 'shared')
        self.assertEqual(workflow.upgrade_mode({'packages': [{'name': 'php-zts-cli'}]}, {}, '8.5'), 'static')

    def test_native_inventory_capture_skips_unpublished_plans_and_isolates_stream_failures(self):
        freezer = glibc.runpy.run_path(str(ROOT / 'bin/freeze-migration-baseline.py'))
        captured = []
        def snapshot(args):
            captured.append((args.type, args.php))
            if args.php == '8.6':
                raise RuntimeError('HTTP 522 for this stream')
            return {'schema': 1, 'type': args.type, 'php_stream': args.php, 'architecture': args.arch}
        freezer['snapshot_inventory'] = snapshot
        with tempfile.TemporaryDirectory() as directory:
            stage = Path(directory)
            plan = {'php_requests': {stream: {'full': True} for stream in ('8.4', '8.5', '8.6')}}
            glibc.write(stage / 'rpm/shared-library-plan.json', plan)
            glibc.write(stage / 'deb/shared-library-plan.json', dict(plan, build_only=True))
            with patch.object(glibc, 'STAGE', stage), patch.object(glibc.runpy, 'run_path', return_value=freezer), \
                    patch.dict(os.environ, TYPE='rpm', MATRIX_ARCH='x86_64', PHP_VERSIONS='8.5,8.6'):
                glibc.capture_native_inventories()
            self.assertEqual(captured, [('rpm', '8.5'), ('rpm', '8.6')])
            snapshots = stage / 'common/rpm/native-inventories'
            self.assertNotIn('error', glibc.read(snapshots / '8.5.json'))
            self.assertIn('HTTP 522', glibc.read(snapshots / '8.6.json')['error'])
            self.assertEqual(list((stage / 'common/deb/native-inventories').iterdir()), [])

    def test_abi_consumer_scope_uses_actual_stream_inventory_and_preserves_explicit_requests(self):
        plan = {'type': 'apk', 'php_requests': {'8.6': {'full': False, 'packages': ['cli', 'mysqlnd_parsec', 'swoole'],
                'compile_packages': ['cli', 'mysqlnd_parsec', 'swoole'], 'compile_full': True}},
                'abi_consumer_requests': {'8.6': {'full': False,
                    'consumers': ['php-zts-cli', 'php-zts-mysqlnd_parsec', 'php-zts-swoole'],
                    'packages': ['cli', 'mysqlnd_parsec', 'swoole']}},
                'expected_consumers': {'8.6': ['php-zts-cli', 'php-zts-mysqlnd_parsec', 'php-zts-swoole']}}
        snapshot = {'type': 'apk', 'php_stream': '8.6', 'architecture': 'arm64', 'inventory': [
            {'name': 'php-zts-cli', 'architecture': 'aarch64'},
            {'name': 'php-zts-mysqlnd_parsec', 'architecture': 'x86_64'}]}
        original = copy.deepcopy(plan)
        self.assertFalse(glibc.scope_abi_consumers(plan, dict(snapshot, error='offline'), '8.6'))
        self.assertEqual(plan, original)
        self.assertTrue(glibc.scope_abi_consumers(plan, snapshot, '8.6', ['swoole']))
        self.assertEqual(plan['php_requests']['8.6']['packages'], ['cli', 'swoole'])
        self.assertEqual(plan['php_requests']['8.6']['compile_packages'], ['cli', 'swoole'])
        self.assertEqual(plan['expected_consumers']['8.6'], ['php-zts-cli'])
        self.assertEqual(plan['abi_consumer_requests']['8.6']['packages'], ['cli'])
        self.assertTrue(plan['php_requests']['8.6']['compile_full'])

    def test_native_inventory_refinement_updates_format_and_common_plans_before_receipts(self):
        run_path = glibc.runpy.run_path
        freezer = glibc.runpy.run_path(str(ROOT / 'bin/freeze-migration-baseline.py'))
        freezer['snapshot_inventory'] = lambda args: {'type': args.type, 'php_stream': args.php,
            'architecture': args.arch, 'inventory': [{'name': 'php-zts-cli', 'architecture': args.arch}]}
        with tempfile.TemporaryDirectory() as temporary:
            stage = Path(temporary)
            for kind in ('rpm', 'deb'):
                glibc.write(stage / kind / 'shared-library-plan.json', {'schema': 1, 'type': kind, 'abi_break': True,
                    'candidate_fingerprint': 'compiled-inputs', 'baseline_fingerprint': 'captured-baseline',
                    'php_requests': {'8.6': {'full': False, 'packages': ['cli', 'swoole'], 'compile': True, 'compile_full': True}},
                    'expected_consumers': {'8.6': ['php-zts-cli', 'php-zts-swoole']},
                    'abi_consumer_requests': {'8.6': {'full': False, 'consumers': ['php-zts-cli', 'php-zts-swoole'], 'packages': ['cli', 'swoole']}}})
            with patch.object(glibc, 'STAGE', stage), patch.object(glibc.runpy, 'run_path', side_effect=lambda path: freezer if str(path).endswith('freeze-migration-baseline.py') else run_path(path)), \
                    patch.dict(os.environ, TYPE='rpm', MATRIX_ARCH='arm64', PHP_VERSIONS='8.6', PACKAGES=''):
                glibc.capture_native_inventories()
            common = glibc.read(stage / 'common/shared-library-plan.json')
            self.assertEqual(common['php_requests']['8.6']['packages'], ['cli'])
            for kind in ('rpm', 'deb'):
                actual = glibc.read(stage / kind / 'shared-library-plan.json')
                self.assertEqual(actual, glibc.read(stage / 'common' / kind / 'shared-library-plan.json'))
                self.assertEqual(actual['candidate_fingerprint'], 'compiled-inputs')
                self.assertEqual(actual['baseline_fingerprint'], 'captured-baseline')
                self.assertEqual(actual['expected_consumers']['8.6'], ['php-zts-cli'])
                self.assertEqual(common['format_selection'][kind]['php_requests']['8.6']['packages'], ['cli'])

    def test_native_bindings_add_stream_specific_consumers_and_skip_completed_abi_rollouts(self):
        plan = {'type': 'apk', 'libraries': ['openssl'], 'abi_rebuild_roots': ['openssl'],
                'expected_libraries': {'openssl': {'abi_capability': 'php-zts-lib-openssl-abi-2'}},
                'abi_core_rebuild_streams': ['8.4'],
                'php_requests': {'8.4': {'full': False, 'packages': ['cli', 'curl', 'mysqlnd_parsec'], 'compile_full': True}},
                'expected_consumers': {'8.4': ['php-zts-cli', 'php-zts-curl', 'php-zts-mysqlnd_parsec']},
                'abi_consumer_requests': {'8.4': {'full': False, 'consumers': ['php-zts-cli', 'php-zts-curl', 'php-zts-mysqlnd_parsec'],
                    'packages': ['cli', 'curl', 'mysqlnd_parsec']}}}
        snapshot = {'type': 'apk', 'php_stream': '8.4', 'architecture': 'arm64', 'inventory': [
            {'name': 'php-zts-lib-openssl', 'version': '3.6.5-r1', 'provides': ['php-zts-lib-openssl-abi-1']},
            {'name': 'php-zts-cli', 'version': '8.4.27-r4', 'requires': ['php-zts-lib-openssl-abi-1']},
            {'name': 'php-zts-mysqlnd', 'version': '8.4.27-r2', 'requires': ['php-zts-lib-openssl-abi-1']},
            {'name': 'php-zts-curl', 'version': '8.4.27-r2', 'requires': []}]}
        self.assertTrue(glibc.scope_abi_consumers(plan, snapshot, '8.4'))
        self.assertEqual(plan['php_requests']['8.4']['packages'], ['cli', 'curl', 'mysqlnd'])
        self.assertEqual(plan['expected_consumers']['8.4'], ['php-zts-cli', 'php-zts-curl', 'php-zts-mysqlnd'])
        self.assertEqual(plan['published_consumers']['8.4']['php-zts-mysqlnd'], '8.4.27-r2')
        previous = copy.deepcopy(plan)
        snapshot['inventory'][0]['provides'] = ['php-zts-lib-openssl-abi-2']
        for package in snapshot['inventory'][1:3]:
            package['requires'] = ['php-zts-lib-openssl-abi-2']
        self.assertTrue(glibc.scope_abi_consumers(plan, snapshot, '8.4', ['curl']))
        self.assertEqual(plan['php_requests']['8.4']['packages'], ['curl'])
        self.assertEqual(plan['expected_consumers']['8.4'], [])
        snapshot['inventory'].append({'name': 'php-zts-lib-curl', 'requires': ['php-zts-lib-openssl-abi-1']})
        glibc.scope_abi_consumers(previous, snapshot, '8.4')
        self.assertEqual(previous['php_requests']['8.4']['packages'], ['cli', 'curl', 'mysqlnd'])

    def test_host_extraction_preserves_recorded_payload_asset_modes(self):
        import yaml
        data = yaml.safe_load((ROOT / '.github/workflows/build-rpm-modular-packages.yml').read_text())
        step = next(step for job in data['jobs'].values() for step in job.get('steps', [])
                    if step.get('name') == 'Extract sealed common payload')
        with tempfile.TemporaryDirectory(prefix='spp-sealed-extraction-') as temporary:
            root = Path(temporary)
            payload = root / 'source/build/8.5'
            for relative, mode in [('include/fixture.h', 0o664), ('packaging/frankenphp/postinstall.sh', 0o775)]:
                path = payload / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('Frozen package asset\n')
                path.chmod(mode)
            script = '''require $argv[1] . '/vendor/autoload.php';
                staticphp\\util\\BuildPayload::record($argv[2], [], '8.5.0', 'x86_64');
                staticphp\\util\\BuildPayload::seal($argv[2]);'''
            subprocess.run(['php', '-r', script, str(ROOT), str(payload)], check=True, capture_output=True)
            subprocess.run(['tar', '-cf', str(root / 'payload.tar'), '-C', str(root / 'source'), 'build'], check=True)
            subprocess.run(['zstd', '-q', str(root / 'payload.tar'), '-o', str(root / 'php-payload.tar.zst')], check=True)
            subprocess.run(['bash', '-e', '-o', 'pipefail', '-c', 'umask 022\n' + step['run']], cwd=root, check=True)
            restored = root / 'build/8.5'
            verify = ['php', str(ROOT / 'bin/build-payload'), 'verify', '--root', str(restored)]
            subprocess.run(verify, check=True, capture_output=True)
            self.assertEqual((restored / 'include/fixture.h').stat().st_mode & 0o777, 0o664)
            self.assertEqual((restored / 'packaging/frankenphp/postinstall.sh').stat().st_mode & 0o777, 0o775)
            (restored / 'include/fixture.h').chmod(0o644)
            rejected = subprocess.run(verify, capture_output=True, text=True)
            self.assertNotEqual(rejected.returncode, 0)
            self.assertIn('Recorded package assets changed', rejected.stdout + rejected.stderr)

    def test_real_alpine_target_record_selects_services_and_tls_once_per_architecture(self):
        with tempfile.TemporaryDirectory(prefix='spp-apk-target-record-') as temporary:
            root = Path(temporary)
            for arch in ['arm64', 'x86_64']:
                target = root / arch
                (target / 'stage').mkdir(parents=True)
                helpers = target / '.github/actions/shared-libraries'
                helpers.mkdir(parents=True)
                shutil.copy2(HELPERS / 'workflow.py', helpers / 'workflow.py')
                (target / 'stage/shared-library-plan.json').write_text(json.dumps({'php_requests': {
                    stream: {'full': True} for stream in workflow.STREAMS}}))
                env = dict(os.environ, TYPE='apk', IN_BUILDER='true', MODE='full', LIBRARIES='',
                           MATRIX_ARCH=arch, TARGET=arch, ALMA='', CACHE_KEY='fixture', SOURCE_RUN_ID='42',
                           IMAGE='ghcr.io/example/builder@sha256:' + 'a' * 64)
                subprocess.run(['bash', HELPERS / 'run.sh', 'record'], cwd=target, env=env, check=True, capture_output=True)
                shutil.copytree(target / 'stage', root / ('plans-' + arch))
                shutil.rmtree(target)
            result = subprocess.check_output([sys.executable, HELPERS / 'workflow.py', 'matrix', '--plans', root], text=True)
            entries = json.loads(next(line.split('=', 1)[1] for line in result.splitlines() if line.startswith('matrix=')))['include']
            self.assertEqual(len(entries), 10)
            for arch in ['arm64', 'x86_64']:
                selected = [entry for entry in entries if entry['arch'] == arch]
                self.assertEqual([entry['php-version'] for entry in selected if entry['runtime-checks']], ['8.6'])
                self.assertEqual([entry['php-version'] for entry in selected if entry['tls-checks']], ['8.6'])

    def test_alpine_inline_completion_removes_the_transfer_job_only_for_successful_sdks(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            glibc.write(root / 'plan/shared-library-plan.json', {'libraries': [], 'php_requests': {'8.5': {'packages': ['curl'], 'compile': False}}})
            glibc.write(root / 'plan/target.json', {'type': 'apk', 'target': 'arm64', 'arch': 'arm64',
                        'image': 'producer', 'payload_stream': '8.5'})
            for successful in ['arm64', '']:
                result = subprocess.check_output([sys.executable, HELPERS / 'workflow.py', 'matrix', '--plans', root,
                                                  '--succeeded', successful], text=True)
                outputs = dict(line.split('=', 1) for line in result.splitlines())
                self.assertEqual(json.loads(outputs['matrix'])['include'], [])
                self.assertEqual(outputs['publish-any'], 'true' if successful else 'false')

    def test_packaging_uses_native_stream_baseline_before_creating_candidates(self):
        import yaml
        apk = yaml.safe_load((ROOT / '.github/actions/apk-packages/action.yml').read_text())['runs']['steps']
        glibc = yaml.safe_load((ROOT / '.github/workflows/build-rpm-modular-packages.yml').read_text())['jobs']['package']['steps']
        for steps, package_step in [(apk, 'Build and test PHP packages'), (glibc, 'Package sealed payload without compilation')]:
            names = [step.get('name') for step in steps]
            self.assertLess(names.index('Freeze actual published baseline'), names.index(package_step))

    def test_payload_cache_key_initializes_build_inputs_and_sealed_output_survives_export_failure(self):
        import yaml
        action = yaml.safe_load((ROOT / '.github/actions/php-payload/action.yml').read_text())
        steps = action['runs']['steps']
        names = [step.get('name') for step in steps]
        key = steps[names.index('Compute sealed PHP cache identity')]['run']
        self.assertIn('run.sh payload-key', key)
        self.assertLess(names.index('Build and seal common PHP payload'), names.index('Stage sealed common payload'))
        self.assertLess(names.index('Stage sealed common payload'), names.index('Export PHP-only sealed cache'))

    def test_sdk_classification_needs_no_temporary_craft_and_scans_consumers_only_after_rebuild(self):
        for kind, rebuilt, cached_variants, manual in [('rpm', False, False, False), ('rpm', False, True, False), ('rpm', True, False, False), ('apk', False, False, False), ('apk', True, False, False), ('rpm', False, False, True), ('rpm', False, False, 'consumer'), ('rpm', 'breaking', False, False), ('apk', 'breaking', False, False), ('rpm', 'pending', False, False), ('apk', 'pending', False, False), ('rpm', 'pending', False, True), ('apk', 'pending', False, True)]:
            with self.subTest(kind=kind, rebuilt=rebuilt, cached_variants=cached_variants), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                sdk, stage = root / 'buildroot', root / 'stage'
                value = {'libraries': {'fixture': {'abi_epoch': 1}}}
                if rebuilt == 'pending':
                    value['libraries']['consumer'] = {'abi_epoch': 1}
                    value['pending_rebuilds'] = {'consumer': {'libfixture.so.1': 'fixture'}}
                glibc.write(sdk / 'shared-libraries.json', value)
                glibc.write(stage / 'library-build-plan.json', {'full': False, 'rebuilt_libraries': ['fixture'] if rebuilt else []})
                glibc.write(stage / 'effective-source-plan.json', {})
                if cached_variants:
                    glibc.write(sdk / 'openssl-variants/variants.json', {})
                calls, scans, scopes = [], [], []
                formats = ('apk',) if kind == 'apk' else ('rpm', 'deb')
                packaging = Barrier(len(formats))
                def baseline(kind, destination, consumers=False, abi_libraries=(), libraries=None):
                    if rebuilt == 'pending':
                        self.assertNotIn('pending_rebuilds', glibc.read(sdk / 'shared-libraries.json'))
                    scans.append((kind, consumers, set(abi_libraries)))
                    scopes.append(libraries)
                    glibc.write(destination / 'shared-libraries.json', dict(value, consumer_inventory_complete=consumers))
                def execute(arguments, **kwargs):
                    args = [str(arg) for arg in arguments]
                    calls.append(args)
                    option = lambda name: Path(args[args.index(name) + 1])
                    if args[0] == 'cp':
                        subprocess.run(args, check=True)
                    elif 'prepare' in args:
                        glibc.write(option('--manifest-output'), value)
                        glibc.write(option('--output'), {'libraries': [], 'php_requests': {}, 'abi_break': rebuilt == 'breaking', 'abi_rebuild_roots': ['fixture'] if rebuilt else []})
                    elif 'expand' in args:
                        glibc.write(option('--output'), {'selected': ['consumer'] if value.get('pending_rebuilds') else []})
                    elif 'run' in args and '.github/actions/shared-libraries/incremental-build.py' in args:
                        self.assertTrue(value.pop('pending_rebuilds'))
                        glibc.write(sdk / 'shared-libraries.json', value)
                        glibc.write(option('--plan'), {'rebuilt_libraries': ['consumer', 'fixture']})
                    elif '.github/actions/shared-libraries/openssl-variants.py' in args:
                        self.assertNotIn('pending_rebuilds', glibc.read(sdk / 'shared-libraries.json'))
                    elif 'stage-publication-retries' in args:
                        glibc.write(option('--output'), {'remaining_libraries': ['fixture']})
                    elif args[:2] == ['php', 'bin/spp']:
                        kind = next(arg.split('=', 1)[1] for arg in args if arg.startswith('--type='))
                        selected = next(arg.split('=', 1)[1] for arg in args if arg.startswith('--buildroot='))
                        self.assertEqual(Path(selected), sdk if kind == 'apk' or manual and not rebuilt else stage / ('sdk-' + kind))
                        self.assertEqual(glibc.read(sdk / 'shared-libraries.json'), value)
                        archives = root / 'dist' / kind
                        archives.mkdir(parents=True, exist_ok=True)
                        (archives / ('fixture.' + kind)).write_bytes(b'package')
                        packaging.wait(timeout=5)
                    elif 'manifest' in args:
                        shutil.copy2(option('--root') / 'shared-libraries.json', option('--output'))
                with patch.object(glibc, 'ROOT', root), patch.object(glibc, 'STAGE', stage), \
                        patch.object(glibc, 'baseline', baseline), patch.object(glibc, 'run', execute), patch.dict(os.environ, TYPE=kind, LIBRARIES='fixture' if manual is True else '', PACKAGES='curl' if manual == 'consumer' else '', PUBLISH='true'):
                    glibc.classify()
                expected = {'fixture'} if rebuilt else set()
                expected_scans = [(format, False, set()) for format in formats]
                if rebuilt == 'breaking':
                    expected_scans += [(format, format == formats[-1], expected) for format in formats]
                self.assertCountEqual(scans, expected_scans)
                if manual == 'consumer':
                    self.assertEqual(scopes, [[] for _ in formats])
                if manual is True:
                    self.assertEqual([set(scope) for scope in scopes], [{'fixture', 'consumer'} if rebuilt else {'fixture'} for _ in formats])
                self.assertFalse((root / 'craft.yml').exists())
                self.assertFalse(any('record' in call for call in calls))
                self.assertEqual(any('seal' in call for call in calls), not manual or bool(rebuilt))
                prepares = [call for call in calls if 'prepare' in call]
                self.assertEqual(len(prepares), len(formats) * (2 if rebuilt == 'breaking' else 1))
                self.assertTrue(all(call[call.index('--rebuilt-libraries') + 1] == ('consumer,fixture' if rebuilt == 'pending' else 'fixture' if rebuilt else '') for call in prepares))
                if rebuilt == 'pending':
                    rebuild = next(index for index, call in enumerate(calls) if 'run' in call and '.github/actions/shared-libraries/incremental-build.py' in call)
                    self.assertLess(rebuild, calls.index(prepares[0]))
                    if kind == 'rpm':
                        profile = next(index for index, call in enumerate(calls) if '.github/actions/shared-libraries/openssl-variants.py' in call)
                        self.assertLess(rebuild, profile)
                self.assertEqual(packaging.n_waiting, 0)
                archive_checks = [call for call in calls if '.github/actions/shared-libraries/payload-archives.py' in call]
                self.assertEqual(len(archive_checks), 0 if kind == 'apk' else 2)
                self.assertEqual(len([call for call in calls if call[:2] == ['php', 'bin/spp']]), len(formats))

    def test_shared_consumer_graph_preserves_native_rpm_versions_and_libraries(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            deb, rpm = root / 'deb', root / 'rpm'
            glibc.write(deb / 'shared-libraries.json', {'consumers': {'8.5': ['php-zts-cli']},
                'consumer_dependencies': {'8.5': {'php-zts-cli': ['openssl']}},
                'consumer_inventory_complete': True, 'consumer_inventory_stream': '8.5',
                'consumer_versions': {'8.5': {'php-zts-cli': 'deb-version'}}})
            glibc.write(rpm / 'shared-libraries.json', {'libraries': {'openssl': {'published_version': 'rpm-version'}},
                'consumer_versions': {'8.5': {'php-zts-cli': 'rpm-version'}}})
            glibc.share_consumers(deb, rpm)
            result = glibc.read(rpm / 'shared-libraries.json')
            self.assertEqual(result['consumer_dependencies']['8.5']['php-zts-cli'], ['openssl'])
            self.assertEqual(result['consumer_versions']['8.5']['php-zts-cli'], 'rpm-version')
            self.assertEqual(result['libraries']['openssl']['published_version'], 'rpm-version')

    def test_format_plans_merge_compile_and_packaging_requests_without_extra_compilation(self):
        value = glibc.combine_plans({
            'rpm': {'php_requests': {'8.6': {'full': True, 'packages': [], 'compile': False, 'compile_full': False, 'compile_packages': []}}},
            'deb': {'php_requests': {'8.6': {'full': False, 'packages': ['imagick'], 'compile': True, 'compile_full': False, 'compile_packages': ['imagick']}}},
        })['php_requests']['8.6']
        self.assertTrue(value['full'])
        self.assertEqual(value['packages'], [])
        self.assertTrue(value['compile'])
        self.assertFalse(value['compile_full'])
        self.assertEqual(value['compile_packages'], ['imagick'])
        self.assertFalse(value['reuse_payload'])
        metadata = glibc.combine_plans({'rpm': {'php_requests': {'8.5': {'full': True, 'compile': False}}}})
        self.assertTrue(metadata['php_requests']['8.5']['reuse_payload'])

    def test_common_abi_merge_keeps_epoch_capability_and_dump_consistent(self):
        common = {'libraries': {'fixture': {'source_hash': 'actual', 'abi_epoch': 1,
                    'abi_capability': 'php-zts-lib-fixture-abi-1', 'abi_capabilities': ['php-zts-lib-fixture-abi-1']}}}
        formats = {'rpm': {'libraries': {'fixture': {'abi_epoch': 2, 'abi_dump': {'libfixture.so.2': 'new.xml'}, 'abi_dump_hashes': {'libfixture.so.2': 'new'}}}},
                   'deb': {'libraries': {'fixture': {'abi_epoch': 1}}}}
        selected = list(glibc.merge_library_abi(common, formats))
        self.assertEqual(selected[0][0], 'rpm')
        value = common['libraries']['fixture']
        self.assertEqual(value['abi_epoch'], 2)
        self.assertEqual(value['abi_capability'], 'php-zts-lib-fixture-abi-2')
        self.assertEqual(value['abi_capabilities'], ['php-zts-lib-fixture-abi-2'])
        self.assertEqual(value['abi_dump'], {'libfixture.so.2': 'new.xml'})
        self.assertEqual(value['source_hash'], 'actual')

    def test_one_successful_payload_fans_out_two_formats_and_failed_target_does_not_cascade(self):
        with tempfile.TemporaryDirectory(prefix='spp-glibc-matrix-') as temporary:
            root = Path(temporary)
            for arch in ['x86_64', 'arm64']:
                target = root / ('shared-plans-glibc-alma8-' + arch)
                target.mkdir()
                (target / 'target.json').write_text(json.dumps({'target': 'alma8-' + arch, 'arch': arch,
                    'image': 'producer', 'package_images': {'rpm': 'rpm-image', 'deb': 'deb-image'}}))
                (target / 'shared-library-plan.json').write_text(json.dumps({'php_requests': {'8.6': {'full': True, 'packages': []}}}))
            result = subprocess.check_output([sys.executable, HELPERS / 'workflow.py', 'packages', '--plans', root,
                '--succeeded', 'alma8-x86_64 8.6'], text=True)
            matrix = json.loads(next(line.split('=', 1)[1] for line in result.splitlines() if line.startswith('matrix=')))['include']
            self.assertEqual(len(matrix), 2)
            self.assertEqual({entry['type'] for entry in matrix}, {'rpm', 'deb'})
            self.assertEqual({entry['arch'] for entry in matrix}, {'x86_64'})
            self.assertTrue(all(entry['image'] == 'producer' for entry in matrix))
            self.assertTrue(all(not entry['packaged'] for entry in matrix))
            for path in root.glob('*/target.json'):
                target = json.loads(path.read_text())
                target.update(payload_stream='8.6', packaged_stream='8.6')
                path.write_text(json.dumps(target))
            result = subprocess.check_output([sys.executable, HELPERS / 'workflow.py', 'packages', '--plans', root,
                '--succeeded', '', '--sdk-succeeded', 'alma8-x86_64'], text=True)
            matrix = json.loads(next(line.split('=', 1)[1] for line in result.splitlines() if line.startswith('matrix=')))['include']
            self.assertEqual({entry['type'] for entry in matrix}, {'rpm', 'deb'})
            self.assertTrue(all(entry['arch'] == 'x86_64' and entry['packaged'] for entry in matrix))
            for kind in ('rpm', 'deb'):
                result = subprocess.check_output([sys.executable, HELPERS / 'workflow.py', 'packages',
                    '--plans', root, '--succeeded', '', '--sdk-succeeded', 'alma8-x86_64',
                    '--formats', kind], text=True)
                selected = json.loads(next(line.split('=', 1)[1] for line in result.splitlines()
                                           if line.startswith('matrix=')))['include']
                self.assertEqual(selected, [entry for entry in matrix if entry['type'] == kind])

    def test_php_only_build_test_does_not_package_or_reseal_the_sdk(self):
        with tempfile.TemporaryDirectory() as temporary:
            stage = Path(temporary)
            with patch.object(glibc, 'STAGE', stage), patch.dict(os.environ, {'PUBLISH': 'false', 'PACKAGES': 'fpm'}), \
                    patch.object(glibc, 'run', side_effect=AssertionError('unnecessary SDK work')):
                for kind in ['rpm', 'deb', 'apk']:
                    directory = glibc.format_stage(kind)
                    glibc.write(directory / 'shared-library-plan.json', {'libraries': []})
                    glibc.write(directory / 'shared-libraries.json', {'libraries': {'fixture': {'version': '1.0'}}})
                    original = (directory / 'shared-libraries.json').read_bytes()
                    glibc.package_libraries(kind)
                    self.assertEqual((directory / 'shared-libraries.json').read_bytes(), original)
                    self.assertEqual(list((directory / 'libraries').iterdir()), [])

    def test_local_dependencies_never_shadow_the_candidate_runtime_layout(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage = root / 'stage'
            glibc.write(stage / 'shared-library-plan.json', {'build_only': True})
            outputs, fixtures = root / 'dist/apk', stage / 'test-dependencies/apk'
            for directory, names in [(outputs, ['php-zts-runtime-layout-2-r0.apk', 'php-zts-curl-8.5.12-r3.apk']),
                                     (fixtures, ['php-zts-runtime-layout-2-r1.apk', 'php-zts-lib-curl-8.16.0-r1.apk'])]:
                directory.mkdir(parents=True)
                for name in names:
                    (directory / name).write_bytes(b'archive')
            with patch.object(glibc, 'ROOT', root), patch.object(glibc, 'STAGE', stage), patch.object(glibc, 'run'), \
                    patch.dict(os.environ, TYPE='apk', PHPV='8.5', MATRIX_ARCH='x86_64'):
                glibc.test_packages()
            self.assertEqual({path.name for path in (stage / 'native-packages').iterdir()},
                             {'php-zts-runtime-layout-2-r0.apk', 'php-zts-curl-8.5.12-r3.apk', 'php-zts-lib-curl-8.16.0-r1.apk'})

    def test_single_stream_reuses_sdk_job_and_only_successful_producers_reach_packaging(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / 'plan'
            target.mkdir()
            plan = {'php_requests': {'8.5': {'packages': ['curl'], 'compile': False}}}
            glibc.write(target / 'shared-library-plan.json', plan)
            glibc.write(target / 'target.json', {'target': 'alma8-x86_64', 'arch': 'x86_64', 'payload_stream': '8.5',
                                               'package_images': {'rpm': 'producer@sha256:' + 'a' * 64, 'deb': 'packager@sha256:' + 'b' * 64}})
            def command(*args):
                result = subprocess.check_output([sys.executable, HELPERS / 'workflow.py', *args], text=True)
                return dict(line.split('=', 1) for line in result.splitlines())
            chosen = command('inline', '--plan', str(target / 'shared-library-plan.json'))
            self.assertEqual(chosen['inline-stream'], '8.5')
            self.assertEqual(json.loads(chosen['inline-request'])['packages'], 'curl')
            self.assertEqual(json.loads(command('matrix', '--plans', str(root))['matrix'])['include'], [])
            for successful in ['', 'alma8-x86_64']:
                packaged = command('packages', '--plans', str(root), '--succeeded', '', '--sdk-succeeded', successful)
                self.assertEqual(len(json.loads(packaged['matrix'])['include']), 2 if successful else 0)
            plan['php_requests']['8.4'] = {'full': True}
            glibc.write(target / 'shared-library-plan.json', plan)
            self.assertEqual(command('inline', '--plan', str(target / 'shared-library-plan.json'))['inline-stream'], '')

    def test_library_only_update_gets_validation_lanes_without_php_payload_jobs(self):
        with tempfile.TemporaryDirectory(prefix='spp-glibc-library-only-') as temporary:
            root = Path(temporary)
            target = root / 'target'
            target.mkdir()
            (target / 'target.json').write_text(json.dumps({'target': 'alma8-x86_64', 'arch': 'x86_64',
                'image': 'producer', 'package_images': {'rpm': 'rpm-image@sha256:' + 'a' * 64, 'deb': 'deb-image@sha256:' + 'b' * 64}}))
            (target / 'shared-library-plan.json').write_text(json.dumps({'php_requests': {}, 'libraries': ['zlib']}))
            self.assertEqual(workflow.matrix(root, workflow.STREAMS)[0], [])
            result = subprocess.check_output([sys.executable, HELPERS / 'workflow.py', 'packages', '--plans', root,
                '--succeeded', ''], text=True)
            values = json.loads(next(line.split('=', 1)[1] for line in result.splitlines() if line.startswith('matrix=')))['include']
            self.assertEqual(len(values), 2)
            self.assertTrue(all(item['library-only'] for item in values))
            self.assertTrue(all(item['tls-checks'] and not item['runtime-checks'] for item in values))

    def test_format_only_library_update_never_creates_an_empty_other_format_lane(self):
        with tempfile.TemporaryDirectory(prefix='spp-glibc-format-') as temporary:
            root = Path(temporary)
            target = root / 'target'
            target.mkdir()
            (target / 'target.json').write_text(json.dumps({'target': 'alma8-x86_64', 'arch': 'x86_64',
                'image': 'producer', 'package_images': {'rpm': 'rpm-image@sha256:' + 'a' * 64, 'deb': 'deb-image@sha256:' + 'b' * 64}}))
            plan = glibc.combine_plans({'rpm': {'libraries': [], 'php_requests': {}},
                                       'deb': {'libraries': ['openssl'], 'php_requests': {}}})
            (target / 'shared-library-plan.json').write_text(json.dumps(plan))
            result = subprocess.check_output([sys.executable, HELPERS / 'workflow.py', 'packages', '--plans', root,
                '--succeeded', ''], text=True)
            values = json.loads(next(line.split('=', 1)[1] for line in result.splitlines() if line.startswith('matrix=')))['include']
            self.assertEqual([value['type'] for value in values], ['deb'])
            result = subprocess.check_output([sys.executable, HELPERS / 'workflow.py', 'matrix', '--plans', root], text=True)
            publish = json.loads(next(line.split('=', 1)[1] for line in result.splitlines() if line.startswith('publish-matrix=')))['include']
            self.assertEqual([value['type'] for value in publish], ['deb'])
            self.assertEqual(publish[0]['package-image-digest'], 'b' * 64)

    def test_native_services_and_tls_are_deduplicated_per_format_and_architecture(self):
        values = [{'target': 'alma8-' + arch, 'type': kind, 'php-version': stream, 'packages': ''}
                  for arch in ['x86_64', 'arm64'] for kind in ['rpm', 'deb'] for stream in workflow.STREAMS]
        workflow.runtime_checks(values)
        self.assertEqual(len([item for item in values if item['runtime-checks']]), 4)
        self.assertEqual(len([item for item in values if item['tls-checks']]), 4)
        self.assertTrue(all(item['php-version'] == '8.6' for item in values if item['runtime-checks'] or item['tls-checks']))
        partial = [{'target': 'x86_64', 'type': 'apk', 'php-version': '8.6', 'packages': 'imagick'}]
        workflow.runtime_checks(partial)
        self.assertTrue(partial[0]['tls-checks'])
        self.assertFalse(partial[0]['runtime-checks'])

    def test_common_abi_cache_transports_receipts_and_keeps_openssl_profiles_separate(self):
        with tempfile.TemporaryDirectory(prefix='spp-format-abi-cache-') as temporary:
            root = Path(temporary)
            source = root / 'rpm'
            abi = source / 'usr/share/php-zts/libraries/abi'
            for name, value in [('grpc/libgrpc.so.50.xml', 'typed-common'), ('grpc/libgrpc.so.50.xml.inputs.json', 'bound-input-proof'),
                                ('openssl/libssl.so.3.xml', 'rpm-openssl'), ('openssl/libssl.so.3.xml.inputs.json', 'rpm-proof')]:
                path = abi / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(value)
            with patch.object(glibc, 'STAGE', root / 'stage'):
                glibc.save_abi_cache(source, 'rpm')
                glibc.seed_abi_cache(root / 'deb', 'deb')
                deb = root / 'deb/usr/share/php-zts/libraries/abi'
                self.assertEqual((deb / 'grpc/libgrpc.so.50.xml.inputs.json').read_text(), 'bound-input-proof')
                self.assertFalse((deb / 'openssl').exists())
                (deb / 'openssl').mkdir()
                (deb / 'openssl/libssl.so.3.xml').write_text('deb-openssl')
                glibc.save_abi_cache(root / 'deb', 'deb')
                glibc.seed_abi_cache(root / 'rpm-next-phase', 'rpm')
                self.assertEqual((root / 'rpm-next-phase/usr/share/php-zts/libraries/abi/openssl/libssl.so.3.xml').read_text(), 'rpm-openssl')

    def test_service_fixture_stages_only_the_real_selected_library_and_its_provenance(self):
        with tempfile.TemporaryDirectory(prefix='spp-runtime-sdk-') as temporary:
            root = Path(temporary)
            source, output = root / 'source', root / 'fixture'
            for relative, contents in {'lib/libz.so.1.3.1': b'actual-zlib', 'licenses/zlib/LICENSE': b'license',
                                       'debug/lib/libz.so.1.3.1.debug': b'actual-debug',
                                       'usr/share/php-zts/libraries/abi/zlib/libz.so.1.xml': b'actual-abi',
                                       'lib/unrelated.so': b'unchanged-other-library'}.items():
                path = source / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(contents)
            (source / 'lib/libz.so.1').symlink_to('libz.so.1.3.1')
            entry = {'files': ['lib/libz.so.1', 'lib/libz.so.1.3.1'], 'license_files': ['licenses/zlib/LICENSE'],
                     'abi_dump': {'libz.so.1': 'usr/share/php-zts/libraries/abi/zlib/libz.so.1.xml'}}
            (source / 'shared-libraries.json').write_text(json.dumps({'libraries': {'zlib': entry, 'other': {}}}))
            record = {'state': 'sealed', 'files': {'lib/libz.so.1.3.1': {'sha256': 'recorded-production-input'}}}
            (source / 'build-payload.json').write_text(json.dumps(record))
            self.assertEqual(runtime.stage_library_sdk(source, output), record)
            self.assertEqual(json.loads((output / 'shared-libraries.json').read_text())['libraries'], {'zlib': entry})
            self.assertEqual(json.loads((output / 'provenance/source-sealed-payload.json').read_text()), record)
            self.assertEqual((output / 'lib/libz.so.1').read_bytes(), b'actual-zlib')
            self.assertEqual((output / 'debug/lib/libz.so.1.3.1.debug').read_bytes(), b'actual-debug')
            self.assertFalse((output / 'lib/unrelated.so').exists())

    def test_partial_source_request_without_cached_core_builds_canonical_payload_once(self):
        with tempfile.TemporaryDirectory(prefix='spp-payload-cache-miss-') as temporary:
            root = Path(temporary)
            stage = root / 'stage'
            for kind in ['rpm', 'deb']:
                (stage / kind).mkdir(parents=True)
                (stage / kind / 'shared-library-plan.json').write_text('{}')
                (stage / kind / 'baseline').mkdir()
                (stage / kind / 'baseline/shared-libraries.json').write_text('{}')
            glibc.write(stage / 'consumer-source-versions.json', {'candidates': [{'name': 'php-zts-imagick'}]})
            calls = []
            def record(arguments, **kwargs):
                calls.append([str(value) for value in arguments])
                if arguments[:3] == ['php', 'bin/spp', 'build']:
                    (root / 'build/8.6').mkdir(parents=True)
            environment = {'PHPV': '8.6', 'COMPILE': 'true', 'COMPILE_PACKAGES': 'imagick', 'PACKAGES': 'imagick',
                           'SOURCE_RUN_ID': '42', 'TARGET': 'alma8-x86_64', 'IMAGE': 'producer', 'TYPE': 'rpm', 'MATRIX_ARCH': 'x86_64'}
            with patch.object(glibc, 'ROOT', root), patch.object(glibc, 'STAGE', stage), patch.object(glibc, 'run', record), \
                    patch.object(glibc, 'bind_library_sources'), patch.dict(os.environ, environment):
                previous = Path.cwd()
                try:
                    os.chdir(root)
                    glibc.payload()
                    build = next(call for call in calls if call[:3] == ['php', 'bin/spp', 'build'])
                    self.assertFalse(any(value.startswith('--packages=') for value in build))
                    preflight = next(call for call in calls if len(call) > 1 and call[1].endswith('/source-versions.py'))
                    self.assertEqual(preflight[preflight.index('--packages') + 1], 'imagick')
                    os.environ['COMPILE'] = 'false'
                    with self.assertRaisesRegex(RuntimeError, 'requires its sealed PHP cache'):
                        glibc.payload()
                finally:
                    os.chdir(previous)

    def test_php_compilation_uses_verified_sdk_sources_without_changing_extension_inputs(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            sdk, downloads = root / 'buildroot', root / 'downloads'
            source = {'hash': 'compiled-hash', 'version': 'previous-library', 'config': {'type': 'ghtar'}}
            manifest = {'libraries': {'libjxl': {'source_hash': source['hash']}, 'gcc-runtime': {}}}
            state = {'libraries': {'libjxl': {'artifact': 'libjxl', 'sources': {'libjxl': {'source': source}}}}}
            snapshot = {'libjxl': {'source': {'hash': 'new-snapshot-hash'}, 'binary': ['retained']},
                        'ext-imagick': {'source': {'hash': 'selected-extension'}}, 'php-src': {'source': {'hash': 'pinned-php'}}}
            glibc.write(sdk / 'shared-libraries.json', manifest)
            glibc.write(sdk / 'shared-library-build-inputs.json', state)
            glibc.write(downloads / '.cache.json', snapshot)
            glibc.bind_library_sources(sdk, downloads)
            actual = glibc.read(downloads / '.cache.json')
            self.assertEqual(actual['libjxl'], {'source': source, 'binary': ['retained']})
            self.assertEqual(actual['ext-imagick'], snapshot['ext-imagick'])
            self.assertEqual(actual['php-src'], snapshot['php-src'])
            state['libraries']['libjxl']['sources']['libjxl']['source']['hash'] = 'unverified'
            glibc.write(sdk / 'shared-library-build-inputs.json', state)
            with self.assertRaisesRegex(RuntimeError, 'source identity differs'):
                glibc.bind_library_sources(sdk, downloads)
            self.assertEqual(glibc.read(downloads / '.cache.json'), actual)

    def test_signing_preserves_exact_publication_retry_archives(self):
        with tempfile.TemporaryDirectory(prefix='spp-retry-signing-') as temporary:
            root = Path(temporary)
            (root / 'dist/rpm').mkdir(parents=True)
            receipt = root / 'cohort/shared-libs-rpm-alma8-x86_64/publication-retries.json'
            receipt.parent.mkdir(parents=True)
            existing, fresh = root / 'dist/rpm/existing.rpm', root / 'dist/rpm/new.rpm'
            existing.write_bytes(b'exact previously signed archive')
            fresh.write_bytes(b'genuinely new archive')
            receipt.write_text(json.dumps({'copied_archives': [{'path': str(existing),
                'sha256': hashlib.sha256(existing.read_bytes()).hexdigest()}]}))
            binaries = root / 'tools'
            binaries.mkdir()
            for name, script in {
                'gpg': 'import sys\nif "--with-colons" in sys.argv: print("fpr:::::::::TESTFINGERPRINT:")\n',
                'gpgconf': 'pass\n',
                'rpmsign': 'import pathlib,sys\np=pathlib.Path(sys.argv[-1]);p.write_bytes(p.read_bytes()+b" signed")\n',
            }.items():
                path = binaries / name
                path.write_text('#!/usr/bin/python3\n' + script)
                path.chmod(0o755)
            environment = dict(os.environ, PATH=str(binaries) + os.pathsep + os.environ['PATH'],
                               RPM_SIGNING_KEY='test fixture key', RPM_SIGNING_PASSWORD='test fixture password', TMPDIR=str(root))
            command = ['bash', str(HELPERS / 'sign-rpm.sh')]
            subprocess.run(command, cwd=root, env=environment, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            self.assertEqual(existing.read_bytes(), b'exact previously signed archive')
            self.assertEqual(fresh.read_bytes(), b'genuinely new archive signed')
            existing.write_bytes(b'same filename but changed bytes')
            rejected = subprocess.run(command, cwd=root, env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            self.assertNotEqual(rejected.returncode, 0)
            self.assertIn(b'changed before signing', rejected.stderr)
            self.assertEqual(fresh.read_bytes(), b'genuinely new archive signed')

    def test_native_receipt_binds_archive_bytes_and_producer_plan(self):
        with tempfile.TemporaryDirectory(prefix='spp-native-receipt-') as temporary:
            root = Path(temporary)
            library = root / 'cohort/shared-libs-rpm-alma8-x86_64'
            consumer = root / 'cohort/shared-consumers-rpm-alma8-x86_64-php8.6'
            library.mkdir(parents=True)
            consumer.mkdir()
            plan = library / 'shared-library-plan.json'
            plan.write_text(json.dumps({'php_requests': {'8.6': {'full': True, 'packages': []}}}))
            image = 'ghcr.io/example/producer@sha256:' + 'a' * 64
            (library / 'target.json').write_text(json.dumps({'arch': 'x86_64', 'source_run_id': '42', 'image': image}))
            report = consumer / 'native-upgrade-receipt.json'
            report.write_text(json.dumps({'ok': True, 'type': 'rpm', 'php_stream': '8.6', 'architecture': 'x86_64'}))
            archive = consumer / 'package.rpm'
            archive.write_bytes(b'validated native archive')
            receipt = workflow.upgrade_receipt(report, plan, consumer, 'rpm', '42', image, '8.6', 'x86_64')
            (consumer / 'shared-library-upgrade-receipt.json').write_text(json.dumps(receipt))
            workflow.verify_upgrades(root / 'cohort', library, 'rpm')
            archive.write_bytes(b'changed after native validation')
            with self.assertRaisesRegex(RuntimeError, 'does not match'):
                workflow.verify_upgrades(root / 'cohort', library, 'rpm')

    def test_extracted_elf_must_match_prepared_producer_bytes_and_modes(self):
        with tempfile.TemporaryDirectory(prefix='spp-archive-identity-') as temporary:
            root = Path(temporary)
            elf = root / 'usr/lib64/php-zts/lib/libfixture.so.1'
            elf.parent.mkdir(parents=True)
            content = b'\x7fELFprepared producer identity fixture'
            elf.write_bytes(content)
            elf.chmod(0o755)
            identity = (hashlib.sha256(content).hexdigest(), 0o755)
            result = archives.verify_extracted(root, {identity: [{'source': 'lib/libfixture.so.1', 'role': 'runtime'}]})
            self.assertIn('/usr/lib/php-zts/lib/libfixture.so.1', result)
            elf.write_bytes(content + b'RPM postprocessing mutation')
            with self.assertRaisesRegex(RuntimeError, 'differs from sealed'):
                archives.verify_extracted(root, {identity: []})


if __name__ == '__main__':
    unittest.main()
