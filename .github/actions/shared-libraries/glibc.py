#!/usr/bin/env python3
"""Plan and package native SDKs; share the EL8 payload between RPM and DEB."""

import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess


ROOT = Path.cwd()
STAGE = ROOT / 'stage'
FORMATS = ('rpm', 'deb')
STREAMS = ('8.2', '8.3', '8.4', '8.5', '8.6')


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


def architecture():
    return 'aarch64' if os.environ['MATRIX_ARCH'] == 'arm64' else 'x86_64'


def baseline(kind, directory, consumers=False, abi_libraries=()):
    args = ['python3', 'bin/shared-library-plan', 'baseline', '--type', kind,
            '--arch', os.environ['MATRIX_ARCH'], '--output', directory]
    args += ['--consumer-stream', '8.5'] if consumers else ['--skip-consumers']
    args += ['--abi-libraries', ','.join(sorted(abi_libraries))]
    if kind == 'rpm':
        host = os.environ.get('RPM_BASE_URL', 'https://rpm.henderkes.com').rstrip('/')
        args += ['--base-url', host + '/' + architecture() + '/el8']
        for version in ('9', '10'):
            args += ['--baseline-url', host + '/' + architecture() + '/el' + version]
    else:
        args += ['--host', 'https://git.henderkes.com',
                 '--password-env', 'FORGEJO_PASSWORD', '--consumer-password-env', 'FORGEJO_PASSWORD']
    run(args)


def share_consumers(source, destination):
    canonical = read(source / 'shared-libraries.json')
    path = destination / 'shared-libraries.json'
    value = read(path)
    for field in ['consumers', 'consumer_dependencies', 'consumer_components',
                  'consumer_inventory_complete', 'consumer_inventory_stream']:
        if field in canonical:
            value[field] = canonical[field]
    write(path, value)


def revision_flags():
    iteration = os.environ.get('ITERATION', '')
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
            'bootstrap': any(plan.get('bootstrap') for plan in plans.values()),
            'libraries': sorted(set().union(*(set(plan.get('libraries', [])) for plan in plans.values()))),
            'abi_rebuild_roots': sorted(set().union(*(set(plan.get('abi_rebuild_roots', [])) for plan in plans.values()))),
            'abi_core_rebuild_streams': sorted(set().union(*(set(plan.get('abi_core_rebuild_streams', [])) for plan in plans.values()))),
            'format_selection': {kind: {name: plan.get(name, {} if name == 'php_requests' else []) for name in ['libraries', 'php_requests']} for kind, plan in plans.items()},
            'format_plans': {kind: hashlib.sha256(json.dumps(plan, sort_keys=True).encode()).hexdigest() for kind, plan in plans.items()}}



def merge_library_abi(common, formats):
    for name, entry in common['libraries'].items():
        kind, current = max(((kind, manifest['libraries'][name]) for kind, manifest in formats.items()),
                            key=lambda item: item[1].get('abi_epoch', 1))
        entry['abi_epoch'] = current.get('abi_epoch', 1)
        capability = 'php-zts-lib-' + name + '-abi-' + str(entry['abi_epoch'])
        entry['abi_capability'] = capability
        entry['abi_capabilities'] = [capability]
        for field in ['abi_dump', 'abi_dump_hashes', 'abi_empty', 'abi_comparison']:
            if field in current:
                entry[field] = current[field]
        yield kind, entry


def seed_abi_cache(root, kind):
    target = Path(root) / 'usr/share/php-zts/libraries/abi'
    for cache in [STAGE / 'abi-cache/common', STAGE / 'abi-cache' / kind]:
        if cache.exists():
            target.mkdir(parents=True, exist_ok=True)
            run(['cp', '-a', str(cache) + '/.', target])


def save_abi_cache(root, kind):
    source = Path(root) / 'usr/share/php-zts/libraries/abi'
    if source.exists():
        for directory in source.iterdir():
            if directory.is_dir():
                copy_tree(directory, STAGE / 'abi-cache' / (kind if directory.name == 'openssl' else 'common') / directory.name)


