#!/usr/bin/env python3
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('library_workflow', ROOT / '.github/actions/shared-libraries/workflow.py')
workflow = importlib.util.module_from_spec(spec)
spec.loader.exec_module(workflow)
spec = importlib.util.spec_from_file_location('library_toolchain', ROOT / '.github/actions/shared-libraries/toolchain.py')
toolchain = importlib.util.module_from_spec(spec)
spec.loader.exec_module(toolchain)


class LibraryBuildIdentityTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix='library-cache-test-')
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        (self.root / 'downloads').mkdir()
        self.sources = {'zlib': {'source': {'hash': 'zlib-one'}}, 'pkg-config': {'source': {'hash': 'tool-one'}}, 'php-src': {'source': {'hash': 'php-one'}}}
        self.inventory = {'schema': 1, 'managed_library_artifacts': ['zlib'], 'library_tool_artifacts': ['pkg-config'], 'irrelevant_artifacts': ['php-src']}
        (self.root / 'downloads/build-plan.json').write_text(json.dumps({'build_inputs': self.inventory}))
        self.environment = {'schema': 1, 'environment': {'SPC_DEFAULT_CFLAGS': '-O3'}, 'options': {'with-shared-libs': True}}
        self.write_sources()

    def write_sources(self):
        (self.root / 'downloads/.cache.json').write_text(json.dumps(self.sources))

    def identities(self, libraries=''):
        return (workflow.fingerprint(self.root, 'rpm-alma8-x86_64', 'gcc16', libraries, self.environment), workflow.compatibility(self.root, 'rpm-alma8-x86_64', 'gcc16', self.environment))

    def test_one_library_change_uses_same_compatible_sdk(self):
        exact, compatible = self.identities()
        self.sources['zlib']['source']['hash'] = 'zlib-two'
        self.write_sources()
        changed, seed = self.identities()
        self.assertNotEqual(exact, changed)
        self.assertEqual(compatible, seed)

    def test_manual_partial_sdk_cannot_be_an_automatic_exact_hit(self):
        self.assertNotEqual(self.identities()[0], self.identities('zlib')[0])
        self.assertEqual(self.identities('zlib,libxml2')[0], self.identities('libxml2,zlib,zlib')[0])
        self.assertEqual(self.identities()[1], self.identities('zlib')[1])

    def test_php_source_and_packaging_changes_keep_seed_compatible(self):
        original = self.identities()
        self.sources['php-src']['source']['hash'] = 'php-two'
        self.write_sources()
        (self.root / 'composer.lock').write_text('PHP-only SPC update')
        path = self.root / 'src/hook/FrankenPhp86.php'
        path.parent.mkdir(parents=True)
        path.write_text('updated PHP-only compatibility hook')
        changed = self.identities()
        self.assertNotEqual(original[0], changed[0])
        self.assertEqual(original[1], changed[1])

    def test_compiler_flags_and_toolchain_invalidate_seed(self):
        original = self.identities()[1]
        self.environment['environment']['SPC_DEFAULT_CFLAGS'] = '-O2'
        self.assertNotEqual(original, self.identities()[1])
        self.environment['environment']['SPC_DEFAULT_CFLAGS'] = '-O3'
        self.assertNotEqual(original, workflow.compatibility(self.root, 'rpm-alma8-x86_64', 'gcc17', self.environment))
        self.assertNotEqual(original, workflow.compatibility(self.root, 'rpm-alma8-arm64', 'gcc16', self.environment))

    def test_build_tool_updates_keep_seed_for_selective_tool_users(self):
        original = self.identities()
        self.sources['pkg-config']['source']['hash'] = 'tool-two'
        self.write_sources()
        changed = self.identities()
        self.assertNotEqual(original[0], changed[0])
        self.assertEqual(original[1], changed[1])

    def test_compiler_specs_contents_invalidate_seed_without_flag_changes(self):
        cc = self.root / 'redhat-hardened-cc1'
        ld = self.root / 'redhat-hardened-ld'
        cc.write_text('*cc1:\noriginal compiler specs\n')
        ld.write_text('*link:\noriginal linker specs\n')
        self.environment['environment'].update({
            'SPC_DEFAULT_CFLAGS': '-O3 -specs=' + str(cc),
            'SPC_DEFAULT_LDFLAGS': '--specs ' + str(ld),
        })

        def identity():
            files = toolchain.compiler_flag_inputs(self.environment)
            self.assertEqual(set(files), {str(cc), str(ld)})
            return workflow.compatibility(self.root, 'rpm-alma8-x86_64', json.dumps(files, sort_keys=True), self.environment)

        original = identity()
        cc.write_text('*cc1:\nupdated compiler specs\n')
        compiler_update = identity()
        self.assertNotEqual(original, compiler_update)
        ld.write_text('*link:\nupdated linker specs\n')
        self.assertNotEqual(compiler_update, identity())

    def test_image_only_changes_keep_compatible_compiler_seed(self):
        environment = self.root / 'environment.json'
        environment.write_text(json.dumps(self.environment))
        results = []
        for image in ['image-one', 'image-two']:
            text = subprocess.check_output([sys.executable, str(ROOT / '.github/actions/shared-libraries/workflow.py'), 'fingerprint', '--target=rpm-alma8-x86_64', '--toolchain=same compiler bits', '--image=' + image, '--build-environment=' + str(environment)], cwd=self.root, text=True)
            results.append(dict(line.split('=', 1) for line in text.splitlines()))
        self.assertNotEqual(results[0]['fingerprint'], results[1]['fingerprint'])
        self.assertEqual(results[0]['compatibility'], results[1]['compatibility'])

    def test_sdk_manifest_changes_invalidate_compiled_library_cache(self):
        spc = self.root / 'spc'
        shutil.copytree(ROOT / 'vendor/crazywhalecc/static-php-cli/src', spc / 'src')
        environment = dict(os.environ, SPP_SPC_ROOT=str(spc))

        def inputs():
            return json.loads(subprocess.check_output(['php', str(ROOT / '.github/actions/shared-libraries/build-inputs.php'),
                                                      str(ROOT), 'rpm', 'alma8-x86_64'], env=environment, text=True))

        before = inputs()
        manifest = spc / 'src/StaticPHP/Util/SharedLibraryManifest.php'
        manifest.write_text(manifest.read_text() + '\n// altered SDK ownership implementation fixture\n')
        after = inputs()
        self.assertNotEqual(workflow.compatibility(self.root, 'rpm-alma8-x86_64', 'gcc16', before),
                            workflow.compatibility(self.root, 'rpm-alma8-x86_64', 'gcc16', after))

    def test_rendered_flags_are_target_specific_and_exclude_php_options(self):
        for kind, target in [('rpm', 'alma8-x86_64'), ('rpm', 'alma10-x86_64'), ('deb', 'amd64'), ('apk', 'arm64')]:
            result = json.loads(subprocess.check_output(['php', str(ROOT / '.github/actions/shared-libraries/build-inputs.php'), str(ROOT), kind, target], text=True))
            with self.subTest(kind=kind, target=target):
                self.assertEqual(result['target'], target)
                self.assertTrue(result['options']['with-shared-libs'])
                self.assertFalse(any('PHP_MAKE' in name or 'FRANKENPHP' in name for name in result['environment']))
                self.assertEqual(result['environment']['SPC_LIBC'], 'musl' if kind == 'apk' else 'glibc')
                flags = result['environment']['SPC_DEFAULT_CFLAGS']
                if kind != 'apk':
                    self.assertNotIn('-march=x86-64-v3', flags)
                    self.assertNotIn('-march=x86-64-v2', flags)
                    self.assertIn('-D_FORTIFY_SOURCE=2', flags)
                    self.assertEqual(result['options']['with-shared-libdir'], '/usr/lib/php-zts/lib')


