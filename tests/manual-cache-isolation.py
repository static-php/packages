import unittest
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]


class ManualCacheIsolation(unittest.TestCase):
    def test_composer_restore_does_not_save_on_manual_no_publish(self):
        for name in ('build-rpm-modular-packages.yml', 'build-apk-forgejo.yml'):
            workflow = yaml.safe_load((ROOT / '.github/workflows' / name).read_text())
            jobs = [job for job in workflow['jobs'].values()
                    if any(step.get('name') == 'Restore Composer downloads' for step in job.get('steps', []))]
            self.assertEqual(len(jobs), 2, name)
            for job in jobs:
                with self.subTest(workflow=name, job=job['name']):
                    restore = next(step for step in job['steps'] if step.get('name') == 'Restore Composer downloads')
                    save = next(step for step in job['steps'] if step.get('name') == 'Save Composer downloads')
                    self.assertTrue(restore['uses'].startswith('actions/cache/restore@'))
                    self.assertTrue(save['uses'].startswith('actions/cache/save@'))
                    self.assertIn('inputs.publish == true', save['if'])
                    self.assertIn("steps.composer-cache.outputs.cache-hit != 'true'", save['if'])
                    self.assertEqual(restore['with']['key'], "composer-${{ hashFiles('composer.lock') }}")
                    self.assertEqual(save['with']['key'], '${{ steps.composer-cache.outputs.cache-primary-key }}')
                    self.assertEqual(restore['with']['path'], save['with']['path'])
                    self.assertEqual(restore['id'], 'composer-cache')
                    self.assertGreater(job['steps'].index(save), job['steps'].index(restore))
                    self.assertFalse(any(step.get('uses', '').startswith('actions/cache@') for step in job['steps']))

    def test_source_state_delete_and_save_require_publication(self):
        workflow = yaml.safe_load((ROOT / '.github/workflows/spc-download.yml').read_text())
        for name in ('Delete previous state cache', 'Save state cache'):
            step = next(step for job in workflow['jobs'].values() for step in job.get('steps', []) if step.get('name') == name)
            self.assertIn("(github.event_name == 'schedule' || inputs.publish == true)", step['if'])
            self.assertIn("hashFiles('downloads/.cache.json') != ''", step['if'])


if __name__ == '__main__':
    unittest.main()
