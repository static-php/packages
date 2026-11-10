import json
from pathlib import Path
import runpy
import shutil
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
planner = runpy.run_path(str(ROOT / 'bin/shared-library-plan'))
freezer = runpy.run_path(str(ROOT / 'bin/freeze-migration-baseline.py'))
fixtures = runpy.run_path(str(ROOT / 'tests/migration-baseline.py'))


@unittest.skipUnless(shutil.which('apk'), 'native APK comparator required')
class ApkPublicationVersions(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory(prefix='spp-apk-publication-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.name = 'php-zts-igbinary'
        self.old = '3.2.17_rc1_p82-r1'
        self.new = '3.2.18.0_p82-r0'
        self.artifacts = self.root / 'artifacts'
        self.artifacts.mkdir()
        self.module = self.artifacts / 'module.apk'
        self.module.write_bytes(fixtures['apk_fixture'](self.name, self.new))
        self.core = self.artifacts / 'core.apk'
        self.core.write_bytes(fixtures['apk_fixture']('php-zts-cli', '8.2.35-r100'))

    def cutoffs(self):
        baseline = planner['empty_manifest']()
        baseline['consumer_versions'] = {'8.2': {self.name: self.old}}
        path = self.root / 'baseline.json'
        path.write_text(json.dumps(baseline))
        args = SimpleNamespace(type='apk', php_version='8.2', baseline_manifest=[str(path)],
                               artifacts=str(self.artifacts), output=str(self.root / 'cutoffs.json'))
        planner['migration_cutoffs'](args)
        return json.loads(Path(args.output).read_text())

    def test_original_name_is_compared_to_its_published_version(self):
        result = self.cutoffs()
        self.assertEqual(result['formats']['apk'], {self.name: self.new})
        self.assertEqual(result['published_versions']['apk'], {self.name: self.old})
        self.assertNotIn('logical_replacements', result)
        self.assertNotIn('replacement_candidates', result)

    def test_lower_or_equal_versions_cannot_evade_migration_ordering(self):
        for version in ['3.2.16.0_p82-r99', self.old, '3.2.17_rc1_p82-r0']:
            self.module.write_bytes(fixtures['apk_fixture'](self.name, version))
            with self.subTest(version=version), self.assertRaisesRegex(RuntimeError, 'must advance|Missing published'):
                self.cutoffs()

    def test_cohort_requires_the_original_package_name(self):
        plan = self.root / 'plan.json'
        plan.write_text(json.dumps({'schema': 1, 'baseline_fingerprint': 'fixture',
                                    'expected_consumers': {'8.2': [self.name]}}))
        args = SimpleNamespace(type='apk', plan=str(plan), baseline_manifest=None,
                               artifacts=str(self.artifacts), output=str(self.root / 'cohort.json'))
        planner['cohort'](args)
        self.assertEqual({p['name'] for p in json.loads(Path(args.output).read_text())['files']}, {self.name, 'php-zts-cli'})
        self.module.write_bytes(fixtures['apk_fixture'](self.name + '-shared', self.new))
        with self.assertRaisesRegex(RuntimeError, 'Incomplete shared-library cohort'):
            planner['cohort'](args)

    def test_frozen_inventory_keeps_original_names_and_published_history(self):
        records, downloads = [], {}
        for name, version in [('php-zts-cli', '8.2.35-r100'), (self.name, self.old), (self.name, self.new)]:
            content = fixtures['apk_fixture'](name, version)
            url = f'https://example.test/{name}-{version}.apk'
            downloads[url] = content
            records.append({'name': name, 'version': version, 'architecture': 'x86_64', 'requires': [], 'provides': [],
                            'publication_url': url, 'published_checksum': {'algorithm': 'apk-control-sha1',
                                'value': freezer['apk_control_checksum'](content)}})
        output = self.root / 'frozen'
        args = SimpleNamespace(type='apk', php='8.2', arch='x86_64', base_url='https://example.test/',
                               profile='all', output=output, include_debug=False)
        with patch.dict(freezer['freeze'].__globals__, {'apk_index': lambda *args: (records, {}, []),
                                                       'fetch': downloads.__getitem__}):
            freezer['freeze'](args)
        result = json.loads((output / 'baseline.json').read_text())
        self.assertEqual({p['name']: p['version'] for p in result['packages']}, {'php-zts-cli': '8.2.35-r100', self.name: self.new})
        self.assertEqual(result['published_packages'], records)
        self.assertNotIn('logical_replacements', result)


if __name__ == '__main__':
    unittest.main()
