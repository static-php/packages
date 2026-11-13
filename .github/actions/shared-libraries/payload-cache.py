#!/usr/bin/env python3
"""Reuse sealed PHP outputs while retaining the current shared-library SDK."""

import argparse
import hashlib
import importlib.util
import io
import json
import os
import posixpath
from pathlib import Path
import re
import shutil
import subprocess
import tarfile
import tempfile


def read(path):
    return json.loads(Path(path).read_text())


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True, indent=2) + '\n')


def safe(value):
    if (not isinstance(value, str) or not value or Path(value).is_absolute() or '..' in Path(value).parts
            or '\\' in value or posixpath.normpath(value) != value or value == '.'):
        raise RuntimeError('Invalid payload-cache path: ' + repr(value))
    return value


def identity(path):
    if path.is_symlink():
        return {'symlink': os.readlink(path)}
    return {'sha256': hashlib.sha256(path.read_bytes()).hexdigest(), 'mode': path.stat().st_mode & 0o777}


def php_file(name):
    return (name.startswith(('modules/', 'include/php/', 'lib/php/build/', 'packaging/'))
            or name == 'lib/frankenphp'
            or bool(re.fullmatch(r'bin/(?:php(?:-cgi|-fpm|dbg|-config|ize)?|frankenphp)', name))
            or bool(re.fullmatch(r'lib/libphp[^/]*\.(?:so(?:\.[^/]*)?|a|la)', name)))


def core_file(name, record=None):
    shared = (record or {}).get('configuration', {}).get('shared-extensions', [])
    shared = shared.split(',') if isinstance(shared, str) else shared
    if name.startswith('include/php/ext/') and len(Path(name).parts) > 3:
        extension = Path(name).parts[3].replace('_', '-')
        if extension in {item.strip().replace('_', '-') for item in shared}:
            return False
    return php_file(name) and not name.startswith(('modules/', 'packaging/')) and name != 'bin/frankenphp'


def abi(manifest):
    return {name: {'sonames': sorted({entry.get('soname') or Path(path).name
                                     for path, entry in record.get('elf', {}).items()
                                     if entry.get('soname') or re.search(r'\.so(?:\.|$)', Path(path).name)})}
            for name, record in sorted(manifest.get('libraries', {}).items())}


def compatible(old, current):
    return all(name in current and set(entry['sonames']) == set(current[name]['sonames'])
               for name, entry in old.items())


def library_graph(manifest):
    owners = {soname: name for name, identity in abi(manifest).items() for soname in identity['sonames']}
    result = {}
    for name, library in manifest['libraries'].items():
        declared = library.get('depends', []) + library.get('dlopen_dependencies', [])
        if any(value not in manifest['libraries'] and value not in owners for value in declared):
            raise RuntimeError('Unknown declared private library dependency: ' + name)
        needed = [value for entry in library.get('elf', {}).values() for value in entry.get('needed', [])]
        result[name] = sorted({owners.get(value, value) for value in declared + needed
                               if value in owners or value in manifest['libraries']} - {name})
    return result


def dependency_closure(roots, graph):
    selected = set(roots)
    pending = list(roots)
    while pending:
        name = pending.pop()
        if name not in graph:
            raise RuntimeError('Missing cached library dependency graph: ' + name)
        for dependency in graph[name]:
            if dependency not in selected:
                selected.add(dependency)
                pending.append(dependency)
    return sorted(selected)


def consumer_bindings(payload, record, abi_identities, graph):
    owners = {soname: name for name, entry in abi_identities.items() for soname in entry['sonames']}
    details, php_owners = {}, {}
    for relative in record['files']:
        path = payload / relative
        if not php_file(relative) or path.is_symlink() or not path.is_file():
            continue
        with path.open('rb') as handle:
            if handle.read(4) != b'\x7fELF':
                continue
        output = subprocess.check_output(['readelf', '--wide', '--dynamic', str(path)], text=True)
        details[relative] = re.findall(r'\(NEEDED\).*\[([^\]]+)\]', output)
        for soname in re.findall(r'\(SONAME\).*\[([^\]]+)\]', output):
            php_owners[soname] = relative
    result = {}
    for relative in details:
        private, visited, pending = set(), set(), [relative]
        while pending:
            name = pending.pop()
            if name in visited:
                continue
            visited.add(name)
            for needed in details[name]:
                if needed in owners:
                    private.add(owners[needed])
                elif needed in php_owners:
                    pending.append(php_owners[needed])
        result[relative] = dependency_closure(private, graph)
    return result


