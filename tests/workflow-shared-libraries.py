#!/usr/bin/env python3

import argparse
import importlib.util
import io
import json
import os
import re
import runpy
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]


def module(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / '.github/actions/shared-libraries' / filename)
    value = importlib.util.module_from_spec(spec)
    with mock.patch.object(sys, 'path', [str(ROOT / '.github/actions/shared-libraries')] + sys.path):
        spec.loader.exec_module(value)
    return value


source = module('source_plan', 'source-plan.py')
workflow = module('shared_workflow', 'workflow.py')


class SourcePlanTests(unittest.TestCase):
    def setUp(self):
        self.metadata = {'php_versions': {stream: stream + '.1' for stream in source.STREAMS}, 'pie_version': '1.0', 'shared_configuration': 'same'}
        self.inventory = {'schema': 1, 'managed_library_artifacts': ['zlib'], 'library_tool_artifacts': ['pkg-config'], 'irrelevant_artifacts': ['php-src', 'ext-imagick', 'go-xcaddy']}

    def plan(self, previous, current):
        return source.plan(previous, current, self.metadata, self.metadata, library_ids=set(self.inventory['managed_library_artifacts']))

    def test_library_only_update_requests_no_php(self):
        old = {'zlib': {'source': {'hash': 'old'}}}
        new = {'zlib': {'source': {'hash': 'new'}}, 're2c': {'source': {'hash': 'new'}}}
        result = source.plan(old, new, self.metadata, self.metadata, library_ids={'zlib'})
        self.assertEqual(result['libraries'], ['zlib'])
        self.assertEqual(result['requests'], {})

    def test_combines_full_and_partial_requests_in_one_plan(self):
        current = json.loads(json.dumps(self.metadata))
        current['php_versions']['8.4'] = '8.4.2'
        result = source.plan({}, {'ext-imagick': {'source': {'hash': 'new'}}}, self.metadata, current)
        self.assertEqual(result['requests']['8.4'], {'full': True, 'packages': []})
        self.assertEqual(result['requests']['8.3'], {'full': False, 'packages': ['imagick']})

    def test_configuration_migration_and_explicit_full_request_are_full(self):
        for force, configuration in [(False, 'new'), (True, 'same')]:
            result = source.plan({}, {}, self.metadata, self.metadata, force=force)
            self.assertEqual(set(result['requests']), set(source.STREAMS) if force else set())
            self.assertTrue(all(request['full'] for request in result['requests'].values()))

    def test_pie_changes_rebuild_only_pie(self):
        for current in [dict(self.metadata, pie_version='2.0'), dict(self.metadata, pie_sha256='new-frozen-bytes')]:
            result = source.plan({}, {}, self.metadata, current)
            self.assertEqual(result['requests'], {})
            self.assertTrue(all(request == {'full': False, 'packages': ['pie']} for request in result['package_requests'].values()))

    def test_binary_only_artifacts_do_not_break_change_detection(self):
        result = source.plan({}, {'pkg-config': {'source': None, 'binary': {}}}, self.metadata, self.metadata, library_ids={'zlib'})
        self.assertEqual(result['requests'], {})
        self.assertEqual(result['libraries'], [])

    def test_library_build_tool_source_and_binary_changes_trigger_without_php(self):
        for old, new in [
            ({'source': None, 'binary': {'linux-x86_64': {'hash': 'old'}}}, {'source': None, 'binary': {'linux-x86_64': {'hash': 'new'}}}),
            ({'source': {'hash': 'old'}}, {'source': {'hash': 'new'}}),
        ]:
            with self.subTest(previous=old):
                result = self.plan({'pkg-config': old}, {'pkg-config': new})
                self.assertEqual(result['library_build_inputs'], [])
                self.assertEqual(result['libraries'], [])
                self.assertEqual(result['requests'], {})

    def test_managed_library_metadata_and_binary_changes_select_the_library(self):
        previous = {'source': {'hash': 'same', 'version': '1.0', 'config': {'ref': 'old'}}, 'binary': {'linux-x86_64': {'hash': 'old'}}}
        for field, value in [('version', '1.1'), ('config', {'ref': 'new'}), ('binary', {'linux-x86_64': {'hash': 'new'}})]:
            with self.subTest(field=field):
                current = json.loads(json.dumps(previous))
                if field == 'binary':
                    current[field] = value
                else:
                    current['source'][field] = value
                result = self.plan({'zlib': previous}, {'zlib': current})
                self.assertEqual(result['libraries'], ['zlib'])
                self.assertEqual(result['library_build_inputs'], [])
                self.assertEqual(result['requests'], {})

    def test_download_timestamps_do_not_trigger_but_nested_configuration_does(self):
        previous = {'source': {'hash': 'same', 'time': 1, 'config': {'time': 'meaningful'}}, 'binary': {'linux-x86_64': {'hash': 'same', 'time': 1}}}
        current = json.loads(json.dumps(previous))
        current['source']['time'] = 2
        current['binary']['linux-x86_64']['time'] = 2
        self.assertEqual(self.plan({'pkg-config': previous}, {'pkg-config': current})['library_build_inputs'], [])
        current['source']['config']['time'] = 'changed configuration'
        self.assertEqual(self.plan({'pkg-config': previous}, {'pkg-config': current})['library_build_inputs'], [])

    def test_go_changes_rebuild_frankenphp_without_libraries_or_php_core(self):
        previous = {'go-xcaddy': {'binary': {'linux-x86_64': {'hash': 'old'}}}, 'ext-imagick': {'source': {'hash': 'old'}}}
        current = {'go-xcaddy': {'binary': {'linux-x86_64': {'hash': 'new'}}}, 'ext-imagick': {'source': {'hash': 'new'}}}
        result = self.plan(previous, current)
        self.assertEqual(result['library_build_inputs'], [])
        self.assertEqual(result['libraries'], [])
        self.assertTrue(all(request == {'full': False, 'packages': ['imagick']} for request in result['requests'].values()))
        self.assertTrue(all(request == {'full': False, 'packages': ['frankenphp']} for request in self.plan({'go-xcaddy': previous['go-xcaddy']}, {'go-xcaddy': current['go-xcaddy']})['requests'].values()))

    def test_unclassified_inputs_and_required_tools_win_over_exclusions(self):
        self.inventory['irrelevant_artifacts'].append('pkg-config')
        result = self.plan({}, {'future-input': {'source': {'hash': 'new'}}, 'pkg-config': {'binary': {'linux-x86_64': {'hash': 'new'}}}})
        self.assertEqual(result['library_build_inputs'], [])
        self.assertEqual(result['requests'], {})

    def test_binary_tool_update_emits_a_downstream_trigger_and_snapshot_request(self):
        with tempfile.TemporaryDirectory(prefix='shared-workflow-test-') as temporary:
            root = Path(temporary)
            (root / 'downloads').mkdir()
            (root / '.download-state').mkdir()
            metadata = dict(self.metadata, shared_configuration='source-only')
            for name in ['downloads/.spc-meta.json', '.download-state/meta.prev.json']:
                (root / name).write_text(json.dumps(metadata))
            for name, hash_value in [('.download-state/lock.prev.json', 'old'), ('downloads/.cache.json', 'new')]:
                (root / name).write_text(json.dumps({'pkg-config': {'source': None, 'binary': {'linux-x86_64': {'hash': hash_value}}}}))
            output = io.StringIO()
            with mock.patch('sys.argv', ['source-plan.py', '--root', str(root)]), mock.patch('sys.stdout', output), mock.patch.object(source.subprocess, 'check_output', return_value=json.dumps(self.inventory)):
                source.main()
            self.assertIn('any_change=false\n', output.getvalue())
            request = json.loads((root / 'downloads/build-plan.json').read_text())
            self.assertEqual(request['requests'], {})
            self.assertEqual(request['libraries'], [])
            self.assertEqual(request['library_build_inputs'], [])
            self.assertEqual(request['build_inputs'], self.inventory)


    def test_global_configuration_changes_do_not_request_full_builds(self):
        for name in ['src/hook/Php86Performance.php', 'config/patches/php86-performance-prs.patch', 'spc.registry.yml']:
            with self.subTest(path=name), tempfile.TemporaryDirectory(prefix='shared-workflow-test-') as temporary:
                root = Path(temporary)
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('original')
                previous = dict(self.metadata, shared_configuration='source-only')
                path.write_text('changed')
                result = source.plan({}, {}, previous, previous)
                self.assertEqual(result['requests'], {})



