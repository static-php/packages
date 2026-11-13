#!/usr/bin/env python3
"""Plan and package native SDKs; share the EL8 payload between RPM and DEB."""

import argparse
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
import hashlib
import json
import os
from pathlib import Path
import shutil
import runpy
import subprocess
import importlib.util
from types import SimpleNamespace


ROOT = Path.cwd()
STAGE = ROOT / 'stage'
FORMATS = ('rpm', 'deb')
STREAMS = ('8.2', '8.3', '8.4', '8.5', '8.6')


@lru_cache(maxsize=1)
def repository_state():
    spec = importlib.util.spec_from_file_location('repository_state', Path(__file__).with_name('repository-state.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run(arguments, **kwargs):
    subprocess.run([str(value) for value in arguments], check=True, **kwargs)


def read(path):
    return json.loads(Path(path).read_text())


def write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True, indent=2) + '\n')


def copy_tree(source, destination):
    source, destination = Path(source), Path(destination)
    if destination.exists():
        shutil.rmtree(destination)
    destination.mkdir(parents=True)
    run(['cp', '-a', '--reflink=auto', str(source) + '/.', destination])


def clean_sdk(sdk):
    record = read(sdk / 'build-payload.json')
    if record.get('kind') != 'sdk':
        raise RuntimeError('Expected a recorded library SDK')
    php_file = runpy.run_path(str(Path(__file__).with_name('payload-cache.py')))['php_file']
    retained = set(record['files']) | set(record.get('packaging_assets', {}))
    for path in sdk.rglob('*'):
        name = str(path.relative_to(sdk))
        if name not in retained and php_file(name) and (path.is_file() or path.is_symlink()):
            path.unlink()
    skipped = sdk / 'skipped-shared-extensions.json'
    if record.get('skipped_shared_extensions') is None:
        skipped.unlink(missing_ok=True)
    else:
        write(skipped, record['skipped_shared_extensions'])


def project_libraries(source, destination, libraries):
    graph = runpy.run_path(str(Path(__file__).with_name('payload-cache.py')))
    libraries = set(libraries)
    if (source / 'openssl-variant.json').is_file():
        libraries.add('openssl')
    record, manifest, excluded = graph['packaging_projection'](source, read(source / 'build-payload.json'),
        read(source / 'shared-libraries.json'), set(), library_selection=libraries)
    if destination.exists():
        shutil.rmtree(destination)
    def ignore(directory, names):
        return [name for name in names if str((Path(directory) / name).relative_to(source)) in excluded or name.endswith('.a')]
    shutil.copytree(source, destination, symlinks=True, ignore=ignore)
    write(destination / 'build-payload.json', record)
    write(destination / 'shared-libraries.json', manifest)
    return set(manifest['libraries'])


def architecture():
    return 'aarch64' if os.environ['MATRIX_ARCH'] == 'arm64' else 'x86_64'


def baseline(kind, directory, consumers=False, libraries=None):
    args = ['python3', 'bin/shared-library-plan', 'baseline', '--type', kind,
            '--arch', os.environ['MATRIX_ARCH'], '--output', directory]
    streams = sorted(set(filter(None, os.environ.get('PHP_VERSIONS', '').split(','))) or set(STREAMS))
    if consumers and kind == 'rpm':
        streams = list(STREAMS)
    if repository_state().restore_baseline(kind, os.environ['MATRIX_ARCH'], streams, Path(directory), consumers, libraries):
        return
    args += ['--php-versions', ','.join(streams)]
    if not consumers:
        args += ['--skip-consumers']
    if libraries is not None:
        args += ['--libraries', ','.join(sorted(libraries))]
    if kind == 'rpm':
        host = os.environ.get('RPM_BASE_URL', 'https://rpm.henderkes.com').rstrip('/')
        args += ['--base-url', host + '/' + architecture() + '/el8']
    else:
        args += ['--host', 'https://git.henderkes.com',
                 '--password-env', 'FORGEJO_PASSWORD', '--consumer-password-env', 'FORGEJO_PASSWORD']
        args += ['--owners', ','.join(stream.replace('.', '') for stream in streams)]
    run(args)



def publication_baseline(kind, directory, plan):
    scopes = [value.get('metadata_scope') for value in plan.get('baseline_states', [])]
    libraries = set().union(*(set(scope) for scope in scopes)) if scopes and all(scope is not None for scope in scopes) else None
    baseline(kind, directory, libraries=libraries)


def share_consumers(source, destination):
    captured = read(source / 'shared-libraries.json')
    path = destination / 'shared-libraries.json'
    value = read(path)
    for field in ['consumers', 'consumer_versions', 'consumer_dependencies', 'consumer_components',
                  'consumer_native_metadata', 'consumer_inventory_complete', 'consumer_inventory_streams',
                  'migration_cutoffs']:
        if field in captured:
            value[field] = captured[field]
    write(path, value)


