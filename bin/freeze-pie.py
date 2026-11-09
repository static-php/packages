#!/usr/bin/env python3
"""Freeze the PIE release selected by the shared source snapshot."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import urllib.error
import urllib.parse
import urllib.request


def version(value):
    value = re.sub(r'^[vV](?=\d)', '', str(value))
    if not re.fullmatch(r'\d+\.\d+\.\d+[A-Za-z0-9.+~-]*', value):
        raise RuntimeError('Invalid frozen PIE release version')
    return value


def fetch(url, authenticated=False):
    headers = {'User-Agent': 'static-php-source-snapshot'}
    if authenticated:
        headers['Accept'] = 'application/vnd.github+json'
        token = os.environ.get('GITHUB_TOKEN') or os.environ.get('GH_TOKEN')
        if token:
            headers['Authorization'] = 'Bearer ' + token
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=90) as response:
        return response.read()


def probe(path, php):
    environment = dict(os.environ, XDEBUG_MODE='off', XDEBUG_SESSION='', XDEBUG_CONFIG='')
    result = subprocess.run([php, str(path), '-V'], capture_output=True, text=True,
                            env=environment, timeout=60, check=True)
    match = re.search(r'(?:\(PIE\)|\bPIE)\s+(\d+\.\d+\.\d+[A-Za-z0-9.+~-]*)',
                      result.stdout + '\n' + result.stderr)
    if not match:
        raise RuntimeError('Frozen PIE archive did not report its release version')
    return version(match.group(1))


def freeze(downloads, requested=None, php='php'):
    downloads = Path(downloads)
    metadata_path = downloads / '.spc-meta.json'
    snapshot = json.loads(metadata_path.read_text())
    tag = requested or snapshot.get('pie_version')
    expected = version(tag)
    release = None
    for candidate in dict.fromkeys([str(tag), 'v' + expected, expected]):
        try:
            release = json.loads(fetch('https://api.github.com/repos/php/pie/releases/tags/' +
                                       urllib.parse.quote(candidate, safe=''), authenticated=True))
            break
        except urllib.error.HTTPError as error:
            if error.code != 404:
                raise
    if release is None:
        raise RuntimeError('Frozen PIE release is unavailable: ' + expected)
    if version(release.get('tag_name')) != expected:
        raise RuntimeError('PIE release metadata does not match the frozen version')
    asset = next((item for item in release.get('assets', []) if item.get('name') == 'pie.phar'), None)
    if asset is None:
        raise RuntimeError('Frozen PIE release has no pie.phar asset')
    url = asset.get('browser_download_url', '')
    if not url.startswith('https://github.com/php/pie/releases/download/'):
        raise RuntimeError('Invalid frozen PIE asset URL')
    published_digest = asset.get('digest')
    cached = downloads / 'pie.phar'
    content = cached.read_bytes() if cached.is_file() else None
    if content is None or published_digest != 'sha256:' + hashlib.sha256(content).hexdigest():
        content = fetch(url)
    sha256 = hashlib.sha256(content).hexdigest()
    if published_digest and published_digest != 'sha256:' + sha256:
        raise RuntimeError('Frozen PIE archive does not match its published SHA256')
    with tempfile.TemporaryDirectory(prefix='spp-pie-freeze-', dir=downloads) as temporary:
        stage = Path(temporary)
        phar = stage / 'pie.phar'
        phar.write_bytes(content)
        phar.chmod(0o755)
        if probe(phar, php) != expected:
            raise RuntimeError('PIE archive version differs from its frozen release')
        record = {'schema': 1, 'version': expected, 'sha256': sha256,
                  'source_tag': release['tag_name'], 'source_url': url}
        snapshot.update(pie_version=expected, pie_sha256=sha256)
        (stage / 'pie.json').write_text(json.dumps(record, indent=2) + '\n')
        (stage / '.spc-meta.json').write_text(json.dumps(snapshot, indent=2) + '\n')
        phar.replace(downloads / 'pie.phar')
        (stage / 'pie.json').replace(downloads / 'pie.json')
        (stage / '.spc-meta.json').replace(metadata_path)
    return record


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--downloads', type=Path, default=Path('downloads'))
    parser.add_argument('--version', help='Explicit source-snapshot release override')
    parser.add_argument('--php', default='php')
    args = parser.parse_args()
    print(json.dumps(freeze(args.downloads, args.version, args.php), sort_keys=True))


if __name__ == '__main__':
    main()
