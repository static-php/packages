#!/usr/bin/env python3
"""Select complete package cohorts from an existing build run."""

import argparse
import io
import json
import os
from pathlib import Path
import re
import runpy
import shutil
import subprocess
import sys
import tempfile
import zipfile
from urllib.request import Request, build_opener, urlopen

from workflow import STREAMS, assemble, emit, image_digest, prune_consumers, verify_upgrades


WORKFLOWS = {
    'glibc': {'.github/workflows/build-rpm-modular-packages.yml', '.github/workflows/build-deb-forgejo.yml', '.github/workflows/package-glibc.yml'},
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
    completed = run.get('status') == 'completed' and run.get('conclusion') in ('success', 'failure', 'cancelled')
    running = run.get('status') in ('queued', 'pending', 'waiting', 'in_progress') and not run.get('conclusion')
    if not (completed or running):
        raise RuntimeError('Unsupported package build state')
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


def publication_matrix(state, plans, selected_arch='all', selected_php='all', allow_empty=False):
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
        if selected_arch not in ('all', arch):
            continue
        source_run = str(target.get('source_run_id', ''))
        if not re.fullmatch('[0-9]+', source_run):
            raise RuntimeError('Missing pinned source-download run')
        image = pinned_image(target.get('image'), 'packages-builder-alpine' if kind == 'apk' else 'packages-builder-rhel-8')
        packaging = image if kind == 'apk' else pinned_image(target.get('package_images', {}).get(kind),
            'packages-builder-rhel-8' if kind == 'rpm' else 'packages-packager-debian')
        selection = plan.get('format_selection', {}).get(kind, plan)
        consumers = {stream for stream, request in selection.get('php_requests', {}).items() if request.get('full') or request.get('packages')}
        streams = sorted(consumers | set(selection.get('library_streams', [])))
        if not set(streams) <= set(STREAMS):
            raise RuntimeError('Unsupported PHP stream in build plan')
        if selected_php != 'all':
            if selected_php not in streams:
                continue
            streams = [selected_php]
        required = ['shared-libs-' + kind + '-' + identity]
        available_streams = []
        if streams:
            artifacts = ['shared-' + ('consumers' if stream in consumers else 'validation') + '-' + kind + '-' + identity + '-php' + stream for stream in streams]
            available = [name for name in artifacts if name in state['artifacts']]
            for missing in sorted(set(artifacts) - set(available)):
                print('::warning::Skipping unavailable stream: ' + missing, file=sys.stderr)
            if not available:
                continue
            available_streams = [name.rsplit('-php', 1)[1] for name in available]
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
                         'build-run-id': state['build_run_id'], 'php-versions': ','.join(sorted(available_streams)),
                         'artifact-ids': ','.join(str(state['artifacts'][name]) for name in required)})
    if not includes and not allow_empty:
        raise RuntimeError('No complete retained ' + kind + ' package cohort is available to publish')
    return includes


def run_ids(value):
    values = [item.strip() for item in value.split(',')]
    if not values or any(not item.isdigit() or int(item) <= 0 for item in values) or len(set(values)) != len(values):
        raise RuntimeError('Provide distinct positive build run IDs separated by commas')
    return values


def batch_matrix(entries, selected_arch='all', selected_php='all'):
    includes, selected = [], set()
    for state, plans in entries:
        for value in publication_matrix(state, plans, selected_arch, selected_php, allow_empty=True):
            identity = 'build' + state['build_run_id']
            scopes = value['php-versions'].split(',') if value['php-versions'] else ['libraries']
            for stream in scopes:
                key = (value['type'], value['arch'], stream)
                if key in selected:
                    raise RuntimeError('Multiple build cohorts selected for ' + ' '.join(key))
                selected.add(key)
            value.update({'publication-id': identity,
                          'scope': 'php' + value['php-versions'] if value['php-versions'] else 'libraries'})
            includes.append(value)
    if not includes:
        raise RuntimeError('No complete retained package cohort is available for this format selection')
    return includes


