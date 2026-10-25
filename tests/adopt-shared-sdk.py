#!/usr/bin/env python3
import hashlib
import json
from pathlib import Path
import runpy
import shutil
import subprocess
import tempfile

module = runpy.run_path(str(Path(__file__).resolve().parents[1] / 'bin/adopt-shared-sdk'))
with tempfile.TemporaryDirectory(prefix='spp-sdk-relocation-') as directory:
    root = Path(directory)
    (root / 'lib').mkdir()
    (root / 'license').write_text('MIT')
    (root / 'value.c').write_text('int value(void) { return 42; }')
    library = root / 'lib/libvalue.so.1'
    subprocess.check_call(['cc', '-shared', '-fPIC', '-g', '-Wl,-soname,libvalue.so.1', '-Wl,--enable-new-dtags,-rpath,/usr/lib64/php-zts/lib', str(root / 'value.c'), '-o', str(library)])
    details = module['elf'](library)
    entry = {'version': '1.0', 'source_hash': 'original-source', 'license': 'MIT', 'license_files': ['license'],
             'files': ['lib/libvalue.so.1', 'license'], 'runtime_libdir': module['OLD'],
             'file_hashes': {name: module['digest'](root / name) for name in ['lib/libvalue.so.1', 'license']},
             'elf': {'lib/libvalue.so.1': {key: details[key] for key in ['needed', 'soname', 'runpath']}},
             'build_identity': {'compiled_ldflags': '-Wl,-rpath,/usr/lib64/php-zts/lib'},
             'runtime_assets': {}}
    manifest = {'schema': 1, 'linkage': 'shared', 'libc': 'glibc', 'architecture': 'x86_64', 'runtime_libdir': module['OLD'], 'libraries': {'value': entry}}
    for filename in ['shared-libraries.json', '.shared-library-ownership.json']:
        (root / filename).write_text(json.dumps(manifest))
    failed = root / 'failed-copy'
    (failed / 'lib').mkdir(parents=True)
    shutil.copy2(library, failed / 'lib/libvalue.so.1')
    shutil.copy2(library, failed / 'lib/z-bad.so.1')
    shutil.copy2(root / 'license', failed / 'license')
    subprocess.check_call(['patchelf', '--set-rpath', '/unexpected/path', str(failed / 'lib/z-bad.so.1')])
    bad_manifest = json.loads(json.dumps(manifest))
    bad_entry = bad_manifest['libraries']['value']
    bad_entry['files'].append('lib/z-bad.so.1')
    bad_entry['file_hashes']['lib/z-bad.so.1'] = module['digest'](failed / 'lib/z-bad.so.1')
    for filename in ['shared-libraries.json', '.shared-library-ownership.json']:
        (failed / filename).write_text(json.dumps(bad_manifest))
    original_copy_hash = module['digest'](failed / 'lib/libvalue.so.1')
    original_manifest = (failed / 'shared-libraries.json').read_bytes()
    try:
        module['adopt'](failed)
    except RuntimeError as error:
        assert 'Unexpected deployment search path' in str(error)
    else:
        raise AssertionError('A late invalid ELF path must reject the import')
    assert module['digest'](failed / 'lib/libvalue.so.1') == original_copy_hash
    assert (failed / 'shared-libraries.json').read_bytes() == original_manifest
    assert not (failed / 'provenance/sdk-relocation').exists()
    original_inputs = json.dumps({'compatibility': 'old-actual-compiler-identity', 'libraries': {'value': {'recipe_hash': 'old-actual-recipe'}}})
    (root / 'shared-library-build-inputs.json').write_text(original_inputs)
    result = module['adopt'](root)
    actual = json.loads((root / 'shared-libraries.json').read_text())
    assert result['recompiled_libraries'] == 0 and result['production_ready'] is False
    assert actual['runtime_libdir'] == module['NEW']
    assert actual['libraries']['value']['build_identity'] == entry['build_identity']
    assert actual['libraries']['value']['source_hash'] == entry['source_hash']
    assert (root / 'shared-library-build-inputs.json').read_text() == original_inputs
    assert module['elf'](library)['text_sha256'] == details['text_sha256']
    assert module['elf'](library)['runpath'] == [module['NEW']]
    proof = json.loads((root / 'provenance/sdk-relocation/proof.json').read_text())
    assert proof['build_input_state_rewritten'] is False
    assert proof['required_runtime_aliases']['deb'] == '/usr/lib64/php-zts -> ../lib/php-zts'
    try:
        module['adopt'](root)
    except RuntimeError:
        pass
    else:
        raise AssertionError('A converted SDK must not be silently reimported')
print('Passed SDK relocation, preserved code/source/compiler identities, explicit provenance and rollback after a late failure.')
