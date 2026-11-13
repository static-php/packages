#!/usr/bin/env python3

import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
HELPERS = ROOT / '.github/actions/shared-libraries'
sys.path.insert(0, str(HELPERS))
import repackage


class Repackage(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.stage = self.root / 'stage'
        self.stage.mkdir()
        self.environment = patch.dict(os.environ, GITHUB_REPOSITORY='static-php/packages', DEFAULT_BRANCH='master',
            TARGET='alma8-x86_64', MATRIX_ARCH='x86_64', PHPV='8.4', PHP_VERSIONS='8.4',
            IMAGE='ghcr.io/static-php/packages-builder-rhel-8@sha256:' + 'a' * 64,
            PACKAGE_IMAGES=json.dumps({'rpm': 'ghcr.io/static-php/packages-builder-rhel-8@sha256:' + 'a' * 64,
                                       'deb': 'ghcr.io/static-php/packages-packager-debian@sha256:' + 'b' * 64}),
            TYPE='rpm', SOURCE_RUN_ID='11', REPACKAGE_RUN_ID='22', REPACKAGE_PAYLOAD_ARTIFACTS='{"8.4":3}',
            CACHE_KEY='repackage-22', BUMP='true', ITERATION='', LIBRARIES='', PACKAGES='')
        self.environment.start()
        self.addCleanup(self.environment.stop)
        for field, value in [('ROOT', self.root), ('STAGE', self.stage)]:
            replacement = patch.object(repackage.glibc, field, value)
            replacement.start()
            self.addCleanup(replacement.stop)
        self.target = {'arch': 'x86_64', 'target': 'alma8-x86_64', 'alma': '8', 'source_run_id': '11',
                       'image': os.environ['IMAGE'], 'package_images': json.loads(os.environ['PACKAGE_IMAGES'])}
        self.manifest = {'schema': 1, 'linkage': 'shared', 'architecture': 'x86_64', 'libc': 'glibc',
            'runtime_libdir': '/usr/lib64/php-zts/lib', 'libraries': {'fixture': {
                'version': '1.0', 'source_hash': 'original', 'files': ['lib/libfixture.so.1'],
                'elf': {'lib/libfixture.so.1': {'soname': 'libfixture.so.1', 'needed': []}}}}}
        self.write = repackage.glibc.write

    def inspection(self, plan=None, missing=False, **changes):
        run = {'id': 22, 'repository': {'full_name': 'static-php/packages'},
               'head_repository': {'full_name': 'static-php/packages'},
               'path': '.github/workflows/build-rpm-modular-packages.yml', 'event': 'workflow_dispatch',
               'head_branch': 'master', 'head_sha': 'c' * 40, 'status': 'completed', 'conclusion': 'success', **changes}
        names = ['shared-plans-glibc-alma8-x86_64', 'shared-sdk-glibc-alma8-x86_64',
                 'shared-sdk-buildroot-glibc-alma8-x86_64', 'shared-payload-glibc-alma8-x86_64-php8.4']
        artifacts = [{'id': index + 1, 'name': name, 'expired': missing and index == 3} for index, name in enumerate(names)]
        def download(arguments, **kwargs):
            self.assertEqual(arguments[:3], ['gh', 'run', 'download'])
            directory = Path(arguments[-1]) / names[0]
            self.write(directory / 'target.json', self.target)
            self.write(directory / 'shared-library-plan.json', plan or {'php_requests': {'8.4': {'full': True}}})
        def api(path, paginate=False):
            return [{'artifacts': artifacts}] if paginate else run
        with patch.object(repackage, 'api', api), patch.object(repackage.subprocess, 'run', download):
            return repackage.inspect(22, 'amd64', '8.4')

    def test_inspection_pins_recorded_images_sources_and_artifact_ids(self):
        result = self.inspection()
        target = result['matrix']['include'][0]
        self.assertEqual(result['run-id'], '11')
        self.assertEqual(target['image'], self.target['image'])
        self.assertEqual(target['repackage_sdk_artifact'], 3)
        self.assertEqual(target['repackage_metadata_artifact'], 2)
        self.assertEqual(target['repackage_payload_artifacts'], {'8.4': 4})

    def test_empty_expired_running_or_untrusted_producers_are_rejected(self):
        for changes in [{'plan': {'php_requests': {'8.4': {'full': False, 'packages': []}}}},
                        {'missing': True}, {'status': 'in_progress', 'conclusion': None},
                        {'head_branch': 'untrusted'}, {'head_repository': {'full_name': 'someone/fork'}}]:
            with self.subTest(changes=changes), self.assertRaises(RuntimeError):
                self.inspection(**changes)

    def test_recorded_partial_producer_can_be_reused(self):
        result = self.inspection(plan={'php_requests': {'8.4': {'full': False, 'packages': ['imagick']}}})
        self.assertEqual(result['matrix']['include'][0]['repackage_payload_artifacts'], {'8.4': 4})

    def library_repackaging(self, partial=False):
        sdk = self.root / 'buildroot'
        self.write(sdk / 'build-payload.json', {'kind': 'sdk', 'state': 'sealed'})
        self.write(sdk / 'shared-libraries.json', self.manifest)
        for kind in ('rpm', 'deb'):
            self.write(self.stage / 'common' / kind / 'shared-library-plan.json',
                       {'php_requests': {'8.4': {'full': False, 'packages': ['imagick']}}, 'libraries': ['fixture']}
                       if partial else {'original': kind})
        calls = []
        def baseline(kind, directory, consumers=False):
            self.assertTrue(consumers)
            value = copy.deepcopy(self.manifest)
            value['libraries']['fixture']['published_version'] = '1.0-7'
            if partial:
                value['libraries']['fixture'].update(version='0.9', source_hash='old source')
            self.write(directory / 'shared-libraries.json', value)
        def run(arguments, **kwargs):
            arguments = [str(value) for value in arguments]
            calls.append(arguments)
            if (arguments[:3] in (['python3', 'bin/shared-library-plan', 'prepare'],
                                 ['python3', '.github/actions/shared-libraries/workflow.py', 'receipt'])
                    or arguments[0] == 'cp'):
                subprocess.run(arguments, check=True, cwd=ROOT)
            else:
                self.assertTrue(arguments[:3] == ['php', 'bin/build-payload', 'verify']
                                or arguments[:3] == ['python3', '.github/actions/shared-libraries/openssl-variants.py', 'apply'])
        with patch.object(repackage.glibc, 'run', run), patch.object(repackage.glibc, 'baseline', baseline), \
                patch.object(repackage.glibc, 'capture_native_inventories'), \
                patch.object(repackage.glibc, 'package_libraries') as package:
            repackage.libraries()
        self.assertEqual(package.call_count, 2)
        plan = json.loads((self.stage / 'common/shared-library-plan.json').read_text())
        self.assertEqual(plan['libraries'], ['fixture'])
        self.assertEqual(plan['php_requests']['8.4']['full'], not partial)
        self.assertEqual(plan['php_requests']['8.4']['packages'], ['imagick'] if partial else [])
        self.assertFalse(any(request['compile'] or request['compile_full'] for request in plan['php_requests'].values()))
        self.assertEqual(json.loads((self.stage / 'common/target.json').read_text())['repackage_run_id'], '22')
        self.assertEqual(json.loads((self.stage / 'common/repackage/shared-libraries.json').read_text()), self.manifest)

    def test_library_repackaging_never_enters_source_or_compilation_paths(self):
        self.library_repackaging()

    def test_partial_repackaging_retains_only_the_recorded_php_requests(self):
        self.library_repackaging(partial=True)

    def payload_fixture(self):
        payload = self.root / 'build/8.4'
        self.write(payload / 'build-payload.json', {'kind': 'consumer', 'state': 'sealed',
            'libc': 'glibc', 'architecture': 'x86_64', 'php_version': '8.4.20'})
        self.write(payload / 'shared-libraries.json', self.manifest)
        (payload / 'php').write_bytes(b'original binary')
        self.write(self.stage / 'target.json', dict(self.target, repackage_run_id='22'))
        self.write(self.stage / 'repackage/shared-libraries.json', self.manifest)
        original = {}
        for kind in ('rpm', 'deb'):
            self.write(self.stage / 'repackage' / (kind + '.json'), {'original': kind})
            original[kind] = hashlib.sha256((self.stage / 'repackage' / (kind + '.json')).read_bytes()).hexdigest()
            self.write(self.stage / kind / 'shared-library-plan.json', {'new': kind})
        self.write(self.stage / 'payload-provenance.json', {'target': 'alma8-x86_64', 'php_stream': '8.4',
            'source_run_id': '11', 'compiled_source_run_id': '10', 'producer_image': self.target['image'],
            'format_plan_sha256': original})
        return payload

    def test_rebinding_provenance_preserves_every_payload_byte_and_compilation_identity(self):
        payload = self.payload_fixture()
        before = {path: path.read_bytes() for path in payload.rglob('*') if path.is_file()}
        with patch.object(repackage.glibc, 'run') as run:
            repackage.payload()
        run.assert_called_once_with(['php', 'bin/build-payload', 'verify', '--root', payload])
        self.assertEqual(before, {path: path.read_bytes() for path in before})
        provenance = json.loads((self.stage / 'payload-provenance.json').read_text())
        self.assertEqual(provenance['compiled_source_run_id'], '10')
        self.assertEqual(provenance['repackaged_from_run_id'], '22')

    def test_mismatched_sdk_and_provenance_are_rejected(self):
        for path, field, value in [('build/8.4/build-payload.json', 'architecture', 'aarch64'),
                ('stage/payload-provenance.json', 'source_run_id', '99'),
                ('stage/payload-provenance.json', 'format_plan_sha256', {}),
                ('build/8.4/shared-libraries.json', 'libraries', {})]:
            with self.subTest(field=field):
                self.payload_fixture()
                record = self.root / path
                data = json.loads(record.read_text())
                data[field] = value
                self.write(record, data)
                with patch.object(repackage.glibc, 'run'), self.assertRaises(RuntimeError):
                    repackage.payload()

    def test_partial_payload_reuse_preserves_and_cannot_expand_its_selection(self):
        payload = self.payload_fixture()
        metadata = json.loads((payload / 'build-payload.json').read_text())
        metadata['packaging_selection'] = {'packages': ['imagick'], 'libraries': ['fixture']}
        self.write(payload / 'build-payload.json', metadata)
        for kind in ('rpm', 'deb'):
            self.write(self.stage / kind / 'shared-library-plan.json', {'php_requests': {'8.4': {'packages': ['imagick']}}})
        with patch.object(repackage.glibc, 'run'):
            repackage.payload()
        self.assertEqual(json.loads((payload / 'build-payload.json').read_text()), metadata)
        for kind in ('rpm', 'deb'):
            self.write(self.stage / kind / 'shared-library-plan.json', {'php_requests': {'8.4': {'full': True}}})
        with patch.object(repackage.glibc, 'run'), self.assertRaisesRegex(RuntimeError, 'exceeds the retained PHP selection'):
            repackage.payload()

    def test_workflow_skips_all_producer_and_cache_save_steps_for_repackaging(self):
        workflow = json.loads(subprocess.check_output(['php', '-r',
            r'require $argv[1]; echo json_encode(\Symfony\Component\Yaml\Yaml::parseFile($argv[2]), JSON_THROW_ON_ERROR);',
            str(ROOT / 'vendor/autoload.php'), str(ROOT / '.github/workflows/build-rpm-modular-packages.yml')], text=True))
        for job, names in {'build-libs': ['Download pinned source snapshot', 'Compute source and toolchain fingerprint',
                'Restore buildroot cache', 'Build changed shared libraries', 'Record target and buildroot'],
                'build-payload': ['Download pinned source snapshot', 'Download exact common SDK',
                    'Extract buildroot', 'Build and stage common PHP payload']}.items():
            steps = {step.get('name'): step for step in workflow['jobs'][job]['steps']}
            for name in names:
                self.assertEqual(steps[name]['if'], "inputs.repackage_run_id == ''", name)
        for name in ('Download retained library SDK', 'Download retained SDK metadata'):
            step = next(step for step in workflow['jobs']['build-libs']['steps'] if step.get('name') == name)
            self.assertTrue(step['with']['merge-multiple'])


if __name__ == '__main__':
    unittest.main()
