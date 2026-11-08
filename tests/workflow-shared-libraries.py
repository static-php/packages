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
        return source.plan(previous, current, self.metadata, self.metadata, configuration='same', library_ids=set(self.inventory['managed_library_artifacts']), build_inputs=self.inventory)

    def test_library_only_update_requests_no_php(self):
        old = {'zlib': {'source': {'hash': 'old'}}}
        new = {'zlib': {'source': {'hash': 'new'}}, 're2c': {'source': {'hash': 'new'}}}
        result = source.plan(old, new, self.metadata, self.metadata, configuration='same', library_ids={'zlib'})
        self.assertEqual(result['libraries'], ['zlib'])
        self.assertEqual(result['requests'], {})

    def test_combines_full_and_partial_requests_in_one_plan(self):
        current = json.loads(json.dumps(self.metadata))
        current['php_versions']['8.4'] = '8.4.2'
        result = source.plan({}, {'ext-imagick': {'source': {'hash': 'new'}}}, self.metadata, current, configuration='same')
        self.assertEqual(result['requests']['8.4'], {'full': True, 'packages': []})
        self.assertEqual(result['requests']['8.3'], {'full': False, 'packages': ['imagick']})

    def test_configuration_migration_and_weekly_retry_are_full(self):
        for force, configuration in [(False, 'new'), (True, 'same')]:
            result = source.plan({}, {}, self.metadata, self.metadata, force=force, configuration=configuration)
            self.assertEqual(set(result['requests']), set(source.STREAMS))
            self.assertTrue(all(request['full'] for request in result['requests'].values()))

    def test_pie_changes_rebuild_only_pie(self):
        for current in [dict(self.metadata, pie_version='2.0'), dict(self.metadata, pie_sha256='new-frozen-bytes')]:
            result = source.plan({}, {}, self.metadata, current, configuration='same')
            self.assertEqual(result['requests'], {})
            self.assertTrue(all(request == {'full': False, 'packages': ['pie']} for request in result['package_requests'].values()))

    def test_binary_only_artifacts_do_not_break_change_detection(self):
        result = source.plan({}, {'pkg-config': {'source': None, 'binary': {}}}, self.metadata, self.metadata, configuration='same', library_ids={'zlib'})
        self.assertEqual(result['requests'], {})
        self.assertEqual(result['libraries'], [])

    def test_library_build_tool_source_and_binary_changes_trigger_without_php(self):
        for old, new in [
            ({'source': None, 'binary': {'linux-x86_64': {'hash': 'old'}}}, {'source': None, 'binary': {'linux-x86_64': {'hash': 'new'}}}),
            ({'source': {'hash': 'old'}}, {'source': {'hash': 'new'}}),
        ]:
            with self.subTest(previous=old):
                result = self.plan({'pkg-config': old}, {'pkg-config': new})
                self.assertEqual(result['library_build_inputs'], ['pkg-config'])
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
        self.assertEqual(self.plan({'pkg-config': previous}, {'pkg-config': current})['library_build_inputs'], ['pkg-config'])

    def test_go_changes_rebuild_frankenphp_without_libraries_or_php_core(self):
        previous = {'go-xcaddy': {'binary': {'linux-x86_64': {'hash': 'old'}}}, 'ext-imagick': {'source': {'hash': 'old'}}}
        current = {'go-xcaddy': {'binary': {'linux-x86_64': {'hash': 'new'}}}, 'ext-imagick': {'source': {'hash': 'new'}}}
        result = self.plan(previous, current)
        self.assertEqual(result['library_build_inputs'], [])
        self.assertEqual(result['libraries'], [])
        self.assertTrue(all(request == {'full': False, 'packages': ['frankenphp', 'imagick']} for request in result['requests'].values()))
        self.assertTrue(all(request == {'full': False, 'packages': ['frankenphp']} for request in self.plan({'go-xcaddy': previous['go-xcaddy']}, {'go-xcaddy': current['go-xcaddy']})['requests'].values()))

    def test_unclassified_inputs_and_required_tools_win_over_exclusions(self):
        self.inventory['irrelevant_artifacts'].append('pkg-config')
        result = self.plan({}, {'future-input': {'source': {'hash': 'new'}}, 'pkg-config': {'binary': {'linux-x86_64': {'hash': 'new'}}}})
        self.assertEqual(result['library_build_inputs'], ['future-input', 'pkg-config'])
        self.assertEqual(result['requests'], {})

    def test_binary_tool_update_emits_a_downstream_trigger_and_snapshot_request(self):
        with tempfile.TemporaryDirectory(prefix='shared-workflow-test-') as temporary:
            root = Path(temporary)
            (root / 'downloads').mkdir()
            (root / '.download-state').mkdir()
            metadata = dict(self.metadata, shared_configuration=source.identity(root))
            for name in ['downloads/.spc-meta.json', '.download-state/meta.prev.json']:
                (root / name).write_text(json.dumps(metadata))
            for name, hash_value in [('.download-state/lock.prev.json', 'old'), ('downloads/.cache.json', 'new')]:
                (root / name).write_text(json.dumps({'pkg-config': {'source': None, 'binary': {'linux-x86_64': {'hash': hash_value}}}}))
            output = io.StringIO()
            with mock.patch('sys.argv', ['source-plan.py', '--root', str(root)]), mock.patch('sys.stdout', output), mock.patch.object(source.subprocess, 'check_output', return_value=json.dumps(self.inventory)):
                source.main()
            self.assertIn('any_change=true\n', output.getvalue())
            request = json.loads((root / 'downloads/build-plan.json').read_text())
            self.assertEqual(request['requests'], {})
            self.assertEqual(request['libraries'], [])
            self.assertEqual(request['library_build_inputs'], ['pkg-config'])
            self.assertEqual(request['build_inputs'], self.inventory)

    def test_tool_request_packages_all_libraries_and_only_abi_break_selects_php(self):
        planner = runpy.run_path(str(ROOT / 'bin/shared-library-plan'))
        namespace = planner['prepare'].__globals__
        request = self.plan({'pkg-config': {'source': {'hash': 'old'}}}, {'pkg-config': {'source': {'hash': 'new'}}})
        candidate = {'schema': 1, 'linkage': 'shared', 'libraries': {name: {'version': '1.0', 'elf': {'lib/lib' + name + '.so.1': {'soname': 'lib' + name + '.so.1'}}} for name in ['fixture', 'other']}}
        for breaking in [False, True]:
            with self.subTest(breaking=breaking), tempfile.TemporaryDirectory(prefix='shared-workflow-test-') as temporary:
                root = Path(temporary)
                baseline = root / 'baseline'
                baseline.mkdir()
                (baseline / 'shared-libraries.json').write_text(json.dumps(candidate))
                (root / 'current.json').write_text(json.dumps(candidate))
                (root / 'request.json').write_text(json.dumps(request))
                target = root / 'plans/shared-plans-deb-amd64'
                target.mkdir(parents=True)
                (target / 'target.json').write_text(json.dumps({'target': 'amd64', 'arch': 'amd64', 'image': 'pinned'}))
                result = {'schema': 1, 'candidate_fingerprint': 'candidate', 'bootstrap': False, 'libraries': {name: {'status': 'breaking' if breaking and name == 'fixture' else 'compatible'} for name in candidate['libraries']}}
                args = argparse.Namespace(manifest=str(root / 'current.json'), baseline=str(baseline), request=str(root / 'request.json'), bootstrap=False, mode='incremental', root=str(root), libraries=None, php_versions=None, packages=None, type='deb', manifest_output=str(root / 'annotated.json'), output=str(target / 'shared-library-plan.json'))
                with mock.patch.dict(namespace, {'compare': mock.Mock(return_value=(json.loads(json.dumps(candidate)), result))}):
                    planner['prepare'](args)
                prepared = json.loads(Path(args.output).read_text())
                self.assertEqual(prepared['libraries'], ['fixture', 'other'])
                matrix, targets = workflow.matrix(root / 'plans', workflow.STREAMS)
                self.assertEqual(len(targets), 1)
                self.assertEqual([value['php-version'] for value in matrix], workflow.STREAMS if breaking else [])

    def test_real_hook_patch_and_registry_changes_request_full_builds(self):
        for name in ['src/hook/Php86Performance.php', 'config/patches/php86-performance-prs.patch', 'spc.registry.yml']:
            with self.subTest(path=name), tempfile.TemporaryDirectory(prefix='shared-workflow-test-') as temporary:
                root = Path(temporary)
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('original')
                previous = dict(self.metadata, shared_configuration=source.identity(root))
                path.write_text('changed')
                result = source.plan({}, {}, previous, previous, configuration=source.identity(root))
                self.assertEqual(set(result['requests']), set(source.STREAMS))
                self.assertTrue(all(request['full'] for request in result['requests'].values()))

    def test_configuration_identity_ignores_generated_build_outputs_and_logs(self):
        with tempfile.TemporaryDirectory(prefix='shared-workflow-test-') as temporary:
            root = Path(temporary)
            original = source.identity(root)
            for name in ['buildroot/lib/libz.so', 'build/8.6/bin/php', 'log/build.log', 'downloads/archive.tar.gz']:
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('generated')
            self.assertEqual(original, source.identity(root))