class MatrixTests(unittest.TestCase):
    def write_plan(self, root, target, requests, **extra):
        directory = root / ('shared-plans-rpm-' + target)
        directory.mkdir()
        (directory / 'target.json').write_text(json.dumps({'target': target, 'arch': target.split('-', 1)[1], 'alma': '8'}))
        (directory / 'shared-library-plan.json').write_text(json.dumps(dict(php_requests=requests, **extra)))

    def test_failed_architecture_does_not_remove_successful_architecture(self):
        with tempfile.TemporaryDirectory(prefix='shared-workflow-test-') as temporary:
            root = Path(temporary)
            for target in ['alma8-x86_64', 'alma8-arm64']:
                self.write_plan(root, target, {'8.4': {'full': True, 'packages': []}})
            builds, publish = workflow.matrix(root, ['8.4'], ['alma8-arm64'])
            self.assertEqual([item['target'] for item in builds], ['alma8-arm64'])
            self.assertEqual([item['target'] for item in publish], ['alma8-arm64'])

    def test_compatible_library_update_publishes_without_php(self):
        with tempfile.TemporaryDirectory(prefix='shared-workflow-test-') as temporary:
            root = Path(temporary)
            self.write_plan(root, 'alma8-x86_64', {'8.4': {'full': False, 'packages': []}})
            builds, publish = workflow.matrix(root, workflow.STREAMS)
            self.assertEqual(builds, [])
            self.assertEqual(len(publish), 1)

    def test_breaking_transition_overrides_manual_php_filter(self):
        with tempfile.TemporaryDirectory(prefix='shared-workflow-test-') as temporary:
            root = Path(temporary)
            requests = {stream: {'full': True, 'packages': []} for stream in workflow.STREAMS}
            self.write_plan(root, 'alma8-x86_64', requests, abi_break=True)
            builds, _ = workflow.matrix(root, ['8.4'])
            self.assertEqual([item['php-version'] for item in builds], workflow.STREAMS)

    def test_partial_request_is_carried_into_build(self):
        with tempfile.TemporaryDirectory(prefix='shared-workflow-test-') as temporary:
            root = Path(temporary)
            self.write_plan(root, 'alma8-x86_64', {'8.4': {'full': False, 'packages': ['imagick', 'redis']}})
            builds, _ = workflow.matrix(root, ['8.4'])
            self.assertEqual(builds[0]['packages'], 'imagick,redis')

    def test_removed_el_targets_are_rejected_before_registry_or_repository_reads(self):
        for alma in ('9', '10'):
            for kind in ('rpm', 'glibc'):
                argv = ['workflow.py', 'setup', '--type=' + kind, '--alma-versions=' + alma, '--resolve-images']
                with self.subTest(kind=kind, alma=alma), mock.patch('sys.argv', argv), \
                        mock.patch.object(workflow.subprocess, 'check_output') as inspect:
                    with self.assertRaisesRegex(RuntimeError, 'Only the EL8'):
                        workflow.main()
                    inspect.assert_not_called()
            result = subprocess.run([sys.executable, str(ROOT / 'bin/freeze-migration-baseline.py'),
                '--type=rpm', '--php=8.5', '--arch=x86_64', '--el=el' + alma, '--output=unused'], capture_output=True, text=True)
            self.assertEqual(result.returncode, 2)
            self.assertIn('invalid choice', result.stderr)

    def test_builder_tags_are_resolved_once_and_pinned_for_all_arches(self):
        digest = 'sha256:' + '1' * 64
        for kind, calls, targets in [('rpm', 1, 2), ('glibc', 2, 2), ('apk', 1, 2)]:
            output = io.StringIO()
            argv = ['workflow.py', 'setup', '--type=' + kind, '--resolve-images']
            with self.subTest(kind=kind), mock.patch('sys.argv', argv), mock.patch('sys.stdout', output), mock.patch.object(workflow.subprocess, 'check_output', return_value=json.dumps({'digest': digest})) as inspect:
                workflow.main()
                values = dict(line.split('=', 1) for line in output.getvalue().splitlines())
                matrix = json.loads(values['matrix'])['include']
                self.assertEqual(inspect.call_count, calls)
                self.assertEqual(len(matrix), targets)
                self.assertTrue(all(value['image'].endswith('@' + digest) for value in matrix))
                if kind == 'glibc':
                    self.assertTrue(all(image.endswith('@' + digest) for value in matrix for image in value['package_images'].values()))

    def test_glibc_builder_digest_pins_only_the_producer(self):
        digest = 'sha256:' + 'a' * 64
        builder = 'ghcr.io/static-php/packages-builder-rhel-8@' + digest
        packager = 'ghcr.io/static-php/packages-packager-debian'
        output = io.StringIO()
        argv = ['workflow.py', 'setup', '--type=glibc', '--builder-digest=' + digest, '--resolve-images']
        with mock.patch('sys.argv', argv), mock.patch('sys.stdout', output), \
                mock.patch.object(workflow.subprocess, 'check_output', side_effect=[json.dumps({'digest': digest}), json.dumps({'digest': 'sha256:' + 'b' * 64})]) as inspect:
            workflow.main()
        matrix = json.loads(dict(line.split('=', 1) for line in output.getvalue().splitlines())['matrix'])['include']
        self.assertEqual([call.args[0][4] for call in inspect.call_args_list], [builder, packager + ':latest'])
        self.assertEqual({value['arch'] for value in matrix}, {'x86_64', 'arm64'})
        for value in matrix:
            self.assertEqual(value['image'], builder)
            self.assertEqual(value['package_images'], {'rpm': builder, 'deb': packager + '@sha256:' + 'b' * 64})

    def test_builder_digest_rejects_invalid_input_before_registry_reads(self):
        for kind, digest in [('glibc', 'latest'), ('glibc', 'sha256:' + 'A' * 64), ('glibc', 'sha256:1234'), ('apk', 'sha256:' + 'a' * 64)]:
            argv = ['workflow.py', 'setup', '--type=' + kind, '--builder-digest=' + digest, '--resolve-images']
            with self.subTest(kind=kind, digest=digest), mock.patch('sys.argv', argv), \
                    mock.patch.object(workflow.subprocess, 'check_output') as inspect:
                with self.assertRaisesRegex(RuntimeError, 'Builder digest requires'):
                    workflow.main()
                inspect.assert_not_called()


class ProgramStackTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('php') and (ROOT / 'vendor/autoload.php').exists(), 'PHP and Composer dependencies required')
    def test_only_apk_programs_request_larger_musl_pthread_stacks(self):
        script = r'''
require $argv[1] . '/vendor/autoload.php';
$result = [];
foreach (['apk' => '3.24', 'deb' => '11', 'rpm' => '8'] as $type => $os) {
    foreach (['x86_64', 'aarch64'] as $arch) {
        foreach (['8.2', '8.3', '8.4', '8.5', '8.6', '8.10'] as $version) {
            $libdir = $type === 'rpm' ? '/usr/lib64' : '/usr/lib';
            $variables = [
                'php_version' => $version, 'php_source_version' => $version,
                'target' => null, 'arch' => $arch, 'os' => $os, 'type' => $type,
                'prefix' => 'php-zts', 'release_suffix' => 'zts' . str_replace('.', '', $version),
                'confdir' => '/etc/php-zts', 'moduledir' => $libdir . '/php-zts/modules',
                'shared_libdir' => $libdir . '/php-zts/lib', 'filter_libraries' => null,
                'filter_packages' => null, 'ci' => false,
                'allow_shared_ext_failure' => version_compare($version, '8.6', '>='),
            ];
            $rendered = staticphp\util\TwigRenderer::renderFile($argv[1] . '/config/templates/craft.yml.twig', $variables);
            $result[] = ['type' => $type, 'arch' => $arch, 'php' => $version,
                'env' => Symfony\Component\Yaml\Yaml::parse($rendered)['extra-env']];
        }
    }
}
echo json_encode($result, JSON_THROW_ON_ERROR);
'''
        rows = json.loads(subprocess.check_output(['php', '-r', script, str(ROOT)], universal_newlines=True))
        self.assertEqual(len(rows), 36)
        for row in rows:
            with self.subTest(type=row['type'], arch=row['arch'], php=row['php']):
                env = row['env']
                key = 'SPC_CMD_VAR_PHP_MAKE_EXTRA_LDFLAGS_PROGRAM'
                if row['type'] == 'apk':
                    self.assertEqual(env[key], '-Wl,-z,stack-size=0x80000')
                    self.assertEqual(env['SPC_LIBC'], 'musl')
                else:
                    self.assertNotIn(key, env)
                for key in ['SPC_DEFAULT_LDFLAGS', 'SPC_CMD_VAR_PHP_MAKE_EXTRA_LDFLAGS', 'SPC_CMD_VAR_PHP_MAKE_EXTRA_LDFLAGS_LIBPHP']:
                    self.assertNotIn('stack-size', env[key])
                self.assertIn('-Wl,-z,noexecstack', env['SPC_DEFAULT_LDFLAGS'])
                self.assertIn('-Wl,--build-id=sha1 -pie -release ', env['SPC_CMD_VAR_PHP_MAKE_EXTRA_LDFLAGS'])
                self.assertEqual(env['SPC_CMD_VAR_PHP_MAKE_EXTRA_LDFLAGS_LIBPHP'], '-Wl,-Bsymbolic-functions -Wc,-ftls-model=initial-exec')