def revision_flags():
    iteration = os.environ.get('ITERATION', '')
    if iteration and os.environ.get('BUMP') == 'true':
        raise RuntimeError('Choose bump=true or an explicit iteration; an explicit iteration cannot safely bump published packages.')
    return ['--iteration=' + iteration] if iteration else ['--bump'] if os.environ.get('BUMP') == 'true' else []


def variants(command, buildroot):
    args = ['python3', '.github/actions/shared-libraries/openssl-variants.py', command,
            '--buildroot', buildroot]
    if command == 'build':
        args += ['--root', '.', '--environment', STAGE / 'library-build-environment.json',
                 '--output', Path(buildroot) / 'openssl-variants']
    return args


def combine_plans(plans):
    requests = {}
    for plan in plans.values():
        for stream, request in plan.get('php_requests', {}).items():
            current = requests.setdefault(stream, {'full': False, 'packages': [], 'compile': False, 'compile_full': False, 'compile_packages': []})
            current['full'] |= bool(request.get('full'))
            current['packages'] = sorted(set(current['packages']) | set(request.get('packages', [])))
            current['compile'] |= bool(request.get('compile', True))
            current['compile_full'] |= bool(request.get('compile_full', request.get('full', False))) if request.get('compile', True) else False
            current['compile_packages'] = sorted(set(current['compile_packages']) | set(request.get('compile_packages', request.get('packages', []))))
    for request in requests.values():
        if request['full']:
            request['packages'] = []
        if request['compile_full']:
            request['compile_packages'] = []
        request['reuse_payload'] = not request['compile']
    return {'schema': 1, 'php_requests': requests,
            'abi_break': any(plan.get('abi_break') for plan in plans.values()),
            'libraries': sorted(set().union(*(set(plan.get('libraries', [])) for plan in plans.values()))),
            'abi_rebuild_roots': sorted(set().union(*(set(plan.get('abi_rebuild_roots', [])) for plan in plans.values()))),
            'abi_core_rebuild_streams': sorted(set().union(*(set(plan.get('abi_core_rebuild_streams', [])) for plan in plans.values()))),
            'format_selection': {kind: {name: plan.get(name, {} if name == 'php_requests' else []) for name in ['libraries', 'library_streams', 'php_requests']} for kind, plan in plans.items()},
            'format_plans': {kind: hashlib.sha256(json.dumps(plan, sort_keys=True).encode()).hexdigest() for kind, plan in plans.items()}}



def format_stage(kind):
    return STAGE if kind == 'apk' else STAGE / kind


def format_sdk(kind):
    return ROOT / 'buildroot' if kind == 'apk' or shared_sdk_packaging() else STAGE / ('sdk-' + kind)


def shared_sdk_packaging():
    selected = set(filter(None, os.environ.get('LIBRARIES', '').split(',')))
    if not selected and not os.environ.get('PACKAGES'):
        return False
    plan = read(STAGE / 'library-build-plan.json')
    if plan.get('full') or plan.get('rebuilt_libraries'):
        return False
    graph = runpy.run_path(str(Path(__file__).with_name('payload-cache.py')))
    closure = graph['dependency_closure'](selected, graph['library_graph'](read(ROOT / 'buildroot/shared-libraries.json')))
    return 'openssl' not in closure


def package_libraries(kind):
    directory = format_stage(kind)
    plan = read(directory / 'shared-library-plan.json')
    if (os.environ.get('PUBLISH') == 'false' and os.environ.get('PACKAGES')
            and not read(directory / 'shared-library-plan.json')['libraries']):
        (directory / 'libraries').mkdir(exist_ok=True)
        return
    selected_sdk = format_sdk(kind)
    shared = kind != 'apk' and selected_sdk == ROOT / 'buildroot'
    projected = None
    if (os.environ.get('PUBLISH') == 'false' and plan['libraries']
            and not any(request.get('full') or request.get('packages') for request in plan['php_requests'].values())
            and len(plan['libraries']) < len(read(selected_sdk / 'shared-libraries.json')['libraries'])):
        destination = STAGE / ('package-sdk-' + kind)
        projected = project_libraries(selected_sdk, destination, plan['libraries'])
        selected_sdk, shared = destination, False
    if shared:
        graph = runpy.run_path(str(Path(__file__).with_name('payload-cache.py')))
        selected = read(directory / 'shared-library-plan.json')['libraries']
        closure = graph['dependency_closure'](selected, graph['library_graph'](read(selected_sdk / 'shared-libraries.json')))
        if 'openssl' in closure:
            selected_sdk = STAGE / ('sdk-' + kind)
            copy_tree(ROOT / 'buildroot', selected_sdk)
            run(variants('apply', selected_sdk) + ['--variants', ROOT / 'buildroot/openssl-variants', '--type', kind])
            shared = False
    manifest = directory / 'shared-libraries.json' if shared else selected_sdk / 'shared-libraries.json'
    if not shared:
        classified = read(directory / 'shared-libraries.json')
        if projected is not None:
            classified['libraries'] = {name: entry for name, entry in classified['libraries'].items() if name in projected}
        write(manifest, classified)
        run(['php', 'bin/build-payload', 'seal', '--root', selected_sdk])
    if kind != 'rpm':
        streams = set(plan.get('library_streams', [])) | {stream for stream, request in plan['php_requests'].items()
                                                        if request.get('full') or request.get('packages')}
        failed = []
        for stream in sorted(streams):
            owner = directory / 'owner-libraries' / stream
            metadata = read(manifest)
            for entry in metadata['libraries'].values():
                entry.setdefault('package_versions', {}).pop(kind, None)
            owner_manifest = owner / 'shared-libraries.json'
            write(owner_manifest, metadata)
            try:
                package_library_archives(kind, directory, selected_sdk, owner_manifest, stream)
            except (RuntimeError, OSError, ValueError, subprocess.CalledProcessError) as error:
                shutil.rmtree(owner)
                failed.append(stream)
                print(f'::warning::{kind} PHP {stream} library packaging failed: {error}; continuing with other streams', flush=True)
        if streams and len(failed) == len(streams):
            raise RuntimeError('Library packaging failed for every selected ' + kind + ' owner')
    else:
        package_library_archives(kind, directory, selected_sdk, manifest)


