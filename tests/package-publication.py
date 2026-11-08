#!/usr/bin/env python3
import contextlib
import io
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / '.github/actions/shared-libraries'))
import publication


class Publication(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='spp-publication-')
        self.addCleanup(self.temporary.cleanup)
        self.plans = Path(self.temporary.name)
        self.run = {'id': 42, 'repository': {'full_name': 'static-php/packages'},
                    'head_repository': {'full_name': 'static-php/packages'}, 'head_branch': 'master',
                    'path': '.github/workflows/build-rpm-modular-packages.yml',
                    'head_sha': 'a' * 40, 'status': 'completed', 'conclusion': 'success', 'event': 'workflow_dispatch'}
        self.artifacts = []

    def artifact(self, name, expired=False):
        artifact = {'name': name, 'id': len(self.artifacts) + 1, 'expired': expired}
        self.artifacts.append(artifact)
        return artifact

    def plan(self, arch='x86_64', family='glibc', selection=None):
        target = ('alma8-' if family == 'glibc' else '') + arch
        name = 'shared-plans-' + family + '-' + target
        directory = self.plans / name
        directory.mkdir()
        image = 'ghcr.io/static-php/packages-builder-' + ('rhel-8' if family == 'glibc' else 'alpine') + '@sha256:' + 'b' * 64
        metadata = {'arch': arch, 'target': target, 'image': image, 'source_run_id': '123',
                    'package_images': {'rpm': image, 'deb': 'ghcr.io/static-php/packages-packager-debian@sha256:' + 'c' * 64}}
        (directory / 'target.json').write_text(json.dumps(metadata))
        selection = selection or {'libraries': ['zlib'], 'php_requests': {'8.4': {'full': True}, '8.6': {'packages': ['imagick']}}}
        plan = {'format_selection': {'rpm': selection, 'deb': selection}} if family == 'glibc' else selection
        (directory / 'shared-library-plan.json').write_text(json.dumps(plan))
        self.artifact(name)
        for kind in (['rpm', 'deb'] if family == 'glibc' else ['apk']):
            self.artifact('shared-libs-' + kind + '-' + target)
            requests = selection.get('php_requests', {})
            for stream in requests:
                self.artifact('shared-consumers-' + kind + '-' + target + '-php' + stream)
            if not requests and selection.get('libraries'):
                self.artifact('shared-validation-' + kind + '-' + target + '-php8.4')
        return directory

    def state(self, kind='rpm'):
        return publication.inspect_run(self.run, self.artifacts, 'static-php/packages', 'master', kind)

    def test_rpm_and_deb_publish_independently_from_one_build(self):
        self.plan()
        self.plan(arch='arm64')
        rpm, deb = [publication.publication_matrix(self.state(kind), self.plans) for kind in ['rpm', 'deb']]
        self.assertEqual(len(rpm), 2)
        self.assertEqual(len(deb), 2)
        for first, second in zip(rpm, deb):
            self.assertEqual(first['target'], second['target'])
            self.assertEqual(first['image'], second['image'])
            self.assertEqual(first['source-run-id'], '123')
            self.assertNotEqual(first['package-image'], second['package-image'])
            self.assertEqual(first['package-image-digest'], 'b' * 64)
            self.assertEqual(second['package-image-digest'], 'c' * 64)
            self.assertFalse(set(first['artifact-ids'].split(',')) & set(second['artifact-ids'].split(',')))
        self.assertEqual({value['type'] for value in rpm}, {'rpm'})
        self.assertEqual({value['type'] for value in deb}, {'deb'})

    def test_apk_uses_its_native_build_and_pinned_image(self):
        self.run['path'] = '.github/workflows/build-apk-forgejo.yml'
        self.plan(family='apk')
        value, = publication.publication_matrix(self.state('apk'), self.plans)
        self.assertEqual(value['type'], 'apk')
        self.assertEqual(value['target'], 'x86_64')
        self.assertEqual(value['package-image'], value['image'])
        self.assertIn('packages-builder-alpine@sha256:', value['image'])
        with self.assertRaisesRegex(RuntimeError, 'supported glibc'):
            self.state('deb')

    def test_publication_digest_rejects_tags_and_malformed_hashes(self):
        image = 'ghcr.io/static-php/packages-packager-debian'
        self.assertEqual(publication.image_digest(image + '@sha256:' + 'a' * 64), 'a' * 64)
        for suffix in ['', ':latest', '@sha256:short', '@sha256:' + 'z' * 64, '@sha256:' + 'a' * 64 + '@extra']:
            with self.subTest(suffix=suffix), self.assertRaisesRegex(RuntimeError, 'pinned by SHA256'):
                publication.image_digest(image + suffix)

    def test_debian_wrapper_run_is_supported(self):
        self.run['path'] = '.github/workflows/build-deb-forgejo.yml'
        self.plan()
        self.assertEqual(self.state('deb')['family'], 'glibc')

    def test_one_failed_architecture_does_not_block_a_complete_cohort(self):
        self.run['conclusion'] = 'failure'
        self.plan()
        self.plan(arch='arm64')
        self.artifacts = [item for item in self.artifacts if item['name'] != 'shared-consumers-rpm-alma8-arm64-php8.6']
        with contextlib.redirect_stderr(io.StringIO()) as warnings:
            values = publication.publication_matrix(self.state(), self.plans)
        self.assertEqual([value['target'] for value in values], ['alma8-x86_64'])
        self.assertIn('Skipping incomplete rpm alma8-arm64', warnings.getvalue())
        self.assertEqual(len(publication.publication_matrix(self.state('deb'), self.plans)), 2)

    def test_expired_consumer_prevents_partial_publication(self):
        self.plan()
        for item in self.artifacts:
            if item['name'] == 'shared-consumers-rpm-alma8-x86_64-php8.4':
                item['expired'] = True
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaisesRegex(RuntimeError, 'No complete retained rpm'):
            publication.publication_matrix(self.state(), self.plans)

    def test_expired_plans_and_builds_without_packages_fail_clearly(self):
        with self.assertRaisesRegex(RuntimeError, 'No retained build plans'):
            self.state()
        self.plan()
        for item in self.artifacts:
            item['expired'] = True
        with self.assertRaisesRegex(RuntimeError, 'expired'):
            self.state()

    def test_library_only_publication_requires_native_validation(self):
        self.plan(selection={'libraries': ['zlib'], 'php_requests': {}})
        value, = publication.publication_matrix(self.state(), self.plans)
        self.assertEqual(len(value['artifact-ids'].split(',')), 2)
        self.artifacts = [item for item in self.artifacts if not item['name'].startswith('shared-validation-rpm-')]
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaisesRegex(RuntimeError, 'No complete'):
            publication.publication_matrix(self.state(), self.plans)

    def test_format_without_planned_changes_is_not_published(self):
        directory = self.plan()
        plan = json.loads((directory / 'shared-library-plan.json').read_text())
        plan['format_selection']['rpm'] = {'libraries': [], 'php_requests': {}}
        (directory / 'shared-library-plan.json').write_text(json.dumps(plan))
        with self.assertRaisesRegex(RuntimeError, 'No complete retained rpm'):
            publication.publication_matrix(self.state(), self.plans)
        self.assertEqual(len(publication.publication_matrix(self.state('deb'), self.plans)), 1)

    def test_untrusted_unfinished_and_wrong_workflow_runs_are_rejected(self):
        self.plan()
        for key, value in [('head_repository', {'full_name': 'other/packages'}), ('head_branch', 'feature'),
                           ('event', 'pull_request'), ('path', '.github/workflows/build-images.yml'),
                           ('status', 'in_progress'), ('conclusion', 'cancelled'), ('head_sha', 'bad')]:
            with self.subTest(key=key), self.assertRaises(RuntimeError):
                publication.inspect_run(dict(self.run, **{key: value}), self.artifacts, 'static-php/packages', 'master', 'rpm')

    def test_duplicate_artifact_names_are_rejected(self):
        self.plan()
        self.artifact(self.artifacts[0]['name'])
        with self.assertRaisesRegex(RuntimeError, 'Ambiguous'):
            self.state()

    def test_target_and_image_must_match_supported_builds(self):
        directory = self.plan()
        path = directory / 'target.json'
        original = json.loads(path.read_text())
        for key, value in [('target', 'alma10-x86_64'), ('arch', 'unknown'), ('source_run_id', ''),
                           ('image', 'untrusted/image@sha256:' + 'a' * 64),
                           ('package_images', {'rpm': 'ghcr.io/static-php/packages-builder-rhel-8:latest'})]:
            with self.subTest(key=key), self.assertRaises(RuntimeError):
                path.write_text(json.dumps(dict(original, **{key: value})))
                publication.publication_matrix(self.state(), self.plans)

    def test_downloaded_cohort_cannot_mix_targets_or_source_snapshots(self):
        directory = self.plan()
        (directory / 'shared-library-plan.json').write_text(json.dumps({'type': 'rpm'}))
        target = json.loads((directory / 'target.json').read_text())
        args = [directory, 'rpm', target['target'], target['arch'], target['image'], target['source_run_id']]
        publication.verify_target(*args)
        for index, wrong in [(1, 'deb'), (2, 'alma8-arm64'), (3, 'arm64'), (4, 'another-image'), (5, '124')]:
            changed = args.copy()
            changed[index] = wrong
            with self.subTest(index=index), self.assertRaises(RuntimeError):
                publication.verify_target(*changed)

    def test_inspection_paginates_and_pins_artifact_ids(self):
        self.plan()
        output = self.plans / 'publication.json'
        with patch.object(publication, 'api', side_effect=[self.run, [{'artifacts': self.artifacts[:2]}, {'artifacts': self.artifacts[2:]}]]) as api:
            with patch.object(sys, 'argv', ['publication.py', 'inspect', '--run-id', '42', '--type', 'rpm',
                                          '--repository', 'static-php/packages', '--branch', 'master', '--output', str(output)]):
                with contextlib.redirect_stdout(io.StringIO()) as printed:
                    publication.main()
        self.assertEqual(api.call_args_list[1].kwargs, {'paginate': True})
        self.assertEqual(json.loads(output.read_text())['artifacts'], {item['name']: item['id'] for item in self.artifacts})
        self.assertIn('plan-artifact-ids=1', printed.getvalue())


