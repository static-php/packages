#!/usr/bin/env python3

import hashlib
import http.server
import importlib.util
import json
import os
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("pinned_source", ROOT / ".github/actions/shared-libraries/pinned-source.py")
pinned = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pinned)


class PinnedSourceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="pinned-library-sources-")
        self.root = Path(self.temporary.name)
        self.routes = {}
        self.requests = []
        owner = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                owner.requests.append(self.path)
                if self.path not in owner.routes:
                    self.send_error(404)
                    return
                value = owner.routes[self.path]
                self.send_response(200)
                self.end_headers()
                self.wfile.write(value)

            def log_message(self, *args):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = "http://127.0.0.1:" + str(self.server.server_port)
        self.old_api = pinned.GITHUB_API
        pinned.GITHUB_API = self.url

    def tearDown(self):
        pinned.GITHUB_API = self.old_api
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.temporary.cleanup()

    def record(self, config=None, body=b"old library release", **fields):
        return {"source": {"cache_type": "archive", "filename": "lib-1.tar.gz", "hash": hashlib.sha1(body).hexdigest(),
                           "version": "v1", "config": config or {"type": "url", "url": self.url + "/lib-1.tar.gz"}, **fields}}

    def ensure(self, record):
        return pinned.ensure_sources(self.root, {"lib": record}, ["lib"])

    def test_release_uses_exact_tag_and_exact_asset_filename(self):
        self.routes["/repos/org/lib/releases/tags/v1"] = json.dumps({"tag_name": "v1", "assets": [
            {"name": "lib-2.tar.gz", "browser_download_url": self.url + "/latest"},
            {"name": "lib-1.tar.gz", "browser_download_url": self.url + "/old"}]}).encode()
        self.routes["/old"] = b"old library release"
        receipts = self.ensure(self.record({"type": "ghrel", "repo": "org/lib", "match": ".*"}))
        self.assertEqual(self.requests, ["/repos/org/lib/releases/tags/v1", "/old"])
        self.assertEqual(receipts[0]["source"], "download")
        self.assertEqual((self.root / "downloads/lib-1.tar.gz").read_bytes(), self.routes["/old"])

    def test_github_tarball_types_remain_at_recorded_tag(self):
        for method, path in (("ghtar", "/tarball-old"), ("ghtagtar", "/repos/org/lib/tarball/refs/tags/v1")):
            with self.subTest(method=method):
                (self.root / "downloads/lib-1.tar.gz").unlink(missing_ok=True)
                self.routes["/repos/org/lib/releases/tags/v1"] = json.dumps({"tag_name": "v1", "tarball_url": self.url + path}).encode()
                self.routes[path] = b"old library release"
                self.ensure(self.record({"type": method, "repo": "org/lib"}))
                self.assertEqual(self.requests[-1], path)

    def test_filelist_uses_recorded_filename_without_polling_listing(self):
        self.routes["/releases/lib-1.tar.gz"] = b"old library release"
        self.ensure(self.record({"type": "filelist", "url": self.url + "/releases/", "regex": ".*"}))
        self.assertEqual(self.requests, ["/releases/lib-1.tar.gz"])

    def test_filelist_template_uses_recorded_version(self):
        self.routes["/v1/lib-1.tar.gz"] = b"old library release"
        self.ensure(self.record({"type": "filelist", "download-url": self.url + "/{version}/{file}"}))
        self.assertEqual(self.requests, ["/v1/lib-1.tar.gz"])

    def test_download_hash_mismatch_does_not_leave_archive_or_temporary_file(self):
        self.routes["/lib-1.tar.gz"] = b"retagged incompatible library"
        with self.assertRaisesRegex(pinned.SourceError, "SHA1 mismatch"):
            self.ensure(self.record())
        self.assertEqual(list((self.root / "downloads").iterdir()), [])

    def test_existing_conflict_stays_byte_identical(self):
        downloads = self.root / "downloads"
        downloads.mkdir()
        path = downloads / "lib-1.tar.gz"
        path.write_bytes(b"new source snapshot")
        with self.assertRaisesRegex(pinned.SourceError, "conflicts"):
            self.ensure(self.record())
        self.assertEqual(path.read_bytes(), b"new source snapshot")
        self.assertEqual(self.requests, [])

    def test_existing_valid_archive_needs_no_upstream(self):
        downloads = self.root / "downloads"
        downloads.mkdir()
        (downloads / "lib-1.tar.gz").write_bytes(b"old library release")
        self.assertEqual(self.ensure(self.record())[0]["source"], "existing")
        self.assertEqual(self.requests, [])

    def test_missing_version_fails_before_any_fetch(self):
        for method in ("ghrel", "ghtar", "ghtagtar"):
            with self.subTest(method=method), self.assertRaisesRegex(pinned.SourceError, "recorded version"):
                self.ensure(self.record({"type": method, "repo": "org/lib"}, version=None))
        self.assertEqual(self.requests, [])

    def test_missing_exact_asset_never_selects_a_similar_release(self):
        self.routes["/repos/org/lib/releases/tags/v1"] = json.dumps({"tag_name": "v1", "assets": [
            {"name": "lib-2.tar.gz", "browser_download_url": self.url + "/latest"}]}).encode()
        with self.assertRaisesRegex(pinned.SourceError, "recorded filename"):
            self.ensure(self.record({"type": "ghrel", "repo": "org/lib"}))
        self.assertNotIn("/latest", self.requests)

    def test_unsafe_filename_is_rejected_before_fetch(self):
        for filename in ("../lib.tar.gz", "/lib.tar.gz", "sub/lib.tar.gz", "..", "sub\\lib.tar.gz"):
            with self.subTest(filename=filename), self.assertRaisesRegex(pinned.SourceError, "basename"):
                self.ensure(self.record(filename=filename))
        self.assertEqual(self.requests, [])

    def test_git_fetches_recorded_commit_while_branch_has_advanced(self):
        repository = self.root / "upstream"
        repository.mkdir()

        def git(*args):
            return subprocess.check_output(["git", "-C", str(repository), *args], stderr=subprocess.DEVNULL, text=True).strip()

        git("init", "--quiet")
        git("config", "user.name", "Test")
        git("config", "user.email", "test@example.invalid")
        (repository / "lib.c").write_text("old release\n")
        (repository / ".gitignore").write_text("ignored.c\n")
        git("add", "lib.c", ".gitignore")
        git("commit", "--quiet", "-m", "old")
        commit = git("rev-parse", "HEAD")
        (repository / "lib.c").write_text("new release\n")
        git("commit", "--quiet", "-am", "new")
        source = {"source": {"cache_type": "git", "dirname": "lib", "hash": commit,
                             "config": {"type": "git", "url": str(repository), "rev": "main", "submodules": []}}}
        self.ensure(source)
        self.assertEqual((self.root / "downloads/lib/lib.c").read_text(), "old release\n")
        self.assertEqual(self.ensure(source)[0]["source"], "existing")
        current = dict(source["source"], hash=git("rev-parse", "HEAD"))
        with self.assertRaisesRegex(pinned.SourceError, "conflicts"):
            self.ensure({"source": current})
        self.assertEqual((self.root / "downloads/lib/lib.c").read_text(), "old release\n")
        path = self.root / "downloads/lib/lib.c"
        path.write_text("local modification\n")
        with self.assertRaisesRegex(pinned.SourceError, "conflicts"):
            self.ensure(source)
        self.assertEqual(path.read_text(), "local modification\n")
        path.write_text("old release\n")
        extra = path.with_name("untracked.c")
        extra.write_text("untracked build source\n")
        with self.assertRaisesRegex(pinned.SourceError, "conflicts"):
            self.ensure(source)
        self.assertEqual(extra.read_text(), "untracked build source\n")
        extra.unlink()
        ignored = path.with_name("ignored.c")
        ignored.write_text("ignored generated source\n")
        with self.assertRaisesRegex(pinned.SourceError, "conflicts"):
            self.ensure(source)
        self.assertEqual(ignored.read_text(), "ignored generated source\n")

    def test_existing_git_accepts_foreign_owned_clean_snapshot(self):
        repository = self.root / "downloads/lib"
        repository.mkdir(parents=True)

        def git(*args):
            return subprocess.check_output(["git", "-C", str(repository), *args], stderr=subprocess.DEVNULL, text=True).strip()

        git("init", "--quiet")
        git("config", "user.name", "Test")
        git("config", "user.email", "test@example.invalid")
        (repository / "lib.c").write_text("pinned source\n")
        git("add", "lib.c")
        git("commit", "--quiet", "-m", "pinned")
        source = {"source": {"cache_type": "git", "dirname": "lib", "hash": git("rev-parse", "HEAD"),
                             "config": {"type": "git", "url": str(repository)}}}
        with patch.dict(os.environ, {"GIT_TEST_ASSUME_DIFFERENT_OWNER": "1"}):
            rejected = subprocess.run(["git", "-C", str(repository), "rev-parse", "HEAD"], capture_output=True, text=True)
            self.assertNotEqual(rejected.returncode, 0)
            self.assertIn("dubious ownership", rejected.stderr)
            self.assertEqual(self.ensure(source)[0]["source"], "existing")
            (repository / "lib.c").write_text("modified source\n")
            with self.assertRaisesRegex(pinned.SourceError, "conflicts"):
                self.ensure(source)
        self.assertEqual(self.requests, [])

    def test_existing_git_requires_initialized_recorded_submodules(self):
        repository = self.root / "downloads/lib"
        repository.mkdir(parents=True)

        def git(*args):
            return subprocess.check_output(["git", "-C", str(repository), *args], stderr=subprocess.DEVNULL, text=True).strip()

        git("init", "--quiet")
        git("config", "user.name", "Test")
        git("config", "user.email", "test@example.invalid")
        (repository / "lib.c").write_text("old release\n")
        git("add", "lib.c")
        git("commit", "--quiet", "-m", "old")
        child = git("rev-parse", "HEAD")
        (repository / ".gitmodules").write_text('[submodule "child"]\npath = child\nurl = ./child\n')
        git("add", ".gitmodules")
        git("update-index", "--add", "--cacheinfo", "160000," + child + ",child")
        git("commit", "--quiet", "-m", "submodule")
        source = {"source": {"cache_type": "git", "dirname": "lib", "hash": git("rev-parse", "HEAD"),
                             "config": {"type": "git", "url": str(repository)}}}
        with self.assertRaisesRegex(pinned.SourceError, "conflicts"):
            self.ensure(source)
        self.assertEqual(self.requests, [])
        self.assertFalse((repository / "child").exists())


if __name__ == "__main__":
    unittest.main()