class MatrixTests(unittest.TestCase):
    def write_plan(self, root, target, requests, **extra):
        directory = root / ('shared-plans-rpm-' + target)
        directory.mkdir()
        (directory / 'target.json').write_text(json.dumps({'target': target, 'arch': 'x86_64', 'alma': target[4]}))
        (directory / 'shared-library-plan.json').write_text(json.dumps(dict(php_requests=requests, **extra)))

    def test_failed_alma_does_not_remove_successful_alma(self):
        with tempfile.TemporaryDirectory(prefix='shared-workflow-test-') as temporary:
            root = Path(temporary)
            for target in ['alma8-x86_64', 'alma9-x86_64']:
                self.write_plan(root, target, {'8.4': {'full': True, 'packages': []}})
            builds, publish = workflow.matrix(root, ['8.4'], ['alma9-x86_64'])
            self.assertEqual([item['target'] for item in builds], ['alma9-x86_64'])
            self.assertEqual([item['target'] for item in publish], ['alma9-x86_64'])

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

    def test_builder_tags_are_resolved_once_and_pinned_for_all_arches(self):
        output = io.StringIO()
        digest = 'sha256:' + '1' * 64
        argv = ['workflow.py', 'setup', '--type=rpm', '--resolve-images']
        with mock.patch('sys.argv', argv), mock.patch('sys.stdout', output), mock.patch.object(workflow.subprocess, 'check_output', return_value=json.dumps({'digest': digest})) as inspect:
            workflow.main()
        values = dict(line.split('=', 1) for line in output.getvalue().splitlines())
        matrix = json.loads(values['matrix'])['include']
        self.assertEqual(inspect.call_count, 3)
        self.assertEqual(len(matrix), 6)
        self.assertTrue(all(value['image'].endswith('@' + digest) for value in matrix))

    def test_failed_builder_resolution_does_not_block_other_targets(self):
        with tempfile.TemporaryDirectory(prefix='shared-workflow-test-') as temporary:
            root = Path(temporary)
            for target in ['alma8-x86_64', 'alma9-x86_64']:
                directory = root / target
                directory.mkdir()
                (directory / 'target.json').write_text(json.dumps({'target': target, 'image': 'pinned'}))
            output = io.StringIO()
            argv = ['workflow.py', 'targets', '--directory', str(root), '--succeeded', 'alma9-x86_64']
            with mock.patch('sys.argv', argv), mock.patch('sys.stdout', output):
                workflow.main()
            values = dict(line.split('=', 1) for line in output.getvalue().splitlines())
            self.assertEqual(json.loads(values['matrix'])['include'], [{'target': 'alma9-x86_64', 'image': 'pinned'}])
            self.assertEqual(values['any'], 'true')


class ProgramStackTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('php') and (ROOT / 'vendor/autoload.php').exists(), 'PHP and Composer dependencies required')
    def test_only_apk_programs_request_larger_musl_pthread_stacks(self):
        script = r'''
require $argv[1] . '/vendor/autoload.php';
$result = [];
foreach (['apk' => '3.21', 'deb' => '11', 'rpm' => '8'] as $type => $os) {
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

    def test_classification_hands_off_resolved_library_package_revisions(self):
        for selected in ['zlib', '']:
            with self.subTest(selected=selected), tempfile.TemporaryDirectory(prefix='shared-workflow-test-') as temporary:
                root = Path(temporary)
                for directory in ['bin', 'shims', 'buildroot/usr/share/php-zts/libraries/abi/zlib', '.github/actions/shared-libraries']:
                    (root / directory).mkdir(parents=True)
                for name in ['incremental-build.py', 'workflow.py']:
                    shutil.copy2(ROOT / '.github/actions/shared-libraries' / name, root / '.github/actions/shared-libraries' / name)
                (root / '.github/actions/shared-libraries/source-plan.py').write_text('''import sys
from pathlib import Path
a=sys.argv[1:];Path(a[a.index('--output')+1]).write_text('{}')
''')
                (root / 'buildroot/usr/share/php-zts/libraries/abi/zlib/libz.so.1.xml').write_text('<abi-corpus/>')
                (root / 'bin/shared-library-plan').write_text('''import json,os,sys
