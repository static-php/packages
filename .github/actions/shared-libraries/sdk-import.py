#!/usr/bin/env python3
"""Acknowledge a proven deployment-only SDK migration without relabelling compilation."""

import argparse
import copy
import hashlib
import json
from pathlib import Path
import re
import os

RECEIPT = 'shared-library-deployment-compatibility.json'
OLD_DIR = '/usr/lib64/php-zts/lib'
NEW_DIR = '/usr/lib/php-zts/lib'
DERIVED_STATE_FIELDS = {'compiler_runtime', 'compiler_runtime_attestation'}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def file_hash(path):
    result = hashlib.sha256()
    with Path(path).open('rb') as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b''):
            result.update(chunk)
    return result.hexdigest()


def read(path):
    return json.loads(Path(path).read_text())


def write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_text(json.dumps(value, sort_keys=True, indent=2) + '\n')
    temporary.replace(path)


def compatibility_key(target, toolchain, environment):
    value = {'target': target, 'toolchain': json.dumps(toolchain, sort_keys=True), 'build_environment': environment}
    return digest(value)[:32]


def compilation_identity(record):
    return {key: value for key, value in record.items() if key not in DERIVED_STATE_FIELDS}


def payload_hash(sdk, relative):
    if not isinstance(relative, str) or Path(relative).is_absolute() or '..' in Path(relative).parts:
        raise RuntimeError('Invalid imported library path')
    path = Path(sdk) / relative
    try:
        path.resolve(strict=True).relative_to(Path(sdk).resolve())
    except (OSError, ValueError):
        raise RuntimeError('Missing or escaping imported library output: ' + relative) from None
    if path.is_symlink():
        return hashlib.sha256(('symlink:' + os.readlink(path)).encode()).hexdigest()
    return file_hash(path)


def validate_environment(previous, current, relocation):
    if previous.get('schema') != 1 or current.get('schema') != 1:
        raise RuntimeError('Missing structured library build environment')
    if previous.get('architecture') != current.get('architecture'):
        raise RuntimeError('SDK import cannot change architecture')
    if previous.get('type') != 'rpm' or previous.get('target') not in ['alma8-x86_64', 'alma8-arm64']:
        raise RuntimeError('SDK import requires an attested EL8 producer')
    expected = copy.deepcopy(previous)
    changes = []
    if expected.get('options', {}).get('with-shared-libdir') == OLD_DIR and current.get('options', {}).get('with-shared-libdir') == NEW_DIR:
        if relocation.get('from') != OLD_DIR or relocation.get('to') != NEW_DIR:
            raise RuntimeError('Missing exact private-layout relocation proof')
        expected['options']['with-shared-libdir'] = NEW_DIR
        changes.append('logical private library directory with required compatibility aliases')
    for name, default in [('GOAMD64', 'v1'), ('GOARM64', 'v8.0')]:
        if name not in expected['environment'] and current.get('environment', {}).get(name) == default:
            expected['environment'][name] = default
            changes.append('explicit ' + name + ' baseline default')
    for name in ['type', 'target']:
        expected[name] = current.get(name)
    if current.get('type') not in ['rpm', 'deb', 'glibc'] or not re.fullmatch(r'(?:alma8-)?(?:x86_64|amd64|arm64|aarch64)', current.get('target', '')):
        raise RuntimeError('Imported deployment target is not the common EL8 glibc family')
    if expected != current:
        raise RuntimeError('SDK import changed compiler flags, recipe implementation or other build inputs')
    return changes


def old_metadata(sdk, provenance):
    sdk, provenance = Path(sdk), Path(provenance)
    attestation = read(provenance / 'attestation.json')
    if attestation.get('schema') != 1 or attestation.get('complete') is not True:
        raise RuntimeError('Missing complete original input attestation')
    files = {'build-environment.json': 'build_environment_descriptor_sha256', 'toolchain.json': 'toolchain_descriptor_sha256'}
    for filename, field in files.items():
        if file_hash(provenance / filename) != attestation.get(field):
            raise RuntimeError('Original attested input bytes changed: ' + filename)
    relocation = read(sdk / 'provenance/sdk-relocation/proof.json')
    if relocation.get('schema') != 1 or relocation.get('kind') != 'validated-legacy-runtime-relocation' or relocation.get('build_input_state_rewritten') is not False:
        raise RuntimeError('SDK has no honest deployment-only relocation proof')
    snapshots = {'shared-library-build-inputs.json': 'state_sha256', 'shared-libraries.json': 'source_manifest_sha256'}
    for filename, field in snapshots.items():
        snapshot = sdk / 'provenance/sdk-relocation' / ('original-' + filename)
        actual = file_hash(snapshot)
        if actual != relocation.get('original_metadata_sha256', {}).get(filename) or actual != attestation.get(field):
            raise RuntimeError('Original SDK compilation/source metadata changed: ' + filename)
    environment, toolchain = read(provenance / 'build-environment.json'), read(provenance / 'toolchain.json')
    state = read(sdk / 'provenance/sdk-relocation/original-shared-library-build-inputs.json')
    key = compatibility_key(environment['type'] + '-' + environment['target'], toolchain, environment)
    if key != state.get('compatibility') or key != attestation.get('compatibility'):
        raise RuntimeError('Original structured inputs do not reproduce the recorded compiler identity')
    return attestation, relocation, environment, toolchain, state


