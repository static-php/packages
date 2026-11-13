#!/usr/bin/env python3
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
HELPERS = ROOT / '.github/actions/shared-libraries'
sys.path.insert(0, str(HELPERS))
def module(filename, name):
    spec = importlib.util.spec_from_file_location(name, HELPERS / filename)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result
imp = module('sdk-import.py', 'sdk_import_test')
inc = module('incremental-build.py', 'sdk_incremental_test')


class SDKImportTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix='spp-sdk-import-')
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.sdk = self.root / 'sdk'
        self.provenance = self.root / 'original-inputs'
        self.provenance.mkdir()
        (self.sdk / 'lib').mkdir(parents=True)
        self.environment = {'schema': 1, 'architecture': 'x86_64', 'type': 'rpm', 'target': 'alma8-x86_64',
            'environment': {'SPC_LIBC': 'glibc', 'SPC_DEFAULT_CFLAGS': '-O3 -fPIC'},
            'implementation': {'compiler_driver': 'same'}, 'options': {'with-shared-libdir': imp.OLD_DIR, 'with-shared-libs': True}}
        self.toolchain = {'schema': 1, 'compiler': 'GCC16', 'target': 'x86_64-linux-gnu', 'system_headers': ['glibc2.28'], 'files': {'gcc': 'same'}, 'flag_files': {'specs': 'same'}}
        self.inventory = {'schema': 1, 'libraries': {name: {'artifact': name, 'depends': [], 'inputs': [name], 'recipe_hash': 'recipe-' + name} for name in ['base', 'cpp']}, 'required_libraries': ['base', 'cpp']}
        self.sources = {name: {'source': {'hash': 'source-' + name}} for name in ['base', 'cpp']}
        self.old_key = imp.compatibility_key('rpm-alma8-x86_64', self.toolchain, self.environment)
        self.state = {'schema': 1, 'compatibility': self.old_key, 'sources': self.sources,
            'libraries': {name: dict(recipe, sources={name: self.sources[name]}, dependency_signatures={}) for name, recipe in self.inventory['libraries'].items()}}
        self.manifest = {'schema': 1, 'linkage': 'shared', 'libc': 'glibc', 'architecture': 'x86_64', 'runtime_libdir': imp.OLD_DIR, 'libraries': {}}
        for name in ['base', 'cpp']:
            relative = 'lib/lib' + name + '.so.1'
            (self.sdk / relative).write_text('actual-' + name)
            self.manifest['libraries'][name] = {'source_hash': 'source-' + name, 'files': [relative], 'file_hashes': {relative: imp.payload_hash(self.sdk, relative)}}
        snapshots = self.sdk / 'provenance/sdk-relocation'
        snapshots.mkdir(parents=True)
        imp.write(snapshots / 'original-shared-library-build-inputs.json', self.state)
        imp.write(snapshots / 'original-shared-libraries.json', self.manifest)
        imp.write(self.provenance / 'build-environment.json', self.environment)
        imp.write(self.provenance / 'toolchain.json', self.toolchain)
        imp.write(self.provenance / 'library-inventory.json', self.inventory)
        self.attestation = {'schema': 1, 'complete': True, 'compatibility': self.old_key,
            'state_sha256': imp.file_hash(snapshots / 'original-shared-library-build-inputs.json'),
            'source_manifest_sha256': imp.file_hash(snapshots / 'original-shared-libraries.json'),
            'build_environment_descriptor_sha256': imp.file_hash(self.provenance / 'build-environment.json'),
            'toolchain_descriptor_sha256': imp.file_hash(self.provenance / 'toolchain.json')}
        imp.write(self.provenance / 'attestation.json', self.attestation)
        self.relocation = {'schema': 1, 'kind': 'validated-legacy-runtime-relocation', 'build_input_state_rewritten': False,
            'from': imp.OLD_DIR, 'to': imp.NEW_DIR, 'original_metadata_sha256': {
                'shared-library-build-inputs.json': self.attestation['state_sha256'], 'shared-libraries.json': self.attestation['source_manifest_sha256']},
            'elf_files': {relative: {'after_sha256': value} for entry in self.manifest['libraries'].values() for relative, value in entry['file_hashes'].items()}}
        imp.write(snapshots / 'proof.json', self.relocation)
        self.manifest['runtime_libdir'] = imp.NEW_DIR
        imp.write(self.sdk / 'shared-libraries.json', self.manifest)
        imp.write(self.sdk / 'shared-library-build-inputs.json', self.state)
        self.current = copy.deepcopy(self.environment)
        self.current['options']['with-shared-libdir'] = imp.NEW_DIR
        self.current['environment'].update(GOAMD64='v1', GOARM64='v8.0')
        semantic = {'mode': 'shared', 'soname': 'libstdc++.so.6', 'headers_sha256': 'same-headers'}
        self.runtime = {'schema': 1, 'compiler_runtime': {'schema': 1, 'semantic': semantic},
            'libraries': {'base': {'uses_cxx_runtime': False, 'embedded_cxx_runtime': False, 'compiler_runtime_inputs': None, 'fingerprint': imp.digest(None)},
                          'cpp': {'uses_cxx_runtime': True, 'embedded_cxx_runtime': True, 'compiler_runtime_inputs': semantic, 'fingerprint': imp.digest(semantic)}},
            'must_rebuild': ['cpp'], 'uncertain_stripped_libraries': []}
        self.previous_runtime = {'schema': 1, 'semantic': {'mode': 'static', 'headers_sha256': 'same-headers'}}

    def approve(self, **overrides):
        arguments = dict(sdk=self.sdk, provenance=self.provenance, environment=self.current, toolchain=self.toolchain,
                         inventory=self.inventory, target='glibc-alma8-x86_64', runtime_scan=self.runtime, previous_runtime=self.previous_runtime)
        arguments.update(overrides)
        return imp.approve(**arguments)

    def test_layout_import_preserves_actual_compilation_identity(self):
        original = (self.sdk / 'shared-library-build-inputs.json').read_bytes()
        receipt = self.approve()
        self.assertEqual(receipt['compilation_compatibility'], self.old_key)
        self.assertNotEqual(receipt['deployment_compatibility'], self.old_key)
        self.assertEqual(receipt['must_rebuild'], ['cpp'])
        self.assertEqual((self.sdk / 'shared-library-build-inputs.json').read_bytes(), original)
        imp.write(self.sdk / imp.RECEIPT, receipt)
        self.assertEqual(imp.validate(self.sdk, self.state, receipt['deployment_compatibility']), receipt)

    def test_compiler_or_header_change_cannot_be_imported(self):
        toolchain = copy.deepcopy(self.toolchain)
        toolchain['system_headers'] = ['glibc2.31']
        with self.assertRaisesRegex(RuntimeError, 'compiler/binutils'):
            self.approve(toolchain=toolchain)
        runtime = copy.deepcopy(self.previous_runtime)
        runtime['semantic']['headers_sha256'] = 'different'
        with self.assertRaisesRegex(RuntimeError, r'C\+\+ headers'):
            self.approve(previous_runtime=runtime)

    def test_flags_or_implementation_change_cannot_be_imported(self):
        for section, name in [('environment', 'SPC_DEFAULT_CFLAGS'), ('implementation', 'compiler_driver')]:
            current = copy.deepcopy(self.current)
            current[section][name] = 'different'
            with self.assertRaisesRegex(RuntimeError, 'compiler flags'):
                self.approve(environment=current)

    def test_recipe_change_selects_targeted_rebuild(self):
        inventory = copy.deepcopy(self.inventory)
        inventory['libraries']['base']['recipe_hash'] = 'new-base-recipe'
        self.assertEqual(self.approve(inventory=inventory)['must_rebuild'], ['base', 'cpp'])

    def test_ordinary_library_bytes_must_match_relocation_proof(self):
        path = self.sdk / 'lib/libbase.so.1'
        path.write_text('unproven-rebuild')
        self.manifest['libraries']['base']['file_hashes']['lib/libbase.so.1'] = imp.payload_hash(self.sdk, 'lib/libbase.so.1')
        imp.write(self.sdk / 'shared-libraries.json', self.manifest)
        with self.assertRaisesRegex(RuntimeError, 'beyond loader relocation'):
            self.approve()

    def test_deployment_context_mismatch_rejects_instead_of_full_rebuild(self):
        receipt = self.approve()
        imp.write(self.sdk / imp.RECEIPT, receipt)
        with self.assertRaisesRegex(RuntimeError, 'does not authorize'):
            imp.validate(self.sdk, self.state, 'unproven-new-compiler')

    def test_runtime_migration_does_not_select_unchanged_sources(self):
        receipt = self.approve()
        plan = inc.selection(self.inventory, self.sources, self.manifest, self.state, receipt['deployment_compatibility'],
                             deployment_import=receipt, compiler_runtime=self.runtime)
        self.assertFalse(plan['full'])
        self.assertEqual(plan['selected'], [])
        self.assertEqual(plan['reused'], ['base', 'cpp'])

    def test_completed_cpp_refresh_preserves_ordinary_record(self):
        receipt = self.approve()
        runtime = copy.deepcopy(self.runtime)
        runtime['must_rebuild'] = []
        runtime['libraries']['cpp']['embedded_cxx_runtime'] = False
        build = {'state': 'completed', 'selected': ['cpp'], 'protected_output_hashes_unchanged': True}
        result = imp.refresh_rebuilt(self.state, self.manifest, self.inventory, receipt, runtime, build, ['cpp'])
        self.assertEqual(result['compatibility'], self.old_key)
        self.assertEqual(result['libraries']['base'], self.state['libraries']['base'])
        self.assertEqual(result['libraries']['cpp']['compiled_compatibility'], receipt['deployment_compatibility'])
        imp.write(self.sdk / imp.RECEIPT, receipt)
        imp.validate(self.sdk, result, receipt['deployment_compatibility'])
        plan = inc.selection(self.inventory, self.sources, self.manifest, result, receipt['deployment_compatibility'], deployment_import=receipt, compiler_runtime=runtime)
        self.assertEqual(plan['selected'], [])
        next_state = inc.record_state(plan, self.manifest)
        self.assertEqual(next_state['compatibility'], self.old_key)
        self.assertEqual(next_state['libraries']['base']['recipe_hash'], self.state['libraries']['base']['recipe_hash'])
        self.assertEqual(next_state['deployment_compatibility'], receipt['deployment_compatibility'])
        imp.validate(self.sdk, next_state, receipt['deployment_compatibility'])

    def test_new_cpp_headers_do_not_select_unchanged_sources(self):
        runtime = copy.deepcopy(self.runtime)
        runtime['must_rebuild'] = []
        runtime['libraries']['cpp']['embedded_cxx_runtime'] = False
        receipt = self.approve(runtime_scan=runtime)
        runtime['libraries']['cpp']['fingerprint'] = 'new-cpp-headers'
        plan = inc.selection(self.inventory, self.sources, self.manifest, self.state, receipt['deployment_compatibility'], deployment_import=receipt, compiler_runtime=runtime)
        self.assertEqual(plan['selected'], [])
        self.assertEqual(plan['reused'], ['base', 'cpp'])

    def test_completed_refresh_uses_attested_actual_source_snapshot(self):
        self.state['libraries']['cpp']['sources']['cpp']['source']['frozen_tree_deviation'] = {'tracked_tree_clean': False}
        receipt = self.approve()
        runtime = copy.deepcopy(self.runtime)
        runtime['must_rebuild'] = []
        runtime['libraries']['cpp']['embedded_cxx_runtime'] = False
        sources = {'cpp': {'source': {'hash': 'source-cpp'}}}
        build = {'complete': True, 'selected': ['cpp'], 'protected_output_hashes_unchanged': True,
                 'rebuilt_sources_sha256': imp.digest(sources)}
        result = imp.refresh_rebuilt(self.state, self.manifest, self.inventory, receipt, runtime, build, ['cpp'], sources)
        self.assertEqual(result['libraries']['cpp']['sources']['cpp'], sources['cpp'])
        self.assertEqual(result['sources']['cpp'], sources['cpp'])
        self.assertEqual(result['libraries']['base'], self.state['libraries']['base'])
        self.assertIn('frozen_tree_deviation', self.state['libraries']['cpp']['sources']['cpp']['source'])
        current_sources = dict(self.sources, cpp=sources['cpp'])
        plan = inc.selection(self.inventory, current_sources, self.manifest, result, receipt['deployment_compatibility'],
                             deployment_import=receipt, compiler_runtime=runtime)
        self.assertEqual(plan['selected'], [])

    def test_unattested_actual_source_snapshot_is_rejected(self):
        receipt = self.approve()
        runtime = copy.deepcopy(self.runtime)
        runtime['must_rebuild'] = []
        runtime['libraries']['cpp']['embedded_cxx_runtime'] = False
        build = {'complete': True, 'selected': ['cpp'], 'protected_output_hashes_unchanged': True}
        with self.assertRaisesRegex(RuntimeError, 'exact actual source snapshot'):
            imp.refresh_rebuilt(self.state, self.manifest, self.inventory, receipt, runtime, build, ['cpp'], self.sources)
        build['rebuilt_sources_sha256'] = imp.digest(self.sources)
        with self.assertRaisesRegex(RuntimeError, 'unselected library'):
            imp.refresh_rebuilt(self.state, self.manifest, self.inventory, receipt, runtime, build, ['cpp'], self.sources)

    def test_actual_source_snapshot_cannot_relabel_other_compiled_bytes(self):
        receipt = self.approve()
        runtime = copy.deepcopy(self.runtime)
        runtime['must_rebuild'] = []
        runtime['libraries']['cpp']['embedded_cxx_runtime'] = False
        sources = {'cpp': {'source': {'hash': 'source-not-built'}}}
        build = {'complete': True, 'selected': ['cpp'], 'protected_output_hashes_unchanged': True,
                 'rebuilt_sources_sha256': imp.digest(sources)}
        with self.assertRaisesRegex(RuntimeError, 'pinned actual source'):
            imp.refresh_rebuilt(self.state, self.manifest, self.inventory, receipt, runtime, build, ['cpp'], sources)

    def test_source_input_change_without_rebuild_is_rejected(self):
        receipt = self.approve()
        imp.write(self.sdk / imp.RECEIPT, receipt)
        bad = copy.deepcopy(self.state)
        bad['libraries']['base']['sources']['base']['source']['hash'] = 'changed'
        with self.assertRaisesRegex(RuntimeError, 'without rebuilding'):
            imp.validate(self.sdk, bad, receipt['deployment_compatibility'])

    def test_static_cpp_output_cannot_claim_completed_refresh(self):
        receipt = self.approve()
        build = {'complete': True, 'selected': ['cpp'], 'protected_output_hashes_unchanged': True}
        with self.assertRaisesRegex(RuntimeError, r'unaccepted C\+\+ runtime'):
            imp.refresh_rebuilt(self.state, self.manifest, self.inventory, receipt, self.runtime, build, ['cpp'])


if __name__ == '__main__':
    unittest.main()