def read_plan_archive(repository, artifact_id):
    cache = runpy.run_path(str(Path(__file__).resolve().parents[1] / 'buildroot-cache/cache.py'))
    token = os.environ.get('GH_TOKEN') or os.environ.get('GITHUB_TOKEN')
    headers = {'Accept': 'application/vnd.github+json', 'User-Agent': 'static-php-packages'}
    if token:
        headers['Authorization'] = 'Bearer ' + token
    base = os.environ.get('GITHUB_API_URL', 'https://api.github.com').rstrip('/')
    request = Request(f'{base}/repos/{repository}/actions/artifacts/{artifact_id}/zip', headers=headers)
    with build_opener(cache['AssetRedirectHandler']()).open(request, timeout=60) as response:
        return response.read()


def prepare_publication(builds, kind, repository, branch, selected_arch='all', selected_php='all'):
    builds = run_ids(builds)
    entries = []
    with tempfile.TemporaryDirectory(prefix='spp-publication-plans-') as temporary:
        for run_id in builds:
            endpoint = f'repos/{repository}/actions/runs/{run_id}'
            run = api(endpoint)
            if run.get('status') != 'completed':
                raise RuntimeError('Publication requires completed package build runs')
            artifacts = [item for page in api(endpoint + '/artifacts?per_page=100', True) for item in page['artifacts']]
            state = inspect_run(run, artifacts, repository, branch, kind)
            plans = Path(temporary) / run_id
            for name in state['plans']:
                content = read_plan_archive(repository, state['artifacts'][name])
                with zipfile.ZipFile(io.BytesIO(content)) as archive:
                    for filename in ('target.json', 'shared-library-plan.json'):
                        data = archive.read(filename)
                        json.loads(data)
                        destination = plans / name / filename
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        destination.write_bytes(data)
            entries.append((state, plans))
        return batch_matrix(entries, selected_arch, selected_php)


def verify_target(directory, kind, target, arch, image, source_run):
    recorded = json.loads((directory / 'target.json').read_text())
    expected = {'target': target, 'arch': arch, 'image': image, 'source_run_id': source_run}
    if any(str(recorded.get(key, '')) != value for key, value in expected.items()):
        raise RuntimeError('Package artifacts do not match the selected build target')
    plan = json.loads((directory / 'shared-library-plan.json').read_text())
    if plan.get('type') != kind:
        raise RuntimeError('Package artifacts belong to a different format')


def api(path, paginate=False):
    base = os.environ.get('GITHUB_API_URL', 'https://api.github.com').rstrip('/') + '/'
    url = base + path.lstrip('/')
    headers = {'Accept': 'application/vnd.github+json', 'User-Agent': 'static-php-packages'}
    token = os.environ.get('GH_TOKEN') or os.environ.get('GITHUB_TOKEN')
    if token:
        headers['Authorization'] = 'Bearer ' + token
    pages = []
    while url:
        if not url.startswith(base):
            raise RuntimeError('GitHub pagination left the API origin')
        with urlopen(Request(url, headers=headers), timeout=60) as response:
            page = json.load(response)
            if not paginate:
                return page
            pages.append(page)
            next_page = re.search(r'<([^>]+)>;\s*rel="next"', response.headers.get('Link', ''))
            url = next_page.group(1) if next_page else None
    return pages


def run_logged(arguments, environment, log):
    with log.open('a') as output, subprocess.Popen(list(arguments), env=environment, stdout=subprocess.PIPE,
                                                 stderr=subprocess.STDOUT, text=True, errors='replace', bufsize=1) as process:
        for line in process.stdout:
            output.write(line)
            output.flush()
            print(line, end='', flush=True)
        process.stdout.close()
        if process.wait():
            raise subprocess.CalledProcessError(process.returncode, arguments)