class WorkflowCacheOrderTests(unittest.TestCase):
    def test_all_formats_restore_before_compile_and_publish_final_sdk(self):
        for filename in ['build-rpm-modular-packages.yml', 'build-apk-forgejo.yml']:
            document = yaml.safe_load((ROOT / '.github/workflows' / filename).read_text())
            steps = document['jobs']['build-libs']['steps']
            names = [step.get('name', '') for step in steps]
            with self.subTest(workflow=filename):
                self.assertLess(names.index('Extract restored buildroot'), names.index('Build changed shared libraries'))
                self.assertLess(names.index('Classify ABI and package shared libraries'), names.index('Pack buildroot'))
                self.assertLess(names.index('Pack buildroot'), names.index('Save buildroot cache'))
                build = steps[names.index('Build changed shared libraries')]
                self.assertNotIn('if', build)
                restore = steps[names.index('Restore buildroot cache')]
                self.assertIn('seed-key', restore['with'])
                save = steps[names.index('Save buildroot cache')]
                self.assertIn('cache-current', save['if'])
                self.assertIn('sdk-rebuilt', save['if'])
                producer = document['jobs']['build-payload' if 'rpm' in filename else 'build']['steps']
                consumers = [step for step in producer if step.get('name') == 'Download exact common SDK']
                self.assertEqual(len(consumers), 1)
                self.assertTrue(consumers[0]['with']['name'].startswith('shared-sdk-buildroot-'))
                php_cache = next(step for step in producer if step.get('name') == 'Restore sealed PHP payload')
                self.assertEqual(php_cache['with']['path'], 'php-payload-cache.tar.zst')
                self.assertIn('seed-key', php_cache['with'])
                packing = steps[names.index('Pack buildroot')]['run']
                self.assertEqual('--zstd' in packing, filename != 'build-rpm-modular-packages.yml')


