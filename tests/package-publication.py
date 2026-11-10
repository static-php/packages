#!/usr/bin/env python3
import contextlib
import io
import json
import os
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
    def test_upload_failure_report_omits_traceback_and_names_archive(self):
        reason = 'Upload failed for https://mirror.test 85/php-zts-cgi_8.5.12-6_arm64.deb: HTTP 500 after 3 attempts'
        lines = ['Traceback (most recent call last):', '  File "helper.py", line 56',
                 '    raise RuntimeError()', 'RuntimeError: ' + reason]
        self.assertEqual(publication.failure_reason(lines), reason)

    def test_publication_and_builds_queue_without_replacing_waiting_runs(self):
        for name in ('publish-package-cohort.yml', 'build-rpm-modular-packages.yml', 'build-apk-forgejo.yml'):
            workflow = yaml.safe_load((ROOT / '.github/workflows' / name).read_text())
            self.assertEqual(workflow['concurrency']['queue'], 'max')
            self.assertFalse(workflow['concurrency']['cancel-in-progress'])

    def test_artifact_api_paginates_without_the_gh_executable(self):
        requests = []
        def response(request, timeout):
            requests.append(request)
            page = io.BytesIO(json.dumps({'artifacts': [{'id': len(requests)}]}).encode())
            page.headers = {'Link': '<https://api.github.com/repos/test/packages/artifacts?page=2>; rel="next"'} if len(requests) == 1 else {}
            return page
        with patch.object(publication, 'urlopen', side_effect=response), patch.dict(os.environ, {'GH_TOKEN': 'test-token', 'GITHUB_API_URL': 'https://api.github.com'}):
            self.assertEqual(publication.api('repos/test/packages/artifacts', paginate=True), [{'artifacts': [{'id': 1}]}, {'artifacts': [{'id': 2}]}])
        self.assertEqual(requests[1].full_url, 'https://api.github.com/repos/test/packages/artifacts?page=2')
        self.assertEqual(requests[0].get_header('Authorization'), 'Bearer test-token')

    def test_publication_output_is_live_and_saved_before_process_exits(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            signal = root / 'continue'
            log = root / 'publication.log'
            class LiveOutput(io.StringIO):
                def write(self, value):
                    if 'Uploading 1/2' in value:
                        self.saved_before_exit = 'Uploading 1/2' in log.read_text()
                        signal.touch()
                    return super().write(value)
            output = LiveOutput()
            script = ('import pathlib,sys,time\nprint("Uploading 1/2: first.apk", flush=True)\n'
                      'deadline = time.monotonic() + 5\n'
                      'while not pathlib.Path(sys.argv[1]).exists():\n'
                      ' if time.monotonic() > deadline: sys.exit(2)\n'
                      ' time.sleep(0.01)\n'
                      'print("Uploading 2/2: second.apk", file=sys.stderr, flush=True)\nsys.exit(3)\n')
            with contextlib.redirect_stdout(output), self.assertRaises(subprocess.CalledProcessError) as error:
                publication.run_logged([sys.executable, '-c', script, str(signal)], os.environ.copy(), log)
            self.assertEqual(error.exception.returncode, 3)
            self.assertTrue(output.saved_before_exit)
            self.assertEqual(output.getvalue(), log.read_text())
            self.assertIn('Uploading 2/2: second.apk', output.getvalue())

    def test_rpm_upload_targets_only_real_el8_and_refreshes_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            log = root / 'commands.jsonl'
            stub = '#!/usr/bin/env python3\nimport json,os,sys\nwith open(os.environ["COMMAND_LOG"], "a") as log:\n log.write(json.dumps(sys.argv) + "\\n")\n'
            for name in ('ssh', 'rsync'):
                path = root / name
                path.write_text(stub)
                path.chmod(0o755)
            environment = dict(os.environ, TYPE='rpm', MODE='full', LIBRARIES='', RPM_HOST='test-host',
                RPM_DIRECTORY='/mnt/data/rpm/aarch64/el8', COMMAND_LOG=str(log), PATH=str(root) + os.pathsep + os.environ['PATH'])
            subprocess.run(['bash', str(ROOT / '.github/actions/shared-libraries/run.sh'), 'publish-rpm'],
                           cwd=ROOT, env=environment, check=True, input='', text=True, capture_output=True)
            commands = [json.loads(line) for line in log.read_text().splitlines()]
            self.assertEqual(len(commands), 3)
            self.assertIn("test ! -L '/mnt/data/rpm/aarch64/el8'", commands[0][-1])
            self.assertEqual(commands[1][-1], 'test-host:/mnt/data/rpm/aarch64/el8/')
            self.assertNotIn('--delete', commands[1])
            update = commands[2][-1]
            self.assertIn('"$python" - --platform el8 --arch', update)
            self.assertIn('createrepo_c --update --retain-old-md-by-age 2h .; modifyrepo_c --mdtype=modules modules.yaml repodata', update)
            for forbidden in ('.php-zts', '/el9', '/el10', ' ln ', ' mv ', ' rm '):
                self.assertNotIn(forbidden, json.dumps(commands))
            environment['RPM_DIRECTORY'] = '/mnt/data/rpm/aarch64/el9'
            result = subprocess.run(['bash', str(ROOT / '.github/actions/shared-libraries/run.sh'), 'publish-rpm'],
                                    cwd=ROOT, env=environment, capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(len(log.read_text().splitlines()), 3)

    def test_failed_validation_preflight_and_upload_do_not_stop_other_streams(self):
        for kind in ('rpm', 'deb', 'apk'):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                library = root / 'cohort' / f'shared-libs-{kind}-target'
                library.mkdir(parents=True)
                (library / 'shared-library-plan.json').write_text(json.dumps({'abi_break': True,
                    'php_requests': {f'8.{n}': {'full': True} for n in range(2, 7)}}))
                events = []
                def verify(source, library, kind, scope):
                    events.append((scope[0], 'verify'))
                    if scope == ['8.2']:
                        raise RuntimeError('missing native receipt')
                def run(command, environment, log):
                    stream = environment['PUBLICATION_PHP_STREAMS']
                    phase = command[-1]
                    events.append((stream, phase))
                    if stream == '8.4' and phase in ('upload-1', 'publish-rpm'):
                        with log.open('a') as output:
                            output.write('::warning::Skipping upload of example.apk: Cannot verify example: connection reset\n'
                                         'Traceback:\n  caller\n  publisher\n  raise RuntimeError\nRuntimeError: cohort rejected\n')
                    if stream == '8.3' and phase == 'publish' or stream == '8.4' and phase in ('upload-1', 'publish-rpm') or phase == 'upload-2':
                        raise subprocess.CalledProcessError(1, command)
                previous = Path.cwd()
                try:
                    os.chdir(root)
                    with patch.object(publication, 'prune_consumers'), patch.object(publication, 'verify_upgrades', side_effect=verify), \
                            patch.object(publication, 'assemble'), patch.object(publication, 'run_logged', side_effect=run), \
                            patch.dict(os.environ, {'GITHUB_STEP_SUMMARY': str(root / 'summary')}), contextlib.redirect_stdout(io.StringIO()):
                        self.assertFalse(publication.publish_available(root / 'cohort', kind, 'target'))
                        primary_summary = (root / 'summary').read_text()
                        self.assertIn('Cannot verify example: connection reset', primary_summary)
                        self.assertFalse(any(phase == 'upload-2' for _, phase in events))
                        if kind != 'rpm':
                            self.assertFalse(publication.publish_available(root / 'cohort', kind, 'target', mirror=True))
                            mirrored = [stream for stream, phase in events if phase == 'upload-2']
                            self.assertEqual(mirrored, ['8.5', '8.6'])
                            self.assertLess(events.index(('8.6', 'upload-1')), events.index(('8.5', 'upload-2')))
                            self.assertEqual(json.loads((root / 'log' / f'publication-{kind}-target.json').read_text()),
                                             ['8.5', '8.6'])
                finally:
                    os.chdir(previous)
                upload = 'publish-rpm' if kind == 'rpm' else 'upload-1'
                self.assertNotIn(('8.2', 'publish'), events)
                self.assertNotIn(('8.3', upload), events)
                for stream in ('8.5', '8.6'):
                    self.assertLess(events.index((stream, 'verify')), events.index((stream, 'publish')))
                    self.assertLess(events.index((stream, 'publish')), events.index((stream, upload)))
                    self.assertIn(f'PHP {stream}: published', primary_summary)

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

    def test_retry_selects_only_requested_architecture_and_stream_artifacts(self):
        self.plan()
        self.plan(arch='arm64')
        selected, = publication.publication_matrix(self.state('deb'), self.plans, 'arm64', '8.6')
        self.assertEqual(selected['arch'], 'arm64')
        ids = set(selected['artifact-ids'].split(','))
        names = {a['name'] for a in self.artifacts if str(a['id']) in ids}
        self.assertEqual(names, {'shared-libs-deb-alma8-arm64', 'shared-consumers-deb-alma8-arm64-php8.6'})

    def test_selected_stream_retry_never_publishes_other_streams(self):
        source = self.plans / 'cohort'
        library = source / 'shared-libs-apk-arm64'
        library.mkdir(parents=True)
        (library / 'shared-library-plan.json').write_text(json.dumps({'php_requests': {'8.2': {'full': True}, '8.3': {'full': True}}}))
        previous = Path.cwd()
        try:
            os.chdir(self.plans)
            with patch.object(publication, 'prune_consumers'), patch.object(publication, 'assemble'), \
                    patch.object(publication, 'verify_upgrades') as verify, patch.object(publication, 'run_logged') as run, \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertTrue(publication.publish_available(source, 'apk', 'arm64', selected_php='8.2'))
            verify.assert_called_once_with(source, library, 'apk', ['8.2'])
            self.assertEqual([call.args[1]['PUBLICATION_PHP_STREAMS'] for call in run.call_args_list], ['8.2', '8.2'])
            self.assertEqual(json.loads((self.plans / 'log/publication-apk-arm64.json').read_text()), ['8.2'])
        finally:
            os.chdir(previous)

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
        self.assertEqual({value['target'] for value in values}, {'alma8-x86_64', 'alma8-arm64'})
        self.assertIn('Skipping unavailable stream: shared-consumers-rpm-alma8-arm64-php8.6', warnings.getvalue())
        self.assertEqual(len(publication.publication_matrix(self.state('deb'), self.plans)), 2)

    def test_expired_consumer_does_not_block_other_streams(self):
        self.plan()
        for item in self.artifacts:
            if item['name'] == 'shared-consumers-rpm-alma8-x86_64-php8.4':
                item['expired'] = True
        with contextlib.redirect_stderr(io.StringIO()):
            value, = publication.publication_matrix(self.state(), self.plans)
        self.assertEqual(len(value['artifact-ids'].split(',')), 2)

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

    def test_untrusted_invalid_and_wrong_workflow_runs_are_rejected(self):
        self.plan()
        for key, value in [('head_repository', {'full_name': 'other/packages'}), ('head_branch', 'feature'),
                           ('event', 'pull_request'), ('path', '.github/workflows/build-images.yml'),
                           ('status', 'in_progress'), ('conclusion', 'skipped'), ('head_sha', 'bad')]:
            with self.subTest(key=key), self.assertRaises(RuntimeError):
                publication.inspect_run(dict(self.run, **{key: value}), self.artifacts, 'static-php/packages', 'master', 'rpm')

    def test_running_build_can_publish_already_staged_cohorts(self):
        self.plan()
        self.artifacts = [item for item in self.artifacts
                          if item['name'] != 'shared-consumers-rpm-alma8-x86_64-php8.6']
        for status in ('queued', 'pending', 'waiting', 'in_progress'):
            with self.subTest(status=status):
                self.run.update(status=status, conclusion=None)
                state = self.state()
                selected = publication.publication_matrix(state, self.plans, selected_php='8.4')
                self.assertEqual(len(selected), 1)
                with self.assertRaisesRegex(RuntimeError, 'No complete retained'):
                    publication.publication_matrix(state, self.plans, selected_php='8.6')

    def test_cancelled_run_retains_its_completed_package_cohorts(self):
        self.plan()
        expected = publication.publication_matrix(self.state(), self.plans)
        self.run['conclusion'] = 'cancelled'
        self.assertEqual(publication.publication_matrix(self.state(), self.plans), expected)

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
        self.assertEqual(set(inputs), {'format', 'build_run_id', 'mirror_run_id', 'skip_mirror', 'arch', 'php_version'})
        self.assertEqual(inputs['arch']['default'], 'all')
        self.assertEqual(inputs['php_version']['default'], 'all')
        self.assertFalse(inputs['skip_mirror']['default'])
        self.assertIn('!inputs.skip_mirror', self.workflow['jobs']['mirror']['if'])
        self.assertEqual(self.workflow['jobs']['mirror']['strategy']['max-parallel'], 2)
        self.assertIn("inputs.mirror_run_id == ''", self.workflow['jobs']['publish']['if'])
        self.assertEqual(self.workflow['jobs']['mirror']['with']['primary-run-id'], '${{ inputs.mirror_run_id }}')
        download = next(step for step in self.publisher['jobs']['publish-glibc']['steps']
                        if step['name'] == 'Download primary publication results')
        self.assertEqual(download['with']['run-id'], '${{ inputs.primary-run-id || github.run_id }}')
        declared = self.publisher[True]['workflow_call']['inputs']
        required = {name for name, value in declared.items() if value.get('required')}
        for name in ['build-rpm-modular-packages.yml', 'build-apk-forgejo.yml', 'publish-packages.yml']:
            jobs = yaml.safe_load((ROOT / '.github/workflows' / name).read_text())['jobs']
            job = jobs['publish']
            mirror = jobs['mirror']
            self.assertIn('publish', mirror['needs'])
            self.assertIn('!cancelled()', mirror['if'])
            self.assertTrue(mirror['with']['mirror'])
            self.assertEqual(mirror['strategy']['matrix'], job['strategy']['matrix'])
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
            'group': "${{ inputs.mirror && format('shared-publish-host2-{0}-{1}', inputs.format, inputs.target) || format('shared-publish-{0}-{1}', inputs.format, inputs.target) }}", 'cancel-in-progress': False, 'queue': 'max'})
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
        signing = next(step for step in steps if step['name'] == 'Publish each validated PHP stream independently')
        self.assertEqual(ssh['with']['ssh-private-key'], '${{ secrets.GITHUBRPMHENDERKESPRIVATEKEY }}')
        self.assertEqual(signing['env']['RPM_SIGNING_KEY'], '${{ secrets.DEB_GPG_PRIVATE_KEY }}')
        self.assertEqual(signing['env']['RPM_SIGNING_PASSWORD'], '${{ secrets.DEB_GPG_PASSWORD }}')

    def test_publication_never_compiles_and_checks_receipts_before_signing(self):
        steps = self.publisher['jobs']['publish-glibc']['steps']
        names = [step['name'] for step in steps]
        self.assertLess(names.index('Verify selected build target'), names.index('Publish each validated PHP stream independently'))
        self.assertNotIn('bin/spp', json.dumps(self.workflow))
        download = next(step for step in steps if step['name'] == 'Download staged native packages')['with']
        self.assertEqual(download['artifact-ids'], '${{ inputs.artifact-ids || steps.selected-artifacts.outputs.ids }}')
        self.assertNotIn('pattern', download)
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
