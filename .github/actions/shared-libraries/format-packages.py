#!/usr/bin/env python3
"""Select successful producer outputs for an independent native format run."""

import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time

from publication import api, inspect_run
from workflow import STREAMS, csv, emit, image_digest, matrix, tls_checks


def select(state, plans, jobs, kind, streams):
    successful = {job['name'].split(' / ')[-1] for job in jobs if job.get('conclusion') == 'success'}
    sdks = {match[2] for name in successful if (match := re.fullmatch(r'(Build libs shared|Repackage libraries) \(([^)]+)\)', name))}
    payloads = {(match[2], match[3]) for name in successful if (match := re.fullmatch(r'(Build|Reuse) payload \(([^ ]+) php([^)]+)\)', name))}
    consumers, targets = matrix(plans, streams, sdks)
    libraries, includes, publishing = [], [], []
    for target in targets:
        identity = target['target']
        selection = target.get('format_selection', {}).get(kind, {})
        if not selection.get('libraries') and not any(request.get('full') or request.get('packages') for request in selection.get('php_requests', {}).values()):
            continue
        metadata = state['artifacts']['shared-sdk-glibc-' + identity]
        sdk = state['artifacts'].get('shared-sdk-buildroot-glibc-' + identity)
        if selection.get('libraries') and sdk is None:
            print('::warning::Skipping ' + identity + ': no retained library SDK', file=sys.stderr)
            continue
        base = dict(target, type=kind, **{'package-image': target['package_images'][kind],
                    'metadata-artifact': metadata, 'sdk-artifact': sdk,
                    'libraries-required': bool(selection.get('libraries'))})
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
        probes = selection.get('library_streams') or (['8.5'] if not requested and selection.get('libraries') else [])
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
    for attempt in range(30):
        if run.get('status') == 'completed':
            break
        time.sleep(2)
        run = api(endpoint)
    artifacts = [item for page in api(endpoint + '/artifacts?per_page=100', True) for item in page['artifacts']]
    state = inspect_run(run, artifacts, repository, os.environ['DEFAULT_BRANCH'], kind)
    if run['status'] != 'completed':
        raise RuntimeError('Packaging requires a completed producer')
    plans = Path('plans')
    subprocess.run(['gh', 'run', 'download', run_id, '--repo', repository, '--pattern', 'shared-plans-glibc-*', '--dir', str(plans)], check=True)
    jobs = [job for page in api(endpoint + '/jobs?per_page=100', True) for job in page['jobs']]
    libraries, packages, publishing = select(state, plans, jobs, kind, csv(os.environ.get('PHP_VERSIONS', ''), STREAMS))
    for key, values in [('libraries', libraries), ('packages', packages), ('publish', publishing)]:
        emit(key + '-matrix', {'include': values})
        emit(key + '-any', str(bool(values)).lower())


if __name__ == '__main__':
    main()