class ClassificationBuildOrderTests(unittest.TestCase):
    def run_classify(self, manual):
        temporary = tempfile.TemporaryDirectory(prefix='library-classify-test-')
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        for name in ['bin', 'shims', 'downloads', 'stage', 'buildroot', '.github/actions/shared-libraries']:
            (root / name).mkdir(parents=True, exist_ok=True)
        (root / 'downloads/build-plan.json').write_text(json.dumps({'requests': {stream: {'full': True} for stream in workflow.STREAMS}}))
        (root / 'buildroot/shared-libraries.json').write_text('{"libraries":{"a":{},"b":{}}}')
        (root / 'stage/library-build-plan.json').write_text('{"selected":["a"],"rebuilt_libraries":["a"]}')
        (root / 'bin/shared-library-plan').write_text('''import json,sys
from pathlib import Path
a=sys.argv[1:]
def flag(n): return a[a.index(n)+1]
if a[0]=='stage-publication-retries':
 p=Path(flag('--destination'));p.mkdir(parents=True,exist_ok=True);Path(flag('--output')).write_text(json.dumps({'remaining_libraries':['a','b'] if json.loads(Path(flag('--plan')).read_text())['libraries'] else [],'layout_required':False}))
elif a[0]=='baseline':
 p=Path(flag('--output'));p.mkdir(parents=True);(p/'shared-libraries.json').write_text('{"libraries":{"a":{},"b":{}}}')
elif a[0]=='prepare':
 with Path('events').open('a') as f:f.write(json.dumps({'phase':'classify','bootstrap':'--bootstrap' in a,'mode':flag('--mode'),'rebuilt':flag('--rebuilt-libraries')})+'\\n')
 Path(flag('--output')).write_text(json.dumps({'libraries':['a','b'] if flag('--libraries') else [],'abi_rebuild_roots':['a'] if flag('--libraries') else []}))
''')
        (root / '.github/actions/shared-libraries/source-plan.py').write_text('''import json,sys
from pathlib import Path
a=sys.argv[1:];Path(a[a.index('--output')+1]).write_text(Path('downloads/build-plan.json').read_text())
''')
        (root / '.github/actions/shared-libraries/incremental-build.py').write_text('''import json,sys
from pathlib import Path
a=sys.argv[1:]
def flag(n): return a[a.index(n)+1]
if a[0]=='validate-final':
 with Path('events').open('a') as f:f.write(json.dumps({'phase':'validate-final'})+'\\n')
 assert not json.loads(Path('buildroot/shared-libraries.json').read_text()).get('pending_rebuilds')
 sys.exit(0)
p=json.loads(Path(flag('--plan')).read_text())
if a[0]=='expand':
 p['selected']=[] if 'b' in p['rebuilt_libraries'] else ['b'];Path(flag('--output')).write_text(json.dumps(p))
elif a[0]=='run':
 with Path('events').open('a') as f:f.write(json.dumps({'phase':'compile','selected':p['selected']})+'\\n')
 p['rebuilt_libraries']+=p['selected'];Path(flag('--plan')).write_text(json.dumps(p))
''')
        for name in ['git', 'composer']:
            p = root / 'shims' / name
            p.write_text('#!/bin/sh\nexit 0\n')
            p.chmod(0o755)
        php = root / 'shims/php'
        php.write_text('''#!/bin/sh
if [ "$1" = bin/spp ] && [ "$2" = package ]; then
 printf '%s\\n' '{"phase":"package"}' >> events
 mkdir -p dist/deb
 printf archive > dist/deb/a.deb
 printf '{}' > dist/deb/shared-library-consumers.json
fi
''')
        php.chmod(0o755)
        env = dict(os.environ, PATH=str(root / 'shims') + ':' + os.environ['PATH'], TYPE='deb', MODE='full' if manual else 'incremental', LIBRARIES='a' if manual else '', PHP_VERSIONS='', PACKAGES='', ITERATION='', BUMP='false', MATRIX_ARCH='amd64')
        result = subprocess.run(['bash', str(ROOT / '.github/actions/shared-libraries/run.sh'), 'classify'], cwd=root, env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        return root, [json.loads(line) for line in (root / 'events').read_text().splitlines()]

    def test_abi_reverse_rebuild_finishes_before_packaging(self):
        root, events = self.run_classify(True)
        self.assertEqual([event['phase'] for event in events], ['classify', 'compile', 'classify', 'validate-final', 'package'])
        self.assertEqual(events[0], {'phase': 'classify', 'bootstrap': False, 'mode': 'incremental', 'rebuilt': 'a'})
        self.assertEqual(events[1]['selected'], ['b'])
        self.assertEqual(events[2]['rebuilt'], 'a,b')
        self.assertTrue((root / 'stage/sdk-rebuilt').is_file())

    def test_monday_php_retry_does_not_bootstrap_existing_libraries(self):
        root, events = self.run_classify(False)
        self.assertEqual(events, [{'phase': 'classify', 'bootstrap': False, 'mode': 'incremental', 'rebuilt': 'a'}, {'phase': 'validate-final'}])
        self.assertFalse((root / 'stage/sdk-rebuilt').exists())


if __name__ == '__main__':
    unittest.main()
