#!/usr/bin/env python3
"""Read selected JSON metadata from a retained build without fetching its SDK archives."""

import argparse
import io
import json
from pathlib import Path
import re
import subprocess
import time
import zipfile

import requests

def request(session, method, url, **kwargs):
    for attempt in range(3):
        try:
            response = session.request(method, url, **kwargs)
            if response.status_code != 429 and response.status_code < 500:
                return response
            response.close()
        except requests.RequestException:
            pass
        if attempt < 2:
            time.sleep(attempt + 1)
    raise RuntimeError('Artifact transport failed after three attempts')


class RemoteZip(io.RawIOBase):
    def __init__(self, url):
        self.session = requests.Session()
        self.url, self.position, self.cache = url, 0, []
        response = request(self.session, 'HEAD', url, timeout=60)
        if response.status_code != 200:
            raise RuntimeError('Cannot inspect artifact size: HTTP ' + str(response.status_code))
        self.size = int(response.headers['Content-Length'])
        start = max(0, self.size - 131072)
        self.cache.append((start, self.range(start, self.size - 1)))

    def range(self, start, end):
        for attempt in range(3):
            response = request(self.session, 'GET', self.url, headers={'Range': f'bytes={start}-{end}'}, stream=True, timeout=60)
            if response.status_code != 206:
                response.close()
                raise RuntimeError('Artifact bounded read failed: HTTP ' + str(response.status_code))
            expected = f'bytes {start}-{end}/{self.size}'
            if response.headers.get('Content-Range') != expected:
                response.close()
                raise RuntimeError('Artifact returned an unexpected byte range')
            try:
                data = response.content
                break
            except requests.RequestException:
                response.close()
                if attempt == 2:
                    raise RuntimeError('Artifact range transfer failed after three attempts') from None
                time.sleep(attempt + 1)
        if len(data) != end - start + 1:
            raise RuntimeError('Artifact range length mismatch')
        return data

    def seekable(self):
        return True

    def readable(self):
        return True

    def tell(self):
        return self.position

    def seek(self, offset, whence=0):
        self.position = offset if whence == 0 else self.position + offset if whence == 1 else self.size + offset
        return self.position

    def read(self, count=-1):
        if count < 0:
            count = self.size - self.position
        count = min(count, self.size - self.position)
        if count <= 0:
            return b''
        if count > 32 * 1024 * 1024:
            raise RuntimeError('Refusing an unbounded artifact member read')
        for start, data in self.cache:
            if start <= self.position and self.position + count <= start + len(data):
                result = data[self.position - start:self.position - start + count]
                self.position += len(result)
                return result
        start = self.position
        end = min(self.size - 1, start + max(count, 131072) - 1)
        data = self.range(start, end)
        self.cache.append((start, data))
        self.position += count
        return data[:count]


def read_metadata(run_id, artifact, selected):
    pages = None
    for attempt in range(3):
        result = subprocess.run(['gh', 'api', '--paginate', '--slurp',
            f'repos/{repository()}/actions/runs/{run_id}/artifacts'], text=True, capture_output=True)
        if result.returncode == 0:
            pages = json.loads(result.stdout)
            break
        if attempt < 2:
            time.sleep(attempt + 1)
    if pages is None:
        raise RuntimeError('Cannot list retained build artifacts after three attempts')
    matches = [record for page in pages for record in page['artifacts']
               if record['name'] == artifact and not record['expired']]
    if len(matches) != 1:
        raise RuntimeError('Missing or ambiguous retained metadata artifact: ' + artifact)
    token = subprocess.check_output(['gh', 'auth', 'token'], text=True).strip()
    response = request(requests, 'GET', f"https://api.github.com/repos/{repository()}/actions/artifacts/{matches[0]['id']}/zip",
        headers={'Authorization': 'Bearer ' + token}, allow_redirects=False, timeout=30)
    if response.status_code != 302:
        raise RuntimeError('Cannot access retained metadata artifact: HTTP ' + str(response.status_code))
    with zipfile.ZipFile(RemoteZip(response.headers['Location'])) as archive:
        records = {name: archive.read(name) for name in selected}
    for data in records.values():
        json.loads(data)
    return records


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-id', required=True, type=int)
    parser.add_argument('--plan-run-id', type=int)
    parser.add_argument('--artifact', required=True)
    parser.add_argument('--type', choices=('rpm', 'deb', 'apk'), required=True)
    parser.add_argument('--php', required=True)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    if not re.fullmatch(r'\d+\.\d+', args.php):
        parser.error('Unsupported PHP stream')
    prefix = '' if args.type == 'apk' else args.type + '/'
    plan = prefix + 'shared-library-plan.json'
    selected = [plan, prefix + 'native-inventories/' + args.php + '.json']
    records = read_metadata(args.run_id, args.artifact, selected)
    if args.plan_run_id:
        replacement = read_metadata(args.plan_run_id, args.artifact, [plan])[plan]
        if json.loads(replacement).get('expected_consumers', {}).get(args.php):
            records[plan] = replacement
    for name, data in records.items():
        path = args.output / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    print('Read retained upgrade metadata:', args.artifact, ', '.join(selected), flush=True)


def repository():
    import os
    value = os.environ.get('GITHUB_REPOSITORY', 'static-php/packages')
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', value):
        raise RuntimeError('Invalid repository identity')
    return value


if __name__ == '__main__':
    main()
