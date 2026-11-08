#!/usr/bin/env python3
"""Recover the exact recorded sources for an SDK library rebuild."""

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import urllib.parse
import urllib.request


GITHUB_API = "https://api.github.com"


class SourceError(RuntimeError):
    pass


class _Redirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        redirected = super().redirect_request(request, fp, code, msg, headers, newurl)
        if redirected is not None and urllib.parse.urlsplit(request.full_url).netloc != urllib.parse.urlsplit(newurl).netloc:
            redirected.remove_header("Authorization")
        return redirected


def _open(url, accept=None):
    if not isinstance(url, str) or urllib.parse.urlsplit(url).scheme not in ("http", "https"):
        raise SourceError("Pinned source has no HTTP download URL")
    headers = {"User-Agent": "php-zts-pinned-library-source"}
    if accept:
        headers["Accept"] = accept
    if urllib.parse.urlsplit(url).netloc == urllib.parse.urlsplit(GITHUB_API).netloc:
        token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
        if token:
            headers["Authorization"] = "Bearer " + token
    try:
        return urllib.request.build_opener(_Redirect()).open(urllib.request.Request(url, headers=headers), timeout=120)
    except Exception:
        raise SourceError("Cannot fetch the pinned source; its recorded upstream URL must remain available") from None


def _json(url):
    with _open(url, "application/vnd.github+json") as response:
        try:
            return json.load(response)
        except (ValueError, UnicodeError):
            raise SourceError("Invalid metadata for the pinned GitHub release") from None


def _basename(value):
    if not isinstance(value, str) or not value or value in (".", "..") or Path(value).name != value or "\\" in value or "\x00" in value:
        raise SourceError("Recorded source filename or directory must be a basename")
    return value


def _hash(path):
    digest = hashlib.sha1()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _source(name, records):
    value = records.get(name)
    if not isinstance(value, dict):
        raise SourceError("Missing recorded source for " + name)
    source = value.get("source", value)
    if not isinstance(source, dict) or not isinstance(source.get("config"), dict):
        raise SourceError("Missing recorded source configuration for " + name)
    if not isinstance(source.get("hash"), str) or not re.fullmatch(r"[0-9a-fA-F]{40}", source["hash"]):
        raise SourceError("Missing exact recorded SHA1 or commit for " + name)
    return source


def _version(source):
    value = source.get("version")
    if not isinstance(value, str) or not value.strip():
        raise SourceError("Pinned GitHub or version-template source has no recorded version")
    return value