def format_stage(kind):
    return STAGE if kind == 'apk' else STAGE / kind


def format_sdk(kind):
    return ROOT / 'buildroot' if kind == 'apk' else STAGE / ('sdk-' + kind)


def package_libraries(kind):
    directory = format_stage(kind)
    selected_sdk = format_sdk(kind)
    shutil.copy2(directory / 'shared-libraries.json', selected_sdk / 'shared-libraries.json')
    run(['php', 'bin/build-payload', 'seal', '--root', selected_sdk])
    abi = selected_sdk / 'usr/share/php-zts/libraries/abi'
    if abi.exists():
        copy_tree(abi, directory / 'abi')
    retry_args = ['python3', 'bin/shared-library-plan', 'stage-publication-retries',
                  '--plan', directory / 'shared-library-plan.json', '--destination', ROOT / 'dist' / kind,
                  '--manifest', selected_sdk / 'shared-libraries.json', '--manifest-output', selected_sdk / 'shared-libraries.json',
                  '--output', directory / 'publication-retries.json']
    for baseline_path in directory.glob('baseline/shared-libraries.json'):
        retry_args += ['--baseline-manifest', baseline_path]
    run(retry_args)
    selected = read(directory / 'publication-retries.json')['remaining_libraries']
    if selected or read(directory / 'publication-retries.json').get('layout_required'):
        environment = dict(os.environ, SPP_BASELINE_MANIFESTS=json.dumps([str(path.resolve()) for path in directory.glob('baseline/shared-libraries.json')]), SPP_SHARED_LIBRARY_PLAN=str(directory / 'shared-library-plan.json'))
        selection = ['--libraries=' + ','.join(selected)] if selected else ['--packages=runtime_layout']
        run(['php', 'bin/spp', 'package', '--libs-only', '--type=' + kind, '--buildroot=' + str(selected_sdk),
             *selection, '--debuginfo', *revision_flags()], env=environment)
    if list((ROOT / 'dist' / kind).glob('*.' + kind)):
        run(['python3', '.github/actions/shared-libraries/payload-archives.py', '--root', selected_sdk,
             '--packages', ROOT / 'dist' / kind, '--type', kind, '--output', directory / 'payload-archive-receipt.json'])
    run(['php', 'bin/build-payload', 'manifest', '--root', selected_sdk, '--type', kind,
         '--output', directory / 'shared-libraries.json'])
    packages = directory / 'libraries'
    packages.mkdir(exist_ok=True)
    for archive in (ROOT / 'dist' / kind).glob('*.' + kind):
        shutil.copy2(archive, packages / archive.name)
    consumer_manifest = ROOT / 'dist' / kind / 'shared-library-consumers.json'
    if consumer_manifest.exists():
        shutil.copy2(consumer_manifest, packages / consumer_manifest.name)


