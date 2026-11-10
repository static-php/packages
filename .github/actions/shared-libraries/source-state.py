#!/usr/bin/env python3
"""Recover source comparison state from a preceding immutable source snapshot."""

import argparse
import json
import os
from pathlib import Path
import runpy
import tempfile
from urllib.parse import quote


def read(path):
    return json.loads(path.read_text()) if path.is_file() else {}


def restore(root, repository, before, allow_sources=False, github=None, fetch=None, allow_empty=False):
    state = root / '.download-state'
    lock, metadata = state / 'lock.prev.json', state / 'meta.prev.json'
    if read(lock) and read(metadata).get('shared_configuration'):
        return 'cache'
    if allow_sources:
        previous = root / 'downloads'
        if read(previous / '.cache.json') and read(previous / '.spc-meta.json').get('shared_configuration'):
            state.mkdir(parents=True, exist_ok=True)
            lock.write_bytes((previous / '.cache.json').read_bytes())
            metadata.write_bytes((previous / '.spc-meta.json').read_bytes())
            return 'source-cache'
    if allow_empty:
        state.mkdir(parents=True, exist_ok=True)
        lock.write_text('{}\n')
        metadata.write_text('{}\n')
        return 'explicit-full'
    helpers = Path(__file__).parent
    if github is None:
        cache = runpy.run_path(str(helpers.parent / 'buildroot-cache/cache.py'))
        github = cache['GitHub'](repository, os.environ.get('GITHUB_TOKEN', ''))
    fetch = fetch or runpy.run_path(str(helpers / 'source-snapshot.py'))['fetch']
    current = github.api('/actions/runs/' + str(before))
    branch = quote(current['head_branch'], safe='')
    for page in range(1, 101):
        runs = github.api('/actions/workflows/' + str(current['workflow_id'])
                          + '/runs?status=success&branch=' + branch + '&per_page=100&page=' + str(page))['workflow_runs']
        for run in runs:
            if run['created_at'] >= current['created_at']:
                continue
            artifacts = github.api('/actions/runs/' + str(run['id']) + '/artifacts?per_page=100')['artifacts']
            if not any(not item['expired'] and item['name'] in ('downloads-inputs', 'downloads-tarball') for item in artifacts):
                continue
            with tempfile.TemporaryDirectory(prefix='source-state-', dir=root) as temporary:
                previous = Path(temporary)
                fetch(previous, repository, run['id'])
                sources = read(previous / 'downloads/.cache.json')
                inputs = read(previous / 'downloads/.spc-meta.json')
                if not sources or not inputs.get('shared_configuration'):
                    raise RuntimeError('Previous source snapshot has no usable comparison state: ' + str(run['id']))
                state.mkdir(parents=True, exist_ok=True)
                lock.write_text(json.dumps(sources, sort_keys=True) + '\n')
                metadata.write_text(json.dumps(inputs, sort_keys=True) + '\n')
                (state / 'baseline.json').write_text(json.dumps({'repository': repository, 'run_id': str(run['id'])}) + '\n')
            return str(run['id'])
        if len(runs) < 100:
            break
    raise RuntimeError('No previous source comparison state is available; refusing to classify every input as changed')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path('.'))
    parser.add_argument('--repository', required=True)
    parser.add_argument('--before', required=True)
    parser.add_argument('--allow-source-cache', action='store_true')
    parser.add_argument('--allow-empty', action='store_true')
    args = parser.parse_args()
    print('Source comparison baseline: ' + restore(args.root.resolve(), args.repository, args.before, args.allow_source_cache, allow_empty=args.allow_empty))
