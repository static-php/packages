#!/usr/bin/env python3
"""Select complete package cohorts from an existing build run."""

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import threading

from workflow import STREAMS, assemble, emit, image_digest, prune_consumers, verify_upgrades


WORKFLOWS = {
    'glibc': {'.github/workflows/build-rpm-modular-packages.yml', '.github/workflows/build-deb-forgejo.yml'},
    'apk': {'.github/workflows/build-apk-forgejo.yml'},
}


def inspect_run(run, artifacts, repository, branch, kind):
    family = 'apk' if kind == 'apk' else 'glibc'
    if run.get('repository', {}).get('full_name') != repository or run.get('head_repository', {}).get('full_name') != repository:
        raise RuntimeError('The build must belong to this repository')
    if run.get('path') not in WORKFLOWS[family] or run.get('event') not in ('workflow_dispatch', 'repository_dispatch', 'schedule'):
        raise RuntimeError('The selected run is not a supported ' + family + ' package build')
    if run.get('head_branch') != branch:
        raise RuntimeError('Only builds from the default branch can be published')
    if run.get('status') != 'completed' or run.get('conclusion') not in ('success', 'failure'):
        raise RuntimeError('Wait for the package build to finish before publishing')
    if not re.fullmatch(r'[a-f0-9]{40}', run.get('head_sha', '')):
        raise RuntimeError('Missing build commit identity')
    available = {}
    for artifact in artifacts:
        if artifact.get('expired') or not artifact['name'].startswith('shared-'):
            continue
        if artifact['name'] in available:
            raise RuntimeError('Ambiguous build artifact: ' + artifact['name'])
        if not isinstance(artifact.get('id'), int) or artifact['id'] <= 0:
            raise RuntimeError('Invalid build artifact ID')
        available[artifact['name']] = artifact['id']
    plans = sorted(name for name in available if re.fullmatch(r'shared-plans-' + family + r'-(?:alma8-)?(?:x86_64|arm64)', name))
    if not plans:
        raise RuntimeError('No retained build plans; the build failed before staging or its artifacts expired')
    return {'build_run_id': str(run['id']), 'commit': run['head_sha'], 'type': kind, 'family': family,
            'artifacts': available, 'plans': plans}


def pinned_image(image, name):
    if not isinstance(image, str) or not re.fullmatch(r'ghcr\.io/static-php/' + name + r'@sha256:[a-f0-9]{64}', image):
        raise RuntimeError('Missing pinned ' + name + ' image')
    return image


def publication_matrix(state, plans):
    includes = []
    kind, family = state['type'], state['family']
    for name in state['plans']:
        directory = plans / name
        target = json.loads((directory / 'target.json').read_text())
        plan = json.loads((directory / 'shared-library-plan.json').read_text())
        arch = target.get('arch')
        identity = ('alma8-' if family == 'glibc' else '') + str(arch)
        if arch not in ('x86_64', 'arm64') or target.get('target') != identity or name != 'shared-plans-' + family + '-' + identity:
            raise RuntimeError('Build plan target does not match its artifact')
        source_run = str(target.get('source_run_id', ''))
        if not re.fullmatch('[0-9]+', source_run):
            raise RuntimeError('Missing pinned source-download run')
        image = pinned_image(target.get('image'), 'packages-builder-alpine' if kind == 'apk' else 'packages-builder-rhel-8')
        packaging = image if kind == 'apk' else pinned_image(target.get('package_images', {}).get(kind),
            'packages-builder-rhel-8' if kind == 'rpm' else 'packages-packager-debian')
        selection = plan.get('format_selection', {}).get(kind, plan)
        streams = sorted(stream for stream, request in selection.get('php_requests', {}).items() if request.get('full') or request.get('packages'))
        if not set(streams) <= set(STREAMS):
            raise RuntimeError('Unsupported PHP stream in build plan')
        required = ['shared-libs-' + kind + '-' + identity]
        if streams:
            consumers = ['shared-consumers-' + kind + '-' + identity + '-php' + stream for stream in streams]
            available = [name for name in consumers if name in state['artifacts']]
            for missing in sorted(set(consumers) - set(available)):
                print('::warning::Skipping unavailable stream: ' + missing, file=sys.stderr)
            if not available:
                continue
            required += available
        elif selection.get('libraries'):
            validations = ['shared-validation-' + kind + '-' + identity + '-php' + stream for stream in STREAMS]
            required += [next((name for name in validations if name in state['artifacts']), validations[3])]
        else:
            continue
        missing = [name for name in required if name not in state['artifacts']]
        if missing:
            print('::warning::Skipping incomplete ' + kind + ' ' + identity + ': missing or expired ' + ', '.join(missing), file=sys.stderr)
            continue
        includes.append({'type': kind, 'target': identity, 'arch': arch, 'image': image,
                         'package-image': packaging, 'package-image-digest': image_digest(packaging), 'source-run-id': source_run,
                         'artifact-ids': ','.join(str(state['artifacts'][name]) for name in required)})
    if not includes:
        raise RuntimeError('No complete retained ' + kind + ' package cohort is available to publish')
    return includes