def classify():
    formats = ('apk',) if os.environ.get('TYPE') == 'apk' else FORMATS
    glibc = formats == FORMATS
    sdk = ROOT / 'buildroot'
    STAGE.mkdir(exist_ok=True)
    run(['python3', '.github/actions/shared-libraries/source-plan.py', '--reconcile', '--root', '.',
         '--output', STAGE / 'effective-source-plan.json'])
    build_plan = read(STAGE / 'library-build-plan.json')
    rebuilt = set(read(sdk / 'shared-libraries.json')['libraries']) if build_plan.get('full') else set(build_plan.get('rebuilt_libraries', []))
    for kind in formats:
        run(['php', 'tests/package-versions.php', '--type=' + kind])
        baseline(kind, format_stage(kind) / 'baseline', consumers=bool(rebuilt) and kind == formats[-1], abi_libraries=rebuilt)
    if rebuilt and glibc:
        share_consumers(STAGE / 'deb/baseline', STAGE / 'rpm/baseline')
    variants_ready = not glibc
    if glibc and not rebuilt and (sdk / 'openssl-variants/variants.json').is_file():
        run(variants('build', sdk))
        variants_ready = True
    for phase in range(100):
        plans, reverse_roots = {}, set()
        for kind in formats:
            directory = format_stage(kind)
            selected_sdk = format_sdk(kind)
            if selected_sdk != sdk:
                copy_tree(sdk, selected_sdk)
            if glibc and variants_ready:
                run(variants('apply', selected_sdk) + ['--variants', sdk / 'openssl-variants', '--type', kind])
            if glibc:
                seed_abi_cache(selected_sdk, kind)
            args = ['python3', 'bin/shared-library-plan', 'prepare', '--type', kind,
                    '--manifest', selected_sdk / 'shared-libraries.json', '--root', selected_sdk,
                    '--baseline', directory / 'baseline', '--mode', os.environ.get('MODE', 'full'),
                    '--libraries', os.environ.get('LIBRARIES', ''), '--php-versions', os.environ.get('PHP_VERSIONS', ''),
                    '--packages', os.environ.get('PACKAGES', ''), '--output', directory / 'shared-library-plan.json',
                    '--manifest-output', directory / 'shared-libraries.json', '--rebuilt-libraries', ','.join(sorted(rebuilt))]
            if (STAGE / 'effective-source-plan.json').is_file():
                args += ['--request', STAGE / 'effective-source-plan.json']
            old = directory / 'baseline/shared-libraries.json'
            request = read(STAGE / 'effective-source-plan.json')
            if not os.environ.get('LIBRARIES') and (os.environ.get('MODE', 'full') == 'full' or
                    (not old.exists() or not read(old).get('libraries')) and
                    all(request.get('requests', {}).get(stream, {}).get('full') for stream in STREAMS)):
                args += ['--bootstrap']
            run(args)
            if glibc:
                save_abi_cache(selected_sdk, kind)
            plan = read(directory / 'shared-library-plan.json')
            plans[kind] = plan
            reverse_roots.update(plan.get('abi_rebuild_roots', []))
            for dependencies in read(selected_sdk / 'shared-libraries.json').get('pending_rebuilds', {}).values():
                reverse_roots.update(dependencies.values())
        if not reverse_roots:
            if variants_ready:
                break
            run(['python3', '.github/actions/shared-libraries/incremental-build.py', 'validate-final', '--root', '.'])
            run(variants('build', sdk))
            run(['php', 'bin/build-payload', 'seal', '--root', sdk])
            variants_ready = True
            continue
        variants_ready = not glibc
        run(['python3', '.github/actions/shared-libraries/incremental-build.py', 'expand', '--root', '.',
             '--plan', STAGE / 'library-build-plan.json', '--libraries', ','.join(sorted(reverse_roots)),
             '--output', STAGE / 'library-reverse-plan.json'])
        if not read(STAGE / 'library-reverse-plan.json').get('selected'):
            raise RuntimeError('ABI transition requires libraries absent from the incremental build plan')
        run(['python3', '.github/actions/shared-libraries/incremental-build.py', 'run', '--root', '.',
             '--plan', STAGE / 'library-reverse-plan.json', '--type', formats[0]])
        rebuilt.update(read(STAGE / 'library-reverse-plan.json').get('rebuilt_libraries', []))
        shutil.copy2(STAGE / 'library-reverse-plan.json', STAGE / 'library-build-plan.json')
        (STAGE / 'sdk-rebuilt').touch()
    else:
        raise RuntimeError('Library ABI planning did not converge')
    run(['python3', '.github/actions/shared-libraries/incremental-build.py', 'validate-final', '--root', '.'])
    with ThreadPoolExecutor(max_workers=len(formats)) as executor:
        list(executor.map(package_libraries, formats))
    if not glibc:
        return
    sdk_manifest = read(sdk / 'shared-libraries.json')
    format_manifests = {kind: read(STAGE / kind / 'shared-libraries.json') for kind in FORMATS}
    for kind, entry in merge_library_abi(sdk_manifest, format_manifests):
        for relative in entry.get('abi_dump', {}).values():
            source = STAGE / ('sdk-' + kind) / relative
            destination = sdk / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
            proof = source.with_suffix(source.suffix + '.inputs.json')
            if proof.exists():
                shutil.copy2(proof, destination.with_suffix(destination.suffix + '.inputs.json'))
    write(sdk / 'shared-libraries.json', sdk_manifest)
    common = STAGE / 'common'
    write(common / 'shared-library-plan.json', combine_plans(plans))
    for kind in formats:
        for name in ['shared-library-plan.json', 'shared-libraries.json']:
            target = common / kind / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(STAGE / kind / name, target)
        if (STAGE / kind / 'abi').exists():
            copy_tree(STAGE / kind / 'abi', common / kind / 'abi')
        source = STAGE / kind / 'baseline'
        if source.exists():
            copy_tree(source, common / kind / 'baseline')