def approve(sdk, provenance, environment, toolchain, inventory, target, runtime_scan, previous_runtime):
    sdk, provenance = Path(sdk), Path(provenance)
    attestation, relocation, old_environment, old_toolchain, original_state = old_metadata(sdk, provenance)
    if old_toolchain != toolchain:
        raise RuntimeError('SDK import cannot change compiler/binutils, system headers, specs or compiler runtime dependencies')
    changes = validate_environment(old_environment, environment, relocation)
    previous_inventory = read(provenance / 'library-inventory.json')
    original_manifest = read(sdk / 'provenance/sdk-relocation/original-shared-libraries.json')
    state = read(sdk / 'shared-library-build-inputs.json')
    manifest = read(sdk / 'shared-libraries.json')
    if state.get('compatibility') != original_state.get('compatibility'):
        raise RuntimeError('Original compilation compatibility was relabelled')
    if manifest.get('runtime_libdir') != NEW_DIR or manifest.get('architecture') != environment['architecture']:
        raise RuntimeError('SDK deployment manifest does not match the validated migration')
    if runtime_scan.get('schema') != 1 or runtime_scan.get('uncertain_stripped_libraries'):
        raise RuntimeError('SDK has unproven stripped C++ runtime inputs')
    current_runtime = runtime_scan.get('compiler_runtime', {})
    if previous_runtime.get('schema') != 1 or current_runtime.get('schema') != 1:
        raise RuntimeError('Missing old/current C++ header and selector evidence')
    if previous_runtime.get('semantic', {}).get('headers_sha256') != current_runtime.get('semantic', {}).get('headers_sha256'):
        raise RuntimeError('SDK import cannot silently change compiled C++ headers')
    if current_runtime.get('semantic', {}).get('mode') != 'shared':
        raise RuntimeError('Current compiler must select the shared C++ runtime')
    libraries = {}
    required = set(runtime_scan.get('must_rebuild', []))
    deployment = compatibility_key(target, toolchain, environment)
    for name, record in state.get('libraries', {}).items():
        old = original_state.get('libraries', {}).get(name)
        if old is None:
            raise RuntimeError('Imported library lacks its actual original input identity: ' + name)
        current_recipe = inventory.get('libraries', {}).get(name)
        original_recipe = previous_inventory.get('libraries', {}).get(name)
        if current_recipe is None or original_recipe is None:
            raise RuntimeError('SDK import has no original/current managed recipe: ' + name)
        compiled_recipe = current_recipe if record.get('compiled_compatibility') == deployment else original_recipe
        if current_recipe != compiled_recipe:
            required.add(name)
        for field in ['artifact', 'depends', 'inputs', 'recipe_hash']:
            if record.get(field) != compiled_recipe.get(field):
                raise RuntimeError('SDK compilation state does not match its recipe: ' + name)
        entry = manifest.get('libraries', {}).get(name)
        if entry is None:
            raise RuntimeError('Imported source state has no matching library: ' + name)
        artifact = record.get('artifact')
        expected_source = record.get('sources', {}).get(artifact, {}).get('source', {}).get('hash')
        if expected_source is None or entry.get('source_hash') != expected_source:
            raise RuntimeError('Imported compiled source identity is unproven: ' + name)
        scanned = runtime_scan.get('libraries', {}).get(name)
        if scanned is None:
            raise RuntimeError('Library has no C/C++ runtime inspection: ' + name)
        for relative in entry['files']:
            actual = payload_hash(sdk, relative)
            if actual != entry.get('file_hashes', {}).get(relative):
                raise RuntimeError('Imported SDK output hash is stale: ' + relative)
            if not scanned.get('uses_cxx_runtime'):
                expected = relocation.get('elf_files', {}).get(relative, {}).get('after_sha256')
                if expected is None:
                    expected = original_manifest['libraries'][name].get('file_hashes', {}).get(relative)
                if actual != expected:
                    raise RuntimeError('Unrebuilt ordinary library changed beyond loader relocation: ' + name)
        # Old static consumers remain explicit rebuild roots, never accepted as fresh.
        libraries[name] = {'compilation_record_sha256': digest(compilation_identity(record)),
                           'recipe_hash': record['recipe_hash'], 'source_hash': entry['source_hash'],
                           'compiler_runtime': {'fingerprint': scanned['fingerprint'], 'inputs': scanned['compiler_runtime_inputs'],
                                                'uses_cxx_runtime': scanned['uses_cxx_runtime']}}
    return {'schema': 1, 'kind': 'validated-deployment-compatibility',
            'compilation_compatibility': original_state['compatibility'], 'deployment_compatibility': deployment,
            'target': target, 'environment': environment, 'toolchain': toolchain,
            'original_environment': old_environment, 'original_toolchain': old_toolchain,
            'accepted_changes': changes, 'cpp_headers_verified': True,
            'relocation_proof_sha256': file_hash(sdk / 'provenance/sdk-relocation/proof.json'),
            'original_attestation': attestation, 'libraries': libraries,
            'must_rebuild': sorted(required & libraries.keys()),
            'actual_compilation_identity_preserved': True}


