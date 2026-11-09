import copy
import hashlib
import os
from pathlib import Path
import runpy
import json
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]
planner = runpy.run_path(str(ROOT / 'bin/shared-library-plan'))
namespace = planner['dump_abi'].__globals__


def library():
    return {'version': '1.0', 'files': ['lib/libfixture.so.1'],
            'elf': {'lib/libfixture.so.1': {'soname': 'libfixture.so.1', 'needed': []}}}


class AbiDumpCache(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory(prefix='abi-dump-cache-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.runtime = self.root / 'lib/libfixture.so.1'
        self.runtime.parent.mkdir()
        self.runtime.write_bytes(b'fixture runtime bytes')
        self.debug = self.root / 'debug/lib/libfixture.so.1.debug'
        self.debug.parent.mkdir(parents=True)
        self.debug.write_bytes(b'fixture debug bytes')
        self.output = self.root / planner['METADATA'] / 'abi/fixture/libfixture.so.1.xml'
        self.receipt = self.output.with_suffix('.xml.inputs.json')
        self.tool = 'libabigail-test-build-1'
        self.flags = ['--no-corpus-path', '--no-show-locs']
        self.invocations = []
        self.generate_failure = False
        self.inputs_available = True
        self.input_patch = patch.dict(namespace, {'abi_dump_inputs': self.inputs, 'run': self.command})
        self.input_patch.start()
        self.addCleanup(self.input_patch.stop)

    def inputs(self, root, path, flags=None, context=None):
        if not self.inputs_available:
            return None
        return {'runtime_sha256': hashlib.sha256(self.runtime.read_bytes()).hexdigest(),
                'debug_sha256': hashlib.sha256(self.debug.read_bytes()).hexdigest() if self.debug.exists() else None,
                'tool': self.tool, 'flags': self.flags + ([] if flags is None else list(flags))}

    def command(self, command):
        self.assertEqual(Path(command[0]).name, 'abidw')
        self.assertIn('--out-file', command)
        self.invocations.append(command)
        if self.generate_failure:
            return SimpleNamespace(returncode=1, stdout='', stderr='debug information unavailable')
        output = Path(command[command.index('--out-file') + 1])
        output.write_text('<abi-corpus><abi-instr><function-decl name="fixture"/></abi-instr></abi-corpus>')
        return SimpleNamespace(returncode=0, stdout='', stderr='')

    def dump(self):
        entry = library()
        planner['dump_abi'](self.root, 'fixture', entry)
        return entry

    def test_identical_verified_inputs_reuse_one_dump(self):
        first = self.dump()
        self.assertTrue(self.receipt.is_file())
        second = self.dump()
        self.assertEqual(len(self.invocations), 1)
        self.assertEqual(first['abi_dump_hashes'], second['abi_dump_hashes'])
        self.assertEqual(second['abi_empty'], [])

    def baseline(self):
        entry = self.dump()
        entry['abi_epoch'] = 7
        baseline = self.root / 'baseline'
        dump = baseline / entry['abi_dump']['libfixture.so.1']
        dump.parent.mkdir(parents=True)
        shutil.copy2(self.output, dump)
        value = {'schema': 1, 'linkage': 'shared', 'architecture': 'x86_64', 'libc': 'glibc',
                 'runtime_libdir': '/usr/lib/php-zts/lib', 'libraries': {'fixture': entry}}
        return baseline, value

    def test_unchanged_library_reuses_dump_and_published_generation_without_comparison(self):
        baseline, previous = self.baseline()
        difference = Mock(side_effect=AssertionError('Unchanged ABI must not invoke abidiff'))
        with patch.dict(namespace, {'abi_difference': difference}):
            updated, result = planner['compare'](previous, previous, baseline, self.root)
        difference.assert_not_called()
        self.assertEqual(len(self.invocations), 1)
        self.assertEqual(updated['libraries']['fixture']['abi_epoch'], 7)
        self.assertEqual(result['libraries']['fixture']['status'], 'compatible')

    def test_explicit_unchanged_selection_skips_all_abi_tools_and_xml_reads(self):
        baseline, previous = self.baseline()
        forbidden = {name: Mock(side_effect=AssertionError('Unchanged ABI must be reused directly'))
                     for name in ['dump_abi', 'abi_dump_inputs', 'validate_abi_dump', 'abi_difference', 'file_sha256']}
        with patch.dict(namespace, forbidden):
            updated, result = planner['compare'](previous, previous, baseline, self.root, rebuilt_libraries=set())
        for operation in forbidden.values():
            operation.assert_not_called()
        self.assertEqual(updated['libraries']['fixture']['abi_epoch'], 7)
        self.assertEqual(result['libraries']['fixture']['status'], 'compatible')

    def test_only_the_regenerated_library_enters_abi_analysis(self):
        baseline, previous = self.baseline()
        other = copy.deepcopy(previous['libraries']['fixture'])
        other.update(files=['lib/libother.so.1'], elf={'lib/libother.so.1': {'soname': 'libother.so.1', 'needed': []}},
                     abi_dump={'libother.so.1': previous['libraries']['fixture']['abi_dump']['libfixture.so.1']},
                     abi_dump_hashes={'libother.so.1': previous['libraries']['fixture']['abi_dump_hashes']['libfixture.so.1']})
        previous['libraries']['other'] = other
        dump = Mock()
        with patch.dict(namespace, {'dump_abi': dump}):
            planner['compare'](previous, previous, baseline, self.root, rebuilt_libraries={'fixture'})
        self.assertEqual([call.args[1] for call in dump.call_args_list], ['fixture'])

    def test_patch_and_same_version_rebuilds_skip_abi_tools(self):
        baseline, previous = self.baseline()
        previous['libraries']['fixture']['version'] = '1.2.3'
        for version in ['1.2.3', '1.2.4']:
            candidate = copy.deepcopy(previous)
            candidate['libraries']['fixture']['version'] = version
            forbidden = {name: Mock(side_effect=AssertionError('Patch rebuild must not inspect ABI'))
                         for name in ['dump_abi', 'abi_difference', 'file_sha256', 'cached_abi']}
            with self.subTest(version=version), patch.dict(namespace, forbidden):
                updated, result = planner['compare'](previous, candidate, baseline, self.root, rebuilt_libraries={'fixture'})
            self.assertEqual(updated['libraries']['fixture']['abi_epoch'], 7)
            self.assertEqual(result['libraries']['fixture']['check'], 'skipped-patch-or-same-version')

    def test_major_minor_and_unknown_versions_require_checks(self):
        for old, new in [('1.2.3', '1.3.0'), ('1.2.3', '2.0.0'), ('1.2', '1.3'), ('master', 'master'), ('1.2.3', '1.2.4rc1')]:
            with self.subTest(old=old, new=new):
                self.assertTrue(planner['needs_abi_check']({'version': old}, {'version': new}))
        self.assertFalse(planner['needs_abi_check'](None, {'version': '1.2.4', 'abi_reference_version': '1.2.3'}))

    def test_cold_patch_rebuild_retains_the_last_checked_reference(self):
        baseline, previous = self.baseline()
        previous['libraries']['fixture']['version'] = '1.2.3'
        candidate = copy.deepcopy(previous)
        candidate['libraries']['fixture'] = dict(library(), version='1.2.4')
        with TemporaryDirectory() as temporary, patch.dict(namespace, {'dump_abi': Mock(side_effect=AssertionError('Patch ABI scan'))}):
            updated, _ = planner['compare'](previous, candidate, baseline, temporary, rebuilt_libraries={'fixture'})
            entry = updated['libraries']['fixture']
            self.assertEqual(entry['abi_reference_version'], '1.2.3')
            self.assertTrue((Path(temporary) / entry['abi_dump']['libfixture.so.1']).is_file())

    def test_minor_upgrade_checks_only_its_rebuilt_library(self):
        baseline, previous = self.baseline()
        previous['libraries']['fixture']['version'] = '1.2.3'
        previous['libraries']['other'] = copy.deepcopy(previous['libraries']['fixture'])
        candidate = copy.deepcopy(previous)
        candidate['libraries']['fixture']['version'] = '1.3.0'
        dump = Mock()
        with patch.dict(namespace, {'dump_abi': dump}):
            updated, _ = planner['compare'](previous, candidate, baseline, self.root, rebuilt_libraries={'fixture'})
        self.assertEqual([call.args[1] for call in dump.call_args_list], ['fixture'])
        self.assertEqual(updated['libraries']['fixture']['abi_reference_version'], '1.3.0')

    def test_unchanged_library_with_no_sonames_reuses_empty_abi_inventory(self):
        entry = dict(version='1.0', files=[], elf={}, abi_dump={}, abi_dump_hashes={}, abi_empty=[])
        value = {'libraries': {'fixture': entry}}
        with patch.dict(namespace, {'dump_abi': Mock(side_effect=AssertionError('unchanged library'))}):
            updated, result = planner['compare'](value, value, self.root, self.root, rebuilt_libraries=set())
        self.assertEqual(result['libraries']['fixture']['status'], 'compatible')

    def test_changed_published_baseline_requires_rebuild_without_analyzing_unchanged_outputs(self):
        baseline, previous = self.baseline()
        published = copy.deepcopy(previous)
        published['libraries']['fixture']['abi_dump_hashes']['libfixture.so.1'] = 'f' * 64
        dump, difference = Mock(), Mock()
        with patch.dict(namespace, {'dump_abi': dump, 'abi_difference': difference}), self.assertRaisesRegex(RuntimeError, 'baseline changed.*rebuild fixture'):
            planner['compare'](published, previous, baseline, self.root, rebuilt_libraries=set())
        dump.assert_not_called()
        difference.assert_not_called()

    def test_cached_breaking_result_is_reused_without_advancing_epoch_twice(self):
        baseline, previous = self.baseline()
        candidate = copy.deepcopy(previous)
        candidate['libraries']['fixture']['abi_dump_hashes']['libfixture.so.1'] = 'e' * 64
        with patch.dict(namespace, {'dump_abi': Mock(), 'abi_difference': Mock(return_value='breaking')}):
            updated, _ = planner['compare'](previous, candidate, baseline, self.root, rebuilt_libraries={'fixture'})
        forbidden = {name: Mock(side_effect=AssertionError('Cached ABI result must be reused'))
                     for name in ['dump_abi', 'abi_difference', 'file_sha256']}
        with patch.dict(namespace, forbidden):
            retried, result = planner['compare'](previous, updated, baseline, self.root, rebuilt_libraries=set())
        self.assertEqual(retried['libraries']['fixture']['abi_epoch'], 8)
        self.assertEqual(result['libraries']['fixture']['status'], 'breaking')

    def test_unchanged_library_without_classified_cache_is_rejected(self):
        baseline, previous = self.baseline()
        candidate = copy.deepcopy(previous)
        candidate['libraries']['fixture'].pop('abi_dump_hashes')
        with self.assertRaisesRegex(RuntimeError, 'no classified cached ABI'):
            planner['compare'](previous, candidate, baseline, self.root, rebuilt_libraries=set())

    def test_new_published_baseline_requires_comparison_even_without_a_rebuild(self):
        baseline, previous = self.baseline()
        (baseline / previous['libraries']['fixture']['abi_dump']['libfixture.so.1']).write_text(
            '<abi-corpus><abi-instr><function-decl name="changed"/></abi-instr></abi-corpus>')
        difference = Mock(return_value='breaking')
        with patch.dict(namespace, {'abi_difference': difference}):
            updated, _ = planner['compare'](previous, previous, baseline, self.root)
            retried, _ = planner['compare'](previous, updated, baseline, self.root)
        self.assertEqual(difference.call_count, 2)
        self.assertEqual(len(self.invocations), 1)
        self.assertEqual(updated['libraries']['fixture']['abi_epoch'], 8)
        self.assertEqual(retried['libraries']['fixture']['abi_epoch'], 8)

    def test_changed_comparison_failure_preserves_published_generation(self):
        baseline, previous = self.baseline()
        (baseline / previous['libraries']['fixture']['abi_dump']['libfixture.so.1']).write_text(
            '<abi-corpus><abi-instr><function-decl name="changed"/></abi-instr></abi-corpus>')
        difference = Mock(side_effect=RuntimeError('ABI comparison failed'))
        with patch.dict(namespace, {'abi_difference': difference}), self.assertRaisesRegex(RuntimeError, 'comparison failed'):
            planner['compare'](previous, previous, baseline, self.root)
        self.assertEqual(previous['libraries']['fixture']['abi_epoch'], 7)

    def test_runtime_bytes_change_regenerates_dump(self):
        self.dump()
        self.runtime.write_bytes(b'changed runtime bytes')
        self.dump()
        self.assertEqual(len(self.invocations), 2)

    def test_external_debug_bytes_change_regenerates_dump(self):
        self.dump()
        self.debug.write_bytes(b'changed debug bytes')
        self.dump()
        self.assertEqual(len(self.invocations), 2)

    def test_tool_identity_change_regenerates_dump(self):
        self.dump()
        self.tool = 'libabigail-test-build-2'
        self.dump()
        self.assertEqual(len(self.invocations), 2)

    def test_analysis_flags_change_regenerates_dump(self):
        self.dump()
        self.flags.append('--load-all-types')
        self.dump()
        self.assertEqual(len(self.invocations), 2)

    def test_corrupt_dump_regenerates_instead_of_trusting_receipt(self):
        self.dump()
        self.output.write_text('<abi-corpus><abi-instr><type-decl name="different"/></abi-instr></abi-corpus>')
        self.dump()
        self.assertEqual(len(self.invocations), 2)

    def test_missing_dump_regenerates(self):
        self.dump()
        self.output.unlink()
        self.dump()
        self.assertEqual(len(self.invocations), 2)

    def test_corrupt_receipt_regenerates(self):
        self.dump()
        self.receipt.write_text('{interrupted receipt')
        self.dump()
        self.assertEqual(len(self.invocations), 2)

    def test_unbound_input_does_not_reuse_old_dump(self):
        self.dump()
        self.inputs_available = False
        self.generate_failure = True
        with self.assertRaisesRegex(RuntimeError, 'debug ABI'):
            self.dump()
        self.assertEqual(len(self.invocations), 2)

    def test_failed_regeneration_cannot_leave_reusable_old_proof(self):
        self.dump()
        self.debug.unlink()
        self.generate_failure = True
        with self.assertRaisesRegex(RuntimeError, 'debug ABI'):
            self.dump()
        self.generate_failure = False
        self.dump()
        self.assertEqual(len(self.invocations), 3)


class StreamingAbiValidation(unittest.TestCase):
    def test_large_typed_dump_validation_has_bounded_memory(self):
        with TemporaryDirectory(prefix='abi-stream-validation-') as temporary:
            output = Path(temporary) / 'large.xml'
            with output.open('w') as handle:
                handle.write('<abi-corpus><abi-instr>')
                for _ in range(200):
                    handle.write('<type-decl name="fixture"/>' * 1000)
                handle.write('</abi-instr></abi-corpus>')
            script = ('import json,resource,runpy,sys; p=runpy.run_path(sys.argv[1]); '
                      'result=p["validate_abi_dump"](sys.argv[2],"unused-typed-source"); '
                      'print(json.dumps({"empty":result,"rss_kib":resource.getrusage(resource.RUSAGE_SELF).ru_maxrss}))')
            result = subprocess.run([sys.executable, '-c', script, str(ROOT / 'bin/shared-library-plan'), str(output)],
                                    capture_output=True, text=True, check=True)
            proof = json.loads(result.stdout)
            self.assertFalse(proof['empty'])
            self.assertLess(proof['rss_kib'], 64 * 1024)

    def test_typed_prefix_does_not_hide_malformed_xml_tail(self):
        with TemporaryDirectory(prefix='abi-stream-invalid-') as temporary:
            output = Path(temporary) / 'invalid.xml'
            output.write_text('<abi-corpus><abi-instr><type-decl/></abi-instr><broken></abi-corpus>')
            with self.assertRaises(namespace['ET'].ParseError):
                planner['validate_abi_dump'](output, 'unused-typed-source')


@unittest.skipUnless(all(shutil.which(tool) for tool in ['gcc', 'objcopy', 'strip', 'abidw', 'readelf']),
                     'GCC, binutils and libabigail required')
class NativeAbiDumpCache(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory(prefix='native-abi-dump-cache-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.runtime = self.root / 'lib/libfixture.so.1'
        self.runtime.parent.mkdir()
        source = self.root / 'source.c'
        source.write_text('struct input { int value; }; int fixture(struct input *input) { return input->value; }')
        subprocess.run(['gcc', '-shared', '-fPIC', '-g', '-Wl,-soname,libfixture.so.1',
                        '-o', str(self.runtime), str(source)], check=True, capture_output=True)
        self.output = self.root / planner['METADATA'] / 'abi/fixture/libfixture.so.1.xml'
        self.original_run = namespace['run']
        self.invocations = []
        self.command_patch = patch.dict(namespace, {'run': self.command})
        self.command_patch.start()
        self.addCleanup(self.command_patch.stop)

    def command(self, command):
        if Path(command[0]).name == 'abidw' and '--out-file' in command:
            self.invocations.append(command)
        return self.original_run(command)

    def dump(self):
        planner['dump_abi'](self.root, 'fixture', library())

    def test_native_identical_elf_reuses_verified_dump(self):
        self.dump()
        self.dump()
        self.assertEqual(len(self.invocations), 1)

    def test_native_unrelated_debug_file_does_not_invalidate_this_library(self):
        unrelated = self.root / 'debug/lib/other.so.1.debug'
        unrelated.parent.mkdir(parents=True)
        unrelated.write_bytes(b'unrelated debug input')
        self.dump()
        unrelated.write_bytes(b'changed unrelated debug input')
        self.dump()
        self.assertEqual(len(self.invocations), 1)

    def test_native_identical_copied_root_reuses_verified_dump(self):
        self.dump()
        with TemporaryDirectory(prefix='native-abi-copy-') as temporary:
            copied = Path(temporary) / 'sdk'
            shutil.copytree(self.root, copied)
            planner['dump_abi'](copied, 'fixture', library())
        self.assertEqual(len(self.invocations), 1)

    def test_native_captured_inputs_adopt_completed_dump_and_reject_changed_input(self):
        captured = planner['abi_dump_inputs'](self.root, 'lib/libfixture.so.1', debug_scope='tree')
        self.assertIsNotNone(captured)
        self.dump()
        proof = self.output.with_suffix('.xml.inputs.json')
        proof.unlink()
        output_hash = hashlib.sha256(self.output.read_bytes()).hexdigest()
        planner['adopt_abi_dump'](self.root, 'lib/libfixture.so.1', self.output, captured, output_hash)
        self.dump()
        self.assertEqual(len(self.invocations), 1)
        with self.runtime.open('ab') as handle:
            handle.write(b'input changed during old analysis')
        with self.assertRaisesRegex(RuntimeError, 'captured input proof'):
            planner['adopt_abi_dump'](self.root, 'lib/libfixture.so.1', self.output, captured, output_hash)

    def test_native_changed_elf_invalidates_old_dump(self):
        self.dump()
        with self.runtime.open('ab') as handle:
            handle.write(b'changed ELF input')
        self.dump()
        self.assertEqual(len(self.invocations), 2)

    def test_native_corrupt_dump_regenerates(self):
        self.dump()
        self.output.write_text('<abi-corpus/>')
        self.dump()
        self.assertEqual(len(self.invocations), 2)

    def test_native_missing_debug_never_reuses_cached_types(self):
        self.dump()
        subprocess.run(['strip', '--strip-debug', str(self.runtime)], check=True, capture_output=True)
        with self.assertRaisesRegex(RuntimeError, 'type information'):
            self.dump()
        self.assertEqual(len(self.invocations), 2)

    def split_debug(self):
        debug = self.root / 'debug' / str(self.runtime).lstrip('/')
        debug = debug.with_name(debug.name + '.debug')
        debug.parent.mkdir(parents=True)
        subprocess.run(['objcopy', '--only-keep-debug', str(self.runtime), str(debug)], check=True, capture_output=True)
        subprocess.run(['strip', '--strip-debug', str(self.runtime)], check=True, capture_output=True)
        subprocess.run(['objcopy', '--add-gnu-debuglink=' + str(debug), str(self.runtime)], check=True, capture_output=True)
        return debug

    def test_native_external_debug_bytes_are_bound_even_when_runtime_is_unchanged(self):
        debug = self.split_debug()
        runtime_hash = hashlib.sha256(self.runtime.read_bytes()).hexdigest()
        self.dump()
        with debug.open('ab') as handle:
            handle.write(b'changed separate debug input')
        try:
            self.dump()
        except RuntimeError as error:
            self.assertIn('type information', str(error))
        self.assertEqual(hashlib.sha256(self.runtime.read_bytes()).hexdigest(), runtime_hash)
        self.assertEqual(len(self.invocations), 2)

    def test_native_missing_external_debug_does_not_reuse_cached_types(self):
        debug = self.split_debug()
        self.dump()
        debug.unlink()
        with self.assertRaisesRegex(RuntimeError, 'type information'):
            self.dump()
        self.assertEqual(len(self.invocations), 2)

    def test_native_flag_change_regenerates_dump(self):
        self.dump()
        with patch.dict(namespace, {'ABI_DUMP_FLAGS': tuple(reversed(namespace['ABI_DUMP_FLAGS']))}):
            self.dump()
        self.assertEqual(len(self.invocations), 2)

    def test_native_tool_binary_change_regenerates_dump(self):
        tools = self.root / 'tools'
        tools.mkdir()
        executable = tools / 'abidw'
        shutil.copy2(shutil.which('abidw'), executable)
        with patch.dict(os.environ, {'PATH': str(tools) + os.pathsep + os.environ['PATH']}):
            self.dump()
            with executable.open('ab') as handle:
                handle.write(b'changed analysis tool bytes')
            self.dump()
        self.assertEqual(len(self.invocations), 2)


if __name__ == '__main__':
    unittest.main()