class WorkflowContracts(unittest.TestCase):
    def setUp(self):
        self.workflow = yaml.safe_load((ROOT / '.github/workflows/publish-packages.yml').read_text())
        self.publisher = yaml.safe_load((ROOT / '.github/workflows/publish-package-cohort.yml').read_text())

    def test_format_dispatch_and_existing_builds_use_the_same_publisher(self):
        inputs = self.workflow[True]['workflow_dispatch']['inputs']
        self.assertEqual(inputs['format']['options'], ['rpm', 'deb', 'apk'])
        self.assertEqual(set(inputs), {'format', 'build_run_id'})
        declared = self.publisher[True]['workflow_call']['inputs']
        required = {name for name, value in declared.items() if value.get('required')}
        for name in ['build-rpm-modular-packages.yml', 'build-apk-forgejo.yml', 'publish-packages.yml']:
            job = yaml.safe_load((ROOT / '.github/workflows' / name).read_text())['jobs']['publish']
            self.assertEqual(job['uses'], './.github/workflows/publish-package-cohort.yml')
            self.assertEqual(job['secrets'], 'inherit')
            self.assertTrue(required <= set(job['with']) <= set(declared))
            self.assertFalse(set(job) & {'steps', 'runs-on', 'container', 'env', 'concurrency'})
            self.assertIn('!cancelled()', job['if'])
            self.assertFalse(job['strategy']['fail-fast'])
            if name == 'publish-packages.yml':
                self.assertEqual(job['with']['build-run-id'], '${{ inputs.build_run_id }}')
                self.assertEqual(job['with']['artifact-ids'], '${{ matrix.artifact-ids }}')
                self.assertEqual(job['with']['source-run-id'], '${{ matrix.source-run-id }}')
            else:
                self.assertEqual(job['with']['source-run-id'], '${{ needs.setup-matrix.outputs.downloads-run-id }}')
            if name != 'build-apk-forgejo.yml':
                self.assertEqual(job['with']['package-image-digest'], '${{ matrix.package-image-digest }}')
        self.assertEqual(self.publisher['concurrency'], {
            'group': 'shared-publish-${{ inputs.format }}-${{ inputs.target }}', 'cancel-in-progress': False})
        self.assertNotIn('container', self.publisher['jobs']['publish-apk'])
        self.assertEqual(self.publisher['jobs']['publish-glibc']['container']['image'],
                         "ghcr.io/static-php/${{ inputs.format == 'rpm' && 'packages-builder-rhel-8' || 'packages-packager-debian' }}@sha256:${{ inputs.package-image-digest }}")
        self.assertEqual(self.publisher['jobs']['publish-glibc']['steps'], self.publisher['jobs']['publish-apk']['steps'])

    def test_publisher_reads_inherited_secrets_directly(self):
        inputs = self.publisher[True]['workflow_call']['inputs']
        self.assertFalse(set(inputs) & {'rpm-ssh-key', 'rpm-signing-key', 'rpm-signing-password'})
        self.assertFalse((ROOT / '.github/actions/publish-packages/action.yml').exists())
        steps = self.publisher['jobs']['publish-glibc']['steps']
        ssh = next(step for step in steps if step['name'] == 'Set up SSH key')
        signing = next(step for step in steps if step['name'] == 'Sign RPM packages')
        self.assertEqual(ssh['with']['ssh-private-key'], '${{ secrets.GITHUBRPMHENDERKESPRIVATEKEY }}')
        self.assertEqual(signing['env']['RPM_SIGNING_KEY'], '${{ secrets.DEB_GPG_PRIVATE_KEY }}')
        self.assertEqual(signing['env']['RPM_SIGNING_PASSWORD'], '${{ secrets.DEB_GPG_PASSWORD }}')

    def test_publication_never_compiles_and_checks_receipts_before_signing(self):
        steps = self.publisher['jobs']['publish-glibc']['steps']
        names = [step['name'] for step in steps]
        self.assertLess(names.index('Verify native ordinary upgrade receipts'), names.index('Sign RPM packages'))
        self.assertLess(names.index('Verify complete cohort and all upgrade guards'), names.index('Publish cohort to first existing registry'))
        self.assertLess(names.index('Verify complete cohort and all upgrade guards'), names.index('Stage and promote canonical RPM generation'))
        commands = [step['run'] for step in steps if 'run.sh' in step.get('run', '')]
        self.assertEqual({command.rsplit(' ', 1)[1] for command in commands}, {'assemble', 'publish', 'upload-1', 'upload-2', 'publish-rpm'})
        self.assertNotIn('bin/spp', json.dumps(self.workflow))
        download = next(step for step in steps if step['name'] == 'Download complete staged cohort')['with']
        self.assertEqual(download['artifact-ids'], '${{ inputs.artifact-ids }}')
        self.assertEqual(download['github-token'], '${{ github.token }}')

    def test_artifacts_needed_for_later_publication_last_seven_days(self):
        retained = {'Stage target plan', 'Stage RPM library packages', 'Stage DEB library packages',
                    'Stage validated consumer packages', 'Stage library packages', 'Stage consumer packages'}
        for name in ['build-rpm-modular-packages.yml', 'build-apk-forgejo.yml']:
            data = yaml.safe_load((ROOT / '.github/workflows' / name).read_text())
            for job in data['jobs'].values():
                for step in job.get('steps', []):
                    if step.get('name') in retained:
                        self.assertEqual(step['with']['retention-days'], 7)

    def test_yaml_actions_expressions_and_shell_contracts(self):
        groups = [job.get('steps', []) for data in [self.workflow, self.publisher] for job in data['jobs'].values()]
        for steps in groups:
            for step in steps:
                if 'uses' in step and not step['uses'].startswith('./'):
                    self.assertRegex(step['uses'], r'@[a-f0-9]{40}$')
                if 'run' in step:
                    script = re.sub(r'\$\{\{.*?\}\}', 'VALUE', step['run'])
                    result = subprocess.run(['bash', '-n'], input=script, text=True, capture_output=True)
                    self.assertEqual(result.returncode, 0, result.stderr)
        for job in self.workflow['jobs'].values():
            needs = job.get('needs', [])
            needs = [needs] if isinstance(needs, str) else needs
            self.assertTrue(set(re.findall(r'needs\.([a-z-]+)', json.dumps(job))) <= set(needs))
        self.assertEqual(self.publisher['defaults']['run']['shell'], 'bash')


if __name__ == '__main__':
    unittest.main()
