#!/usr/bin/env python3
"""Restore verified SDKs and sealed PHP payloads from the persistent release cache."""

import hashlib
import json
import os
import re
import shutil
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path


MAX_ASSET_SIZE = 2 * 1024 ** 3
ASSET_PART_SIZE = 1024 ** 3


class CacheError(RuntimeError):
    pass


class AssetRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, url):
        redirected = super().redirect_request(request, response, code, message, headers, url)
        if redirected is not None and urllib.parse.urlsplit(request.full_url)[:2] != urllib.parse.urlsplit(url)[:2]:
            redirected.remove_header("Authorization")
        return redirected


def cache_names(key):
    if not re.fullmatch(r"(?:buildroot-|php-payload-)[A-Za-z0-9_.-]{1,160}", key):
        raise CacheError("Invalid compiled cache identity")
    asset = key + ".tar.zst"
    name = re.sub(r"-[a-f0-9]{32,64}$", "", asset.removesuffix(".tar.zst"))
    name = re.sub(r"^buildroot-shared-", "buildroot-", name)
    return name + ".tar.zst", name + ".json"


def digest(path):
    value = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


class GitHub:
    def __init__(self, repository, token=""):
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
            raise CacheError("Invalid repository")
        self.repository = repository
        self.token = token
        self.release_ids = {}
        self.asset_inventories = {}

    def request(self, url, method="GET", data=None, headers=None, missing=False):
        request_headers = {"Accept": "application/vnd.github+json", "User-Agent": "buildroot-cache"}
        if self.token:
            request_headers["Authorization"] = "Bearer " + self.token
        request_headers.update(headers or {})
        if isinstance(data, dict):
            data = json.dumps(data).encode()
            request_headers["Content-Type"] = "application/json"
        try:
            with urllib.request.urlopen(urllib.request.Request(url, data=data, headers=request_headers, method=method), timeout=180) as response:
                body = response.read()
                return json.loads(body) if body else None
        except urllib.error.HTTPError as error:
            if missing and error.code == 404:
                return None
            try:
                details = json.loads(error.read(65536))
                message = str(details.get('message', ''))
                if details.get('errors'):
                    message += ' ' + json.dumps(details['errors'])
            except (ValueError, TypeError, AttributeError):
                message = ''
            finally:
                error.close()
            raise CacheError("GitHub cache request failed: HTTP " + str(error.code) + (': ' + message if message else '')) from error

    def api(self, suffix, **kwargs):
        return self.request("https://api.github.com/repos/" + self.repository + suffix, **kwargs)

    def download(self, tag, asset, destination):
        if tag not in self.asset_inventories:
            if tag not in self.release_ids:
                release = self.api("/releases/tags/" + urllib.parse.quote(tag, safe=""), missing=True)
                self.release_ids[tag] = release["id"] if release is not None else None
            release_id = self.release_ids[tag]
            inventory = {}
            if release_id is not None:
                for page in range(1, 101):
                    assets = self.api("/releases/" + str(release_id) + "/assets?per_page=100&page=" + str(page))
                    inventory.update({value["name"]: value["id"] for value in assets})
                    if len(assets) < 100:
                        break
            self.asset_inventories[tag] = inventory
        asset_id = self.asset_inventories[tag].get(asset)
        if asset_id is None:
            return False
        return self.download_id(asset_id, destination)

    def download_id(self, asset_id, destination):
        url = "https://api.github.com/repos/" + self.repository + "/releases/assets/" + str(asset_id)
        headers = {"User-Agent": "buildroot-cache", "Accept": "application/octet-stream"}
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
        try:
            with urllib.request.build_opener(AssetRedirectHandler()).open(urllib.request.Request(url, headers=headers), timeout=180) as response, Path(destination).open("wb") as output:
                while block := response.read(1024 * 1024):
                    output.write(block)
            return True
        except urllib.error.HTTPError as error:
            if error.code == 404:
                return False
            raise CacheError("Cache download failed: HTTP " + str(error.code)) from error

    def release(self, tag):
        release = self.api("/releases/tags/" + urllib.parse.quote(tag, safe=""), missing=True)
        if release is None:
            try:
                release = self.api("/releases", method="POST", data={"tag_name": tag, "name": "Build cache", "body": "Compiled SDK and PHP payload caches.", "prerelease": True})
            except CacheError:
                release = self.api("/releases/tags/" + urllib.parse.quote(tag, safe=""), missing=True)
                if release is None:
                    raise
        return release

    def list_assets(self, release):
        result = []
        for page in range(1, 101):
            assets = self.api("/releases/" + str(release["id"]) + "/assets?per_page=100&page=" + str(page))
            result.extend(assets)
            if len(assets) < 100:
                break
        return result

    def find_asset(self, release, name):
        return next((asset for asset in self.list_assets(release) if asset['name'] == name), None)

    def rename(self, asset_id, name):
        return self.api('/releases/assets/' + str(asset_id), method='PATCH', data={'name': name})

    def delete(self, asset_id):
        self.api('/releases/assets/' + str(asset_id), method='DELETE', missing=True)

    def upload(self, release, name, path, immutable=False):
        url = "https://uploads.github.com/repos/" + self.repository + "/releases/" + str(release["id"]) + "/assets?name=" + urllib.parse.quote(name, safe="")
        path = Path(path)
        if path.stat().st_size >= MAX_ASSET_SIZE:
            raise CacheError('Release asset must be smaller than 2 GiB: ' + name + ' (' + str(path.stat().st_size) + ' bytes)')
        print('Uploading cache asset ' + name + ' (' + str(path.stat().st_size) + ' bytes)', flush=True)
        for attempt in range(4):
            asset = self.find_asset(release, name)
            if asset is not None:
                if immutable:
                    if asset.get("size") != path.stat().st_size:
                        raise CacheError("Existing content-addressed asset has an unexpected size")
                    return
                self.api("/releases/assets/" + str(asset["id"]), method="DELETE", missing=True)
            try:
                with path.open("rb") as stream:
                    return self.request(url, method="POST", data=stream, headers={"Content-Type": "application/octet-stream", "Content-Length": str(path.stat().st_size)})
            except CacheError as error:
                if attempt == 3 or not isinstance(error.__cause__, urllib.error.HTTPError) or error.__cause__.code != 422:
                    raise