def package_library_archives(kind, directory, sdk, manifest, stream=None):
    owner = directory / 'owner-libraries' / stream if stream else directory
    dist = owner if stream else ROOT / 'dist'
    packages = dist / kind
    plan_path = directory / 'shared-library-plan.json'
    manifest_option = ['--library-manifest=' + str(manifest)]
    environment = dict(os.environ, SPP_DIST_PATH=str(dist), SPP_SHARED_LIBRARY_PLAN=str(plan_path))
    baseline = directory / 'baseline'
    if stream:
        baseline = baseline / 'owners' / stream
        snapshot = format_stage(kind) / 'native-inventories' / (stream + '.json')
        environment.update(SPP_NATIVE_INVENTORY=str(snapshot), SPP_FORGEJO_OWNER=stream.replace('.', ''))
    environment['SPP_BASELINE_MANIFESTS'] = json.dumps([str(path.resolve()) for path in baseline.glob('shared-libraries.json')])
    retry_args = ['python3', 'bin/shared-library-plan', 'stage-publication-retries',
                  '--plan', plan_path, '--destination', packages,
                  '--manifest', manifest, '--manifest-output', manifest,
                  '--output', owner / 'publication-retries.json']
    for baseline_path in (directory / 'baseline').glob('shared-libraries.json'):
        retry_args += ['--baseline-manifest', baseline_path]
    if stream:
        retry_args += ['--php-stream', stream]
    run(retry_args)
    retry = read(owner / 'publication-retries.json')
    selected = retry['remaining_libraries']
    if selected or retry.get('layout_required'):
        selection = ['--libraries=' + ','.join(selected)] if selected else ['--packages=runtime_layout']
        run(['php', 'bin/spp', 'package', '--libs-only', '--type=' + kind, '--buildroot=' + str(sdk),
             *manifest_option, *selection, '--debuginfo', *(['--phpv=' + stream] if stream else []),
             *revision_flags()], env=environment)
    run(['php', 'bin/build-payload', 'manifest', '--root', sdk, '--type', kind,
         *manifest_option, '--output', owner / 'shared-libraries.json'], env=environment)
    if not stream:
        destination = directory / 'libraries'
        destination.mkdir(exist_ok=True)
        for archive in packages.glob('*.' + kind):
            shutil.copy2(archive, destination / archive.name)
        consumer_manifest = packages / 'shared-library-consumers.json'
        if consumer_manifest.exists():
            shutil.copy2(consumer_manifest, destination / consumer_manifest.name)


def rebuild_library_consumers(roots, rebuilt, kind):
    run(['python3', '.github/actions/shared-libraries/incremental-build.py', 'expand', '--root', '.',
         '--plan', STAGE / 'library-build-plan.json', '--libraries', ','.join(sorted(roots)),
         '--output', STAGE / 'library-reverse-plan.json'])
    if not read(STAGE / 'library-reverse-plan.json').get('selected'):
        return False
    run(['python3', '.github/actions/shared-libraries/incremental-build.py', 'run', '--root', '.',
         '--plan', STAGE / 'library-reverse-plan.json', '--type', kind])
    rebuilt.update(read(STAGE / 'library-reverse-plan.json').get('rebuilt_libraries', []))
    shutil.copy2(STAGE / 'library-reverse-plan.json', STAGE / 'library-build-plan.json')
    (STAGE / 'sdk-rebuilt').touch()
    return True


def pending_library_roots(sdk):
    return {provider for dependencies in read(sdk / 'shared-libraries.json').get('pending_rebuilds', {}).values()
            for provider in dependencies.values()}