def verify_target(directory, kind, target, arch, image, source_run):
    recorded = json.loads((directory / 'target.json').read_text())
    expected = {'target': target, 'arch': arch, 'image': image, 'source_run_id': source_run}
    if any(str(recorded.get(key, '')) != value for key, value in expected.items()):
        raise RuntimeError('Package artifacts do not match the selected build target')
    plan = json.loads((directory / 'shared-library-plan.json').read_text())
    if plan.get('type') != kind:
        raise RuntimeError('Package artifacts belong to a different format')


def api(path, paginate=False):
    command = ['gh', 'api', path]
    if paginate:
        command += ['--paginate', '--slurp']
    return json.loads(subprocess.check_output(command, text=True))


def publish_available(source, kind, target, mirror=False):
    library = source / f'shared-libs-{kind}-{target}'
    plan = json.loads((library / 'shared-library-plan.json').read_text())
    prune_consumers(source, library)
    streams = sorted(stream for stream, request in plan.get('php_requests', {}).items() if request.get('full') or request.get('packages'))
    scopes = [[stream] for stream in streams] or [None]
    results = []
    outcomes = Path('log') / f'publication-{kind}-{target}.json'
    if mirror:
        if not outcomes.is_file():
            print(f'No primary publication results for {kind} {target}; skipping mirror', flush=True)
            return True
        published = json.loads(outcomes.read_text())
        scopes = [scope for scope in scopes if (','.join(scope) if scope else 'libraries') in published]
    with tempfile.TemporaryDirectory(prefix='spp-signed-rpms-') as signed:
        for scope in scopes:
            label = ','.join(scope) if scope else 'libraries'
            environment = dict(os.environ, PUBLICATION_PHP_STREAMS=','.join(scope or []), SIGNED_RPM_CACHE=signed, PYTHONUNBUFFERED='1')
            log = Path('log') / (f'publication-{kind}-{target}-php{label}' + ('-mirror.log' if mirror else '.log'))
            log.parent.mkdir(exist_ok=True)
            def run(*arguments):
                print(f'{kind.upper()} {target} PHP {label}: {arguments[-1]}', flush=True)
                finished = threading.Event()
                def progress():
                    while not finished.wait(30):
                        with log.open('rb') as output:
                            output.seek(max(0, output.seek(0, 2) - 4096))
                            lines = output.read().decode(errors='replace').splitlines()
                        detail = lines[-1][-300:] if lines else 'waiting for output'
                        print(f'{kind.upper()} {target} PHP {label}: {arguments[-1]} running; {detail}', flush=True)
                log.touch(exist_ok=True)
                monitor = threading.Thread(target=progress, daemon=True)
                monitor.start()
                try:
                    with log.open('a') as output:
                        subprocess.run(list(arguments), check=True, env=environment, stdout=output, stderr=subprocess.STDOUT)
                except subprocess.CalledProcessError as error:
                    detail = ' | '.join(log.read_text(errors='replace').splitlines()[-4:])[-800:]
                    raise RuntimeError((detail or str(error)) + f' (details: {log})') from error
                finally:
                    finished.set()
                    monitor.join()
            try:
                verify_upgrades(source, library, kind, scope)
                destination = Path('dist') / kind
                if destination.exists():
                    shutil.rmtree(destination)
                assemble(source, destination, kind, scope)
                if mirror:
                    run('bash', '.github/actions/shared-libraries/run.sh', 'upload-2')
                    results.append((label, 'mirrored'))
                else:
                    if kind == 'rpm':
                        run('bash', '.github/actions/shared-libraries/sign-rpm.sh')
                    run('bash', '.github/actions/shared-libraries/run.sh', 'publish')
                    run('bash', '.github/actions/shared-libraries/run.sh', 'publish-rpm' if kind == 'rpm' else 'upload-1')
                    results.append((label, 'published'))
                    outcomes.write_text(json.dumps([stream for stream, state in results if state == 'published']) + '\n')
            except (RuntimeError, OSError, ValueError, subprocess.CalledProcessError) as error:
                results.append((label, 'failed: ' + str(error)))
                level = 'warning' if mirror else 'error'
                print(f'::{level}::{kind} PHP {label}: {error}; continuing with other streams', flush=True)
    summary = '\n'.join(f'{kind.upper()} {target} PHP {stream}: {state}' for stream, state in results)
    print(summary, flush=True)
    if os.environ.get('GITHUB_STEP_SUMMARY'):
        with open(os.environ['GITHUB_STEP_SUMMARY'], 'a') as output:
            output.write(summary + '\n')
    return mirror or all(state == 'published' for _, state in results)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    inspect = sub.add_parser('inspect')
    inspect.add_argument('--run-id', required=True)
    inspect.add_argument('--type', choices=('rpm', 'deb', 'apk'), required=True)
    inspect.add_argument('--repository', default=os.environ.get('GITHUB_REPOSITORY'))
    inspect.add_argument('--branch', required=True)
    inspect.add_argument('--output', type=Path, required=True)
    matrix = sub.add_parser('matrix')
    matrix.add_argument('--state', type=Path, required=True)
    matrix.add_argument('--plans', type=Path, required=True)
    verify = sub.add_parser('verify-target')
    verify.add_argument('--directory', type=Path, required=True)
    sub.add_parser('publish-available').add_argument('--mirror', action='store_true', default=os.environ.get('MIRROR') == 'true')
    artifacts = sub.add_parser('artifact-ids')
    artifacts.add_argument('--run-id', required=True)
    args = parser.parse_args()
    if args.command == 'inspect':
        if not re.fullmatch('[0-9]+', args.run_id) or not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', args.repository or ''):
            raise RuntimeError('A numeric build run ID and repository are required')
        endpoint = 'repos/' + args.repository + '/actions/runs/' + args.run_id
        run = api(endpoint)
        artifacts = [item for page in api(endpoint + '/artifacts?per_page=100', paginate=True) for item in page['artifacts']]
        state = inspect_run(run, artifacts, args.repository, args.branch, args.type)
        args.output.write_text(json.dumps(state, sort_keys=True) + '\n')
        emit('plan-artifact-ids', ','.join(str(state['artifacts'][name]) for name in state['plans']))
    elif args.command == 'matrix':
        state = json.loads(args.state.read_text())
        includes = publication_matrix(state, args.plans)
        emit('matrix', {'include': includes})
        emit('any', 'true')
    elif args.command == 'artifact-ids':
        endpoint = 'repos/' + os.environ['GITHUB_REPOSITORY'] + '/actions/runs/' + args.run_id + '/artifacts?per_page=100'
        pattern = r'shared-(?:libs|consumers|validation)-' + re.escape(os.environ['TYPE'] + '-' + os.environ['TARGET']) + r'(?:-php8\.[2-6])?'
        ids = [str(item['id']) for page in api(endpoint, paginate=True) for item in page['artifacts']
               if not item.get('expired') and re.fullmatch(pattern, item['name'])]
        if not ids:
            raise RuntimeError('No staged native packages available')
        emit('ids', ','.join(ids))
    elif args.command == 'publish-available':
        if not publish_available(Path('cohort'), os.environ['TYPE'], os.environ['TARGET'], args.mirror):
            raise SystemExit(1)
    else:
        verify_target(args.directory, *(os.environ[name] for name in ['TYPE', 'TARGET', 'MATRIX_ARCH', 'IMAGE', 'SOURCE_RUN_ID']))


if __name__ == '__main__':
    main()
