#!/usr/bin/env python3

from pathlib import Path
import json
import os
import re
import subprocess
import tempfile
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[1]


def workflow(name):
    data = yaml.safe_load((ROOT / '.github/workflows' / name).read_text())
    return data, data.get('on', data.get(True))


class SharedGlibcEntrypoints(unittest.TestCase):
    def test_debian_forwards_to_the_single_glibc_workflow(self):
        data, events = workflow('build-deb-forgejo.yml')
        self.assertNotIn('repository_dispatch', events)
        self.assertEqual(set(data['jobs']), {'build'})
        job = data['jobs']['build']
        self.assertEqual(job['uses'], './.github/workflows/build-rpm-modular-packages.yml')
        self.assertEqual(job['secrets'], 'inherit')
        self.assertNotIn('steps', job)
        inputs = events['workflow_dispatch']['inputs']
        self.assertEqual(set(job['with']), set(inputs) | {'formats'})
        self.assertEqual(job['with']['formats'], 'deb')
        for name in inputs:
            self.assertEqual(job['with'][name], '${{ inputs.' + name + ' }}')
        self.assertFalse(inputs['bump']['default'])
        self.assertFalse(events['workflow_call']['inputs']['bump']['default'])
        self.assertFalse(inputs['publish']['default'])
        self.assertFalse(events['workflow_call']['inputs']['publish']['default'])

    def test_automatic_source_updates_dispatch_glibc_once(self):
        data, events = workflow('spc-download.yml')
        steps = [step for job in data['jobs'].values() for step in job.get('steps', [])]
        trigger = next(step for step in steps if step.get('name') == 'Trigger downstream workflows')
        dispatch = trigger['run']
        self.assertFalse(events['workflow_dispatch']['inputs']['publish']['default'])
        self.assertFalse(events['workflow_dispatch']['inputs']['bump']['default'])
        self.assertEqual(trigger['env']['PUBLISH'], "${{ inputs.publish || false }}")
        self.assertEqual(trigger['env']['BUMP'], "${{ inputs.bump || false }}")
        self.assertIn('build-rpm-modular-packages.yml build-apk-forgejo.yml', dispatch)
        self.assertNotIn('build-deb-forgejo.yml', dispatch)
        self.assertIn('downloads_run_id="$GITHUB_RUN_ID"', dispatch)
        with tempfile.TemporaryDirectory(prefix='spp-dispatch-test-') as directory:
            root = Path(directory)
            capture = root / 'calls.jsonl'
            gh = root / 'gh'
            gh.write_text('#!/usr/bin/python3\nimport json,os,sys\n'
                          'with open(os.environ["CAPTURE"], "a") as output:\n'
                          ' output.write(json.dumps(sys.argv[1:]) + "\\n")\n')
            gh.chmod(0o755)
            cases = [('review/single-el8', 'false', 'false'), ('review/single-el8', 'true', 'false'),
                     ('review/single-el8', 'true', 'true'), ('master', 'true', 'false')]
            for branch, publish, bump in cases:
                with self.subTest(branch=branch, publish=publish, bump=bump):
                    capture.write_text('')
                    environment = dict(os.environ, PATH=directory + os.pathsep + os.environ['PATH'],
                                       CAPTURE=str(capture), GITHUB_RUN_ID='12345',
                                       GITHUB_REF_NAME=branch, PUBLISH=publish, BUMP=bump)
                    subprocess.run(['/bin/bash', '-c', dispatch], check=True, env=environment)
                    calls = [json.loads(line) for line in capture.read_text().splitlines()]
                    fields = ['-f', 'mode=incremental', '-f', 'downloads_run_id=12345',
                              '-f', 'publish=' + publish, '-f', 'bump=' + bump]
                    self.assertEqual(calls, [['workflow', 'run', target, '--ref', branch, *fields] for target in
                                            ['build-rpm-modular-packages.yml', 'build-apk-forgejo.yml']])

    def test_job_filters_accept_reusable_workflow_names_and_exclude_failures(self):
        data, _ = workflow('build-rpm-modular-packages.yml')
        cases = [
            ('compute-build-matrix', 'Build libs shared', False),
            ('compute-build-matrix', 'Repackage libraries', False),
        ]
        for job, label, payload in cases:
            with self.subTest(job=job, label=label):
                script = next(step['run'] for step in data['jobs'][job]['steps']
                              if '.jobs[]' in step.get('run', ''))
                expression = re.search(r"--jq '([^']+)'", script).group(1)
                sed = re.search(r"\| sed (-nE|-E) '([^']+)'", script)
                substitution = sed.group(2)
                suffixes = ['alma8-x86_64 php8.5', 'alma8-arm64 php8.6'] if payload else ['alma8-x86_64', 'alma8-arm64']
                names = [label + ' (' + suffix + ')' for suffix in suffixes]
                jobs = {'jobs': [
                    {'name': names[0], 'conclusion': 'success'},
                    {'name': 'DEB caller / Nested caller / ' + names[1], 'conclusion': 'success'},
                    {'name': 'DEB caller / ' + label + ' (alma8-failed php8.4)', 'conclusion': 'failure'},
                    {'name': label + ' (alma8-skipped php8.4)', 'conclusion': 'skipped'},
                    {'name': 'DEB caller / Unrelated job', 'conclusion': 'success'},
                ]}
                selected = subprocess.run(['jq', '-r', expression], input=json.dumps(jobs),
                                          text=True, capture_output=True, check=True)
                self.assertEqual(selected.stdout.splitlines(), names + (['Unrelated job'] if payload else []))
                tuples = subprocess.run(['sed', sed.group(1), substitution], input=selected.stdout,
                                        text=True, capture_output=True, check=True)
                expected = [suffix.replace(' php', ' ') for suffix in suffixes]
                self.assertEqual(tuples.stdout.splitlines(), expected)

    def test_optional_mirror_cannot_bypass_cohort_validation(self):
        data, _ = workflow('package-glibc.yml')
        publisher = data['jobs']['publish']['uses']
        steps = yaml.safe_load((ROOT / publisher).read_text())['jobs']['publish-glibc']['steps']
        publish = next(step for step in steps if step.get('name') == 'Publish each validated PHP stream independently')
        self.assertTrue(publish['run'].endswith('publication.py publish-available'))
        self.assertFalse(publish.get('continue-on-error', False))

    def test_apk_expressions_reference_declared_inputs_and_job_dependencies(self):
        data, events = workflow('build-apk-forgejo.yml')
        declared = events['workflow_dispatch']['inputs']
        for job in data['jobs'].values():
            text = json.dumps(job)
            self.assertTrue(set(re.findall(r'inputs\.([a-z_]+)', text)) <= set(declared))
            dependencies = job.get('needs', [])
            dependencies = [dependencies] if isinstance(dependencies, str) else dependencies
            self.assertTrue(set(re.findall(r'needs\.([a-z-]+)', text)) <= set(dependencies))


if __name__ == '__main__':
    unittest.main()