def classify():
    formats = ('apk',) if os.environ.get('TYPE') == 'apk' else FORMATS
    glibc = formats == FORMATS
    sdk = ROOT / 'buildroot'
    STAGE.mkdir(exist_ok=True)
    if os.environ.get('LIBRARIES'):
        write(STAGE / 'effective-source-plan.json', {})
    else:
        run(['python3', '.github/actions/shared-libraries/source-plan.py', '--reconcile', '--root', '.',
             '--output', STAGE / 'effective-source-plan.json'])
    build_plan = read(STAGE / 'library-build-plan.json')
    rebuilt = set(read(sdk / 'shared-libraries.json')['libraries']) if build_plan.get('full') else set(build_plan.get('rebuilt_libraries', []))
    capture_baselines()
    consumers_ready = all(read(format_stage(kind) / 'baseline/shared-libraries.json').get('consumer_inventory_complete') for kind in formats)
    shared = glibc and shared_sdk_packaging()
    variants_ready = not glibc or shared
    for phase in range(len(read(sdk / 'shared-libraries.json')['libraries']) + 2):
        pending = {provider for dependencies in read(sdk / 'shared-libraries.json').get('pending_rebuilds', {}).values()
                   for provider in dependencies.values()}
        if pending and rebuild_library_consumers(pending, rebuilt, formats[0]):
            shared = False
            variants_ready = not glibc
            continue
        if not variants_ready:
            run(variants('build', sdk))
            variants_ready = True
        plans, reverse_roots = {}, set()
        for kind in formats:
            directory = format_stage(kind)
            metadata = read(sdk / 'shared-libraries.json')
            if glibc and not shared and 'openssl' in metadata['libraries']:
                metadata['libraries']['openssl'] = read(sdk / 'openssl-variants' / kind / 'overlay.json')['entry']
            producer_manifest = directory / 'producer-manifest.json'
            write(producer_manifest, metadata)
            args = ['python3', 'bin/shared-library-plan', 'prepare', '--type', kind,
                    '--manifest', producer_manifest,
                    '--baseline', directory / 'baseline', '--mode', os.environ.get('MODE', 'full'),
                    '--libraries', os.environ.get('LIBRARIES', ''), '--php-versions', os.environ.get('PHP_VERSIONS', ''),
                    '--packages', os.environ.get('PACKAGES', ''), '--output', directory / 'shared-library-plan.json',
                    '--manifest-output', directory / 'shared-libraries.json', '--rebuilt-libraries', ','.join(sorted(rebuilt))]
            args += revision_flags()
            if (STAGE / 'effective-source-plan.json').is_file():
                args += ['--request', STAGE / 'effective-source-plan.json']
            run(args)
            plan = read(directory / 'shared-library-plan.json')
            plans[kind] = plan
            reverse_roots.update(plan.get('abi_rebuild_roots', []))
            for dependencies in metadata.get('pending_rebuilds', {}).values():
                reverse_roots.update(dependencies.values())
        needs_consumers = any(plan.get('abi_break') for plan in plans.values())
        if needs_consumers and not consumers_ready:
            for kind in formats:
                consumer_baseline = format_stage(kind) / 'consumer-baseline'
                baseline(kind, consumer_baseline, consumers=True, libraries=[])
                share_consumers(consumer_baseline, format_stage(kind) / 'baseline')
            consumers_ready = True
            continue
        if reverse_roots and rebuild_library_consumers(reverse_roots, rebuilt, formats[0]):
            shared = False
            variants_ready = not glibc
            continue
        break
    else:
        raise RuntimeError('Library ABI planning did not converge')
    run(['python3', '.github/actions/shared-libraries/incremental-build.py', 'validate-final', '--root', '.'])
    if glibc and not shared and (rebuilt or read(sdk / 'build-payload.json').get('state') != 'sealed') and (os.environ.get('PUBLISH') != 'false' or not os.environ.get('LIBRARIES')
            or any(request.get('full') or request.get('packages') for plan in plans.values() for request in plan['php_requests'].values())):
        run(['php', 'bin/build-payload', 'seal', '--root', sdk])
    capture_native_inventories()
    plans = {kind: read(format_stage(kind) / 'shared-library-plan.json') for kind in formats}
    if not glibc:
        package_libraries('apk')
        return
    sdk_manifest = read(sdk / 'shared-libraries.json')
    for entry in sdk_manifest['libraries'].values():
        for field in list(entry):
            if field.startswith('abi_'):
                entry.pop(field)
    write(sdk / 'shared-libraries.json', sdk_manifest)
    common = STAGE / 'common'
    write(common / 'sdk-shared-libraries.json', sdk_manifest)
    write(common / 'shared-library-plan.json', combine_plans(plans))
    for kind in formats:
        for name in ['shared-library-plan.json', 'shared-libraries.json']:
            target = common / kind / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(STAGE / kind / name, target)
        source = STAGE / kind / 'baseline'
        if source.exists():
            copy_tree(source, common / kind / 'baseline')