def record():
    target = {'arch': os.environ['MATRIX_ARCH'], 'alma': '8', 'target': os.environ['TARGET'],
              'image': os.environ['IMAGE'], 'source_run_id': os.environ['SOURCE_RUN_ID'],
              'package_images': json.loads(os.environ['PACKAGE_IMAGES'])}
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


def preflight_consumers(stream, package_type, packages):
    candidates_path = STAGE / 'consumer-source-versions.json'
    source_arguments = ['python3', '.github/actions/shared-libraries/source-versions.py', '--root', '.',
                        '--stream', stream, '--output', candidates_path]
    if package_type == 'apk':
        source_arguments += ['--type=apk']
    run(source_arguments)
    if packages:
        names = {'frankenphp' if name == 'frankenphp' else 'pie-zts' if name == 'pie' else 'php-zts-' + name
                 for name in packages.split(',')}
        candidates = read(candidates_path)
        candidates['candidates'] = [candidate for candidate in candidates['candidates'] if candidate['name'] in names]
        if not candidates['candidates']:
            raise RuntimeError('No immutable source version evidence for the requested PHP build')
        write(candidates_path, candidates)
    for kind in ('apk',) if package_type == 'apk' else FORMATS:
        directory = STAGE if kind == 'apk' else STAGE / kind
        arguments = ['php', 'bin/preflight-consumer-versions', '--type=' + kind,
                     '--candidates=' + str(candidates_path), '--architecture=' + architecture(),
                     '--output=' + str(STAGE / ('consumer-version-preflight-' + kind + '.json')), *revision_flags()]
        baselines = list(directory.glob('baseline/shared-libraries.json'))
        if not baselines:
            raise RuntimeError('Cold PHP build requires captured native package version baselines')
        for baseline_path in baselines:
            arguments += ['--baseline-manifest=' + str(baseline_path)]
        environment = dict(os.environ, SPP_SHARED_LIBRARY_PLAN=str(directory / 'shared-library-plan.json'))
        run(arguments, env=environment)


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
        if cached.is_file():
            if saved_sdk.exists():
                raise RuntimeError('Unfinished cached PHP restore owns stage/sdk-before-php')
            (ROOT / 'buildroot').rename(saved_sdk)
            sdk_moved = True
            args = ['python3', '.github/actions/shared-libraries/payload-cache.py', 'restore', '--root', '.',
                    '--stream', stream, '--sdk', saved_sdk, '--payload', ROOT / 'buildroot',
                    '--compatibility', STAGE / 'library-compatibility',
                    '--key', (STAGE / 'payload-cache-key').read_text().strip(),
                    '--seed-key', (STAGE / 'payload-cache-seed-key').read_text().strip(), '--archive', cached]
            allow_all_components = compile_requested and not compile_packages
            if compile_requested and compile_packages:
                args += ['--compile-packages', compile_packages]
            elif allow_all_components:
                args += ['--allow-all-components']
            if compile_requested and os.environ.get('ABI_REBUILD_LIBRARIES'):
                args += ['--abi-rebuild-libraries', os.environ['ABI_REBUILD_LIBRARIES']]
            run(args)
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
        else:
            compile_packages = ''
        if compile_requested:
            preflight_consumers(stream, package_type, compile_packages)
            flags = ['--phpv=' + stream, '--prefix=-zts', '--type=' + package_type, '--debuginfo']
            if compile_packages:
                flags += ['--packages=' + compile_packages]
            run(['php', 'bin/spp', 'build', *flags])
            compiled_source = os.environ['SOURCE_RUN_ID']
        elif not restored:
            raise RuntimeError('No sealed PHP payload available for packaging')
        run(['php', 'bin/build-payload', 'refresh-assets', '--root', root, '--downloads', 'downloads'])
        run(['php', 'bin/build-payload', 'seal', '--root', root])
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
    root = ROOT / 'build' / stream
    provenance = read(STAGE / 'payload-provenance.json')
    expected = hashlib.sha256((STAGE / kind / 'shared-library-plan.json').read_bytes()).hexdigest()
    if provenance.get('php_stream') != stream or provenance.get('source_run_id') != os.environ['SOURCE_RUN_ID'] or provenance.get('format_plan_sha256', {}).get(kind) != expected:
        raise RuntimeError('PHP payload provenance does not match the selected format plan')
    run(['php', 'bin/build-payload', 'verify', '--root', root])
    # This is a fresh format-local extraction of the sealed common producer artifact.
    run(variants('apply', root) + ['--variants', root / 'openssl-variants', '--type', kind])
    shutil.copy2(STAGE / kind / 'shared-libraries.json', root / 'shared-libraries.json')
    if (STAGE / kind / 'abi').exists():
        copy_tree(STAGE / kind / 'abi', root / 'usr/share/php-zts/libraries/abi')
    run(['php', 'bin/build-payload', 'seal', '--root', root])
    flags = ['--phpv=' + stream, '--prefix=-zts', '--type=' + kind, '--debuginfo', *revision_flags()]
    if os.environ.get('PACKAGES'):
        flags += ['--packages=' + os.environ['PACKAGES']]
    environment = dict(os.environ)
    environment['SPP_SHARED_LIBRARY_PLAN'] = str(STAGE / kind / 'shared-library-plan.json')
    environment['SPP_BASELINE_MANIFESTS'] = json.dumps([str(path.resolve()) for path in [
        STAGE / kind / 'baseline/shared-libraries.json', STAGE / 'published-baseline/baseline.json'] if path.is_file()])
    run(['php', 'tests/package-versions.php', '--type=' + kind])
    run(['php', 'tests/package-iterations.php'])
    run(['python3', 'tests/upgrade-guards.py'])
    if kind == 'deb':
        run(['python3', 'tests/forgejo-publication.py'])
    run(['php', 'bin/spp', 'package', *flags], env=environment)
    run(['php', 'bin/build-payload', 'verify', '--root', root])
    run(['python3', '.github/actions/shared-libraries/payload-archives.py', '--root', root,
         '--packages', ROOT / 'dist' / kind, '--type', kind, '--output', ROOT / 'dist' / kind / 'payload-archive-receipt.json'])
    run(['python3', '.github/actions/shared-libraries/workflow.py', 'receipt', '--kind', 'consumer',
         '--php-stream', stream, '--plan', STAGE / kind / 'shared-library-plan.json',
         '--source-run-id', os.environ['SOURCE_RUN_ID'], '--image', provenance['producer_image'],
         '--output', ROOT / 'dist' / kind / 'shared-library-build-receipt.json'])


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=['classify', 'record', 'payload', 'package'])
    args = parser.parse_args()
    {'classify': classify, 'record': record, 'payload': payload, 'package': package}[args.command]()


if __name__ == '__main__':
    main()
