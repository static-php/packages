#!/usr/bin/env python3
"""Select successful producer outputs for an independent native format run."""

import json
import os
from pathlib import Path
import re
import subprocess
import sys

from publication import api, inspect_run
from workflow import STREAMS, csv, emit, image_digest, matrix, tls_checks


def select(state, plans, jobs, kind, streams, architectures=('x86_64', 'arm64'), reuse_libraries=True):
    successful = {job['name'].split(' / ')[-1] for job in jobs if job.get('conclusion') == 'success'}
    sdks = {match[2] for name in successful if (match := re.fullmatch(r'(Build libs shared|Repackage libraries) \(([^)]+)\)', name))}
    payloads = {(match[2], match[3]) for name in successful if (match := re.fullmatch(r'(Build|Reuse) payload \(([^ ]+) php([^)]+)\)', name))}
    consumers, targets = matrix(plans, streams, sdks)
    libraries, includes, publishing = [], [], []
    for target in targets:
        if target['arch'] not in architectures:
            continue
        identity = target['target']
        selection = target.get('format_selection', {}).get(kind, {})
        if not selection.get('libraries') and not any(request.get('full') or request.get('packages') for request in selection.get('php_requests', {}).values()):
            continue
        metadata = state['artifacts']['shared-sdk-glibc-' + identity]
        sdk = state['artifacts'].get('shared-sdk-buildroot-glibc-' + identity)
        retained = state['artifacts'].get('shared-libs-' + kind + '-' + identity) if reuse_libraries else None
        if selection.get('libraries') and sdk is None and retained is None:
            print('::warning::Skipping ' + identity + ': no retained library SDK', file=sys.stderr)
            continue
        base = dict(target, type=kind, **{'package-image': target['package_images'][kind],
                    'metadata-artifact': metadata, 'sdk-artifact': sdk,
                    'libraries-artifact': retained,
                    'libraries-required': bool(selection.get('libraries')) and retained is None})
        active = set()
        for value in consumers:
            stream = value['php-version']
            if value['target'] != identity or (identity, stream) not in payloads and target.get('payload_stream') != stream:
                continue
            request = selection.get('php_requests', {}).get(stream, {})
            payload = state['artifacts'].get('shared-payload-glibc-' + identity + '-php' + stream)
            if not payload or not (request.get('full') or request.get('packages')):
                continue
            active.add(stream)
            includes.append(dict(base, **{'php-version': stream, 'packages': '' if request.get('full') else ','.join(request['packages']),
                                         'payload-artifact': payload, 'library-only': False}))
        requested = {stream for stream, request in selection.get('php_requests', {}).items() if request.get('full') or request.get('packages')}
        default_probe = ['8.5' if '8.5' in streams else sorted(streams)[-1]] if streams else []
        probes = selection.get('library_streams') or (default_probe if not requested and selection.get('libraries') else [])
        probes = set(probes).intersection(streams)
        for stream in set(probes) - requested:
            includes.append(dict(base, **{'php-version': stream, 'packages': '', 'library-only': True}))
        if active or set(probes) - requested:
            libraries.append(base)
            publishing.append(dict(base, **{'package-image-digest': image_digest(base['package-image'])}))
    tls_checks(includes)
    return libraries, includes, publishing


def main():
    repository, run_id, kind = (os.environ[name] for name in ('GITHUB_REPOSITORY', 'BUILD_RUN_ID', 'TYPE'))
    if not run_id.isdigit() or kind not in ('rpm', 'deb'):
        raise RuntimeError('A producer run ID and one native format are required')
    endpoint = f'repos/{repository}/actions/runs/{run_id}'
    run = api(endpoint)
    artifacts = [item for page in api(endpoint + '/artifacts?per_page=100', True) for item in page['artifacts']]
    state = inspect_run(run, artifacts, repository, os.environ['DEFAULT_BRANCH'], kind)
    plans = Path('plans')
    subprocess.run(['gh', 'run', 'download', run_id, '--repo', repository, '--pattern', 'shared-plans-glibc-*', '--dir', str(plans)], check=True)
    jobs = [job for page in api(endpoint + '/jobs?per_page=100', True) for job in page['jobs']]
    architectures = csv(os.environ.get('ARCHITECTURES', ''), ['x86_64', 'arm64'])
    if not architectures or set(architectures) - {'x86_64', 'arm64'}:
        raise RuntimeError('Unsupported packaging architecture')
    reuse = os.environ.get('BUMP', 'false') != 'true' and not os.environ.get('ITERATION')
    libraries, packages, publishing = select(state, plans, jobs, kind, csv(os.environ.get('PHP_VERSIONS', ''), STREAMS), architectures, reuse)
    if not libraries:
        raise RuntimeError('No successful producer target has the retained outputs required for this packaging selection')
    for key, values in [('libraries', libraries), ('packages', packages), ('publish', publishing)]:
        emit(key + '-matrix', {'include': values})
        emit(key + '-any', str(bool(values)).lower())


if __name__ == '__main__':
    main()