def scope_abi_consumers(plan, snapshot, stream, requested=()):
    abi = plan.get('abi_consumer_requests', {}).get(stream, {})
    inventory = snapshot.get('inventory', snapshot.get('published_packages'))
    if (snapshot.get('error') or inventory is None or abi.get('full')
            or snapshot.get('type') != plan.get('type') or snapshot.get('php_stream') != stream):
        return False
    consumers, components = abi.get('consumers', []), abi.get('packages', [])
    if not consumers or len(consumers) != len(components):
        return False
    planner = runpy.run_path(str(ROOT / 'bin/shared-library-plan'))
    compare = lru_cache(maxsize=None)(planner['native_compare'])
    normalize = lambda arch: {'amd64': 'x86_64', 'arm64': 'aarch64'}.get(arch, arch)
    latest = {}
    def version(package):
        value = package.get('version', '')
        return str(package.get('epoch', '0')) + ':' + value if plan['type'] == 'rpm' and ':' not in value else value
    for package in inventory:
        arch = package.get('architecture', package.get('arch', snapshot['architecture']))
        if (arch not in ('all', 'noarch') and normalize(arch) != normalize(snapshot['architecture'])
                or package['name'].endswith(('-debuginfo', '-dbgsym'))):
            continue
        previous = latest.get(package['name'])
        if previous is None or version(package) != version(previous) and compare(plan['type'], version(package), version(previous)) > 0:
            latest[package['name']] = package
    libraries = set(plan.get('libraries', []))
    owners = {'php-zts-lib-' + library: library for library in libraries}
    dependencies = {name: sorted({owners[item] for item in package.get('requires', []) if item in owners})
                    for name, package in latest.items()
                    if not name.startswith('php-zts-lib-') and name != 'php-zts-runtime-layout'}
    previous = next((baseline['stream_libraries'][stream] for baseline in plan.get('baseline_states', [])
                     if stream in baseline.get('stream_libraries', {})), None)
    native = planner['abi_consumers']({'libraries': {name: {'elf': {}} for name in libraries},
        'stream_libraries': {stream: previous} if previous is not None else {},
        'consumers': {stream: list(dependencies)}, 'consumer_dependencies': {stream: dependencies},
        'consumer_inventory_complete': True}, set(plan.get('abi_rebuild_roots', libraries)) if previous is not None else libraries,
        {'libraries': plan.get('expected_libraries', {})} if previous is not None else None)[stream]
    if native['full']:
        raise RuntimeError('Cannot map native ABI consumers for PHP ' + stream)
    pairs = {name: component for name, component in zip(consumers, components) if name in latest}
    pairs.update(zip(native['consumers'], native['packages']))
    before = json.dumps([abi, plan['php_requests'][stream], plan.get('expected_consumers', {}).get(stream),
                         plan.get('published_consumers', {}).get(stream)], sort_keys=True)
    removed = set(components) - set(pairs.values()) - set(requested)
    request = plan['php_requests'][stream]
    for field in ('packages', 'compile_packages'):
        if field in request:
            request[field] = [name for name in request[field] if name not in removed]
    if not request.get('full'):
        request['packages'] = sorted(set(request.get('packages', [])) | set(pairs.values()))
        if not request.get('compile_full'):
            request['compile_packages'] = sorted(set(request.get('compile_packages', [])) | set(pairs.values()))
    abi['consumers'] = sorted(pairs)
    abi['packages'] = [pairs[name] for name in abi['consumers']]
    if stream in plan.get('expected_consumers', {}):
        plan['expected_consumers'][stream] = sorted((set(plan['expected_consumers'][stream]) - set(consumers)) | set(pairs))
    plan.setdefault('published_consumers', {})[stream] = {name: version(latest[name]) for name in pairs if latest[name].get('version')}
    changed = before != json.dumps([abi, request, plan.get('expected_consumers', {}).get(stream), plan['published_consumers'][stream]], sort_keys=True)
    if changed:
        print('PHP ' + stream + ' ABI consumers from native inventory: ' + ', '.join(abi['consumers']), flush=True)
    return changed


def capture_native_inventories():
    formats = ('apk',) if os.environ.get('TYPE') == 'apk' else FORMATS
    freezer = runpy.run_path(str(ROOT / 'bin/freeze-migration-baseline.py'))
    freezer['published_inventory'].__globals__['fetch'] = lru_cache(maxsize=None)(freezer['fetch'])
    wanted = set(os.environ.get('PHP_VERSIONS', '').split(',')) - {''} or set(STREAMS)
    source_plan = read(STAGE / 'effective-source-plan.json') if (STAGE / 'effective-source-plan.json').is_file() else {}
    plans, changed = {}, False
    for kind in formats:
        plan = read(format_stage(kind) / 'shared-library-plan.json')
        plans[kind] = plan
        output = (STAGE if kind == 'apk' else STAGE / 'common' / kind) / 'native-inventories'
        output.mkdir(parents=True, exist_ok=True)
        streams = [stream for stream, request in plan.get('php_requests', {}).items()
                   if (request.get('full') or request.get('packages'))
                   and (stream in wanted or plan.get('abi_break'))]
        streams = sorted(set(streams) | set(plan.get('library_streams', [])))
        if not streams and plan.get('libraries'):
            streams = ['8.5']
        for stream in streams:
            args = SimpleNamespace(type=kind, php=stream, arch=os.environ['MATRIX_ARCH'], el='el8', base_url=None)
            try:
                retained = output / (stream + '.json')
                snapshot = read(retained) if retained.is_file() else None
                if snapshot is None or snapshot.get('error'):
                    snapshot = freezer['snapshot_inventory'](args)
                print(f'Captured {kind} PHP {stream} published inventory', flush=True)
            except Exception as error:
                snapshot = dict(schema=1, type=kind, php_stream=stream, architecture=args.arch, error=str(error))
                print(f'::warning::{kind} PHP {stream} inventory unavailable: {error}', flush=True)
            write(output / (stream + '.json'), snapshot)
            requested = set(filter(None, os.environ.get('PACKAGES', '').split(',')))
            for field in ('requests', 'package_requests'):
                requested.update(source_plan.get(field, {}).get(stream, {}).get('packages', []))
            changed |= scope_abi_consumers(plan, snapshot, stream, requested)
    if changed:
        for kind, plan in plans.items():
            write(format_stage(kind) / 'shared-library-plan.json', plan)
            if kind != 'apk':
                write(STAGE / 'common' / kind / 'shared-library-plan.json', plan)
        if formats == FORMATS:
            write(STAGE / 'common/shared-library-plan.json', combine_plans(plans))


