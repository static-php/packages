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
        self.assertEqual(set(job['with']), set(inputs))
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
        self.assertEqual(trigger['env']['PUBLISH'], "${{ github.event_name == 'schedule' || inputs.publish }}")
        self.assertEqual(trigger['env']['BUMP'], "${{ github.event_name != 'workflow_dispatch' || inputs.bump }}")
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
                     ('review/single-el8', 'true', 'true'), ('master', 'true', 'true')]
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
        cases = {
            'compute-libs-matrix': ('Resolve builder', False),
            'compute-build-matrix': ('Build libs shared', False),
            'compute-package-matrix': ('Build payload', True),
        }
        for job, (label, payload) in cases.items():
            with self.subTest(job=job):
                script = next(step['run'] for step in data['jobs'][job]['steps']
                              if '.jobs[]' in step.get('run', ''))
                expression = re.search(r"--jq '([^']+)'", script).group(1)
                substitution = re.search(r"\| sed -E '([^']+)'", script).group(1)
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
                self.assertEqual(selected.stdout.splitlines(), names)
                tuples = subprocess.run(['sed', '-E', substitution], input=selected.stdout,
                                        text=True, capture_output=True, check=True)
                expected = [suffix.replace(' php', ' ') for suffix in suffixes]
                self.assertEqual(tuples.stdout.splitlines(), expected)

    def test_second_registry_cannot_bypass_cohort_validation(self):
        data, _ = workflow('build-rpm-modular-packages.yml')
        steps = data['jobs']['publish']['steps']
        guard = next(step for step in steps if step.get('name') == 'Verify complete cohort and all upgrade guards')
        second = next(step for step in steps if step.get('name') == 'Publish DEB cohort to second existing registry')
        cases = [('failure', False, 'deb', False), ('skipped', False, 'deb', False),
                 ('success', False, 'deb', True), ('success', True, 'deb', False),
                 ('success', False, 'rpm', False)]
        for outcome, cancelled, kind, expected in cases:
            with self.subTest(outcome=outcome, cancelled=cancelled, kind=kind):
                expression = second['if'].strip().removeprefix('${{').removesuffix('}}').strip()
                expression = expression.replace('cancelled()', repr(cancelled)).replace('matrix.type', repr(kind))
                expression = expression.replace('steps.' + guard['id'] + '.outcome', repr(outcome))
                expression = expression.replace('&&', ' and ').replace('||', ' or ')
                expression = re.sub(r'!(?!=)', 'not ', expression)
                self.assertEqual(eval(expression, {'__builtins__': {}}, {}), expected)

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