class ReleaseCache:
    def __init__(self, backend, key, path, seed_key=""):
        self.tag = "cache"
        self.asset, self.receipt = cache_names(key)
        if seed_key and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,220}", seed_key):
            raise CacheError("Invalid compatibility cache key")
        self.backend, self.key, self.path, self.seed_key = backend, key, Path(path), seed_key

    def metadata(self, tag, name, temporary):
        destination = temporary / "metadata.json"
        if not self.backend.download(tag, name, destination):
            if name != self.receipt or not self.backend.download(tag, name + '.previous', destination):
                return None
        try:
            value = json.loads(destination.read_text())
        except (ValueError, OSError) as error:
            raise CacheError("Invalid cache metadata") from error
        if not isinstance(value, dict):
            raise CacheError("Invalid cache metadata")
        return value

    def validate(self, value, expected_key=None, compatibility=True):
        if value.get("schema") != 2 or not isinstance(value.get("key"), str):
            raise CacheError("Unsupported cache metadata")
        simple_asset, _ = cache_names(value["key"])
        if simple_asset != self.asset or value.get("asset") != simple_asset or value.get("tag") != self.tag:
            raise CacheError("Cache metadata does not identify its exact artifact")
        if expected_key is not None and value["key"] != expected_key:
            raise CacheError("Exact cache identity mismatch")
        if compatibility and self.seed_key and value.get("compatibility") != self.seed_key:
            raise CacheError("Cache compatibility identity mismatch")
        if not re.fullmatch(r"[a-f0-9]{64}", value.get("sha256", "")) or not isinstance(value.get("size"), int) or value["size"] < 1:
            raise CacheError("Cache metadata has no valid digest/size")
        if 'asset_id' in value and (type(value['asset_id']) is not int or value['asset_id'] < 1):
            raise CacheError('Cache metadata has an invalid asset ID')
        if 'parts' in value:
            parts = value['parts']
            if 'asset_id' in value or not isinstance(parts, list) or not 1 <= len(parts) <= 1000:
                raise CacheError('Invalid multipart cache metadata')
            ids = set()
            for index, part in enumerate(parts, 1):
                if (not isinstance(part, dict) or part.get('asset') != self.asset + '.part-' + str(index).zfill(4)
                        or type(part.get('asset_id')) is not int or part['asset_id'] < 1 or part['asset_id'] in ids
                        or type(part.get('size')) is not int or not 0 < part['size'] < MAX_ASSET_SIZE
                        or not isinstance(part.get('sha256'), str) or not re.fullmatch(r'[a-f0-9]{64}', part['sha256'])):
                    raise CacheError('Invalid multipart cache part')
                ids.add(part['asset_id'])
            if sum(part['size'] for part in parts) != value['size']:
                raise CacheError('Multipart cache size mismatch')

    def install(self, value, temporary):
        candidate = temporary / "buildroot.tar.zst"
        if 'parts' in value:
            with candidate.open('wb') as output:
                for part in value['parts']:
                    chunk = temporary / 'part'
                    if not self.backend.download_id(part['asset_id'], chunk):
                        raise CacheError('Compiled cache receipt points to a missing archive part: ' + part['asset'])
                    if chunk.stat().st_size != part['size'] or digest(chunk) != part['sha256']:
                        raise CacheError('Compiled buildroot cache part checksum mismatch: ' + part['asset'])
                    with chunk.open('rb') as source:
                        shutil.copyfileobj(source, output)
                    chunk.unlink()
            downloaded = True
        else:
            downloaded = (self.backend.download_id(value['asset_id'], candidate) if value.get('asset_id')
                          else self.backend.download(self.tag, value['asset'], candidate))
        if not downloaded:
            raise CacheError('Compiled cache receipt points to a missing archive; refusing a cold rebuild: ' + value['asset'])
        if (candidate.stat().st_size != value["size"] or digest(candidate) != value["sha256"]):
            raise CacheError("Compiled buildroot cache checksum mismatch")
        candidate.replace(self.path)
        return True

    def restore(self, required=False):
        for attempt in range(3):
            try:
                return self._restore(required)
            except (CacheError, urllib.error.URLError, TimeoutError, ConnectionError) as error:
                cause = error.__cause__ or error
                if isinstance(cause, urllib.error.HTTPError):
                    retryable = cause.code in [408, 429] or 500 <= cause.code < 600
                else:
                    retryable = isinstance(cause, (urllib.error.URLError, TimeoutError, ConnectionError))
                if not retryable:
                    raise
                if attempt < 2:
                    print("::warning::Cache service unavailable; retrying restore")
                    time.sleep(2 ** attempt)
                elif required:
                    raise
                else:
                    print("::warning::Cache service unavailable; continuing without a compiled cache")
        return {"cache-hit": "false", "seed-hit": "false", "cache-current": "false", "restored-key": ""}

    def _restore(self, required=False):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="buildroot-cache-", dir=self.path.parent) as directory:
            temporary = Path(directory)
            value = self.metadata(self.tag, self.receipt, temporary)
            if value is not None:
                self.validate(value, compatibility=False)
                if value["key"] == self.key:
                    self.validate(value, self.key, compatibility=False)
                    self.install(value, temporary)
                    return {"cache-hit": "true", "seed-hit": "false", "cache-current": "true", "restored-key": self.key}
                if not required and self.seed_key:
                    self.validate(value, compatibility=False)
                    self.install(value, temporary)
                    return {"cache-hit": "false", "seed-hit": "true", "cache-current": "false", "restored-key": value["key"]}
        if required:
            raise CacheError("No exact compiled buildroot cache found for " + self.key)
        return {"cache-hit": "false", "seed-hit": "false", "cache-current": "false", "restored-key": ""}

    def save(self):
        if not self.path.is_file() or self.path.stat().st_size < 1:
            raise CacheError("No compiled buildroot to save")
        checksum = digest(self.path)
        value = {"schema": 2, "key": self.key, "tag": self.tag, "asset": self.asset, "compatibility": self.seed_key or None, "sha256": checksum, "size": self.path.stat().st_size, "created_at": datetime.now(timezone.utc).isoformat()}
        release = self.backend.release(self.tag)
        suffix = '.upload-' + uuid.uuid4().hex[:12]
        if value['size'] >= MAX_ASSET_SIZE:
            return self.save_parts(value, release, suffix)
        staged = self.backend.find_asset(release, self.asset)
        if staged is None or staged.get('size') != value['size'] or staged.get('digest') != 'sha256:' + checksum:
            staged = self.backend.upload(release, self.asset + suffix, self.path)
        value['asset_id'] = staged['id']
        with tempfile.TemporaryDirectory(prefix="buildroot-cache-", dir=self.path.parent) as directory:
            metadata = Path(directory) / "metadata.json"
            metadata.write_text(json.dumps(value, sort_keys=True) + "\n")
            pointer = self.backend.upload(release, self.receipt + suffix, metadata)
        previous = self.backend.find_asset(release, self.receipt + '.previous')
        current = self.backend.find_asset(release, self.receipt)
        if current:
            if previous:
                self.backend.delete(previous['id'])
            self.backend.rename(current['id'], self.receipt + '.previous')
        # The committed pointer can restore by ID before the readable archive name changes.
        self.backend.rename(pointer['id'], self.receipt)
        current = self.backend.find_asset(release, self.asset)
        if current and current['id'] != staged['id']:
            self.backend.delete(current['id'])
        if staged['name'] != self.asset:
            self.backend.rename(staged['id'], self.asset)
        previous = self.backend.find_asset(release, self.receipt + '.previous')
        if previous:
            self.backend.delete(previous['id'])
        for asset in self.backend.list_assets(release):
            if asset['id'] != staged['id'] and asset['name'].startswith((self.asset + '.upload-', self.asset + '.part-', self.receipt + '.upload-')):
                self.backend.delete(asset['id'])
        return value

    def save_parts(self, value, release, suffix):
        value['parts'] = []
        staged_parts = []
        with tempfile.TemporaryDirectory(prefix='buildroot-cache-', dir=self.path.parent) as directory:
            temporary = Path(directory)
            with self.path.open('rb') as source:
                remaining = value['size']
                while remaining:
                    name = self.asset + '.part-' + str(len(value['parts']) + 1).zfill(4)
                    chunk = temporary / 'part'
                    size = min(remaining, ASSET_PART_SIZE)
                    with chunk.open('wb') as output:
                        pending = size
                        while pending:
                            block = source.read(min(pending, 1024 * 1024))
                            if not block:
                                raise CacheError('Compiled cache archive changed during upload')
                            output.write(block)
                            pending -= len(block)
                    checksum = digest(chunk)
                    staged = self.backend.find_asset(release, name)
                    if staged is None or staged.get('size') != size or staged.get('digest') != 'sha256:' + checksum:
                        staged = self.backend.upload(release, name + suffix, chunk)
                    value['parts'].append(dict(asset=name, asset_id=staged['id'], size=size, sha256=checksum))
                    staged_parts.append(staged)
                    remaining -= size
            metadata = temporary / 'metadata.json'
            metadata.write_text(json.dumps(value, sort_keys=True) + '\n')
            pointer = self.backend.upload(release, self.receipt + suffix, metadata)
        previous = self.backend.find_asset(release, self.receipt + '.previous')
        current = self.backend.find_asset(release, self.receipt)
        if current:
            if previous:
                self.backend.delete(previous['id'])
            self.backend.rename(current['id'], self.receipt + '.previous')
        self.backend.rename(pointer['id'], self.receipt)
        # The receipt commits every part ID before any previous archive is removed.
        retained = {part['asset_id'] for part in value['parts']}
        for asset in self.backend.list_assets(release):
            if (asset['id'] not in retained and (asset['name'] == self.asset
                    or asset['name'].startswith((self.asset + '.part-', self.asset + '.upload-', self.receipt + '.upload-')))):
                self.backend.delete(asset['id'])
        for part, staged in zip(value['parts'], staged_parts):
            if staged['name'] != part['asset']:
                self.backend.rename(staged['id'], part['asset'])
        previous = self.backend.find_asset(release, self.receipt + '.previous')
        if previous:
            self.backend.delete(previous['id'])
        return value


def main(environment=None):
    environment = os.environ if environment is None else environment
    cache = ReleaseCache(GitHub(environment["REPO"], environment.get("GH_TOKEN", "")), environment["KEY"], environment.get("LOCAL_FILE", "buildroot.tar.zst"), environment.get("SEED_KEY", ""))
    if environment["MODE"] == "restore":
        output = cache.restore(environment.get("FAIL_ON_MISS", "false").lower() == "true")
        destination = environment.get("GITHUB_OUTPUT")
        if destination:
            with open(destination, "a") as stream:
                stream.write("".join(name + "=" + value + "\n" for name, value in output.items()))
        print(json.dumps(output, sort_keys=True))
    elif environment["MODE"] == "save":
        value = cache.save()
        print("Saved " + value["tag"] + "/" + value["asset"])
    else:
        raise CacheError("Unknown cache operation")


if __name__ == "__main__":
    main()