def record():
    target = {'arch': os.environ['MATRIX_ARCH'], 'alma': '8', 'target': os.environ['TARGET'],
              'image': os.environ['IMAGE'], 'source_run_id': os.environ['SOURCE_RUN_ID'],
              'package_images': json.loads(os.environ['PACKAGE_IMAGES'])}
    target['sdk_metadata_only'] = (STAGE / 'common/sdk-shared-libraries.json').is_file()
    if (STAGE / 'payload-provenance.json').is_file():
        target['payload_stream'] = read(STAGE / 'payload-provenance.json')['php_stream']
    for directory in [STAGE / 'common', *[STAGE / kind for kind in FORMATS]]:
        write(directory / 'target.json', target)
        (directory / 'cache-key').write_text(os.environ['CACHE_KEY'] + '\n')
    if (STAGE / 'library-compatibility').exists():
        shutil.copy2(STAGE / 'library-compatibility', STAGE / 'common/library-compatibility')
    for kind in FORMATS:
        directory = STAGE / kind
        run(['python3', '.github/actions/shared-libraries/workflow.py', 'receipt', '--kind', 'libraries',
             '--plan', directory / 'shared-library-plan.json', '--source-run-id', os.environ['SOURCE_RUN_ID'],
             '--image', os.environ['IMAGE'], '--output', directory / 'shared-library-build-receipt.json'])


def bind_library_sources(sdk, downloads):
    manifest = read(sdk / 'shared-libraries.json')
    state = read(sdk / 'shared-library-build-inputs.json')
    cache = read(downloads / '.cache.json')
    for name, entry in manifest['libraries'].items():
        recorded = state['libraries'].get(name, {})
        artifact = recorded.get('artifact')
        if artifact is None:
            if name != 'gcc-runtime':
                raise RuntimeError('SDK library has no recorded source artifact: ' + name)
            continue
        source = recorded.get('sources', {}).get(artifact, {}).get('source')
        if not isinstance(source, dict) or source.get('hash') != entry.get('source_hash'):
            raise RuntimeError('SDK library source identity differs from its compiled record: ' + name)
        cache[artifact] = dict(cache.get(artifact, {}), source=source)
    write(downloads / '.cache.json', cache)