from pathlib import Path
args=sys.argv[1:]
def flag(name): return args[args.index(name)+1]
if args[0]=='stage-publication-retries':
    Path(flag('--destination')).mkdir(parents=True,exist_ok=True)
    Path(flag('--output')).write_text(json.dumps({'remaining_libraries':[os.environ['SELECTED']] if os.environ['SELECTED'] else [],'layout_required':False}))
elif args[0]=='baseline':
    Path(flag('--output')).mkdir(parents=True)
elif args[0]=='prepare':
    if '--request' in args:
        assert Path(flag('--request')).is_file(), 'Missing request passed to planner'
    Path(flag('--output')).write_text(json.dumps({'libraries':[os.environ['SELECTED']] if os.environ['SELECTED'] else []}))
    Path(flag('--manifest-output')).write_text(json.dumps({'schema':1,'linkage':'shared','libraries':{'zlib':{'package_versions':{'deb':'1.3.1-1'}}}}))
''')
                for name in ['git', 'composer']:
                    path = root / 'shims' / name
                    path.write_text('#!/bin/sh\nexit 0\n')
                    path.chmod(0o755)
                php = root / 'shims/php'
                php.write_text('''#!/bin/sh
if [ "$1" = bin/build-payload ] && [ "$2" = manifest ]; then
    while [ "$1" != --output ]; do shift; done
    cp buildroot/shared-libraries.json "$2"
elif [ "$1" = bin/spp ] && [ "$2" = package ]; then
    printf '%s' '{"libraries":{"zlib":{"package_versions":{"deb":"1.3.1-2"}}}}' > buildroot/shared-libraries.json
    mkdir -p dist/deb
    printf archive > dist/deb/zlib.deb
    printf '{}' > dist/deb/shared-library-consumers.json
fi
''')
                php.chmod(0o755)
                env = dict(os.environ, PATH=str(root / 'shims') + ':' + os.environ['PATH'], TYPE='deb', MODE='full', LIBRARIES='', PHP_VERSIONS='', PACKAGES='', ITERATION='', BUMP='false', MATRIX_ARCH='amd64', SELECTED=selected)
                result = subprocess.run(['bash', str(ROOT / '.github/actions/shared-libraries/run.sh'), 'classify'], cwd=root, env=env, capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                manifest = json.loads((root / 'stage/shared-libraries.json').read_text())
                self.assertEqual(manifest['libraries']['zlib']['package_versions']['deb'], '1.3.1-2' if selected else '1.3.1-1')
                self.assertTrue((root / 'stage/abi/zlib/libz.so.1.xml').is_file())

    def test_annotated_abi_files_are_restored_before_php_build(self):
        with tempfile.TemporaryDirectory(prefix='shared-workflow-test-') as temporary:
            root = Path(temporary)
            for directory in ['stage/abi/example', 'buildroot', 'shims', 'tests']:
                (root / directory).mkdir(parents=True)
            (root / 'stage/shared-libraries.json').write_text('{"planned_epoch":2}')
            (root / 'stage/shared-library-plan.json').write_text('{"schema":1}')
            helper = root / '.github/actions/shared-libraries/workflow.py'
            helper.parent.mkdir(parents=True)
            shutil.copy2(ROOT / '.github/actions/shared-libraries/workflow.py', helper)
            (root / 'stage/abi/example/libexample.so.1.xml').write_text('<abi-corpus/>')
            (helper.parent / 'glibc.py').write_text('''from pathlib import Path
