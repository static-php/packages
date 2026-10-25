#!/usr/bin/env python3
"""Restore exact SDKs or compatible seeds from the weekly release cache."""

import hashlib
import json
import os
import re
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timezone
from pathlib import Path


class CacheError(RuntimeError):
    pass


def split_key(key):
    match = re.fullmatch(r"([A-Za-z0-9][A-Za-z0-9_.-]{0,170})-([0-9]{4})-([0-9]{2})", key)
    if not match:
        raise CacheError("Cache key must end in a valid ISO week: -YYYY-WW")
    prefix, year, week = match.groups()
    try:
        date.fromisocalendar(int(year), int(week), 1)
    except ValueError as error:
        raise CacheError("Invalid cache ISO week") from error
    return prefix + ".tar.zst", "cache-" + year + "-" + week


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
            raise CacheError("GitHub cache request failed: HTTP " + str(error.code)) from error

    def api(self, suffix, **kwargs):
        return self.request("https://api.github.com/repos/" + self.repository + suffix, **kwargs)

    def tags(self):
        tags = []
        for page in range(1, 11):
            releases = self.api("/releases?per_page=100&page=" + str(page))
            tags.extend(value["tag_name"] for value in releases if re.fullmatch(r"cache-[0-9]{4}-[0-9]{2}", value.get("tag_name", "")) and not value.get("draft"))
            if len(releases) < 100:
                break
        return sorted(set(tags), reverse=True)

    def download(self, tag, asset, destination):
        url = "https://github.com/" + self.repository + "/releases/download/" + urllib.parse.quote(tag, safe="") + "/" + urllib.parse.quote(asset, safe="")
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "buildroot-cache"}), timeout=180) as response, Path(destination).open("wb") as output:
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
                release = self.api("/releases", method="POST", data={"tag_name": tag, "name": "[cache] " + tag, "body": "Compiled buildroot caches.", "prerelease": True})
            except CacheError:
                release = self.api("/releases/tags/" + urllib.parse.quote(tag, safe=""), missing=True)
                if release is None:
                    raise
        return release

    def find_asset(self, release, name):
        for page in range(1, 101):
            assets = self.api("/releases/" + str(release["id"]) + "/assets?per_page=100&page=" + str(page))
            for asset in assets:
                if asset["name"] == name:
                    return asset
            if len(assets) < 100:
                break
        return None

    def upload(self, release, name, path, immutable=False):
        url = "https://uploads.github.com/repos/" + self.repository + "/releases/" + str(release["id"]) + "/assets?name=" + urllib.parse.quote(name, safe="")
        path = Path(path)
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
                    self.request(url, method="POST", data=stream, headers={"Content-Type": "application/octet-stream", "Content-Length": str(path.stat().st_size)})
                return
            except CacheError as error:
                if attempt == 3 or not isinstance(error.__cause__, urllib.error.HTTPError) or error.__cause__.code != 422:
                    raise


class ReleaseCache:
    def __init__(self, backend, key, path, seed_key=""):
        self.asset, self.tag = split_key(key)
        if seed_key and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,220}", seed_key):
            raise CacheError("Invalid compatibility cache key")
        self.backend, self.key, self.path, self.seed_key = backend, key, Path(path), seed_key

    def metadata(self, tag, name, temporary):
        destination = temporary / "metadata.json"
        if not self.backend.download(tag, name, destination):
            return None
        try:
            value = json.loads(destination.read_text())
        except (ValueError, OSError) as error:
            raise CacheError("Invalid cache metadata") from error
        if not isinstance(value, dict):
            raise CacheError("Invalid cache metadata")
        return value

    def validate(self, value, expected_key=None):
        if value.get("schema") != 1 or not isinstance(value.get("key"), str):
            raise CacheError("Unsupported cache metadata")
        asset, tag = split_key(value["key"])
        hashed_asset = asset.removesuffix(".tar.zst") + "-" + str(value.get("sha256")) + ".tar.zst"
        if value.get("asset") not in [asset, hashed_asset] or value.get("tag") != tag:
            raise CacheError("Cache metadata does not identify its exact artifact")
        if expected_key is not None and value["key"] != expected_key:
            raise CacheError("Exact cache identity mismatch")
        if self.seed_key and value.get("compatibility") != self.seed_key:
            raise CacheError("Cache compatibility identity mismatch")
        if not re.fullmatch(r"[a-f0-9]{64}", value.get("sha256", "")) or not isinstance(value.get("size"), int) or value["size"] < 1:
            raise CacheError("Cache metadata has no valid digest/size")

    def install(self, tag, asset, value, temporary):
        candidate = temporary / "buildroot.tar.zst"
        if not self.backend.download(tag, asset, candidate):
            return False
        if value is not None and (candidate.stat().st_size != value["size"] or digest(candidate) != value["sha256"]):
            raise CacheError("Compiled buildroot cache checksum mismatch")
        candidate.replace(self.path)
        return True

    def restore(self, required=False):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tags = [self.tag] + [tag for tag in self.backend.tags() if tag < self.tag]
        with tempfile.TemporaryDirectory(prefix="buildroot-cache-", dir=self.path.parent) as directory:
            temporary = Path(directory)
            for tag in tags:
                exact_key = self.key.rsplit("-", 2)[0] + "-" + tag.removeprefix("cache-")
                value = self.metadata(tag, self.asset + ".json", temporary)
                if value is not None:
                    self.validate(value, exact_key)
                elif self.seed_key:
                    continue
                if self.install(tag, value["asset"] if value is not None else self.asset, value, temporary):
                    return {"cache-hit": "true", "seed-hit": "false", "cache-current": str(tag == self.tag).lower(), "restored-key": exact_key}
            if not required and self.seed_key:
                for tag in tags:
                    value = self.metadata(tag, self.seed_key + ".json", temporary)
                    if value is None:
                        continue
                    self.validate(value)
                    if self.install(value["tag"], value["asset"], value, temporary):
                        return {"cache-hit": "false", "seed-hit": "true", "cache-current": "false", "restored-key": value["key"]}
        if required:
            raise CacheError("No exact compiled buildroot cache found for " + self.key)
        return {"cache-hit": "false", "seed-hit": "false", "cache-current": "false", "restored-key": ""}

    def save(self):
        if not self.path.is_file() or self.path.stat().st_size < 1:
            raise CacheError("No compiled buildroot to save")
        checksum = digest(self.path)
        asset = self.asset.removesuffix(".tar.zst") + "-" + checksum + ".tar.zst"
        value = {"schema": 1, "key": self.key, "tag": self.tag, "asset": asset, "compatibility": self.seed_key or None, "sha256": checksum, "size": self.path.stat().st_size, "created_at": datetime.now(timezone.utc).isoformat()}
        release = self.backend.release(self.tag)
        with tempfile.TemporaryDirectory(prefix="buildroot-cache-", dir=self.path.parent) as directory:
            metadata = Path(directory) / "metadata.json"
            metadata.write_text(json.dumps(value, sort_keys=True) + "\n")
            self.backend.upload(release, asset, self.path, immutable=True)
            self.backend.upload(release, self.asset + ".json", metadata)
            if self.seed_key:
                self.backend.upload(release, self.seed_key + ".json", metadata)
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
