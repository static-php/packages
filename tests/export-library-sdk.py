#!/usr/bin/env python3
import json
from pathlib import Path
import runpy
import tempfile

helper = runpy.run_path(str(Path(__file__).resolve().parents[1] / 'bin/export-library-sdk'))
with tempfile.TemporaryDirectory(prefix='spp-sdk-export-') as directory:
    base = Path(directory)
    source = base / 'source'
    for name in ['lib', 'include/php', 'modules', 'bin']:
        (source / name).mkdir(parents=True, exist_ok=True)
    for name, content in [('lib/libvalue.so.1', 'library'), ('include/library.h', 'library headers'), ('license', 'MIT'),
                          ('bin/php', 'old PHP'), ('include/php/php.h', 'old PHP headers'), ('modules/old.so', 'old PHP module'),
                          ('bin/frankenphp', 'unowned old FrankenPHP')]:
        (source / name).write_text(content)
    (source / 'lib/libvalue.so').symlink_to('libvalue.so.1')
    entry = {'files': ['lib/libvalue.so.1', 'lib/libvalue.so', 'license'], 'license_files': ['license'],
             'abi_dump': {'libvalue.so.1': 'abi/missing-legacy.xml'},
             'file_hashes': {name: helper['file_hash'](source / name) for name in ['lib/libvalue.so.1', 'lib/libvalue.so', 'license']}}
    manifest = {'schema': 1, 'linkage': 'shared', 'libraries': {'value': entry}}
    (source / 'shared-libraries.json').write_text(json.dumps(manifest))
    (source / '.shared-library-ownership.json').write_text(json.dumps(manifest))
    build = {'value': {'files': ['lib/libvalue.so.1', 'lib/libvalue.so', 'include/library.h', 'license']},
             'php': {'files': ['bin/php', 'include/php/php.h', 'modules/old.so']}}
    (source / '.build.json').write_text(json.dumps(build))
    destination = base / 'sdk'
    result = helper['export'](source, destination)
    assert (destination / 'include/library.h').read_text() == 'library headers'
    assert (destination / 'lib/libvalue.so').is_symlink()
    assert not (destination / 'bin/php').exists()
    assert not (destination / 'bin/frankenphp').exists()
    assert not (destination / 'include/php').exists()
    assert not (destination / 'modules').exists()
    assert not (destination / 'abi').exists()
    assert list(json.loads((destination / '.build.json').read_text())) == ['value']
    assert result['library_ownership_intersection'] == []
    assert (source / 'bin/php').read_text() == 'old PHP'
    entry['files'].append('include/php/php.h')
    (source / 'shared-libraries.json').write_text(json.dumps(manifest))
    try:
        helper['export'](source, base / 'bad-sdk')
    except RuntimeError as error:
        assert 'intersects library ownership' in str(error)
    else:
        raise AssertionError('Must not remove a library-owned path as PHP output')
    assert not (base / 'bad-sdk').exists()
print('Passed library-only SDK export, developer header preservation, stale consumer exclusion and ownership conflict rejection.')