def component_file(relative, record):
    if relative == 'bin/frankenphp':
        return 'frankenphp'
    suffix = profile_helper().suffix(record) or ''
    shared = profile_helper().names(record.get('configuration', {}).get('shared-extensions'))
    return next((name for name in shared if relative == 'modules/' + name + suffix + '.so'), None)


def runtime_helper():
    spec = importlib.util.spec_from_file_location('payload_compiler_runtime', Path(__file__).with_name('compiler-runtime-inputs.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def profile_helper():
    spec = importlib.util.spec_from_file_location('payload_profile', Path(__file__).with_name('payload-profile.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def runtime_inputs(root, refresh=False):
    path = root / 'stage/payload-compiler-runtime.json'
    value = read(path) if path.is_file() and not refresh else runtime_helper().compiler_runtime_inputs()
    if value.get('schema') != 1 or value.get('fingerprint') != digest(value.get('semantic')):
        raise RuntimeError('Invalid current compiler runtime inputs')
    if refresh or not path.is_file():
        write(path, value)
    return value


def source_inputs(root, stream):
    spec = importlib.util.spec_from_file_location('payload_source_plan', Path(__file__).with_name('source-plan.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    metadata = read(root / 'downloads/.spc-meta.json')
    metadata['php_source_hashes'] = module.php_archive_inputs(root, metadata, verify=True)
    metadata['extension_sources'] = module.extension_source_inputs(root)
    sources = read(root / 'downloads/.cache.json')
    request = module.plan(sources, sources, metadata, metadata, configuration=module.identity(root),
                          package_configuration=module.package_identity(root), components=module.component_identities(root),
                          compiled_in=module.core_components(root), active=module.active_components(root))
    core = request['payload_core_inputs'][stream]
    components = request['payload_component_inputs'][stream]
    if not re.fullmatch(r'[a-f0-9]{64}', core) or not isinstance(components, dict):
        raise RuntimeError('Invalid recorded PHP source inputs')
    return {'core': core, 'components': components,
            'core_sources': core_sources(module, root, sources, metadata, stream),
            'component_sources': component_sources(module, sources, metadata, stream, components)}


def core_sources(module, root, sources, metadata, stream):
    compiled = module.core_components(root)
    artifacts = module.stream_artifacts(sources, metadata, stream)
    return {'php': metadata.get('php_versions', {}).get(stream),
            'php_source': metadata.get('php_source_hashes', {}).get(stream),
            'artifacts': {name: (value.get('source') or {}).get('hash') for name, value in artifacts.items()
                          if name.replace('_', '-') in compiled}}


def component_sources(module, sources, metadata, stream, names):
    artifacts = module.stream_artifacts(sources, metadata, stream)
    return {name: (artifacts.get(name, {}).get('source') or {}).get('hash')
            for name in names if name not in ('go', 'go-xcaddy')}


def recorded_source_metadata(root, run_id):
    run_id = str(run_id)
    if not run_id.isdigit():
        raise RuntimeError('Cached PHP core has no recorded source run')
    path = root / 'stage' / ('php-core-sources-' + run_id + '.json')
    if path.is_file():
        records = read(path)
    else:
        spec = importlib.util.spec_from_file_location('retained_source_artifact', Path(__file__).with_name('artifact-json.py'))
        artifact = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(artifact)
        records = artifact.source_metadata(run_id)
        write(path, records)
    return records


def recorded_core_sources(root, manifest, stream):
    existing = manifest['inputs'].get('core_sources')
    if existing is not None:
        return existing
    run_id = manifest.get('compiled_source_runs', {}).get('core', manifest['source_run_id'])
    records = recorded_source_metadata(root, run_id)
    spec = importlib.util.spec_from_file_location('retained_source_plan', Path(__file__).with_name('source-plan.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    result = core_sources(module, root, records['.cache.json'], records['.spc-meta.json'], stream)
    if result['php'] != manifest['build_payload']['source_version'] or not result['php_source']:
        raise RuntimeError('Recorded PHP source snapshot does not identify the cached core')
    return result


def recorded_component_sources(root, manifest, stream):
    existing = manifest['inputs'].get('component_sources')
    if existing is not None:
        return existing
    spec = importlib.util.spec_from_file_location('retained_component_sources', Path(__file__).with_name('source-plan.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    runs = manifest.get('compiled_source_runs', {}).get('components', {})
    result = {}
    for name in manifest['inputs']['components']:
        if name in ('go', 'go-xcaddy'):
            continue
        records = recorded_source_metadata(root, runs.get(name, manifest['source_run_id']))
        result.update(component_sources(module, records['.cache.json'], records['.spc-meta.json'], stream, [name]))
    return result


def keys(root, stream, manifest, compiler, runtime=None):
    inputs = source_inputs(root, stream)
    environment = {key: manifest.get(key) for key in ['architecture', 'libc', 'runtime_libdir']}
    seed = digest({'schema': 1, 'core': inputs['core'], 'compiler': compiler, 'environment': environment})
    runtime = runtime or runtime_inputs(root)
    exact = digest({'seed': seed, 'components': inputs['components'], 'abi': abi(manifest), 'cxx_link': runtime['fingerprint']})
    prefix = 'php-payload-' + manifest['libc'] + '-' + manifest['architecture'] + '-' + stream.replace('.', '')
    return {'key': prefix + '-' + exact[:32], 'seed-key': prefix + '-seed-' + seed[:32],
            'input_fingerprint': exact, 'core_fingerprint': seed,
            'inputs': inputs, 'library_abi': abi(manifest), 'environment': environment, 'compiler_runtime': runtime}


def export(args):
    payload = args.payload.resolve()
    subprocess.run(['php', str(args.root / 'bin/build-payload'), 'verify', '--root', str(payload)], check=True)
    record = read(payload / 'build-payload.json')
    if record.get('state') != 'sealed':
        raise RuntimeError('Only sealed PHP payloads can be cached')
    if record.get('packaging_selection'):
        raise RuntimeError('A selected packaging artifact cannot replace the complete PHP cache')
    stream = '.'.join(record['php_version'].split('.')[:2])
    compiler = args.compatibility.read_text().strip()
    sdk_manifest = read(payload / 'shared-libraries.json')
    state = keys(args.root, stream, sdk_manifest, compiler)
    if state['key'] != args.key or state['seed-key'] != args.seed_key:
        raise RuntimeError('PHP cache export inputs do not match its requested key')
    seed_path = payload / 'consumer-cache-seed.json'
    seed = read(seed_path) if seed_path.is_file() else {}
    profiles = profile_helper()
    coverage = profiles.cache_export_coverage(record, seed, args.root, stream)
    allowed = lambda name: profiles.php_file_allowed(name, record, profile=coverage)
    record = json.loads(json.dumps(record))
    for name in list(record['files']):
        if php_file(name) and not allowed(name):
            record['files'].pop(name)
            record.get('prepared', {}).pop(name, None)
    files = {}
    for path in sorted(payload.rglob('*')):
        name = str(path.relative_to(payload))
        if (path.is_file() or path.is_symlink()) and (allowed(name) or name == 'skipped-shared-extensions.json'):
            files[name] = identity(path)
    for name, prepared in record.get('prepared', {}).items():
        if allowed(name):
            for entry in prepared.values():
                relative = safe(entry['path'])
                files[relative] = identity(payload / relative)
    if 'bin/php' not in files or not any(name.startswith('lib/libphp') for name in files):
        raise RuntimeError('A cached PHP payload requires its CLI and embed library')
    core_reused = (seed.get('state') == 'verified' and bool(seed.get('core_files'))
                   and all((payload / name).exists() and identity(payload / name) == value
                           for name, value in seed.get('core_files', {}).items()))
    if core_reused and seed.get('rebuild_packages'):
        context = record.get('build_context', {})
        if (context.get('kind') != 'partial-consumer-build' or context.get('core_seed_key') != seed.get('seed_key')
                or set(seed['rebuild_packages']) - set(context.get('rebuilt_packages', []))):
            raise RuntimeError('Cached PHP consumers still require their selected source/runtime rebuilds')
    previous_runs = seed.get('compiled_source_runs', {})
    core_run = str(previous_runs.get('core', seed.get('compiled_source_run_id', args.source_run_id))) if core_reused else str(args.source_run_id)
    previous_components = previous_runs.get('components', {})
    rebuilt = set(seed.get('rebuild_packages', [])) if core_reused else set(state['inputs']['components'])
    component_runs = {name: (str(args.source_run_id) if not core_reused or
                            ('frankenphp' if name in ('go', 'go-xcaddy') else name) in rebuilt
                            else str(previous_components.get(name, seed.get('compiled_source_run_id', args.source_run_id))))
                      for name in state['inputs']['components']}
    scan = runtime_helper().scan_payload(payload, state['compiler_runtime'], files=set(files))
    consumers = {name: value for name, value in scan['consumers'].items()
                 if {item.get('file') if isinstance(item, dict) else item for item in value.get('files', [])} & files.keys()}
    invalid = set(scan.get('must_rebuild', [])) | set(scan.get('uncertain_stripped_consumers', []))
    if invalid & consumers.keys():
        raise RuntimeError('PHP payload has unverified or embedded static C++ runtime: ' + ','.join(sorted(invalid & consumers.keys())))
    manifest = dict(schema=1, key=args.key, seed_key=args.seed_key, php_stream=stream,
                    source_run_id=str(args.source_run_id), inputs=state['inputs'], environment=state['environment'],
                    compiled_source_runs={'core': core_run, 'components': component_runs},
                    coverage=coverage,
                    compiler_runtime=state['compiler_runtime'], compiler_consumers=consumers,
                    library_dependencies=library_graph(sdk_manifest),
                    consumer_library_dependencies=consumer_bindings(payload, record, state['library_abi'], library_graph(sdk_manifest)),
                    library_abi=state['library_abi'], build_payload=record, files=files)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='spp-payload-export-') as temporary:
        archive_path = Path(temporary) / 'payload.tar'
        with tarfile.open(archive_path, 'w') as archive:
            content = (json.dumps(manifest, sort_keys=True, indent=2) + '\n').encode()
            header = tarfile.TarInfo('cache.json')
            header.size, header.mode = len(content), 0o644
            archive.addfile(header, io.BytesIO(content))
            for name in files:
                archive.add(payload / name, arcname='files/' + safe(name), recursive=False)
        with args.output.open('wb') as destination:
            subprocess.run(['zstd', '-q', '-c', str(archive_path)], stdout=destination, check=True)
    print(json.dumps({'key': args.key, 'seed_key': args.seed_key, 'files': len(files), 'output': str(args.output)}))


def extract(archive, destination):
    process = subprocess.Popen(['zstd', '-q', '-dc', str(archive)], stdout=subprocess.PIPE)
    try:
        with tarfile.open(fileobj=process.stdout, mode='r|') as source:
            first = source.next()
            if first is None or first.name != 'cache.json' or not first.isfile():
                raise RuntimeError('Missing initial PHP cache metadata')
            manifest = json.load(source.extractfile(first))
            allowed = {name for name in manifest.get('files', {}) if php_file(name) or name == 'skipped-shared-extensions.json'}
            for name, prepared in manifest.get('build_payload', {}).get('prepared', {}).items():
                if not php_file(name):
                    continue
                for kind, item in prepared.items():
                    path = safe(item['path'])
                    if (kind == 'runtime' and path != 'payload/runtime/' + name
                            or kind == 'debug' and not path.startswith('payload/debug/')):
                        raise RuntimeError('Unexpected PHP prepared-cache path')
                    allowed.add(path)
            if set(manifest.get('files', {})) - allowed:
                raise RuntimeError('PHP cache must not replace current SDK library files')
            expected = {'cache.json', *('files/' + safe(name) for name in manifest.get('files', {}))}
            seen = {'cache.json'}
            for member in source:
                if member is first:
                    continue
                if member.name in seen:
                    raise RuntimeError('Duplicate PHP cache archive path')
                seen.add(member.name)
                if member.name not in expected:
                    raise RuntimeError('PHP cache archive inventory mismatch')
                name = member.name.removeprefix('files/')
                recorded = manifest['files'][name]
                target = destination / safe(name)
                target.parent.mkdir(parents=True, exist_ok=True)
                if member.issym():
                    link = member.linkname
                    if Path(link).is_absolute() or not (target.parent / link).resolve().is_relative_to(destination.resolve()):
                        raise RuntimeError('PHP cache symlink escapes payload')
                    target.symlink_to(link)
                elif member.isfile():
                    with target.open('wb') as output:
                        shutil.copyfileobj(source.extractfile(member), output)
                    target.chmod(member.mode & 0o777)
                else:
                    raise RuntimeError('Unexpected PHP cache archive member')
                if identity(target) != recorded:
                    raise RuntimeError('PHP cache content mismatch: ' + name)
            if seen != expected:
                raise RuntimeError('PHP cache archive inventory mismatch')
            while process.stdout.read(1024 * 1024):
                pass
    finally:
        process.stdout.close()
        process.wait()
    if process.returncode:
        raise subprocess.CalledProcessError(process.returncode, process.args)
    return manifest


def changed_components(old, current):
    names = {name for name in old.keys() | current.keys() if old.get(name) != current.get(name)}
    return {'frankenphp' if name in ('go', 'go-xcaddy') else name for name in names}


def restore_payload(args):
    sdk = args.sdk.resolve()
    state = keys(args.root, args.stream, read(sdk / 'shared-libraries.json'), args.compatibility.read_text().strip())
    if state['key'] != args.key or state['seed-key'] != args.seed_key:
        raise RuntimeError('PHP cache restore inputs do not match its requested key')
    if args.payload.exists():
        raise RuntimeError('Restore PHP payload only into a new format-independent directory')
    packages = {name.strip().removeprefix('ext-') for name in args.compile_packages.split(',') if name.strip()}
    shared = profile_helper().expected_profile(args.root, args.stream)['shared']
    packages = {next((item for item in shared if item.replace('_', '-') == name.replace('_', '-')), name) for name in packages}
    requested_packages = set(packages)
    args.payload.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='spp-payload-restore-', dir=args.payload.parent) as temporary:
        staged = Path(temporary) / 'files'
        staged.mkdir()
        manifest = extract(args.archive, staged)
        source_reuse = getattr(args, 'reuse_core_sources', False)
        if (manifest.get('schema') != 1 or manifest.get('php_stream') != args.stream
                or not source_reuse and manifest.get('seed_key') != args.seed_key):
            raise RuntimeError('PHP cache core/compiler/stream does not match the current request')
        if manifest.get('environment') != state['environment']:
            raise RuntimeError('Cached PHP payload is incompatible with the current SDK environment')
        same_core = manifest['inputs']['core'] == state['inputs']['core']
        if source_reuse and not same_core:
            previous = recorded_core_sources(args.root, manifest, args.stream)
            current = state['inputs']['core_sources']
            if not current.get('php_source'):
                raise RuntimeError('Current PHP core has no verified source archive identity')
            same_core = previous == current
            if not same_core:
                print(json.dumps({'restored': False, 'core_source_changed': True,
                                  'previous_sources': previous, 'current_sources': current}))
                return
        incompatible = {name for name, entry in manifest['library_abi'].items()
                        if not compatible({name: entry}, state['library_abi'])}
        allowed_abi = {name.strip() for name in getattr(args, 'abi_rebuild_libraries', '').split(',') if name.strip()}
        if incompatible - allowed_abi:
            raise RuntimeError('Cached PHP payload is incompatible with the current SDK ABI')
        if manifest.get('coverage', {}).get('full') is not True:
            raise RuntimeError('Cached PHP payload has no verified complete profile')
        coverage = profile_helper().cache_export_coverage(manifest['build_payload'],
                    {'state': 'verified', 'coverage': manifest['coverage']}, args.root, args.stream)
        write(staged / 'build-payload.json', manifest['build_payload'])
        previous_runtime = manifest.get('compiler_consumers', {})
        scanned = {item['file'] for value in previous_runtime.values() for item in value.get('files', [])}
        expected = {name for name in manifest['build_payload']['files']
                    if php_file(name) and name in manifest['files'] and not (staged / name).is_symlink()}
        if source_reuse:
            scan = {'consumers': previous_runtime}
            print('Preserved recorded compiler runtime analysis for reused PHP sources', flush=True)
        elif (manifest.get('compiler_runtime') == state['compiler_runtime'] and scanned == expected
                and previous_runtime and not any(value.get('embedded_cxx_runtime') for value in previous_runtime.values())):
            scan = {'consumers': previous_runtime}
            print('Reused compiler runtime analysis for verified, unchanged PHP cache bytes', flush=True)
        else:
            scan = runtime_helper().scan_payload(staged, state['compiler_runtime'])
        runtime_rebuilds = set(scan.get('must_rebuild', [])) | set(scan.get('uncertain_stripped_consumers', []))
        runtime_rebuilds |= {name for name, value in scan['consumers'].items()
                            if (value.get('uses_cxx_runtime') or value.get('uses_cxx_headers'))
                            and previous_runtime.get(name, {}).get('fingerprint') != value['fingerprint']}
        if 'core' in runtime_rebuilds:
            raise RuntimeError('Cached PHP core requires a full rebuild for changed compiler runtime inputs')
        packages.update(runtime_rebuilds)
        if source_reuse:
            changes = changed_components(recorded_component_sources(args.root, manifest, args.stream), state['inputs']['component_sources'])
        else:
            changes = changed_components(manifest['inputs']['components'], state['inputs']['components'])
        if getattr(args, 'allow_all_components', False):
            packages.update(changes)
        if not same_core or changes - packages:
            raise RuntimeError('Cached PHP sources differ outside the requested compile packages')
        cache_exact = manifest.get('key') == args.key
        allowed_packages = set(packages)
        packages = set(changes) | runtime_rebuilds
        removed_files, proof = set(), None
        if incompatible:
            graph = manifest.get('library_dependencies')
            bindings = manifest.get('consumer_library_dependencies')
            if not isinstance(graph, dict) or set(graph) != set(manifest['library_abi']) or not isinstance(bindings, dict):
                raise RuntimeError('Cached PHP payload lacks verified consumer library closure for a partial ABI rebuild')
            actual_bindings = consumer_bindings(staged, manifest['build_payload'], manifest['library_abi'], graph)
            if actual_bindings != bindings:
                raise RuntimeError('Cached PHP consumer dependency closure changed')
            for name, dependencies in bindings.items():
                if incompatible.intersection(dependencies):
                    component = component_file(name, manifest['build_payload'])
                    if component is None or not getattr(args, 'allow_all_components', False) and component not in allowed_packages:
                        raise RuntimeError('Cached PHP consumer requires an ABI rebuild outside the selected packages: ' + name)
                    packages.add(component)
            removed_raw = {name for name in manifest['build_payload']['files']
                           if component_file(name, manifest['build_payload']) in packages}
            retained_roots = set()
            for name, dependencies in bindings.items():
                if name in removed_raw:
                    continue
                if incompatible.intersection(dependencies):
                    raise RuntimeError('Cached PHP consumer requires an ABI rebuild outside the selected packages: ' + name)
                retained_roots.update(dependencies)
            removed_files.update(removed_raw)
            for name in removed_raw:
                removed_files.update(entry['path'] for entry in manifest['build_payload'].get('prepared', {}).get(name, {}).values())
            proof = {'schema': 1, 'cached_metadata_path': 'provenance/cached-consumer/build-payload.json',
                     'incompatible_libraries': sorted(incompatible), 'rebuild_packages': sorted(packages),
                     'retained_library_abi': {name: {'sonames': sorted(set(manifest['library_abi'][name]['sonames']))}
                                              for name in sorted(retained_roots)},
                     'removed_files': sorted(removed_files), 'library_dependencies': graph,
                     'retained_dlopen_libraries': []}
        selection = set(filter(None, getattr(args, 'package_selection', '').split(',')))
        projected = None
        if selection and 'devel' not in selection and not packages and not incompatible:
            current = read(sdk / 'build-payload.json')
            combined = json.loads(json.dumps(manifest['build_payload']))
            for field in ('files', 'prepared'):
                combined[field] = {**{name: value for name, value in current.get(field, {}).items() if not php_file(name)},
                                   **{name: value for name, value in combined.get(field, {}).items() if php_file(name)}}
            projected, selected_sdk, excluded = packaging_projection(staged, combined, read(sdk / 'shared-libraries.json'), selection)
            removed_files.update(excluded)
            manifest['build_payload'] = projected
            def ignore(directory, names):
                return [name for name in names if str((Path(directory) / name).relative_to(sdk)) in excluded or name.endswith('.a')]
            shutil.copytree(sdk, args.payload, symlinks=True, ignore=ignore)
            write(args.payload / 'shared-libraries.json', selected_sdk)
            write(args.payload / 'build-payload.json', projected)
        else:
            shutil.copytree(sdk, args.payload, symlinks=True)
        for path in sorted(args.payload.rglob('*'), reverse=True):
            name = str(path.relative_to(args.payload))
            if (path.is_file() or path.is_symlink()) and (php_file(name) or name == 'skipped-shared-extensions.json'):
                path.unlink()
        for name in manifest['files']:
            if name in removed_files or projected is not None and name.endswith('.a'):
                continue
            target = args.payload / name
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.is_file() or target.is_symlink():
                target.unlink()
            (staged / name).rename(target)
        metadata = args.payload / 'provenance/cached-consumer/build-payload.json'
        write(metadata, manifest['build_payload'])
        graft = ['php', str(args.root / 'bin/build-payload'), 'graft', '--root', str(args.payload), '--metadata', str(metadata)]
        if proof is not None:
            proof['cached_metadata_sha256'] = hashlib.sha256(metadata.read_bytes()).hexdigest()
            proof_path = args.payload / 'provenance/cached-consumer/abi-reuse-proof.json'
            write(proof_path, proof)
            graft += ['--reuse-proof', str(proof_path)]
        subprocess.run(graft, check=True)
        if projected is not None:
            recorded = read(args.payload / 'build-payload.json')
            recorded['packaging_selection'] = projected['packaging_selection']
            write(args.payload / 'build-payload.json', recorded)
        receipt = {'schema': 1, 'state': 'verified', 'key': state['input_fingerprint'], 'seed_key': state['core_fingerprint'],
                   'cache_key': args.key, 'cache_seed_key': args.seed_key,
                   'php_stream': args.stream, 'rebuild_packages': sorted(packages), 'changed_components': sorted(changes),
                   'requested_rebuild_packages': sorted(requested_packages), 'cache_exact': cache_exact,
                   'abi_rebuild_required': bool(incompatible and packages), 'abi_rebuild_libraries': sorted(incompatible),
                   'runtime_rebuild_packages': sorted(runtime_rebuilds),
                   'coverage': coverage,
                   'cached_metadata_path': str(metadata.relative_to(args.payload)),
                   'cached_metadata_sha256': hashlib.sha256(metadata.read_bytes()).hexdigest(),
                   'compiled_source_run_id': manifest.get('compiled_source_runs', {}).get('core', manifest['source_run_id']),
                   'compiled_source_runs': manifest.get('compiled_source_runs', {'core': manifest['source_run_id'], 'components': {}}),
                   'payload_source_run_id': manifest['source_run_id'],
                   'core_files': {name: entry for name, entry in manifest['files'].items() if core_file(name, manifest['build_payload'])}}
        write(args.payload / 'consumer-cache-seed.json', receipt)
    print(json.dumps({'restored': True, 'compiled_source_run_id': manifest['source_run_id'], 'changed_components': sorted(changes),
                      'packaging_selection': sorted(selection) if projected is not None else []}))


def restore(args):
    existed = args.payload.exists()
    try:
        restore_payload(args)
    except BaseException:
        if not existed and args.payload.exists():
            shutil.rmtree(args.payload)
        raise


def validation_hosts(packages):
    hosts = {'cli'}
    if packages - {'cli', 'cgi', 'fpm', 'embed', 'frankenphp', 'devel', 'common', 'pie'}:
        hosts.add('frankenphp')
    if 'frankenphp' in packages | hosts:
        hosts.add('embed')
    return hosts - packages


def packaging_projection(payload, record, manifest, packages, library_selection=None):
    excluded = set()
    if library_selection is not None or packages and 'devel' not in packages:
        retained = set()
        graph = library_graph(manifest)
        if library_selection is not None:
            libraries = set(dependency_closure(library_selection, graph))
        else:
            consumers = packages | validation_hosts(packages)
            profile = profile_helper()
            shared = set(profile.names(record.get('configuration', {}).get('shared-extensions')))
            metadata = profile.extension_metadata(Path(__file__).resolve().parents[3], include_suggests=True)
            pending = list(consumers & shared)
            while pending:
                for dependency in metadata.get(pending.pop(), {}).get('depends', []):
                    if dependency in shared and dependency not in consumers:
                        consumers.add(dependency)
                        pending.append(dependency)
            retained = {'bin/php'}
            retained.update('bin/php-' + name for name in consumers & {'cgi', 'fpm'})
            retained.update(name for name in record['files'] if component_file(name, record) in consumers)
            if 'embed' in consumers:
                retained.update(name for name in record['files'] if name.startswith('lib/libphp'))
            consumers = dict(record, files={name: value for name, value in record['files'].items() if name in retained})
            bindings = consumer_bindings(payload, consumers, abi(manifest), graph)
            libraries = set(dependency_closure(({'openssl'} & graph.keys()) | {name for values in bindings.values() for name in values}, graph))
        for name, entry in manifest['libraries'].items():
            paths = set(entry['files']) | set(entry.get('runtime_assets', {}))
            if name in libraries:
                retained.update(paths)
            else:
                excluded.update(paths)
        for name, prepared in record.get('prepared', {}).items():
            if name not in retained:
                excluded.add(name)
                excluded.update(value['path'] for value in prepared.values())
        excluded.update(set(record['files']) - retained)
        for field in ('files', 'prepared'):
            record[field] = {name: value for name, value in record.get(field, {}).items() if name in retained}
        for field in ('library_files', 'library_abi', 'library_build_provenance'):
            record[field] = {name: value for name, value in record.get(field, {}).items() if name in libraries}
        record['packaging_selection'] = {'packages': sorted(packages), 'libraries': sorted(libraries),
                                        'producer_manifest_sha256': hashlib.sha256((payload / 'build-payload.json').read_bytes()).hexdigest()}
        manifest['libraries'] = {name: value for name, value in manifest['libraries'].items() if name in libraries}
    return record, manifest, excluded


def project_payload(payload, packages):
    record = read(payload / 'build-payload.json')
    if record.get('state') not in ('recorded', 'sealed'):
        raise RuntimeError('Packaging requires a recorded producer payload')
    if record.get('packaging_selection') or not packages or 'devel' in packages:
        return
    record, manifest, excluded = packaging_projection(payload, record, read(payload / 'shared-libraries.json'), packages)
    for name in excluded:
        (payload / safe(name)).unlink(missing_ok=True)
    for path in payload.rglob('*.a'):
        path.unlink()
    write(payload / 'build-payload.json', record)
    write(payload / 'shared-libraries.json', manifest)
    if record['state'] == 'sealed':
        subprocess.run(['php', 'bin/build-payload', 'verify', '--root', str(payload)], check=True)
    print(json.dumps({'packaging_packages': sorted(packages), 'library_count': len(manifest['libraries']),
                      'elf_count': len(record['files'])}), flush=True)


def package_archive(args):
    payload = args.payload.resolve()
    record = read(payload / 'build-payload.json')
    if record.get('state') != 'sealed':
        raise RuntimeError('Packaging artifacts require a sealed producer payload')
    manifest = read(payload / 'shared-libraries.json')
    packages = set(filter(None, args.packages.split(',')))
    record, manifest, excluded = packaging_projection(payload, record, manifest, packages)
    replacements = {'build-payload.json': record, 'shared-libraries.json': manifest} if record.get('packaging_selection') else {}
    prefix = str(payload.relative_to(args.root))
    with subprocess.Popen(['zstd', '-T0', '-f', '-o', str(args.output)], stdin=subprocess.PIPE) as compressor:
        with tarfile.open(fileobj=compressor.stdin, mode='w|') as archive:
            for path in sorted(payload.rglob('*')):
                name = str(path.relative_to(payload))
                if path.is_dir() and not path.is_symlink() or name in excluded or name.endswith('.a') and packages and 'devel' not in packages:
                    continue
                if name in replacements:
                    content = (json.dumps(replacements[name], sort_keys=True, indent=2) + '\n').encode()
                    info = archive.gettarinfo(str(path), arcname=prefix + '/' + name)
                    info.size = len(content)
                    archive.addfile(info, io.BytesIO(content))
                else:
                    archive.add(path, arcname=prefix + '/' + name, recursive=False)
            archive.add(args.root / 'stage/payload-provenance.json', arcname='stage/payload-provenance.json')
        compressor.stdin.close()
        if compressor.wait():
            raise RuntimeError('Cannot compress packaging payload')
    print(json.dumps({'packaging_packages': sorted(packages), 'library_count': len(manifest['libraries']),
                      'elf_count': len(record['files']), 'archive_bytes': args.output.stat().st_size}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    key = sub.add_parser('key')
    export_parser = sub.add_parser('export')
    restore_parser = sub.add_parser('restore')
    project_parser = sub.add_parser('project')
    project_parser.add_argument('--root', type=Path, default=Path('.'))
    project_parser.add_argument('--payload', type=Path, required=True)
    project_parser.add_argument('--packages', required=True)
    package_parser = sub.add_parser('package-archive')
    package_parser.add_argument('--root', type=Path, default=Path('.'))
    package_parser.add_argument('--payload', type=Path, required=True)
    package_parser.add_argument('--packages', default='')
    package_parser.add_argument('--output', type=Path, required=True)
    for command in (key, export_parser, restore_parser):
        command.add_argument('--root', type=Path, default=Path('.'))
        command.add_argument('--compatibility', type=Path, required=True)
    key.add_argument('--stream', required=True)
    key.add_argument('--manifest', type=Path, required=True)
    for command in (export_parser, restore_parser):
        command.add_argument('--payload', type=Path, required=True)
        command.add_argument('--key', required=True)
        command.add_argument('--seed-key', required=True)
    export_parser.add_argument('--source-run-id', required=True)
    export_parser.add_argument('--output', type=Path, required=True)
    restore_parser.add_argument('--stream', required=True)
    restore_parser.add_argument('--archive', type=Path, required=True)
    restore_parser.add_argument('--sdk', type=Path, required=True)
    restore_parser.add_argument('--compile-packages', default='')
    restore_parser.add_argument('--reuse-core-sources', action='store_true', help='Verify the recorded PHP source snapshot for a selected extension update')
    restore_parser.add_argument('--package-selection', default='', help='Restore only unchanged selected packages and their native test dependencies')
    restore_parser.add_argument('--abi-rebuild-libraries', default='', help='Permit changed SDK ABIs only when every bound PHP consumer is selected for rebuilding')
    restore_parser.add_argument('--allow-all-components', action='store_true', help='Derive affected shared targets from a verified unchanged core seed')
    args = parser.parse_args()
    args.root = args.root.resolve()
    if args.command == 'key':
        state = keys(args.root, args.stream, read(args.manifest), args.compatibility.read_text().strip(), runtime_inputs(args.root, refresh=True))
        for name in ('key', 'seed-key'):
            print(name + '=' + state[name])
    else:
        {'export': export, 'restore': restore, 'package-archive': package_archive,
         'project': lambda args: project_payload(args.payload, set(filter(None, args.packages.split(','))))}[args.command](args)


if __name__ == '__main__':
    main()