def validate(sdk, state, compatibility):
    path = Path(sdk) / RECEIPT
    if not path.is_file():
        return None
    receipt = read(path)
    if receipt.get('schema') != 1 or receipt.get('kind') != 'validated-deployment-compatibility' or receipt.get('actual_compilation_identity_preserved') is not True:
        raise RuntimeError('Invalid imported SDK compatibility receipt')
    if receipt.get('deployment_compatibility') != compatibility or compatibility_key(receipt['target'], receipt['toolchain'], receipt['environment']) != compatibility:
        raise RuntimeError('Imported SDK does not authorize the current compiler/deployment inputs')
    previous = receipt.get('original_environment', {})
    old_toolchain = receipt.get('original_toolchain')
    if old_toolchain != receipt.get('toolchain') or compatibility_key(previous.get('type', '') + '-' + previous.get('target', ''), old_toolchain, previous) != receipt.get('compilation_compatibility'):
        raise RuntimeError('Imported SDK original structured compiler identity changed')
    if state.get('compatibility') != receipt.get('compilation_compatibility'):
        raise RuntimeError('Imported SDK actual compilation identity was overwritten')
    if file_hash(Path(sdk) / 'provenance/sdk-relocation/proof.json') != receipt.get('relocation_proof_sha256'):
        raise RuntimeError('Imported SDK relocation proof changed')
    validate_environment(previous, receipt['environment'], read(Path(sdk) / 'provenance/sdk-relocation/proof.json'))
    for name, accepted in receipt.get('libraries', {}).items():
        record = state.get('libraries', {}).get(name)
        if record is None:
            continue
        if record.get('compiled_compatibility') == compatibility:
            continue
        if digest(compilation_identity(record)) != accepted.get('compilation_record_sha256'):
            raise RuntimeError('Imported SDK compilation inputs changed without rebuilding: ' + name)
    return receipt


