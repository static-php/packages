#!/usr/bin/env python3
"""Transfer frozen source inputs first; materialize the full snapshot for compilation."""

import argparse
import json
import os
from pathlib import Path
import re
import runpy
import shutil
import subprocess
import tarfile
import tempfile
import urllib.request
import zipfile


MARKER = '.source-snapshot.json'


def inputs(downloads, output):
    metadata = json.loads((downloads / '.spc-meta.json').read_text())
    index = json.loads((downloads / '.php-sources.json').read_text())
    cache = json.loads((downloads / '.cache.json').read_text())
    names = {path.name for path in downloads.glob('*.json')} | {'pie.phar', 'pie.json', '.cache.json', '.spc-meta.json', '.php-sources.json'}
    names.update(index['sources'][version]['source']['filename'] for version in metadata['php_versions'].values())
    spx = cache.get('ext-spx', {}).get('source', {}).get('filename')
    if spx:
        names.add(spx)
    with tarfile.open(output, 'w:gz', compresslevel=1) as archive:
        for name in sorted(names - {MARKER}):
            if Path(name).name != name or not (downloads / name).is_file() or (downloads / name).is_symlink():
                raise RuntimeError('Missing frozen source input: ' + name)
            archive.add(downloads / name, arcname=name)


def fetch(root, repository, run_id, full=False):
    if not re.fullmatch(r'[1-9][0-9]*', str(run_id)):
        raise RuntimeError('Source snapshots require a pinned workflow run')
    helper = runpy.run_path(str(Path(__file__).resolve().parents[1] / 'buildroot-cache/cache.py'))
    token = os.environ.get('GITHUB_TOKEN', '')
    github = helper['GitHub'](repository, token)
    artifacts = {}
    for page in range(1, 101):
        batch = github.api('/actions/runs/' + str(run_id) + '/artifacts?per_page=100&page=' + str(page))['artifacts']
        artifacts.update({entry['name']: entry for entry in batch if not entry['expired']})
        if len(batch) < 100:
            break
    name = 'downloads-tarball' if full or 'downloads-inputs' not in artifacts else 'downloads-inputs'
    artifact = artifacts.get(name)
    if artifact is None:
        raise RuntimeError('Pinned source snapshot is missing: ' + name)
    filename = 'downloads.tar.gz' if name == 'downloads-tarball' else 'downloads-inputs.tar.gz'
    downloads = root / 'downloads'
    downloads.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='spp-source-') as temporary:
        zipped = Path(temporary) / 'artifact.zip'
        request = urllib.request.Request('https://api.github.com/repos/' + repository + '/actions/artifacts/' + str(artifact['id']) + '/zip',
            headers={'Authorization': 'Bearer ' + token, 'User-Agent': 'php-zts-source-snapshot'})
        with urllib.request.build_opener(helper['AssetRedirectHandler']()).open(request, timeout=180) as response, zipped.open('wb') as output:
            shutil.copyfileobj(response, output)
        expected = artifact.get('digest')
        if expected and expected != 'sha256:' + helper['digest'](zipped):
            raise RuntimeError('Source artifact checksum mismatch')
        with zipfile.ZipFile(zipped) as archive, archive.open(filename) as source, (Path(temporary) / filename).open('wb') as output:
            shutil.copyfileobj(source, output)
        subprocess.run(['tar', '--no-same-owner', '-xzf', str(Path(temporary) / filename), '-C', str(downloads)], check=True)
    (downloads / MARKER).write_text(json.dumps({'repository': repository, 'run_id': str(run_id), 'complete': name == 'downloads-tarball'}))
    print('Restored ' + name + ' from source run ' + str(run_id), flush=True)


def materialize(root):
    marker = root / 'downloads' / MARKER
    if marker.is_file():
        snapshot = json.loads(marker.read_text())
        if not snapshot['complete']:
            fetch(root, snapshot['repository'], snapshot['run_id'], full=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('inputs', 'fetch', 'materialize'))
    parser.add_argument('--root', type=Path, default=Path('.'))
    parser.add_argument('--repository', default=os.environ.get('GITHUB_REPOSITORY'))
    parser.add_argument('--run-id', default=os.environ.get('SOURCE_RUN_ID'))
    parser.add_argument('--output', type=Path, default=Path('downloads-inputs.tar.gz'))
    args = parser.parse_args()
    if args.command == 'inputs':
        inputs(args.root / 'downloads', args.output)
    elif args.command == 'fetch':
        fetch(args.root, args.repository, args.run_id)
    else:
        materialize(args.root)
