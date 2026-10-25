import gzip
import hashlib
import io
import json
from pathlib import Path
import runpy
import tarfile
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
planner = runpy.run_path(str(ROOT / 'bin/shared-library-plan'))
freezer = runpy.run_path(str(ROOT / 'bin/freeze-migration-baseline.py'))
fixtures = runpy.run_path(str(ROOT / 'tests/migration-baseline.py'))
namespace = planner['cohort'].__globals__


def member(name, content):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode='w') as archive:
        info = tarfile.TarInfo(name)
        info.size = len(content)
        archive.addfile(info, io.BytesIO(content))
    return gzip.compress(output.getvalue(), mtime=0)


class ApkPublicationAliases(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory(prefix='spp-apk-publication-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.logical = 'php-zts-igbinary'
        self.physical = self.logical + '-shared'
        self.records, self.downloads = fixtures['BaselineCapture']().alias_fixture()
        self.artifacts = self.root / 'artifacts'
        self.artifacts.mkdir()
        self.module_path = self.artifacts / 'module.apk'
        self.module_path.write_bytes(self.downloads[self.records[2]['publication_url']])
        self.core_path = self.artifacts / 'core.apk'
        self.core_data = self.downloads[self.records[0]['publication_url']]

    def cutoff(self):
        baseline = planner['empty_manifest']()
        baseline['consumer_versions'] = {'8.2': {self.logical: self.records[1]['version']}}
        path = self.root / 'baseline.json'
        path.write_text(json.dumps(baseline))
        args = SimpleNamespace(type='apk', php_version='8.2', baseline_manifest=[str(path)],
                               artifacts=str(self.artifacts), output=str(self.root / 'cutoffs.json'))
        with patch.dict(namespace, {'native_compare': lambda *args: self.fail('Different physical package names must not compare old malformed versions')}):
            planner['migration_cutoffs'](args)
        return json.loads(Path(args.output).read_text())

    def test_first_shared_cutoff_uses_native_alias_without_comparing_malformed_old_name(self):
        value = self.cutoff()
        self.assertEqual(value['logical_replacements']['apk'], {self.logical: self.physical})
        self.assertEqual(value['formats']['apk'], {self.physical: self.records[2]['version']})
        proof = value['replacement_candidates']['apk'][self.logical]
        self.assertEqual(proof['archive_sha256'], hashlib.sha256(self.module_path.read_bytes()).hexdigest())
        self.assertEqual(proof['module_sha256'], planner['APK_ALIASES']['archive_evidence'](self.module_path)['module_sha256'])

    def test_cutoff_rejects_missing_priority_or_forged_module_identity(self):
        for old, new in [(b'provider_priority = 100', b'provider_priority = 0'),
                         (b'"module_sha256": "', b'"module_sha256": "0')]:
            data = gzip.decompress(self.module_path.read_bytes())
            with tarfile.open(fileobj=io.BytesIO(data), mode='r:', ignore_zeros=True) as archive:
                rebuilt = b''.join(member(item.name, archive.extractfile(item).read().replace(old, new)) for item in archive if item.isfile())
            self.module_path.write_bytes(rebuilt)
            with self.subTest(change=old), self.assertRaises(RuntimeError):
                self.cutoff()
            self.module_path.write_bytes(self.downloads[self.records[2]['publication_url']])

    def cohort(self, cutoffs):
        self.core_path.write_bytes(self.core_data + member('usr/share/php-zts/migration/cutoffs.json', json.dumps(cutoffs).encode()))
        plan = {'schema': 1, 'baseline_fingerprint': 'fixture', 'expected_consumers': {'8.2': [self.logical]}}
        plan_path = self.root / 'plan.json'
        plan_path.write_text(json.dumps(plan))
        args = SimpleNamespace(type='apk', plan=str(plan_path), baseline_manifest=None,
                               artifacts=str(self.artifacts), output=str(self.root / 'cohort.json'))
        planner['cohort'](args)
        return json.loads(Path(args.output).read_text())

    def test_final_cohort_requires_cli_generation_and_embedded_exact_alias_archive(self):
        cutoffs = self.cutoff()
        result = self.cohort(cutoffs)
        self.assertEqual(result['logical_replacements'], {'8.2': {self.logical: self.physical}})
        cutoffs['replacement_candidates']['apk'][self.logical]['archive_sha256'] = '0' * 64
        with self.assertRaisesRegex(RuntimeError, 'cutoffs differ'):
            self.cohort(cutoffs)

    def test_final_cohort_rejects_cli_without_required_negative_policy(self):
        cutoffs = self.cutoff()
        self.core_data = fixtures['apk_fixture']('php-zts-cli', '8.2.35-r100', provides=[cutoffs['replacement_candidates']['apk'][self.logical]['core_capability']])
        with self.assertRaisesRegex(RuntimeError, 'CLI does not enforce'):
            self.cohort(cutoffs)

    def test_frozen_effective_baseline_is_reverified_against_its_archives(self):
        output = self.root / 'frozen'
        output.mkdir()
        stage = self.root / 'stage'
        stage.mkdir()
        with patch.dict(freezer['freeze'].__globals__, {'fetch': self.downloads.__getitem__}):
            effective, replacements, proofs = freezer['effective_apk_inventory'](self.records, '8.2', output, stage)
        baseline = {'schema': 1, 'type': 'apk', 'php_stream': '8.2', 'architecture': 'x86_64',
                    'packages': [], 'published_packages': self.records, 'effective_published_packages': effective,
                    'logical_replacements': {'apk': replacements}, 'replacement_candidates': {'apk': proofs}}
        path = output / 'baseline.json'
        path.write_text(json.dumps(baseline))
        value = planner['cutoff_baseline'](path, 'apk')
        self.assertNotIn(self.logical, value['consumer_versions']['8.2'])
        self.assertIn(self.physical, value['consumer_versions']['8.2'])
        (output / proofs[self.logical]['path']).write_bytes(b'changed archive')
        with self.assertRaisesRegex(RuntimeError, 'archive differs'):
            planner['cutoff_baseline'](path, 'apk')

    def test_cli_only_repackage_retains_published_alias_policy_without_rebuilding_module(self):
        cutoffs = self.cutoff()
        native = planner['APK_ALIASES']['archive_evidence'](self.module_path)
        baseline = planner['empty_manifest']()
        baseline.update(consumer_versions={'8.2': {self.physical: self.records[2]['version']}},
                        migration_cutoffs={'8.2': cutoffs['formats']['apk']},
                        logical_replacements={'8.2': {self.logical: self.physical}},
                        consumer_native_metadata={'8.2': {self.physical: native}})
        baseline_path = self.root / 'shared-baseline.json'
        baseline_path.write_text(json.dumps(baseline))
        self.module_path.unlink()
        args = SimpleNamespace(type='apk', php_version='8.2', baseline_manifest=[str(baseline_path)],
                               artifacts=str(self.artifacts), output=str(self.root / 'inherited.json'))
        with patch.dict(namespace, {'native_compare': lambda *args: 1}):
            planner['migration_cutoffs'](args)
        inherited = json.loads(Path(args.output).read_text())
        self.assertEqual(inherited['logical_replacements']['apk'], {self.logical: self.physical})
        self.assertEqual(inherited['replacement_candidates']['apk'], {})
        self.core_path.write_bytes(self.core_data + member('usr/share/php-zts/migration/cutoffs.json', json.dumps(inherited).encode()))
        plan_path = self.root / 'inherited-plan.json'
        plan_path.write_text(json.dumps({'schema': 1, 'baseline_fingerprint': 'fixture', 'baseline_states': [baseline]}))
        planner['cohort'](SimpleNamespace(type='apk', plan=str(plan_path), baseline_manifest=None,
                                        artifacts=str(self.artifacts), output=str(self.root / 'inherited-cohort.json')))
        result = json.loads((self.root / 'inherited-cohort.json').read_text())
        self.assertEqual(result['logical_replacements'], {'8.2': {self.logical: self.physical}})


if __name__ == '__main__':
    unittest.main()