assert Path('buildroot/usr/share/php-zts/libraries/abi/example/libexample.so.1.xml').is_file()
assert 'planned_epoch' in Path('buildroot/shared-libraries.json').read_text()
''')
            (root / 'tests/upgrade-guards.py').write_text('')
            for name in ['git', 'composer']:
                path = root / 'shims' / name
                path.write_text('#!/bin/sh\nexit 0\n')
                path.chmod(0o755)
            php = root / 'shims/php'
            php.write_text('#!/bin/sh\nif [ "$1" = bin/spp ] && [ "$2" = all ]; then\n test -s buildroot/usr/share/php-zts/libraries/abi/example/libexample.so.1.xml || exit 91\n grep -q planned_epoch buildroot/shared-libraries.json || exit 92\nfi\n')
            php.chmod(0o755)
            env = dict(os.environ, PATH=str(root / 'shims') + ':' + os.environ['PATH'], TYPE='deb', PHPV='8.4', PACKAGES='', ITERATION='', BUMP='false', SOURCE_RUN_ID='101', IMAGE='ghcr.io/builder@sha256:' + '1' * 64)
            result = subprocess.run(['bash', str(ROOT / '.github/actions/shared-libraries/run.sh'), 'php'], cwd=root, env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            receipt = json.loads((root / 'dist/deb/shared-library-build-receipt.json').read_text())
            self.assertEqual(receipt['source_run_id'], '101')
            self.assertEqual(receipt['php_stream'], '8.4')

    def test_cache_changes_with_sources_configuration_and_toolchain(self):
        with tempfile.TemporaryDirectory(prefix='shared-workflow-test-') as temporary:
            root = Path(temporary)
            (root / 'downloads').mkdir()
            cache = root / 'downloads/.cache.json'
            cache.write_text('{"zlib":{"source":{"hash":"one"}}}')
            baseline = workflow.fingerprint(root, 'rpm-alma8-x86_64', 'gcc16')
            self.assertNotEqual(baseline, workflow.fingerprint(root, 'rpm-alma8-x86_64', 'gcc17'))
            cache.write_text('{"zlib":{"source":{"hash":"two"}}}')
            self.assertNotEqual(baseline, workflow.fingerprint(root, 'rpm-alma8-x86_64', 'gcc16'))
            cache.write_text('{"zlib":{"source":{"hash":"one"}}}')
            (root / 'craft.yml').write_text('shared: true')
            self.assertNotEqual(baseline, workflow.fingerprint(root, 'rpm-alma8-x86_64', 'gcc16'))

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


class LibraryCacheInputTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='shared-workflow-test-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / 'downloads').mkdir()
        self.sources = {
            'zlib': {'source': {'hash': 'zlib-one', 'version': '1.3.1', 'config': {'type': 'git'}, 'time': 1}},
            'pkg-config': {'source': None, 'binary': {'linux-x86_64': {'hash': 'tool-one', 'time': 1}}},
            'php-src': {'source': {'hash': 'php-one', 'time': 1}},
            'ext-imagick': {'source': {'hash': 'ext-one', 'time': 1}},
            'go-xcaddy': {'source': None, 'binary': {'linux-x86_64': {'hash': 'go-one', 'time': 1}}},
        }
        self.inventory = {'schema': 1, 'managed_library_artifacts': ['zlib'], 'library_tool_artifacts': ['pkg-config'], 'irrelevant_artifacts': ['php-src', 'ext-imagick', 'go-xcaddy']}
        (self.root / 'downloads/build-plan.json').write_text(json.dumps({'build_inputs': self.inventory}))

    def fingerprint(self):
        (self.root / 'downloads/.cache.json').write_text(json.dumps(self.sources))
        return workflow.fingerprint(self.root, 'deb-amd64', 'image-digest gcc16')

    def test_php_extensions_go_and_download_timestamps_do_not_invalidate_libraries(self):
        original = self.fingerprint()
        self.sources['php-src']['source']['hash'] = 'php-two'
        self.sources['ext-imagick']['source']['hash'] = 'ext-two'
        self.sources['go-xcaddy']['binary']['linux-x86_64']['hash'] = 'go-two'
        self.sources['zlib']['source']['time'] = 2
        self.sources['pkg-config']['binary']['linux-x86_64']['time'] = 2
        self.assertEqual(original, self.fingerprint())

    def test_managed_library_hash_and_configuration_changes_invalidate(self):
        original = self.fingerprint()
        self.sources['zlib']['source']['hash'] = 'zlib-two'
        self.assertNotEqual(original, self.fingerprint())
        self.sources['zlib']['source']['hash'] = 'zlib-one'
        self.sources['zlib']['source']['config']['time'] = 'meaningful-config-field'
        self.assertNotEqual(original, self.fingerprint())

    def test_library_build_tool_changes_invalidate(self):
        original = self.fingerprint()
        self.sources['pkg-config']['binary']['linux-x86_64']['hash'] = 'tool-two'
        self.assertNotEqual(original, self.fingerprint())

    def test_real_build_hooks_patches_and_registry_invalidate_library_cache(self):
        for name in ['src/hook/Php86Performance.php', 'config/patches/php86-performance-prs.patch', 'spc.registry.yml']:
            with self.subTest(path=name):
                path = self.root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('original')
                original = self.fingerprint()
                path.write_text('changed')
                self.assertNotEqual(original, self.fingerprint())

    def test_unclassified_artifacts_are_kept_conservatively(self):
        self.sources['future-library'] = {'source': {'hash': 'unknown-one'}}
        original = self.fingerprint()
        self.sources['future-library']['source']['hash'] = 'unknown-two'
        self.assertNotEqual(original, self.fingerprint())

    def test_legacy_snapshots_normalize_timestamps_but_keep_all_artifacts(self):
        (self.root / 'downloads/build-plan.json').unlink()
        original = self.fingerprint()
        self.sources['zlib']['source']['time'] = 4
        self.sources['pkg-config']['binary']['linux-x86_64']['time'] = 4
        self.assertEqual(original, self.fingerprint())
        self.sources['php-src']['source']['hash'] = 'php-two'
        self.assertNotEqual(original, self.fingerprint())

    def test_legacy_snapshot_is_allowed_for_full_mode_and_rejected_early_for_incremental(self):
        (self.root / 'downloads/build-plan.json').unlink()
        helper = ROOT / '.github/actions/shared-libraries/workflow.py'
        full = subprocess.run(['python3', str(helper), 'validate-source', '--mode', 'full'], cwd=self.root, capture_output=True, text=True)
        self.assertEqual(full.returncode, 0, full.stderr)
        incremental = subprocess.run(['python3', str(helper), 'validate-source', '--mode', 'incremental'], cwd=self.root, capture_output=True, text=True)
        self.assertNotEqual(incremental.returncode, 0)
        self.assertIn('Incremental builds require downloads/build-plan.json', incremental.stderr)
        runner = ROOT / '.github/actions/shared-libraries/run.sh'
        env = dict(os.environ, TYPE='apk', MODE='incremental')
        direct = subprocess.run(['bash', str(runner), 'libs'], cwd=self.root, env=env, capture_output=True, text=True)
        self.assertNotEqual(direct.returncode, 0)
        self.assertIn('Incremental builds require downloads/build-plan.json', direct.stderr)
        self.assertNotIn('docker', direct.stderr)

    def test_library_input_classification_takes_precedence_over_exclusion(self):
        self.inventory['irrelevant_artifacts'].append('zlib')
        (self.root / 'downloads/build-plan.json').write_text(json.dumps({'build_inputs': self.inventory}))
        original = self.fingerprint()
        self.sources['zlib']['source']['hash'] = 'zlib-two'
        self.assertNotEqual(original, self.fingerprint())

    @unittest.skipUnless(shutil.which('php') and (ROOT / 'vendor/autoload.php').exists(), 'PHP and Composer dependencies required')
    def test_registry_inventory_resolves_curl_and_shared_ncurses_artifact(self):
        output = subprocess.check_output(['php', str(ROOT / '.github/actions/shared-libraries/inventory.php'), str(ROOT)], cwd=self.root, universal_newlines=True)
        inventory = json.loads(output)
        self.assertIn('curl', inventory['managed_library_artifacts'])
        self.assertIn('ncurses', inventory['managed_library_artifacts'])
        self.assertNotIn('ncursesw', inventory['managed_library_artifacts'])
        self.assertIn('pkg-config', inventory['library_tool_artifacts'])
        for name in ['php-src', 'php-micro', 'ext-imagick', 'frankenphp', 'go-xcaddy']:
            self.assertIn(name, inventory['irrelevant_artifacts'])


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
            for job_name in (['build-libs', 'build-payload', 'package'] if 'rpm' in name else ['build-libs', 'build']):
                runs = '\n'.join(step.get('run', '') for step in data['jobs'][job_name]['steps'])
                self.assertNotIn(' upload ', runs, name)
                self.assertNotIn('rsync ', runs, name)
            publish = self.publication_steps(data)
            preflight = next(index for index, step in enumerate(publish) if step['name'] in ['Verify complete cohort and all downgrade guards', 'Verify complete cohort and all upgrade guards'])
            upload = next(index for index, step in enumerate(publish) if step['name'].startswith(('Publish cohort', 'Stage and promote')))
            self.assertLess(preflight, upload)
            self.assertIn('!cancelled()', data['jobs']['publish']['if'])

    def test_uniquely_owned_staging_artifacts_can_be_replaced_on_rerun(self):
        names = {'Stage pinned builder', 'Stage target plan', 'Stage library packages', 'Stage consumer packages', 'Stage common SDK metadata', 'Stage common SDK buildroot', 'Stage RPM library packages', 'Stage DEB library packages', 'Stage sealed common payload', 'Stage validated consumer packages'}
        for workflow_name, data in self.workflows():
            artifact_names = []
            for job in data['jobs'].values():
                for step in job.get('steps', []):
                    if step.get('name') in names:
                        self.assertTrue(step['with'].get('overwrite'), workflow_name + ': ' + step['name'])
                        artifact_names.append(step['with']['name'])
            self.assertEqual(len(artifact_names), 8 if 'rpm' in workflow_name else 5)
            self.assertEqual(len(set(artifact_names)), len(artifact_names))

    def test_source_is_downloaded_before_buildroot_cache(self):
        for name, data in self.workflows():
            steps = data['jobs']['build-libs']['steps']
            names = [step['name'] for step in steps]
            self.assertLess(names.index('Extract source snapshot'), names.index('Compute source and toolchain fingerprint'), name)

    def test_every_job_uses_the_same_resolved_source_snapshot(self):
        for name, data in self.workflows():
            setup = data['jobs']['setup-matrix']
            self.assertIn('downloads-run-id', setup['outputs'])
            for job_name in (['build-libs', 'build-payload'] if 'rpm' in name else ['build-libs', 'build']):
                step = next(step for step in data['jobs'][job_name]['steps'] if step['name'] == 'Download pinned source snapshot')
                self.assertEqual(step['with']['run_id'], '${{ needs.setup-matrix.outputs.downloads-run-id }}', name)

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
        data = dict(self.workflows())['build-rpm-modular-packages.yml']
        script = '\n'.join(step.get('run', '') for step in data['jobs']['package']['steps'])
        self.assertIn('glibc-package', script)
        self.assertNotIn('bin/spp all', script)
        self.assertIn('migration-upgrade-containers.py', script)
        self.assertIn('--library-only', script)

    def test_glibc_validation_only_cannot_publish_or_save_release_caches(self):
        data = dict(self.workflows())['build-rpm-modular-packages.yml']
        self.assertIn('inputs.publish == true', data['jobs']['publish']['if'])
        cache = next(step for step in data['jobs']['build-libs']['steps'] if step['name'] == 'Save buildroot cache')
        self.assertIn('inputs.publish == true', cache['if'])
        self.assertEqual(data['jobs']['build-libs']['env']['BUMP'], '${{ inputs.bump || false }}')
        publish = self.publication_steps(data)
        names = [step['name'] for step in publish]
        self.assertLess(names.index('Verify native ordinary upgrade receipts'), names.index('Sign RPM packages'))
        self.assertLess(names.index('Verify complete cohort and all upgrade guards'), names.index('Stage and promote canonical RPM generation'))

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


if __name__ == '__main__':
    unittest.main()