class CacheAndCohortTests(unittest.TestCase):
    def test_alpine_builder_returns_ownership_after_success_and_failure(self):
        for status in [0, 7]:
            with self.subTest(status=status), tempfile.TemporaryDirectory(prefix='shared-workflow-test-') as temporary:
                root = Path(temporary)
                for name in ['dist/apk', 'shims', 'composer-cache', '.github/actions/shared-libraries']:
                    (root / name).mkdir(parents=True)
                (root / '.github/actions/shared-libraries/publish-forgejo.py').write_text(
                    'import sys\nsys.exit(' + str(status) + ')\n')
                chown = root / 'shims/chown'
                chown.write_text('#!/bin/sh\nprintf "%s\\n" "$@" >> "$OWNER_CALL"\n')
                chown.chmod(0o755)
                env = dict(os.environ, TYPE='apk', IN_BUILDER='true', MODE='full',
                           PATH=str(root / 'shims') + ':' + os.environ['PATH'],
                           BUILDER_HOST_UID='1001', BUILDER_HOST_GID='1002',
                           COMPOSER_CACHE_DIR=str(root / 'composer-cache'), OWNER_CALL=str(root / 'ownership'))
                result = subprocess.run(['bash', str(ROOT / '.github/actions/shared-libraries/run.sh'), 'upload-1'],
                                        cwd=root, env=env, capture_output=True, text=True)
                self.assertEqual(result.returncode, status, result.stderr)
                self.assertEqual((root / 'ownership').read_text().splitlines(),
                                 ['-R', '1001:1002', str(root), '-R', '1001:1002', str(root / 'composer-cache')])

    def test_alpine_builder_trusts_only_downloaded_source_repositories(self):
        with tempfile.TemporaryDirectory(prefix='shared-workflow-test-') as temporary:
            root = Path(temporary)
            for name in ['libargon2', 'source with spaces']:
                subprocess.run(['git', 'init', '-q', str(root / 'downloads' / name)], check=True)
            (root / 'downloads/archive').mkdir()
            home = root / 'home'
            home.mkdir()
            env = dict(os.environ, HOME=str(home), GIT_CONFIG_NOSYSTEM='1',
                       GIT_CONFIG_GLOBAL=str(home / '.gitconfig'), TYPE='apk', IN_BUILDER='true', MODE='full')
            result = subprocess.run(['bash', str(ROOT / '.github/actions/shared-libraries/run.sh'), 'unknown'],
                                    cwd=root, env=env, capture_output=True, text=True)
            self.assertIn('Unknown shared-library workflow command', result.stderr)
            trusted = subprocess.check_output(['git', 'config', '--global', '--get-all', 'safe.directory'],
                                              env=env, text=True).splitlines()
            self.assertEqual(trusted, [str(root / 'downloads/libargon2'), str(root / 'downloads/source with spaces')])

    def test_classification_hands_off_independent_owner_revisions_from_one_sdk(self):
        pipeline = module('native_pipeline', 'glibc.py')
        for kind in ['apk', 'rpm', 'deb']:
            for bump in ['false', 'true']:
                with self.subTest(kind=kind, bump=bump), tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    stage = root / 'stage'
                    with mock.patch.object(pipeline, 'ROOT', root), mock.patch.object(pipeline, 'STAGE', stage), \
                            mock.patch.dict(os.environ, BUMP=bump, PUBLISH='true', LIBRARIES='', PACKAGES='', ITERATION=''):
                        sdk, directory = pipeline.format_sdk(kind), pipeline.format_stage(kind)
                        sdk.mkdir(parents=True)
                        directory.mkdir(parents=True, exist_ok=True)
                        planned = {'libraries': {'zlib': {'package_versions': {kind: '1.3.1-8'}}}}
                        pipeline.write(directory / 'shared-libraries.json', planned)
                        pipeline.write(directory / 'shared-library-plan.json', {'libraries': ['zlib'],
                            'library_streams': ['8.3', '8.6'] if kind != 'rpm' else [], 'php_requests': {}})
                        packaged, seals = {}, []
                        def execute(arguments, **kwargs):
                            args = [str(arg) for arg in arguments]
                            option = lambda name: Path(args[args.index(name) + 1])
                            if 'seal' in args:
                                seals.append(option('--root'))
                            elif 'stage-publication-retries' in args:
                                metadata = pipeline.read(option('--manifest'))
                                if kind != 'rpm':
                                    self.assertNotIn(kind, metadata['libraries']['zlib']['package_versions'])
                                pipeline.write(option('--output'), {'remaining_libraries': ['zlib']})
                            elif args[:3] == ['php', 'bin/spp', 'package']:
                                env = kwargs['env']
                                owner = env.get('SPP_FORGEJO_OWNER', 'rpm')
                                manifest = Path(next(arg.split('=', 1)[1] for arg in args if arg.startswith('--library-manifest=')))
                                self.assertIn('--buildroot=' + str(sdk), args)
                                self.assertEqual('--bump' in args, bump == 'true')
                                if kind != 'rpm':
                                    stream = owner[0] + '.' + owner[1:]
                                    self.assertIn('--phpv=' + stream, args)
                                    self.assertEqual(Path(env['SPP_NATIVE_INVENTORY']).name, stream + '.json')
                                    self.assertEqual(Path(env['SPP_DIST_PATH']), directory / 'owner-libraries' / stream)
                                revision = (1 if owner == '83' else 8) + (bump == 'true')
                                metadata = pipeline.read(manifest)
                                metadata['libraries']['zlib']['package_versions'][kind] = '1.3.1-' + str(revision)
                                packaged[owner] = (manifest, metadata)
                            elif 'manifest' in args:
                                owner = kwargs['env'].get('SPP_FORGEJO_OWNER', 'rpm')
                                pipeline.write(option('--output'), packaged[owner][1])
                        with mock.patch.object(pipeline, 'run', execute):
                            pipeline.package_libraries(kind)
                        self.assertEqual(seals, [sdk])
                        self.assertEqual(pipeline.read(sdk / 'shared-libraries.json'), planned)
                        self.assertEqual(set(packaged), {'rpm'} if kind == 'rpm' else {'83', '86'})
                        for owner, (manifest, metadata) in packaged.items():
                            expected = (1 if owner == '83' else 8) + (bump == 'true')
                            result = pipeline.read(directory / 'shared-libraries.json' if kind == 'rpm' else manifest)
                            self.assertEqual(result['libraries']['zlib']['package_versions'][kind], '1.3.1-' + str(expected))

    def test_classified_library_metadata_is_restored_before_php_build(self):
        with tempfile.TemporaryDirectory(prefix='shared-workflow-test-') as temporary:
            root = Path(temporary)
            for directory in ['stage', 'buildroot', 'shims', 'tests']:
                (root / directory).mkdir(parents=True)
            (root / 'stage/shared-libraries.json').write_text('{"libraries":{"example":{"version":"1.0"}}}')
            (root / 'stage/shared-library-plan.json').write_text('{"schema":1}')
            helper = root / '.github/actions/shared-libraries/workflow.py'
            helper.parent.mkdir(parents=True)
            shutil.copy2(ROOT / '.github/actions/shared-libraries/workflow.py', helper)
            (helper.parent / 'glibc.py').write_text('''from pathlib import Path
assert Path('buildroot/shared-libraries.json').read_bytes() == Path('stage/shared-libraries.json').read_bytes()
''')
            (root / 'tests/upgrade-guards.py').write_text('')
            for name in ['git', 'composer']:
                path = root / 'shims' / name
                path.write_text('#!/bin/sh\nexit 0\n')
                path.chmod(0o755)
            php = root / 'shims/php'
            php.write_text('#!/bin/sh\nif [ "$1" = bin/spp ] && [ "$2" = package ]; then\n cmp stage/shared-libraries.json buildroot/shared-libraries.json || exit 92\nfi\n')
            php.chmod(0o755)
            env = dict(os.environ, PATH=str(root / 'shims') + ':' + os.environ['PATH'], TYPE='deb', PHPV='8.4', PACKAGES='', ITERATION='', BUMP='false', SOURCE_RUN_ID='101', IMAGE='ghcr.io/builder@sha256:' + '1' * 64)
            result = subprocess.run(['bash', str(ROOT / '.github/actions/shared-libraries/run.sh'), 'php'], cwd=root, env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            receipt = json.loads((root / 'dist/deb/shared-library-build-receipt.json').read_text())
            self.assertEqual(receipt['source_run_id'], '101')
            self.assertEqual(receipt['php_stream'], '8.4')

    def test_owner_packaging_failure_keeps_other_owners_and_no_partial_archives(self):
        pipeline = module('native_pipeline_isolation', 'glibc.py')
        for failures in [{'8.3'}, {'8.3', '8.6'}]:
            with self.subTest(failures=failures), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                stage = root / 'stage'
                pipeline.write(stage / 'shared-libraries.json', {'libraries': {'zlib': {'package_versions': {}}}})
                pipeline.write(stage / 'shared-library-plan.json', {'libraries': [],
                    'php_requests': {'8.3': {'packages': ['cli']}, '8.6': {'full': True}}})
                attempted = []
                def package(kind, directory, sdk, manifest, stream):
                    attempted.append(stream)
                    (manifest.parent / 'partial.apk').write_text('partial')
                    if stream in failures:
                        raise RuntimeError('owner inventory unavailable')
                with mock.patch.object(pipeline, 'ROOT', root), mock.patch.object(pipeline, 'STAGE', stage), \
                        mock.patch.object(pipeline, 'run'), mock.patch.object(pipeline, 'package_library_archives', side_effect=package), \
                        mock.patch.dict(os.environ, PUBLISH='true'):
                    if len(failures) == 2:
                        with self.assertRaisesRegex(RuntimeError, 'every selected apk owner'):
                            pipeline.package_libraries('apk')
                    else:
                        pipeline.package_libraries('apk')
                self.assertEqual(attempted, ['8.3', '8.6'])
                for stream in attempted:
                    self.assertEqual((stage / 'owner-libraries' / stream).exists(), stream not in failures)
                with self.assertRaisesRegex(RuntimeError, 'Missing library packages for PHP 8.3'):
                    workflow.stage_libraries(stage, root / 'dist', 'apk', '8.3', root / 'selected.json')


    def test_assembly_deduplicates_and_rejects_conflicting_packages(self):
        with tempfile.TemporaryDirectory(prefix='shared-workflow-test-') as temporary:
            root = Path(temporary)
            for artifact in ['libs', 'php']:
                (root / 'artifacts' / artifact).mkdir(parents=True)
                (root / 'artifacts' / artifact / 'lib.deb').write_bytes(b'same')
            workflow.assemble(root / 'artifacts', root / 'dist', 'deb')
            self.assertEqual((root / 'dist/lib.deb').read_bytes(), b'same')
            (root / 'artifacts/php/lib.deb').write_bytes(b'conflict')
            with self.assertRaisesRegex(RuntimeError, 'Conflicting cohort package'):
                workflow.assemble(root / 'artifacts', root / 'dist', 'deb')

    def test_owner_staging_and_publication_select_only_the_matching_archives_and_floors(self):
        for kind in ['deb', 'apk']:
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                library = root / 'artifacts' / ('shared-libs-' + kind + '-x86_64')
                library.mkdir(parents=True)
                (library / 'shared-library-plan.json').write_text(json.dumps({'library_streams': ['8.3', '8.6', '8.4']}))
                for stream, revision in [('8.3', 2), ('8.6', 9)]:
                    owner = library / 'owner-libraries' / stream
                    (owner / kind).mkdir(parents=True)
                    (owner / kind / ('library.' + kind)).write_text(stream)
                    (owner / 'shared-libraries.json').write_text(json.dumps({'libraries': {'zlib': {
                        'package_versions': {kind: '1.3.1-' + str(revision)}}}}))
                    destination = root / ('staged-' + stream)
                    manifest = destination / 'shared-libraries.json'
                    workflow.stage_libraries(library, destination, kind, stream, manifest)
                    self.assertEqual((destination / ('library.' + kind)).read_text(), stream)
                    self.assertEqual(json.loads(manifest.read_text())['libraries']['zlib']['package_versions'][kind], '1.3.1-' + str(revision))
                    published = root / ('published-' + stream)
                    workflow.assemble(root / 'artifacts', published, kind, [stream])
                    self.assertEqual((published / ('library.' + kind)).read_text(), stream)
                with self.assertRaisesRegex(RuntimeError, 'Missing library packages for PHP 8.4'):
                    workflow.stage_libraries(library, root / 'missing', kind, '8.4', root / 'missing.json')
                php_only = root / 'php-only-source'
                php_only.mkdir()
                (php_only / 'shared-library-plan.json').write_text('{}')
                (php_only / 'shared-libraries.json').write_text(json.dumps({'libraries': {'zlib': {'package_versions': {kind: '1.3.1-9'}}}}))
                workflow.stage_libraries(php_only, root / 'php-only', kind, '8.5', root / 'php-only.json')
                self.assertEqual(json.loads((root / 'php-only.json').read_text())['libraries']['zlib']['package_versions'], {})



class ArtifactReceiptTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='shared-workflow-test-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.library = self.root / 'cohort/shared-libs-deb-amd64'
        self.library.mkdir(parents=True)
        self.plan = self.library / 'shared-library-plan.json'
        self.plan.write_text('{"schema":1,"php_requests":{"8.4":{"full":true}}}')
        self.image = 'ghcr.io/builder@sha256:' + '1' * 64
        (self.library / 'target.json').write_text(json.dumps({'source_run_id': '101', 'image': self.image}))
        self.receipt = workflow.build_receipt(self.plan, '101', self.image, 'libraries')
        (self.library / 'shared-library-build-receipt.json').write_text(json.dumps(self.receipt))
        (self.library / 'library.deb').write_bytes(b'library')

    def consumer(self, changes=None, missing=False):
        directory = self.root / 'cohort/shared-consumers-deb-amd64-php8.4'
        directory.mkdir()
        (directory / 'php-cli.deb').write_bytes(b'consumer')
        if not missing:
            receipt = workflow.build_receipt(self.plan, '101', self.image, 'consumer', '8.4')
            receipt.update(changes or {})
            (directory / 'shared-library-build-receipt.json').write_text(json.dumps(receipt))
        return directory

    def test_stale_source_plan_image_and_stream_cannot_count_or_assemble(self):
        for changes in [{'source_run_id': '100'}, {'plan_sha256': 'old-plan'}, {'image': 'ghcr.io/builder@sha256:' + '2' * 64}, {'php_stream': '8.3'}]:
            with self.subTest(changes=changes):
                directory = self.consumer(changes)
                workflow.prune_consumers(self.root / 'cohort', self.library)
                self.assertFalse(directory.exists())
                destination = self.root / 'dist'
                workflow.assemble(self.root / 'cohort', destination, 'deb')
                self.assertEqual(sorted(path.name for path in destination.iterdir()), ['library.deb'])
                shutil.rmtree(destination)

    def test_missing_legacy_consumer_receipt_is_excluded(self):
        directory = self.consumer(missing=True)
        workflow.prune_consumers(self.root / 'cohort', self.library)
        self.assertFalse(directory.exists())

    def test_matching_previous_success_can_be_reused_on_failed_job_rerun(self):
        directory = self.consumer()
        with mock.patch.dict(os.environ, {'GITHUB_RUN_ATTEMPT': '2'}):
            workflow.prune_consumers(self.root / 'cohort', self.library)
        self.assertTrue(directory.is_dir())
        workflow.assemble(self.root / 'cohort', self.root / 'dist', 'deb')
        self.assertTrue((self.root / 'dist/php-cli.deb').is_file())

    def test_invalid_or_missing_library_receipt_fails_closed(self):
        receipt = self.library / 'shared-library-build-receipt.json'
        receipt.write_text(json.dumps(dict(self.receipt, plan_sha256='stale-plan')))
        with self.assertRaisesRegex(RuntimeError, 'invalid build receipt'):
            workflow.prune_consumers(self.root / 'cohort', self.library)
        receipt.unlink()
        with self.assertRaises(FileNotFoundError):
            workflow.prune_consumers(self.root / 'cohort', self.library)


class WorkflowContractTests(unittest.TestCase):
    def publication_steps(self, data):
        call = data['jobs']['publish']
        publisher = yaml.safe_load((ROOT / call['uses']).read_text())
        job = 'publish-apk' if call['with']['format'] == 'apk' else 'publish-glibc'
        return publisher['jobs'][job]['steps']

    def workflows(self):
        for name in ['build-rpm-modular-packages.yml', 'build-apk-forgejo.yml']:
            path = ROOT / '.github/workflows' / name
            yield name, yaml.safe_load(path.read_text())

    def test_builds_stage_and_publication_checks_the_whole_cohort(self):
        for name, data in self.workflows():
            if 'rpm' in name:
                packaging = yaml.safe_load((ROOT / '.github/workflows/package-glibc.yml').read_text())
                data['jobs'].update({key: value for key, value in packaging['jobs'].items() if key not in data['jobs']})
            for job_name in (['build-libs', 'build-payload', 'package'] if 'rpm' in name else ['build-libs', 'build']):
                runs = '\n'.join(step.get('run', '') for step in data['jobs'][job_name]['steps'])
                self.assertNotIn(' upload ', runs, name)
                self.assertNotIn('rsync ', runs, name)
            publish = self.publication_steps(data)
            self.assertTrue(any(step.get('run', '').endswith('publication.py publish-available') for step in publish))
            self.assertIn('!cancelled()', data['jobs']['publish']['if'])

    def test_uniquely_owned_staging_artifacts_can_be_replaced_on_rerun(self):
        names = {'Stage target plan', 'Stage library packages', 'Stage consumer packages', 'Stage common SDK metadata', 'Stage common SDK buildroot', 'Stage RPM library packages', 'Stage DEB library packages', 'Stage sealed common payload', 'Stage validated consumer packages'}
        for workflow_name, data in self.workflows():
            artifact_names = []
            groups = [job.get('steps', []) for job in data['jobs'].values()]
            action = 'php-payload' if 'rpm' in workflow_name else 'apk-packages'
            groups.append(yaml.safe_load((ROOT / '.github/actions' / action / 'action.yml').read_text())['runs']['steps'])
            for steps in groups:
                for step in steps:
                    if step.get('name') in names:
                        self.assertTrue(step['with'].get('overwrite'), workflow_name + ': ' + step['name'])
                        artifact_names.append(step['with']['name'])
            self.assertTrue(artifact_names)
            self.assertEqual(len(set(artifact_names)), len(artifact_names))

    def test_source_is_downloaded_before_buildroot_cache(self):
        for name, data in self.workflows():
            steps = data['jobs']['build-libs']['steps']
            names = [step['name'] for step in steps]
            self.assertLess(names.index('Download pinned source snapshot'), names.index('Compute source and toolchain fingerprint'), name)

    def test_every_job_uses_the_same_resolved_source_snapshot(self):
        for name, data in self.workflows():
            setup = data['jobs']['setup-matrix']
            self.assertIn('downloads-run-id', setup['outputs'])
            for job_name in (['build-libs', 'build-payload'] if 'rpm' in name else ['build-libs', 'build']):
                step = next(step for step in data['jobs'][job_name]['steps'] if step['name'] == 'Download pinned source snapshot')
                self.assertIn('source-snapshot.py fetch', step['run'])
                self.assertEqual(data['jobs'][job_name]['env']['SOURCE_RUN_ID'], '${{ needs.setup-matrix.outputs.downloads-run-id }}', name)

    def test_planned_full_build_overrides_manual_package_filter(self):
        for name, data in self.workflows():
            job = 'build-payload' if 'rpm' in name else 'build'
            self.assertEqual(data['jobs'][job]['env']['PACKAGES'], '${{ matrix.packages }}')

    def test_glibc_has_only_el8_producers_and_format_only_consumers(self):
        result = subprocess.check_output([sys.executable, str(ROOT / '.github/actions/shared-libraries/workflow.py'),
                                          'setup', '--type=glibc', '--architectures=amd64,arm64'], text=True)
        values = json.loads(next(line.split('=', 1)[1] for line in result.splitlines() if line.startswith('matrix=')))['include']
        self.assertEqual(len(values), 2)
        self.assertEqual({value['alma'] for value in values}, {'8'})
        self.assertEqual({value['arch'] for value in values}, {'x86_64', 'arm64'})
        self.assertTrue(all('rhel-8' in value['image'] for value in values))
        data = yaml.safe_load((ROOT / '.github/workflows/package-glibc.yml').read_text())
        script = '\n'.join(step.get('run', '') for step in data['jobs']['package']['steps'])
        self.assertIn('glibc-package', script)
        self.assertNotIn('bin/spp all', script)
        self.assertIn('migration-upgrade-containers.py', script)
        self.assertIn('--library-only', script)

    def test_glibc_validation_only_cannot_publish_or_save_release_caches(self):
        data = dict(self.workflows())['build-rpm-modular-packages.yml']
        packaging = yaml.safe_load((ROOT / '.github/workflows/package-glibc.yml').read_text())
        self.assertIn('inputs.publish == true', packaging['jobs']['publish']['if'])
        runner = (ROOT / '.github/actions/shared-libraries/run.sh').read_text()
        self.assertNotIn('--baseline-directory', runner)
        self.assertIn('--remote-directory \"$RPM_DIRECTORY\"', runner)
        cache = next(step for step in data['jobs']['build-libs']['steps'] if step['name'] == 'Save buildroot cache')
        self.assertIn('inputs.publish == true', cache['if'])
        self.assertEqual(data['jobs']['build-libs']['env']['BUMP'], '${{ inputs.bump || false }}')
        self.assertEqual(packaging['jobs']['package']['env']['TLS_CHECKS'], '${{ matrix.tls-checks }}')
        publish = self.publication_steps(packaging)
        names = [step['name'] for step in publish]
        self.assertLess(names.index('Verify selected build target'), names.index('Publish each validated PHP stream independently'))
        self.assertTrue(any(step.get('run', '').endswith('publication.py publish-available') for step in self.publication_steps(packaging)))

    def test_alpine_keeps_actions_on_glibc_host(self):
        data = dict(self.workflows())['build-apk-forgejo.yml']
        self.assertTrue(all('container' not in job for job in data['jobs'].values()))
        runner = (ROOT / '.github/actions/shared-libraries/run.sh').read_text()
        self.assertIn('exec docker run --rm', runner)

    def test_actions_are_sha_pinned_and_shell_bodies_parse(self):
        paths = list((ROOT / '.github/workflows').glob('build-*.yml')) + [ROOT / '.github/workflows/spc-download.yml']
        for path in paths:
            data = yaml.safe_load(path.read_text())
            for job in data['jobs'].values():
                for step in job.get('steps', []):
                    if 'uses' in step and not step['uses'].startswith('./'):
                        self.assertRegex(step['uses'], r'@[a-f0-9]{40}$', str(path))
                    if 'run' in step:
                        script = re.sub(r'\$\{\{.*?\}\}', 'VALUE', step['run'])
                        result = subprocess.run(['bash', '-n'], input=script, text=True, capture_output=True)
                        self.assertEqual(result.returncode, 0, str(path) + ': ' + result.stderr)

    def test_single_automatic_dispatch_loop(self):
        data = yaml.safe_load((ROOT / '.github/workflows/spc-download.yml').read_text())
        step = next(step for step in data['jobs']['download']['steps'] if step['name'] == 'Trigger downstream workflows')
        self.assertEqual(step['run'].count('gh workflow run'), 1)
        self.assertIn('-f mode=incremental', step['run'])
        self.assertNotIn('-f php_versions=', step['run'])

    def test_daily_downloads_do_not_force_calendar_rebuilds(self):
        data = yaml.safe_load((ROOT / '.github/workflows/spc-download.yml').read_text())
        self.assertEqual(data[True]['schedule'], [{'cron': '0 22 * * *'}])
        step = next(step for step in data['jobs']['download']['steps'] if step['name'] == 'Diff state and compute triggers')
        self.assertEqual(step['env']['FORCE_FULL'], '${{ inputs.force_full }}')
        for name in ['build-apk-forgejo.yml', 'build-rpm-modular-packages.yml']:
            workflow = (ROOT / '.github/workflows' / name).read_text()
            self.assertNotIn('date -u +%G-%V', workflow)



class IndependentFormatWorkflows(unittest.TestCase):
    def test_producer_has_no_native_package_or_publication_jobs(self):
        producer = yaml.safe_load((ROOT / '.github/workflows/build-rpm-modular-packages.yml').read_text())
        packaging = yaml.safe_load((ROOT / '.github/workflows/package-glibc.yml').read_text())
        self.assertNotIn('package', producer['jobs'])
        self.assertNotIn('publish', producer['jobs'])
        dispatch = producer['jobs']['package-formats']['steps'][0]['run']
        self.assertIn('gh workflow run package-glibc.yml', dispatch)
        self.assertIn('format="$format"', dispatch)
        self.assertEqual(packaging['on' if 'on' in packaging else True]['workflow_dispatch']['inputs']['format']['options'], ['rpm', 'deb'])
        sdk = next(step for step in packaging['jobs']['libraries']['steps'] if step.get('name') == 'Download SDK only for selected library packages')
        self.assertEqual(sdk['if'], 'matrix.libraries-required')
        self.assertNotIn('concurrency', packaging)

if __name__ == '__main__':
    unittest.main()