def _url(source):
    config = source["config"]
    method = config.get("type")
    filename = _basename(source.get("filename"))
    if method in ("ghrel", "ghtar", "ghtagtar"):
        version = _version(source)
        repo = config.get("repo")
        if not isinstance(repo, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
            raise SourceError("Pinned GitHub source has no valid recorded repository")
        base = GITHUB_API + "/repos/" + repo
        tag = urllib.parse.quote(version, safe="")
        if method == "ghtagtar":
            return base + "/tarball/refs/tags/" + tag
        release = _json(base + "/releases/tags/" + tag)
        if not isinstance(release, dict) or release.get("tag_name") != version:
            raise SourceError("Pinned GitHub release does not match its recorded version")
        if method == "ghtar":
            return release.get("tarball_url")
        assets = release.get("assets", [])
        if isinstance(assets, list):
            for asset in assets:
                if isinstance(asset, dict) and asset.get("name") == filename:
                    return asset.get("url") or asset.get("browser_download_url")
        raise SourceError("Pinned GitHub release no longer contains the recorded filename")
    if method == "url":
        return config.get("url")
    if method == "filelist":
        template = config.get("download-url")
        if template is not None:
            if not isinstance(template, str):
                raise SourceError("Invalid recorded file-list URL template")
            if "{version}" in template:
                template = template.replace("{version}", urllib.parse.quote(_version(source), safe=""))
            return template.replace("{file}", urllib.parse.quote(filename, safe=""))
        base = config.get("url")
        if isinstance(base, str):
            return base + urllib.parse.quote(filename, safe="")
    raise SourceError("Cannot recover recorded source downloader " + str(method) + " without changing its version")


def _git(args, directory):
    result = subprocess.run(["git", "-c", "safe.directory=" + str(Path(directory).resolve()), "-C", str(directory), *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            env=dict(os.environ, GIT_TERMINAL_PROMPT="0"))
    if result.returncode:
        raise SourceError("Cannot materialize the exact recorded Git commit")
    return result.stdout.strip()


def _clean_git(path, commit, submodules):
    if _git(["rev-parse", "HEAD"], path).lower() != commit or _git(["status", "--porcelain", "--untracked-files=all", "--ignored=matching", "--ignore-submodules=none"], path):
        return False
    if submodules == []:
        return True
    args = ["submodule", "status", "--recursive"] if submodules is None else ["submodule", "status", "--", *submodules]
    # Keep the leading status marker: strip() would hide an unmodified first submodule.
    result = subprocess.run(["git", "-c", "safe.directory=" + str(path.resolve()), "-C", str(path), *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    return result.returncode == 0 and all(line.startswith(" ") for line in result.stdout.splitlines())


def _git_source(downloads, source):
    name = _basename(source.get("dirname"))
    path = downloads / name
    commit = source["hash"].lower()
    config = source["config"]
    url = config.get("url")
    if not isinstance(url, str) or not url:
        raise SourceError("Pinned Git source has no recorded repository URL")
    submodules = config.get("submodules")
    if submodules is not None and (not isinstance(submodules, list) or any(not isinstance(value, str) or not value or Path(value).is_absolute() or ".." in Path(value).parts for value in submodules)):
        raise SourceError("Pinned Git source has invalid recorded submodules")
    if path.exists() or path.is_symlink():
        if path.is_symlink() or not path.is_dir() or not (path / ".git").exists() or not _clean_git(path, commit, submodules):
            raise SourceError("Recorded Git directory conflicts with this source snapshot; use a fresh snapshot")
        return "existing", name
    temporary = Path(tempfile.mkdtemp(prefix=".pinned-git-", dir=downloads))
    try:
        _git(["init", "--quiet"], temporary)
        _git(["remote", "add", "origin", url], temporary)
        _git(["fetch", "--quiet", "--depth=1", "origin", commit], temporary)
        _git(["checkout", "--quiet", "--detach", "FETCH_HEAD"], temporary)
        if _git(["rev-parse", "HEAD"], temporary).lower() != commit:
            raise SourceError("Fetched Git source differs from its recorded commit")
        if submodules is None:
            _git(["submodule", "update", "--init", "--recursive", "--depth=1"], temporary)
        elif submodules:
            _git(["submodule", "update", "--init", "--depth=1", "--", *submodules], temporary)
        if not _clean_git(temporary, commit, submodules):
            raise SourceError("Fetched Git working tree differs from its recorded source")
        temporary.rename(path)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
    return "download", name


def ensure_sources(root: Path, records: dict, artifacts: list):
    """Make exact recorded sources available without overwriting snapshot inputs."""
    downloads = Path(root) / "downloads"
    if downloads.is_symlink():
        raise SourceError("Source snapshot downloads directory must not be a symlink")
    downloads.mkdir(parents=True, exist_ok=True)
    receipts = []
    for name in dict.fromkeys(artifacts):
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_.+-]+", name):
            raise SourceError("Invalid recorded source artifact name")
        source = _source(name, records)
        cache_type = source.get("cache_type")
        expected = source["hash"].lower()
        if cache_type == "git":
            origin, filename = _git_source(downloads, source)
        elif cache_type in ("archive", "file"):
            filename = _basename(source.get("filename"))
            destination = downloads / filename
            if destination.exists() or destination.is_symlink():
                if destination.is_symlink() or not destination.is_file() or _hash(destination) != expected:
                    raise SourceError("Recorded source filename conflicts with this source snapshot: " + filename + "; use a fresh snapshot")
                origin = "existing"
            else:
                url = _url(source)
                descriptor, temporary_name = tempfile.mkstemp(prefix=".pinned-source-", dir=downloads)
                temporary = Path(temporary_name)
                try:
                    accept = "application/octet-stream" if source["config"].get("type") == "ghrel" else None
                    with os.fdopen(descriptor, "wb") as output, _open(url, accept) as response:
                        shutil.copyfileobj(response, output, 1024 * 1024)
                    if _hash(temporary) != expected:
                        raise SourceError("Pinned source SHA1 mismatch for " + name + "; refusing a different source payload")
                    temporary.rename(destination)
                finally:
                    temporary.unlink(missing_ok=True)
                origin = "download"
        else:
            raise SourceError("Cannot recover recorded source cache type " + str(cache_type))
        receipts.append({"artifact": name, "cache_type": cache_type, "filename": filename, "hash": expected, "source": origin})
    return receipts
