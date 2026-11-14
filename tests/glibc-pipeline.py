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
runtime = module('runtime-checks')


class Pipeline(unittest.TestCase):
    def test_sdk_cleanup_removes_only_unrecorded_php_outputs(self):
        with tempfile.TemporaryDirectory() as temporary:
            sdk = Path(temporary)
            retained = ['lib/libfixture.so.1', 'lib/libphp-owned.so', 'include/php/owned.h']
            stale = ['bin/php', 'bin/frankenphp', 'lib/frankenphp', 'lib/libphp-zts-86.so',
                     'modules/curl-zts-86.so', 'include/php/leftover.h']
            for name in retained + stale + ['lib/unexpected.so', 'payload/runtime/lib/libfixture.so.1']:
                path = sdk / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b'unchanged')
            glibc.write(sdk / 'build-payload.json', {'kind': 'sdk', 'files': {name: {} for name in retained[:2]},
                'packaging_assets': {retained[2]: {}}, 'skipped_shared_extensions': None})
            glibc.write(sdk / 'skipped-shared-extensions.json', {'skipped': ['old-php-extension']})
            recorded = (sdk / 'build-payload.json').read_bytes()
            glibc.clean_sdk(sdk)
            self.assertTrue(all((sdk / name).read_bytes() == b'unchanged' for name in retained))
            self.assertTrue((sdk / 'lib/unexpected.so').is_file())
            self.assertTrue((sdk / 'payload/runtime/lib/libfixture.so.1').is_file())
            self.assertFalse(any((sdk / name).exists() for name in stale))
            self.assertFalse((sdk / 'skipped-shared-extensions.json').exists())
            self.assertEqual((sdk / 'build-payload.json').read_bytes(), recorded)

    def test_library_publication_reads_each_selected_owner_after_partial_rollout(self):
        with patch.dict(os.environ, LIBRARIES='openssl', PUBLISH='true', PHP_VERSIONS='8.4,8.6', MATRIX_ARCH='arm64'), \
                patch.object(glibc, 'run') as run:
            for kind in ('apk', 'deb'):
                glibc.baseline(kind, Path('baseline'), libraries={'openssl', 'zlib'})
                arguments = run.call_args.args[0]
                self.assertEqual(arguments[arguments.index('--owners') + 1], '84,86')
                self.assertEqual(arguments[arguments.index('--libraries') + 1], 'openssl,zlib')
            with patch.dict(os.environ, PHP_VERSIONS=''):
                glibc.baseline('apk', Path('baseline'))
                arguments = run.call_args.args[0]
                self.assertEqual(arguments[arguments.index('--owners') + 1], '82,83,84,85,86')
            with patch.dict(os.environ, PUBLISH='false'):
                glibc.baseline('apk', Path('baseline'))
                self.assertEqual(run.call_args.args[0][run.call_args.args[0].index('--owners') + 1], '84,86')

    def test_publication_rechecks_the_recorded_library_baseline_scope(self):
        for kind in ('rpm', 'deb', 'apk'):
            for states, expected in [([], None), ([{'libraries': {'libaom': {}}}], None),
                    ([{'metadata_scope': [], 'libraries': {}}], set()),
                    ([{'metadata_scope': ['libaom', 'libjxl', 'brotli'], 'libraries': {'libaom': {}}}], {'libaom', 'libjxl', 'brotli'}),
                    ([{'metadata_scope': ['libaom']}, {'metadata_scope': ['libjxl']}], {'libaom', 'libjxl'})]:
                with self.subTest(kind=kind, states=states), patch.object(glibc, 'baseline') as baseline:
                    glibc.publication_baseline(kind, Path('current-baseline'), {'baseline_states': states})
                    baseline.assert_called_once_with(kind, Path('current-baseline'), libraries=expected)

    def test_publication_guard_accepts_recorded_scope_and_rejects_real_changes(self):
        planner = glibc.runpy.run_path(str(ROOT / 'bin/shared-library-plan'))
        for kind in ('rpm', 'deb', 'apk'):
            for arch in ('x86_64', 'arm64'):
                for scope in (None, [], ['libaom', 'libjxl', 'missing']):
                    for change in ('unchanged', 'outside-scope', 'selected-library', 'runtime-layout'):
                        with self.subTest(kind=kind, arch=arch, scope=scope, change=change), tempfile.TemporaryDirectory() as temporary:
                            root = Path(temporary)
                            libraries = {name: {'version': '1.0', 'elf': {}, 'package_versions': {}}
                                         for name in ('libaom', 'libjxl', 'other')}
                            def project(remote, selected):
                                result = copy.deepcopy(remote)
                                if selected is not None:
                                    result['libraries'] = {name: value for name, value in result['libraries'].items() if name in selected}
                                    result['metadata_scope'] = sorted(selected)
                                return result
                            remote = {'schema': 1, 'linkage': 'shared', 'runtime_libdir': '/usr/lib/php-zts/lib',
                                      'architecture': 'aarch64' if arch == 'arm64' else arch,
                                      'libc': 'musl' if kind == 'apk' else 'glibc', 'libraries': libraries}
                            previous = project(remote, scope)
                            plan = {'schema': 1, 'baseline_fingerprint': planner['fingerprint'](previous),
                                    'baseline_states': [previous]}
                            if change in ('outside-scope', 'selected-library'):
                                remote['libraries']['other' if change == 'outside-scope' else 'libjxl']['version'] = '2.0'
                            elif change == 'runtime-layout':
                                remote['runtime_layout'] = {'package_version': '2-1', 'archive_sha256': 'changed'}
                            def fetch(arguments):
                                selected = arguments[arguments.index('--libraries') + 1].split(',') if '--libraries' in arguments else None
                                selected = set(filter(None, selected)) if selected is not None else None
                                glibc.write(Path(arguments[arguments.index('--output') + 1]) / 'shared-libraries.json', project(remote, selected))
                            with patch.object(glibc, 'run', fetch), patch.dict(os.environ, MATRIX_ARCH=arch, PHP_VERSIONS=''):
                                glibc.publication_baseline(kind, root / 'current', plan)
                            glibc.write(root / 'plan.json', plan)
                            artifacts = root / 'archives'
                            artifacts.mkdir()
                            args = glibc.SimpleNamespace(plan=root / 'plan.json', baseline_manifest=root / 'current/shared-libraries.json',
                                artifacts=artifacts, type=kind, output=root / 'cohort.json', php_streams='')
                            changed = change == 'runtime-layout' or (change == 'selected-library' and scope != []) or (change == 'outside-scope' and scope is None)
                            if changed:
                                with self.assertRaisesRegex(RuntimeError, 'baseline changed during the build'):
                                    planner['cohort'](args)
                            else:
                                planner['cohort'](args)
                                self.assertEqual(json.loads(args.output.read_text())['plan_fingerprint'], planner['digest'](plan))

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
        self.assertEqual(workflow.upgrade_mode( {'php_requests': {'8.5': {'full': True}}}, '8.5'), 'full')
        self.assertEqual(workflow.upgrade_mode( {'php_requests': {'8.5': {'packages': ['curl']}}}, '8.5'), 'shared')

    def test_native_inventory_capture_isolates_stream_failures(self):
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
            glibc.write(stage / 'deb/shared-library-plan.json', dict(plan, library_streams=['8.2', '8.3']))
            with patch.object(glibc, 'STAGE', stage), patch.object(glibc.runpy, 'run_path', return_value=freezer), \
                    patch.dict(os.environ, TYPE='rpm', MATRIX_ARCH='x86_64', PHP_VERSIONS='8.5,8.6'):
                glibc.capture_native_inventories()
            self.assertEqual(captured, [('rpm', '8.5'), ('rpm', '8.6'), ('deb', '8.2'), ('deb', '8.3'), ('deb', '8.5'), ('deb', '8.6')])
            snapshots = stage / 'common/rpm/native-inventories'
            self.assertNotIn('error', glibc.read(snapshots / '8.5.json'))
            self.assertIn('HTTP 522', glibc.read(snapshots / '8.6.json')['error'])
            self.assertIn('HTTP 522', glibc.read(stage / 'common/deb/native-inventories/8.6.json')['error'])
            captured.clear()
            with patch.object(glibc, 'STAGE', stage), patch.object(glibc.runpy, 'run_path', return_value=freezer), \
                    patch.dict(os.environ, TYPE='rpm', MATRIX_ARCH='x86_64', PHP_VERSIONS='8.5,8.6'):
                glibc.capture_native_inventories()
            self.assertEqual(captured, [('rpm', '8.6'), ('deb', '8.6')])

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

    def test_native_bindings_add_stream_specific_consumers(self):
        plan = {'type': 'apk', 'libraries': ['openssl'], 'abi_rebuild_roots': ['openssl'],
                'abi_core_rebuild_streams': ['8.4'],
                'php_requests': {'8.4': {'full': False, 'packages': ['cli', 'curl', 'mysqlnd_parsec'], 'compile_full': True}},
                'expected_consumers': {'8.4': ['php-zts-cli', 'php-zts-curl', 'php-zts-mysqlnd_parsec']},
                'abi_consumer_requests': {'8.4': {'full': False, 'consumers': ['php-zts-cli', 'php-zts-curl', 'php-zts-mysqlnd_parsec'],
                    'packages': ['cli', 'curl', 'mysqlnd_parsec']}}}
        snapshot = {'type': 'apk', 'php_stream': '8.4', 'architecture': 'arm64', 'inventory': [
            {'name': 'php-zts-lib-openssl', 'version': '3.6.5-r1'},
            {'name': 'php-zts-cli', 'version': '8.4.27-r4', 'requires': ['php-zts-lib-openssl']},
            {'name': 'php-zts-mysqlnd', 'version': '8.4.27-r2', 'requires': ['php-zts-lib-openssl']},
            {'name': 'php-zts-curl', 'version': '8.4.27-r2', 'requires': []}]}
        self.assertTrue(glibc.scope_abi_consumers(plan, snapshot, '8.4'))
        self.assertEqual(plan['php_requests']['8.4']['packages'], ['cli', 'curl', 'mysqlnd'])
        self.assertEqual(plan['expected_consumers']['8.4'], ['php-zts-cli', 'php-zts-curl', 'php-zts-mysqlnd'])
        self.assertEqual(plan['published_consumers']['8.4']['php-zts-mysqlnd'], '8.4.27-r2')

    def test_native_bindings_do_not_restore_other_owners_soname_changes(self):
        library = lambda soname: {'elf': {'lib/' + soname: {'soname': soname}}, 'dependencies': []}
        plan = {'type': 'apk', 'libraries': ['first', 'second'], 'abi_rebuild_roots': ['first', 'second'],
                'expected_libraries': {'first': library('libfirst.so.2'), 'second': library('libsecond.so.2')},
                'baseline_states': [{'stream_libraries': {'8.3': {
                    'first': library('libfirst.so.1'), 'second': library('libsecond.so.2')}}}],
                'php_requests': {'8.3': {'packages': ['cli']}},
                'abi_consumer_requests': {'8.3': {'consumers': ['php-zts-cli'], 'packages': ['cli']}}}
        snapshot = {'type': 'apk', 'php_stream': '8.3', 'architecture': 'x86_64', 'inventory': [
            {'name': 'php-zts-cli', 'version': '8.3.36-r1', 'requires': ['php-zts-lib-first']},
            {'name': 'php-zts-curl', 'version': '8.3.36-r1', 'requires': ['php-zts-lib-second']}]}
        glibc.scope_abi_consumers(plan, snapshot, '8.3')
        self.assertEqual(plan['php_requests']['8.3']['packages'], ['cli'])
        self.assertEqual(plan['abi_consumer_requests']['8.3']['consumers'], ['php-zts-cli'])

    def test_host_extraction_preserves_recorded_payload_asset_modes(self):
        import yaml
        data = yaml.safe_load((ROOT / '.github/workflows/package-glibc.yml').read_text())
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
        glibc = yaml.safe_load((ROOT / '.github/workflows/package-glibc.yml').read_text())['jobs']['package']['steps']
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
        for kind, rebuilt, cached_variants, manual in [('rpm', False, False, False), ('rpm', False, True, False), ('rpm', True, False, False), ('apk', False, False, False), ('apk', True, False, False), ('rpm', False, False, True), ('rpm', False, False, 'consumer'), ('rpm', 'breaking', False, False), ('apk', 'breaking', False, False), ('rpm', 'pending', False, False), ('apk', 'pending', False, False)]:
            with self.subTest(kind=kind, rebuilt=rebuilt, cached_variants=cached_variants), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                sdk, stage = root / 'buildroot', root / 'stage'
                value = {'libraries': {'fixture': {}}}
                if rebuilt == 'pending':
                    value['libraries']['consumer'] = {}
                    value['pending_rebuilds'] = {'consumer': {'libfixture.so.1': 'fixture'}}
                glibc.write(sdk / 'shared-libraries.json', value)
                glibc.write(sdk / 'build-payload.json', {'state': 'sealed'})
                glibc.write(stage / 'library-build-plan.json', {'full': False, 'rebuilt_libraries': ['fixture'] if rebuilt else []})
                glibc.write(stage / 'effective-source-plan.json', {})
                if cached_variants:
                    glibc.write(sdk / 'openssl-variants/variants.json', {})
                calls, scans, scopes = [], [], []
                formats = ('apk',) if kind == 'apk' else ('rpm', 'deb')
                packaging = Barrier(len(formats))
                def baseline(kind, destination, consumers=False, libraries=None):
                    scans.append((kind, consumers))
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
                        glibc.write(option('--output'), {'libraries': [], 'php_requests': {'8.5': {'packages': ['cli']}}, 'abi_break': rebuilt == 'breaking', 'abi_rebuild_roots': ['fixture'] if rebuilt else []})
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
                        self.assertTrue(capture.called)
                        kind = next(arg.split('=', 1)[1] for arg in args if arg.startswith('--type='))
                        selected = next(arg.split('=', 1)[1] for arg in args if arg.startswith('--buildroot='))
                        self.assertEqual(Path(selected), sdk if kind == 'apk' or manual else stage / ('sdk-' + kind))
                        self.assertEqual(glibc.read(sdk / 'shared-libraries.json'), value)
                        archives = Path(kwargs['env']['SPP_DIST_PATH']) / kind
                        archives.mkdir(parents=True, exist_ok=True)
                        (archives / ('fixture.' + kind)).write_bytes(b'package')
                        packaging.wait(timeout=5)
                    elif 'manifest' in args:
                        shutil.copy2(option('--root') / 'shared-libraries.json', option('--output'))
                with patch.object(glibc, 'ROOT', root), patch.object(glibc, 'STAGE', stage), \
                        patch.object(glibc, 'baseline', baseline), patch.object(glibc, 'run', execute), \
                        patch.object(glibc, 'capture_native_inventories') as capture, \
                        patch.dict(os.environ, TYPE=kind, LIBRARIES='fixture' if manual is True else '', PACKAGES='curl' if manual == 'consumer' else '', PUBLISH='true'):
                    glibc.classify()
                    capture.assert_called_once_with()
                expected = {'fixture'} if rebuilt else set()
                expected_scans = [(format, False) for format in formats]
                if rebuilt == 'breaking':
                    expected_scans += [(format, True) for format in formats]
                self.assertCountEqual(scans, expected_scans)
                if manual == 'consumer':
                    self.assertEqual(scopes, [[] for _ in formats])
                self.assertFalse((root / 'craft.yml').exists())
                self.assertFalse(any('record' in call for call in calls))
                self.assertEqual(any('seal' in call for call in calls), kind == 'apk' or bool(rebuilt) and not manual)
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
                self.assertEqual(len([call for call in calls if call[:2] == ['php', 'bin/spp']]), 1 if kind == 'apk' else 0)

    def test_format_library_selection_without_openssl_uses_common_sdk_in_place(self):
        for kind, state in [('rpm', 'prepared'), ('deb', 'prepared'), ('rpm', 'sealed'), ('deb', 'sealed')]:
            with self.subTest(kind=kind, state=state), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                stage = root / 'stage'
                glibc.write(stage / 'target.json', {'source_run_id': '42', 'image': 'producer'})
                glibc.write(stage / kind / 'shared-library-plan.json', {'libraries': ['zlib'], 'php_requests': {}, 'library_streams': ['8.5']})
                glibc.write(root / 'buildroot/shared-libraries.json', {'libraries': {'zlib': {'elf': {}}}})
                glibc.write(stage / kind / 'shared-libraries.json', {'libraries': {'zlib': {'elf': {}}}})
                glibc.write(root / 'buildroot/build-payload.json', {'state': state})
                with patch.object(glibc, 'ROOT', root), patch.object(glibc, 'STAGE', stage), \
                        patch.object(glibc, 'package_library_archives') as package, patch.object(glibc, 'run') as run, \
                        patch.dict(os.environ, TYPE=kind):
                    glibc.format_libraries()
                package.assert_called_once()
                self.assertEqual(package.call_args.args[2], root / 'buildroot')
                self.assertTrue((root / 'buildroot').is_dir())
                self.assertFalse((stage / ('sdk-' + kind)).exists())
                self.assertEqual(len(run.call_args_list), 2 if state == 'prepared' else 1)
                self.assertEqual(any('seal' in call.args[0] for call in run.call_args_list), state == 'prepared')
                self.assertIn('receipt', run.call_args.args[0])

    def test_php_packaging_preserves_prebuilt_libraries_with_a_subset(self):
        for kind in ('rpm', 'deb'):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                stage, payload = root / 'stage', root / 'build/8.5'
                plan = stage / kind / 'shared-library-plan.json'
                glibc.write(plan, {})
                glibc.write(stage / 'payload-provenance.json', {'php_stream': '8.5', 'source_run_id': '42',
                    'producer_image': 'producer', 'format_plan_sha256': {kind: hashlib.sha256(plan.read_bytes()).hexdigest()}})
                glibc.write(stage / kind / 'shared-libraries.json', {'libraries': {'libaom': {}}})
                glibc.write(payload / 'build-payload.json', {'packaging_selection': {'packages': ['ssh2'], 'libraries': []}})
                dist = root / 'dist' / kind
                dist.mkdir(parents=True)
                library = dist / ('libaom.' + kind)
                library.write_bytes(b'prebuilt library')
                def run(arguments, **kwargs):
                    if arguments[:3] == ['php', 'bin/spp', 'package']:
                        output = Path(kwargs['env']['SPP_DIST_PATH']) / kind
                        output.mkdir(parents=True)
                        (output / ('ssh2.' + kind)).write_bytes(b'sealed PHP extension')
                with patch.object(glibc, 'ROOT', root), patch.object(glibc, 'STAGE', stage), patch.object(glibc, 'run', run), \
                        patch.dict(os.environ, TYPE=kind, PHPV='8.5', SOURCE_RUN_ID='42', PACKAGES='ssh2'):
                    glibc.package()
                self.assertEqual(library.read_bytes(), b'prebuilt library')
                self.assertEqual((dist / ('ssh2.' + kind)).read_bytes(), b'sealed PHP extension')

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
            for path in root.glob('*/target.json'):
                target = json.loads(path.read_text())
                target.update(payload_stream='8.6')
                path.write_text(json.dumps(target))
            result = subprocess.check_output([sys.executable, HELPERS / 'workflow.py', 'packages', '--plans', root,
                '--succeeded', '', '--sdk-succeeded', 'alma8-x86_64'], text=True)
            matrix = json.loads(next(line.split('=', 1)[1] for line in result.splitlines() if line.startswith('matrix=')))['include']
            self.assertEqual({entry['type'] for entry in matrix}, {'rpm', 'deb'})
            self.assertTrue(all(entry['arch'] == 'x86_64' for entry in matrix))
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
            self.assertTrue(all(not item['tls-checks'] and not item.get('runtime-checks') for item in values))

    def test_owner_library_validation_covers_every_owner_alongside_consumer_jobs(self):
        for kind in ['apk', 'deb']:
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                selection = {'libraries': ['zlib'], 'library_streams': ['8.2', '8.3', '8.6'],
                             'php_requests': {'8.6': {'packages': ['curl']}}}
                target = {'type': kind, 'target': 'x86_64' if kind == 'apk' else 'alma8-x86_64', 'arch': 'x86_64'}
                if kind == 'deb':
                    target['package_images'] = {'deb': 'packager'}
                    plan = glibc.combine_plans({'deb': selection})
                    command = ['packages', '--formats', 'deb', '--succeeded', 'alma8-x86_64 8.6']
                else:
                    plan = selection
                    command = ['matrix']
                glibc.write(root / 'plan/target.json', target)
                glibc.write(root / 'plan/shared-library-plan.json', plan)
                result = subprocess.check_output([sys.executable, HELPERS / 'workflow.py', *command, '--plans', root], text=True)
                values = json.loads(next(line.split('=', 1)[1] for line in result.splitlines() if line.startswith('matrix=')))['include']
                self.assertEqual({value['php-version'] for value in values}, {'8.2', '8.3', '8.6'})
                self.assertEqual({value['php-version'] for value in values if value.get('library-only')}, {'8.2', '8.3'})

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

    def test_service_fixture_stages_only_the_real_selected_library_and_its_provenance(self):
        with tempfile.TemporaryDirectory(prefix='spp-runtime-sdk-') as temporary:
            root = Path(temporary)
            source, output = root / 'source', root / 'fixture'
            for relative, contents in {'lib/libz.so.1.3.1': b'actual-zlib', 'licenses/zlib/LICENSE': b'license',
                                       'debug/lib/libz.so.1.3.1.debug': b'actual-debug',
                                       'lib/unrelated.so': b'unchanged-other-library'}.items():
                path = source / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(contents)
            (source / 'lib/libz.so.1').symlink_to('libz.so.1.3.1')
            entry = {'files': ['lib/libz.so.1', 'lib/libz.so.1.3.1'], 'license_files': ['licenses/zlib/LICENSE']}
            (source / 'shared-libraries.json').write_text(json.dumps({'libraries': {'zlib': entry, 'other': {}}}))
            record = {'state': 'sealed', 'files': {'lib/libz.so.1.3.1': {'sha256': 'recorded-production-input'}}}
            (source / 'build-payload.json').write_text(json.dumps(record))
            self.assertEqual(runtime.stage_library_sdk(source, output), record)
            self.assertEqual(json.loads((output / 'shared-libraries.json').read_text())['libraries'], {'zlib': entry})
            self.assertEqual(json.loads((output / 'provenance/source-sealed-payload.json').read_text()), record)
            self.assertEqual((output / 'lib/libz.so.1').read_bytes(), b'actual-zlib')
            self.assertEqual((output / 'debug/lib/libz.so.1.3.1.debug').read_bytes(), b'actual-debug')
            self.assertFalse((output / 'lib/unrelated.so').exists())

    def test_partial_source_request_without_cached_core_refuses_full_compilation(self):
        with tempfile.TemporaryDirectory(prefix='spp-payload-cache-miss-') as temporary:
            root = Path(temporary)
            stage = root / 'stage'
            for kind in ['rpm', 'deb']:
                (stage / kind).mkdir(parents=True)
                (stage / kind / 'shared-library-plan.json').write_text('{}')
                (stage / kind / 'baseline').mkdir()
                (stage / kind / 'baseline/shared-libraries.json').write_text('{}')
            glibc.write(root / 'buildroot/build-payload.json', {'kind': 'sdk', 'files': {}})
            calls = []
            def record(arguments, **kwargs):
                calls.append([str(value) for value in arguments])
                if arguments[:3] == ['php', 'bin/spp', 'build']:
                    (root / 'build/8.6/bin').mkdir(parents=True)
                    (root / 'build/8.6/bin/php').write_bytes(b'consumer')
                    (root / 'buildroot/bin').mkdir()
                    (root / 'buildroot/bin/php').write_bytes(b'consumer')
            environment = {'PHPV': '8.6', 'COMPILE': 'true', 'COMPILE_PACKAGES': 'imagick', 'PACKAGES': 'imagick',
                           'SOURCE_RUN_ID': '42', 'TARGET': 'alma8-x86_64', 'IMAGE': 'producer', 'TYPE': 'rpm', 'MATRIX_ARCH': 'x86_64'}
            with patch.object(glibc, 'ROOT', root), patch.object(glibc, 'STAGE', stage), patch.object(glibc, 'run', record), \
                    patch.object(glibc, 'bind_library_sources'), patch.dict(os.environ, environment):
                previous = Path.cwd()
                try:
                    os.chdir(root)
                    with self.assertRaisesRegex(RuntimeError, 'requires its cached PHP core'):
                        glibc.payload()
                    self.assertEqual(calls, [])
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

    def test_each_owner_library_payload_requires_its_own_native_validation(self):
        for kind in ['deb', 'apk']:
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                library = root / ('shared-libs-' + kind + '-x86_64')
                library.mkdir()
                plan = library / 'shared-library-plan.json'
                plan.write_text(json.dumps({'libraries': ['zlib'], 'library_streams': ['8.3', '8.6'], 'php_requests': {}}))
                image = 'ghcr.io/example/producer@sha256:' + 'a' * 64
                (library / 'target.json').write_text(json.dumps({'arch': 'x86_64', 'source_run_id': '42', 'image': image}))
                for stream in ['8.3', '8.6']:
                    with self.assertRaisesRegex(RuntimeError, 'Missing successful'):
                        workflow.verify_upgrades(root, library, kind)
                    validation = root / ('shared-validation-' + kind + '-x86_64-php' + stream)
                    validation.mkdir()
                    report = validation / 'native-upgrade-receipt.json'
                    report.write_text(json.dumps({'ok': True, 'type': kind, 'php_stream': stream, 'architecture': 'x86_64'}))
                    (validation / ('library.' + kind)).write_text(stream)
                    receipt = workflow.upgrade_receipt(report, plan, validation, kind, '42', image, stream, 'x86_64', True)
                    (validation / 'shared-library-upgrade-receipt.json').write_text(json.dumps(receipt))
                    workflow.verify_upgrades(root, library, kind, [stream])
                workflow.verify_upgrades(root, library, kind)
                (validation / ('library.' + kind)).write_text('different owner payload')
                with self.assertRaisesRegex(RuntimeError, 'does not match'):
                    workflow.verify_upgrades(root, library, kind)

    def test_library_baselines_cover_target_stream_revisions_in_both_publish_modes(self):
        for kind in ('apk', 'deb'):
            for publish in ('false', 'true'):
                for streams, expected in [('', '82,83,84,85,86'), ('8.2', '82'), ('8.3,8.6', '83,86')]:
                    for libraries in (None, ['attr']):
                        with self.subTest(kind=kind, publish=publish, streams=streams, libraries=libraries), \
                                patch.dict(os.environ, MATRIX_ARCH='arm64', PHP_VERSIONS=streams, PUBLISH=publish, LIBRARIES=''), \
                                patch.object(glibc, 'run') as execute:
                            glibc.baseline(kind, Path('baseline'), libraries=libraries)
                            command = execute.call_args.args[0]
                            self.assertEqual(command[command.index('--owners') + 1], expected)


    def test_consumer_baselines_read_the_requested_streams_without_a_canonical_copy(self):
        for consumers in (False, True):
            with self.subTest(consumers=consumers), patch.dict(os.environ, MATRIX_ARCH='arm64', PHP_VERSIONS='8.2,8.6'), \
                    patch.object(glibc, 'run') as execute:
                glibc.baseline('apk', Path('baseline'), consumers=consumers, libraries=[])
                command = execute.call_args.args[0]
                self.assertEqual(command[command.index('--owners') + 1], '82,86')
                self.assertNotIn('--consumer-stream', command)


    def test_unchanged_library_plan_never_invokes_the_package_builder(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            sdk, stage = root / 'buildroot', root / 'stage'
            glibc.write(sdk / 'shared-libraries.json', {'libraries': {'attr': {}}})
            glibc.write(stage / 'shared-libraries.json', {'libraries': {'attr': {}}})
            glibc.write(stage / 'shared-library-plan.json', {'libraries': [], 'php_requests': {}})
            calls = []
            def execute(arguments, **kwargs):
                args = [str(value) for value in arguments]
                calls.append(args)
                if 'stage-publication-retries' in args:
                    glibc.write(stage / 'publication-retries.json', {'remaining_libraries': [], 'layout_required': False})
            with patch.object(glibc, 'ROOT', root), patch.object(glibc, 'STAGE', stage), patch.object(glibc, 'run', execute), \
                    patch.dict(os.environ, TYPE='apk', PACKAGES='', PUBLISH='false'):
                glibc.package_libraries('apk')
            self.assertFalse(any(args[:2] == ['php', 'bin/spp'] for args in calls))


    def test_consumer_refresh_keeps_streams_and_format_metadata_together(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, destination = root / 'consumer-baseline', root / 'baseline'
            glibc.write(source / 'shared-libraries.json', {'consumers': {'8.5': ['php-zts-cli']},
                'consumer_dependencies': {'8.5': {'php-zts-cli': ['openssl']}},
                'consumer_inventory_complete': True, 'consumer_inventory_streams': ['8.5'],
                'consumer_versions': {'8.5': {'php-zts-cli': 'rpm-version'}}})
            glibc.write(destination / 'shared-libraries.json', {'libraries': {'openssl': {'published_version': 'rpm-version'}},
                'consumer_versions': {'8.5': {'php-zts-cli': 'rpm-version'}}})
            glibc.share_consumers(source, destination)
            result = glibc.read(destination / 'shared-libraries.json')
            self.assertEqual(result['consumer_dependencies']['8.5']['php-zts-cli'], ['openssl'])
            self.assertEqual(result['consumer_versions']['8.5']['php-zts-cli'], 'rpm-version')
            self.assertEqual(result['libraries']['openssl']['published_version'], 'rpm-version')


    def test_real_alpine_target_record_selects_tls_once_per_architecture(self):
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
                self.assertFalse(any(entry.get('runtime-checks') for entry in selected))
                self.assertEqual([entry['php-version'] for entry in selected if entry['tls-checks']], ['8.6'])


    def test_tls_checks_are_deduplicated_per_format_and_architecture(self):
        values = [{'target': 'alma8-' + arch, 'type': kind, 'php-version': stream, 'packages': ''}
                  for arch in ['x86_64', 'arm64'] for kind in ['rpm', 'deb'] for stream in workflow.STREAMS]
        workflow.tls_checks(values)
        self.assertEqual(len([item for item in values if item['tls-checks']]), 4)
        self.assertTrue(all(item['php-version'] == '8.6' for item in values if item['tls-checks']))
        partial = [{'target': 'x86_64', 'type': 'apk', 'php-version': '8.6', 'packages': 'imagick'}]
        workflow.tls_checks(partial)
        self.assertFalse(partial[0]['tls-checks'])
        partial[0]['packages'] = 'curl'
        workflow.tls_checks(partial)
        self.assertTrue(partial[0]['tls-checks'])



if __name__ == '__main__':
    unittest.main()