def payload():
    stream = os.environ['PHPV']
    formats = ('apk',) if os.environ.get('TYPE') == 'apk' else FORMATS
    package_type = 'apk' if formats == ('apk',) else 'rpm'
    root = ROOT / 'build' / stream
    compile_requested = os.environ.get('COMPILE', 'true').lower() == 'true'
    compile_packages = os.environ.get('COMPILE_PACKAGES', os.environ.get('PACKAGES', ''))
    cached = Path('php-payload-cache.tar.zst')
    saved_sdk = STAGE / 'sdk-before-php'
    restored = False
    sdk_moved = False
    compiled_source = os.environ['SOURCE_RUN_ID']
    try:
        if cached.is_file() and not (compile_requested and not compile_packages and not os.environ.get('ABI_REBUILD_LIBRARIES')):
            if saved_sdk.exists():
                raise RuntimeError('Unfinished cached PHP restore owns stage/sdk-before-php')
            (ROOT / 'buildroot').rename(saved_sdk)
            sdk_moved = True
            args = ['python3', '.github/actions/shared-libraries/payload-cache.py', 'restore', '--root', '.',
                    '--stream', stream, '--sdk', saved_sdk, '--payload', ROOT / 'buildroot',
                    '--compatibility', STAGE / 'library-compatibility',
                    '--key', (STAGE / 'payload-cache-key').read_text().strip(),
                    '--seed-key', (STAGE / 'payload-cache-seed-key').read_text().strip(), '--archive', cached]
            if os.environ.get('PUBLISH') == 'false' and os.environ.get('PACKAGES'):
                args += ['--package-selection', os.environ['PACKAGES']]
            allow_all_components = compile_requested and not compile_packages
            if compile_requested and compile_packages:
                args += ['--compile-packages', compile_packages]
            elif allow_all_components:
                args += ['--allow-all-components']
            if compile_requested and os.environ.get('ABI_REBUILD_LIBRARIES'):
                args += ['--abi-rebuild-libraries', os.environ['ABI_REBUILD_LIBRARIES']]
            try:
                run(args)
            except subprocess.CalledProcessError as error:
                if error.returncode != 3 or not compile_requested:
                    raise
                saved_sdk.rename(ROOT / 'buildroot')
                sdk_moved = False
                compile_packages = ''
            else:
                seed = read(ROOT / 'buildroot/consumer-cache-seed.json')
                compile_packages = ','.join(seed['rebuild_packages'])
                compile_requested = bool(compile_packages)
                compiled_source = seed['compiled_source_run_id']
                restored = True
                if not seed['changed_components'] and not seed.get('runtime_rebuild_packages') and not seed.get('abi_rebuild_required') and (not compile_requested or seed.get('cache_exact') or not compile_packages):
                    if root.exists():
                        raise RuntimeError('Package-only restore would replace an existing PHP output directory')
                    root.parent.mkdir(parents=True, exist_ok=True)
                    (ROOT / 'buildroot').rename(root)
                    saved_sdk.rename(ROOT / 'buildroot')
                    sdk_moved = False
                    compile_requested = False
        elif not compile_requested:
            raise RuntimeError('Package-only change requires its sealed PHP cache; no compilation was requested')
        elif compile_packages:
            raise RuntimeError('Selected extension update requires its cached PHP core; refusing a full rebuild')
        else:
            compile_packages = ''
        if compile_requested:
            run(['python3', '.github/actions/shared-libraries/source-snapshot.py', 'materialize', '--php'])
            if compile_packages and not (ROOT / 'source/php-src').is_dir():
                release = read(ROOT / 'downloads/.spc-meta.json')['php_versions'][stream]
                run(['python3', '.github/actions/shared-libraries/php-source.py', '--root', '.', '--version', release, '--select'])
                run(['php', 'vendor/bin/spc', 'extract', 'php-src', '--source-only'])
            flags = ['--phpv=' + stream, '--prefix=-zts', '--type=' + package_type, '--debuginfo']
            if compile_packages:
                flags += ['--packages=' + compile_packages]
            environment = dict(os.environ, SPP_PACKAGING_SELECTION=os.environ.get('PACKAGES', '') if restored and os.environ.get('PUBLISH') == 'false' else '')
            bind_library_sources(ROOT / 'buildroot', ROOT / 'downloads')
            run(['php', 'bin/spp', 'build', *flags], env=environment)
            if not sdk_moved:
                clean_sdk(ROOT / 'buildroot')
            compiled_source = os.environ['SOURCE_RUN_ID']
        elif not restored:
            raise RuntimeError('No sealed PHP payload available for packaging')
        run(['php', 'bin/build-payload', 'refresh-assets', '--root', root, '--downloads', 'downloads'])
        write(STAGE / 'payload-provenance.json', {'schema': 1, 'target': os.environ['TARGET'],
              'php_stream': stream, 'source_run_id': os.environ['SOURCE_RUN_ID'],
              'compiled_source_run_id': compiled_source, 'reused_payload': restored,
              'producer_image': os.environ['IMAGE'], 'format_plan_sha256': {
                  kind: hashlib.sha256((STAGE / 'shared-library-plan.json' if kind == 'apk' else STAGE / kind / 'shared-library-plan.json').read_bytes()).hexdigest() for kind in formats}})
    finally:
        if sdk_moved and saved_sdk.exists():
            if (ROOT / 'buildroot').exists():
                shutil.rmtree(ROOT / 'buildroot')
            saved_sdk.rename(ROOT / 'buildroot')


