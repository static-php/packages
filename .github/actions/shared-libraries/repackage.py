#!/usr/bin/env python3
"""Repackage complete retained EL8 payloads without invoking a compiler."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

import glibc
from publication import api, inspect_run, pinned_image
from workflow import STREAMS, csv, emit


def inspect(run_id, architectures, streams):
    repository = os.environ['GITHUB_REPOSITORY']
    run = api(f'repos/{repository}/actions/runs/{run_id}')
    artifacts = [artifact for page in api(f'repos/{repository}/actions/runs/{run_id}/artifacts?per_page=100', True)
                 for artifact in page['artifacts']]
    state = inspect_run(run, artifacts, repository, os.environ['DEFAULT_BRANCH'], 'rpm')
    if run.get('status') != 'completed':
        raise RuntimeError('Repackaging requires a completed producer run')
    available = state['artifacts']
    selected_arch = csv(architectures, ['x86_64', 'arm64'])
    selected_arch = sorted({'x86_64' if arch == 'amd64' else arch for arch in selected_arch})
    selected_php = csv(streams, STREAMS)
    if (not selected_arch or not selected_php
            or set(selected_arch) - {'x86_64', 'arm64'} or set(selected_php) - set(STREAMS)):
        raise RuntimeError('Unsupported repackaging architecture or PHP stream')
    includes = []
    with tempfile.TemporaryDirectory(prefix='spp-retained-plans-') as temporary:
        subprocess.run(['gh', 'run', 'download', str(run_id), '--repo', repository,
                        '--pattern', 'shared-plans-glibc-*', '--dir', temporary], check=True)
        for arch in selected_arch:
            target_name = 'alma8-' + arch
            directory = Path(temporary) / ('shared-plans-glibc-' + target_name)
            target = glibc.read(directory / 'target.json')
            plan = glibc.read(directory / 'shared-library-plan.json')
            if target.get('arch') != arch or target.get('target') != target_name:
                raise RuntimeError('Retained plan target differs from its artifact')
            pinned_image(target.get('image'), 'packages-builder-rhel-8')
            for kind, image in [('rpm', 'packages-builder-rhel-8'), ('deb', 'packages-packager-debian')]:
                pinned_image(target.get('package_images', {}).get(kind), image)
                selection = plan.get('format_selection', {}).get(kind, plan)
                if any(not selection.get('php_requests', {}).get(stream, {}).get('full') for stream in selected_php):
                    raise RuntimeError('Repackaging requires complete retained PHP payloads: ' + target_name)
            required = ['shared-sdk-glibc-' + target_name, 'shared-sdk-buildroot-glibc-' + target_name]
            required += ['shared-payload-glibc-' + target_name + '-php' + stream for stream in selected_php]
            if any(name not in available for name in required):
                raise RuntimeError('Required sealed SDK or PHP payload is missing or expired: ' + target_name)
            target.update(repackage_sdk_artifact=available[required[1]],
                          repackage_metadata_artifact=available[required[0]],
                          repackage_payload_artifacts={stream: available['shared-payload-glibc-' + target_name + '-php' + stream]
                                                       for stream in selected_php})
            includes.append(target)
    sources = {str(target['source_run_id']) for target in includes}
    if len(sources) != 1 or not next(iter(sources)).isdigit():
        raise RuntimeError('Retained targets must use one recorded source snapshot')
    return {'matrix': {'include': includes}, 'run-id': sources.pop(), 'php-versions': ','.join(selected_php)}


def packaging_requests(plan):
    for request in plan['php_requests'].values():
        request.update(compile=False, compile_full=False, compile_packages=[],
                       reuse_payload=bool(request.get('full') or request.get('packages')))
    return plan


def libraries():
    root, stage = glibc.ROOT, glibc.STAGE
    sdk = root / 'buildroot'
    glibc.run(['php', 'bin/build-payload', 'verify', '--root', sdk])
    record = glibc.read(sdk / 'build-payload.json')
    if record.get('kind') != 'sdk' or record.get('state') != 'sealed' or record.get('packaging_selection'):
        raise RuntimeError('Repackaging requires a complete sealed library SDK')
    common = stage / 'common'
    retained = common / 'repackage'
    glibc.write(retained / 'shared-libraries.json', glibc.read(sdk / 'shared-libraries.json'))
    plans = {}
    for kind in glibc.FORMATS:
        directory = stage / kind
        directory.mkdir(exist_ok=True)
        shutil.copy2(common / kind / 'shared-library-plan.json', retained / (kind + '.json'))
        selected_sdk = stage / ('sdk-' + kind)
        glibc.copy_tree(sdk, selected_sdk)
        glibc.run(glibc.variants('apply', selected_sdk) + ['--variants', sdk / 'openssl-variants', '--type', kind])
        glibc.baseline(kind, directory / 'baseline', consumers=True)
        glibc.run(['python3', 'bin/shared-library-plan', 'prepare', '--type', kind,
                   '--manifest', selected_sdk / 'shared-libraries.json', '--baseline', directory / 'baseline',
                   '--mode', 'full', '--rebuilt-libraries', '', '--php-versions', os.environ.get('PHP_VERSIONS', ''),
                   '--output', directory / 'shared-library-plan.json',
                   '--manifest-output', directory / 'shared-libraries.json', *glibc.revision_flags()])
        plan = packaging_requests(glibc.read(directory / 'shared-library-plan.json'))
        glibc.write(directory / 'shared-library-plan.json', plan)
    glibc.capture_native_inventories()
    for kind in glibc.FORMATS:
        glibc.package_libraries(kind)
        directory = stage / kind
        plans[kind] = glibc.read(directory / 'shared-library-plan.json')
        for name in ('shared-library-plan.json', 'shared-libraries.json'):
            shutil.copy2(directory / name, common / kind / name)
        glibc.copy_tree(directory / 'baseline', common / kind / 'baseline')
    glibc.write(common / 'shared-library-plan.json', glibc.combine_plans(plans))
    glibc.record()
    for directory in (common, *(stage / kind for kind in glibc.FORMATS)):
        target = glibc.read(directory / 'target.json')
        target.update(repackage_run_id=os.environ['REPACKAGE_RUN_ID'],
                      repackage_payload_artifacts=json.loads(os.environ['REPACKAGE_PAYLOAD_ARTIFACTS']))
        glibc.write(directory / 'target.json', target)


def payload():
    root, stage = glibc.ROOT, glibc.STAGE
    stream = os.environ['PHPV']
    directory = root / 'build' / stream
    glibc.run(['php', 'bin/build-payload', 'verify', '--root', directory])
    record = glibc.read(directory / 'build-payload.json')
    if (record.get('kind') != 'consumer' or record.get('state') != 'sealed'
            or record.get('packaging_selection') or record.get('libc') != 'glibc'
            or record.get('architecture') != glibc.architecture()
            or not record.get('php_version', '').startswith(stream + '.')):
        raise RuntimeError('Repackaging requires a complete sealed PHP payload for this target')
    target = glibc.read(stage / 'target.json')
    provenance = glibc.read(stage / 'payload-provenance.json')
    if any(str(provenance.get(key)) != str(value) for key, value in {
            'target': os.environ['TARGET'], 'php_stream': stream, 'source_run_id': os.environ['SOURCE_RUN_ID'],
            'producer_image': os.environ['IMAGE']}.items()):
        raise RuntimeError('Retained payload provenance differs from its producer target')
    expected = glibc.read(stage / 'repackage/shared-libraries.json')['libraries']
    actual = glibc.read(directory / 'shared-libraries.json')['libraries']
    fields = ('version', 'source_hash', 'build_identity', 'elf', 'files', 'runtime_assets')
    if (set(actual) != set(expected)
            or any(actual[name].get(key) != entry.get(key) for name, entry in expected.items() for key in fields)):
        raise RuntimeError('Retained PHP payload does not match the retained library SDK')
    for kind in glibc.FORMATS:
        original = hashlib.sha256((stage / 'repackage' / (kind + '.json')).read_bytes()).hexdigest()
        if provenance.get('format_plan_sha256', {}).get(kind) != original:
            raise RuntimeError('Retained payload was not built against its recorded format plan')
    provenance.update(repackaged_from_run_id=target['repackage_run_id'], reused_payload=True,
                      format_plan_sha256={kind: hashlib.sha256((stage / kind / 'shared-library-plan.json').read_bytes()).hexdigest()
                                          for kind in glibc.FORMATS})
    glibc.write(stage / 'payload-provenance.json', provenance)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('inspect', 'libraries', 'payload'))
    parser.add_argument('--run-id', type=int)
    parser.add_argument('--architectures', default='')
    parser.add_argument('--php-versions', default='')
    args = parser.parse_args()
    if args.command == 'inspect':
        if not args.run_id or args.run_id <= 0:
            parser.error('A positive retained producer run ID is required')
        for key, value in inspect(args.run_id, args.architectures, args.php_versions).items():
            emit(key, value)
    else:
        {'libraries': libraries, 'payload': payload}[args.command]()


if __name__ == '__main__':
    main()
