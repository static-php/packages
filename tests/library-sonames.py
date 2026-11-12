import copy
from pathlib import Path
import re
import runpy
import shutil
import subprocess
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch


planner = runpy.run_path(str(Path(__file__).resolve().parents[1] / 'bin/shared-library-plan'))


def manifest(version='1.0.0', soname='libfixture.so.1', path=None):
    return {'schema': 1, 'linkage': 'shared', 'libraries': {'fixture': {
        'version': version, 'source_hash': version,
        'elf': {path or 'lib/' + soname: {'soname': soname}}, 'depends': []}}}


class LibrarySonames(unittest.TestCase):
    def test_matching_names_ignore_versions_contents_and_debug_metadata(self):
        for version in ['1.0.0', '1.0.1', '1.1.0', '2.0.0', 'unknown']:
            with self.subTest(version=version):
                old, new = manifest(), manifest(version)
                old['libraries']['fixture'].update(abi_dump={'libfixture.so.1': 'missing.xml'},
                    abi_dump_hashes={'libfixture.so.1': 'old'}, abi_empty=[], abi_reference_version='0.9',
                    abi_comparison={'status': 'breaking'}, abi_epoch=7,
                    abi_capability='php-zts-lib-fixture-abi-7', abi_capabilities=['php-zts-lib-fixture-abi-7'])
                new['libraries']['fixture'].update(copy.deepcopy(old['libraries']['fixture']))
                new['libraries']['fixture']['version'] = version
                new['libraries']['fixture']['elf']['lib/libfixture.so.1']['sha256'] = 'different bytes'
                with patch.object(subprocess, 'run', side_effect=AssertionError('No ABI tools')):
                    annotated, result = planner['compare'](old, new)
                self.assertEqual(result['libraries']['fixture']['status'], 'compatible')
                entry = annotated['libraries']['fixture']
                self.assertFalse(set(entry) & {'abi_dump', 'abi_dump_hashes', 'abi_empty', 'abi_reference_version', 'abi_comparison', 'abi_epoch', 'abi_capability', 'abi_capabilities'})
                self.assertIn('abi_dump', old['libraries']['fixture'])

    def test_soname_change_is_detected_for_every_upstream_version(self):
        for version in ['1.0.0', '1.0.1', '1.1.0', '2.0.0', 'unknown']:
            for soname in ['libfixture.so.2', 'librenamed.so.1']:
                with self.subTest(version=version, soname=soname):
                    annotated, result = planner['compare'](manifest(), manifest(version, soname), rebuilt_libraries=set())
                    self.assertEqual(result['libraries']['fixture']['status'], 'breaking')
                    self.assertNotIn('abi_epoch', annotated['libraries']['fixture'])

    def test_additions_and_removals_change_the_name_set(self):
        old, new = manifest(), manifest()
        new['libraries']['fixture']['elf']['lib/libextra.so.1'] = {'soname': 'libextra.so.1'}
        for previous, current in [(old, new), (new, old)]:
            _, result = planner['compare'](previous, current)
            self.assertEqual(result['libraries']['fixture']['status'], 'breaking')

    def test_real_filename_changes_with_same_soname_are_compatible(self):
        old = manifest(path='lib/libfixture.so.1.0.0')
        new = manifest(path='lib/libfixture.so.1.9.0')
        _, result = planner['compare'](old, new)
        self.assertEqual(result['libraries']['fixture']['status'], 'compatible')

    def test_libraries_without_sonames_use_their_runtime_filename(self):
        old = manifest(soname=None, path='lib/module.so')
        new = manifest(soname=None, path='lib/renamed.so')
        _, result = planner['compare'](old, new)
        self.assertEqual(result['libraries']['fixture']['status'], 'breaking')
        _, result = planner['compare'](old, copy.deepcopy(old))
        self.assertEqual(result['libraries']['fixture']['status'], 'compatible')

    def test_non_library_elf_files_are_not_compared(self):
        old, new = manifest(), manifest()
        new['libraries']['fixture']['elf']['bin/helper'] = {'soname': None}
        _, result = planner['compare'](old, new)
        self.assertEqual(result['libraries']['fixture']['status'], 'compatible')

    def test_no_runtime_libraries_is_an_empty_name_set(self):
        old = manifest()
        old['libraries']['fixture']['elf'] = {}
        _, result = planner['compare'](old, copy.deepcopy(old))
        self.assertEqual(result['libraries']['fixture']['status'], 'compatible')

    @unittest.skipUnless(all(shutil.which(tool) for tool in ['gcc', 'readelf', 'strip']), 'ELF compiler tools required')
    def test_changed_exported_symbols_and_types_with_same_soname_are_compatible(self):
        with TemporaryDirectory() as temporary:
            directory = Path(temporary)
            snapshots = []
            for index, source in enumerate(['struct value { int n; }; int before(struct value *v) { return v->n; }',
                                            'struct value { double n; }; double after(struct value *v) { return v->n; }']):
                code, library = directory / f'{index}.c', directory / f'{index}.so'
                code.write_text(source)
                subprocess.run(['gcc', '-shared', '-fPIC', '-Wl,-soname,libfixture.so.1', str(code), '-o', str(library)], check=True)
                subprocess.run(['strip', '--strip-unneeded', str(library)], check=True)
                dynamic = subprocess.check_output(['readelf', '-d', str(library)], text=True)
                soname = re.search(r'\(SONAME\).*\[([^\]]+)\]', dynamic)[1]
                snapshots.append(manifest(soname=soname))
            _, result = planner['compare'](*snapshots)
            self.assertEqual(result['libraries']['fixture']['status'], 'compatible')


if __name__ == '__main__':
    unittest.main()