def package():
    kind = os.environ['TYPE']
    if kind not in FORMATS:
        raise RuntimeError('Unsupported glibc package format')
    stream = os.environ['PHPV']
    root = Path(os.environ.get('PACKAGE_ROOT', ROOT / 'build' / stream))
    provenance = read(STAGE / 'payload-provenance.json')
    if read(root / 'build-payload.json').get('packaging_selection') and provenance.get('reused_payload') is False:
        raise RuntimeError('Retained artifact discarded the PHP cohort after a core rebuild; select a complete producer run')
    expected = hashlib.sha256((STAGE / kind / 'shared-library-plan.json').read_bytes()).hexdigest()
    if provenance.get('php_stream') != stream or provenance.get('source_run_id') != os.environ['SOURCE_RUN_ID'] or provenance.get('format_plan_sha256', {}).get(kind) != expected:
        raise RuntimeError('PHP payload provenance does not match the selected format plan')
    run(['php', 'bin/build-payload', 'verify', '--root', root])
    # This is a fresh format-local extraction of the sealed common producer artifact.
    run(variants('apply', root) + ['--variants', root / 'openssl-variants', '--type', kind])
    manifest = read(STAGE / kind / 'shared-libraries.json')
    selection = read(root / 'build-payload.json').get('packaging_selection')
    if selection:
        if set(os.environ.get('PACKAGES', '').split(',')) - set(selection['packages']):
            raise RuntimeError('Packaging selection exceeds the sealed artifact')
        manifest['libraries'] = {name: entry for name, entry in manifest['libraries'].items() if name in selection['libraries']}
    write(root / 'shared-libraries.json', manifest)
    run(['php', 'bin/build-payload', 'seal', '--root', root])
    flags = ['--phpv=' + stream, '--buildroot=' + str(root), '--prefix=-zts', '--type=' + kind, '--debuginfo', *revision_flags()]
    if os.environ.get('PACKAGES'):
        flags += ['--packages=' + os.environ['PACKAGES']]
    packages = STAGE / 'php-packages'
    environment = dict(os.environ, SPP_DIST_PATH=str(packages))
    environment['SPP_SHARED_LIBRARY_PLAN'] = str(STAGE / kind / 'shared-library-plan.json')
    if kind == 'deb':
        environment.update(SPP_NATIVE_INVENTORY=str(STAGE / kind / 'native-inventories' / (stream + '.json')),
                           SPP_FORGEJO_OWNER=stream.replace('.', ''))
    baseline = STAGE / kind / 'baseline'
    if kind == 'deb' and (baseline / 'owners' / stream).is_dir():
        baseline = baseline / 'owners' / stream
    environment['SPP_BASELINE_MANIFESTS'] = json.dumps([str(path.resolve()) for path in [
        baseline / 'shared-libraries.json', STAGE / 'published-baseline/baseline.json'] if path.is_file()])
    run(['php', 'bin/spp', 'package', *flags], env=environment)
    shutil.copytree(packages / kind, ROOT / 'dist' / kind, dirs_exist_ok=True)
    run(['python3', '.github/actions/shared-libraries/workflow.py', 'receipt', '--kind', 'consumer',
         '--php-stream', stream, '--plan', STAGE / kind / 'shared-library-plan.json',
         '--source-run-id', os.environ['SOURCE_RUN_ID'], '--image', provenance['producer_image'],
         '--output', ROOT / 'dist' / kind / 'shared-library-build-receipt.json'])


def format_libraries():
    kind = os.environ['TYPE']
    directory = STAGE / kind
    target = read(STAGE / 'target.json')
    plan = read(directory / 'shared-library-plan.json')
    if plan.get('libraries'):
        selected_sdk = STAGE / ('sdk-' + kind)
        (ROOT / 'buildroot').rename(selected_sdk)
        if 'openssl' in read(selected_sdk / 'shared-libraries.json')['libraries']:
            run(variants('apply', selected_sdk) + ['--variants', selected_sdk / 'openssl-variants', '--type', kind])
        package_libraries(kind)
    else:
        (directory / 'libraries').mkdir(exist_ok=True)
    write(directory / 'target.json', target)
    run(['python3', '.github/actions/shared-libraries/workflow.py', 'receipt', '--kind', 'libraries',
         '--plan', directory / 'shared-library-plan.json', '--source-run-id', target['source_run_id'],
         '--image', target['image'], '--output', directory / 'shared-library-build-receipt.json'])


def capture_baselines():
    formats = ('apk',) if os.environ.get('TYPE') == 'apk' else FORMATS
    plan = read(STAGE / 'library-build-plan.json')
    scope = None
    if os.environ.get('LIBRARIES') and not plan.get('full'):
        manifest = read(ROOT / 'buildroot/shared-libraries.json')
        roots = set(plan.get('requested', os.environ['LIBRARIES'].split(','))) | set(plan.get('selected', [])) | set(plan.get('rebuilt_libraries', []))
        payload = runpy.run_path(str(Path(__file__).with_name('payload-cache.py')))
        inventory = payload['library_graph'](manifest)
        for name, entry in plan.get('inventory', {}).get('libraries', {}).items():
            inventory[name] = sorted(set(inventory.get(name, [])) | set(entry.get('depends', [])))
        affected = set(roots)
        while True:
            dependents = {name for name, dependencies in inventory.items() if affected.intersection(dependencies)} - affected
            if not dependents:
                break
            affected.update(dependents)
        scope = payload['dependency_closure'](affected, inventory)
    elif os.environ.get('PACKAGES') and not plan.get('selected') and not plan.get('rebuilt_libraries') and not plan.get('full'):
        scope = []
    def capture(kind):
        destination = format_stage(kind) / 'baseline'
        if not (destination / 'shared-libraries.json').is_file():
            baseline(kind, destination, libraries=scope)
    with ThreadPoolExecutor(max_workers=len(formats)) as executor:
        list(executor.map(capture, formats))



def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=['baseline', 'classify', 'native-inventories', 'record', 'payload', 'package', 'libraries'])
    args = parser.parse_args()
    {'baseline': capture_baselines, 'classify': classify, 'native-inventories': capture_native_inventories, 'record': record,
     'payload': payload, 'package': package, 'libraries': format_libraries}[args.command]()


if __name__ == '__main__':
    main()