def refresh_rebuilt(state, manifest, inventory, receipt, runtime_scan, build_receipt, names, rebuilt_sources=None):
    names = sorted(set(names))
    if not names or sorted(build_receipt.get('selected', [])) != names or not (build_receipt.get('complete') is True or build_receipt.get('state') == 'completed'):
        raise RuntimeError('A completed exact-scope native rebuild receipt is required')
    if build_receipt.get('protected_output_hashes_unchanged') is not True:
        raise RuntimeError('Native rebuild did not attest unchanged unselected library outputs')
    if state.get('compatibility') != receipt.get('compilation_compatibility'):
        raise RuntimeError('Cannot overwrite original SDK compilation identity')
    if rebuilt_sources is not None:
        if not isinstance(rebuilt_sources, dict) or build_receipt.get('rebuilt_sources_sha256') != digest(rebuilt_sources):
            raise RuntimeError('Completed rebuild does not attest the exact actual source snapshot')
        artifacts = {inventory.get('libraries', {}).get(name, {}).get('artifact') for name in names}
        if not set(rebuilt_sources) <= artifacts:
            raise RuntimeError('Rebuilt source snapshot includes an unselected library')
    result = copy.deepcopy(state)
    for name in names:
        current = runtime_scan.get('libraries', {}).get(name)
        if current is None or current.get('embedded_cxx_runtime') or name in runtime_scan.get('must_rebuild', []):
            raise RuntimeError('Rebuilt output still has an unaccepted C++ runtime: ' + name)
        old = state.get('libraries', {}).get(name)
        recipe = inventory.get('libraries', {}).get(name)
        entry = manifest.get('libraries', {}).get(name)
        if old is None or recipe is None or entry is None:
            raise RuntimeError('Rebuild scope has no recorded recipe/source: ' + name)
        artifact = recipe.get('artifact')
        sources = copy.deepcopy(old.get('sources', {}))
        if rebuilt_sources is not None and artifact in rebuilt_sources:
            sources[artifact] = copy.deepcopy(rebuilt_sources[artifact])
        if entry.get('source_hash') != sources.get(artifact, {}).get('source', {}).get('hash'):
            raise RuntimeError('Rebuilt output differs from the pinned actual source: ' + name)
        record = dict(recipe, sources=copy.deepcopy(sources),
                      compiled_compatibility=receipt['deployment_compatibility'],
                      compiler_runtime={'fingerprint': current['fingerprint'], 'inputs': current['compiler_runtime_inputs'], 'uses_cxx_runtime': current['uses_cxx_runtime']},
                      compiler_runtime_attestation='rebuilt')
        record['dependency_signatures'] = {dependency: {'source_hash': manifest['libraries'][dependency].get('source_hash'),
                                                       'recipe_hash': inventory['libraries'].get(dependency, {}).get('recipe_hash')}
                                           for dependency in old.get('dependency_signatures', {}) if dependency in manifest['libraries']}
        result['libraries'][name] = record
        if rebuilt_sources is not None and artifact in rebuilt_sources:
            result.setdefault('sources', {})[artifact] = copy.deepcopy(rebuilt_sources[artifact])
    result['deployment_compatibility'] = receipt['deployment_compatibility']
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--sdk', type=Path, required=True)
    parser.add_argument('--legacy-provenance', type=Path, required=True)
    parser.add_argument('--environment', type=Path, required=True)
    parser.add_argument('--toolchain', type=Path, required=True)
    parser.add_argument('--inventory', type=Path, required=True)
    parser.add_argument('--target', required=True)
    parser.add_argument('--runtime-scan', type=Path, required=True)
    parser.add_argument('--previous-runtime-inputs', type=Path, required=True)
    parser.add_argument('--compatibility')
    parser.add_argument('--output', type=Path)
    parser.add_argument('--rebuilt-libraries', default='')
    parser.add_argument('--build-receipt', type=Path)
    parser.add_argument('--rebuilt-sources', type=Path)
    args = parser.parse_args()
    receipt = approve(args.sdk, args.legacy_provenance, read(args.environment), read(args.toolchain), read(args.inventory),
                      args.target, read(args.runtime_scan), read(args.previous_runtime_inputs))
    if args.compatibility and receipt['deployment_compatibility'] != args.compatibility:
        raise RuntimeError('Requested deployment compatibility does not match the structured input descriptors')
    if args.rebuilt_libraries:
        if args.build_receipt is None:
            raise RuntimeError('Specify --build-receipt for --rebuilt-libraries')
        state = refresh_rebuilt(read(args.sdk / 'shared-library-build-inputs.json'), read(args.sdk / 'shared-libraries.json'),
                                read(args.inventory), receipt, read(args.runtime_scan), read(args.build_receipt),
                                list(filter(None, args.rebuilt_libraries.split(','))),
                                read(args.rebuilt_sources) if args.rebuilt_sources else None)
        write(args.sdk / 'shared-library-build-inputs.json', state)
    elif args.rebuilt_sources:
        raise RuntimeError('--rebuilt-sources requires --rebuilt-libraries')
    write(args.output or args.sdk / RECEIPT, receipt)
    print(json.dumps({'deployment_compatibility': receipt['deployment_compatibility'], 'compilation_compatibility': receipt['compilation_compatibility'],
                      'reused_input_records': len(receipt['libraries']), 'must_rebuild': receipt['must_rebuild']}, sort_keys=True))


if __name__ == '__main__':
    main()