def publish_available(source, kind, target, selected_php='all', selected_versions=''):
    library = source / f'shared-libs-{kind}-{target}'
    plan = json.loads((library / 'shared-library-plan.json').read_text())
    prune_consumers(source, library)
    streams = sorted({stream for stream, request in plan.get('php_requests', {}).items() if request.get('full') or request.get('packages')}
                     | set(plan.get('library_streams', [])))
    if selected_php != 'all':
        if selected_php not in streams:
            raise RuntimeError('Selected PHP stream is not in the retained build plan: ' + selected_php)
        streams = [selected_php]
    if selected_versions:
        wanted = selected_versions.split(',')
        if not set(wanted) <= set(streams):
            raise RuntimeError('Publication streams are not in the retained build plan')
        streams = sorted(set(wanted))
    scopes = [[stream] for stream in streams] or [None]
    results = []
    outcomes = Path('log') / f'publication-{kind}-{target}.json'
    with tempfile.TemporaryDirectory(prefix='spp-signed-rpms-') as signed:
        for scope in scopes:
            uploading = False
            label = ','.join(scope) if scope else 'libraries'
            environment = dict(os.environ, PUBLICATION_PHP_STREAMS=','.join(scope or []), SIGNED_RPM_CACHE=signed, PYTHONUNBUFFERED='1')
            log = Path('log') / f'publication-{kind}-{target}-php{label}.log'
            log.parent.mkdir(exist_ok=True)
            def run(*arguments):
                print(f'{kind.upper()} {target} PHP {label}: {arguments[-1]}', flush=True)
                log.touch(exist_ok=True)
                try:
                    run_logged(arguments, environment, log)
                except subprocess.CalledProcessError as error:
                    raise RuntimeError(f'{arguments[-1]} failed (details: {log})') from error
            try:
                verify_upgrades(source, library, kind, scope)
                destination = Path('dist') / kind
                if destination.exists():
                    shutil.rmtree(destination)
                assemble(source, destination, kind, scope)
                if kind == 'rpm':
                    run('bash', '.github/actions/shared-libraries/sign-rpm.sh')
                run('bash', '.github/actions/shared-libraries/run.sh', 'publish')
                uploading = True
                run('bash', '.github/actions/shared-libraries/run.sh', 'publish-rpm' if kind == 'rpm' else 'upload')
                run('bash', '.github/actions/shared-libraries/run.sh', 'record-repository')
                results.append((label, 'published'))
                outcomes.write_text(json.dumps([stream for stream, state in results if state == 'published']) + '\n')
            except (RuntimeError, OSError, ValueError, subprocess.CalledProcessError) as error:
                if uploading:
                    try:
                        run('bash', '.github/actions/shared-libraries/run.sh', 'invalidate-repository')
                    except (RuntimeError, OSError, subprocess.CalledProcessError) as invalidation:
                        print(f'::warning::Could not invalidate repository state: {invalidation}', flush=True)
                results.append((label, 'failed: ' + str(error)))
                print(f'::error::{kind} PHP {label}: {error}; continuing with other streams', flush=True)
    summary = '\n'.join(f'{kind.upper()} {target} PHP {stream}: {state}' for stream, state in results)
    print(summary, flush=True)
    if os.environ.get('GITHUB_STEP_SUMMARY'):
        with open(os.environ['GITHUB_STEP_SUMMARY'], 'a') as output:
            output.write(summary + '\n')
    return all(state == 'published' for _, state in results)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    prepare = sub.add_parser('prepare')
    prepare.add_argument('--run-ids', required=True)
    prepare.add_argument('--type', choices=('rpm', 'deb', 'apk'), required=True)
    prepare.add_argument('--repository', default=os.environ.get('GITHUB_REPOSITORY'))
    prepare.add_argument('--branch', required=True)
    prepare.add_argument('--arch', choices=('all', 'x86_64', 'arm64'), default='all')
    prepare.add_argument('--php', choices=('all', *STREAMS), default='all')
    verify = sub.add_parser('verify-target')
    verify.add_argument('--directory', type=Path, required=True)
    publish = sub.add_parser('publish-available')
    publish.add_argument('--php', choices=('all', *STREAMS), default=os.environ.get('PUBLISH_PHP_VERSION') or 'all')
    publish.add_argument('--php-streams', default=os.environ.get('PUBLISH_PHP_VERSIONS', ''))
    artifacts = sub.add_parser('artifact-ids')
    artifacts.add_argument('--run-id', required=True)
    args = parser.parse_args()
    if args.command == 'prepare':
        if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', args.repository or ''):
            raise RuntimeError('A repository is required')
        includes = prepare_publication(args.run_ids, args.type, args.repository, args.branch, args.arch, args.php)
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
        if not publish_available(Path('cohort'), os.environ['TYPE'], os.environ['TARGET'], args.php, args.php_streams):
            raise SystemExit(1)
    else:
        verify_target(args.directory, *(os.environ[name] for name in ['TYPE', 'TARGET', 'MATRIX_ARCH', 'IMAGE', 'SOURCE_RUN_ID']))


if __name__ == '__main__':
    main()
