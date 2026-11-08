#!/usr/bin/env python3

import hashlib
import importlib.util
import json
import tempfile
import unittest
from unittest import mock
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("buildroot_cache", ROOT / ".github/actions/buildroot-cache/cache.py")
cache = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cache)


class MemoryBackend:
    def __init__(self):
        self.assets = {}
        self.events = []

    def tags(self):
        return sorted({tag for tag, asset in self.assets}, reverse=True)

    def download(self, tag, name, destination):
        self.events.append(("download", tag, name))
        if (tag, name) not in self.assets:
            return False
        Path(destination).write_bytes(self.assets[tag, name])
        return True

    def release(self, tag):
        return {"id": tag, "tag_name": tag}

    def upload(self, release, name, path, immutable=False):
        key = (release["tag_name"], name)
        self.events.append(("upload", key[0], name, immutable))
        if not immutable or key not in self.assets:
            self.assets[key] = Path(path).read_bytes()


class ReleaseCacheTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="buildroot-cache-test-")
        self.root = Path(self.temporary.name)
        self.backend = MemoryBackend()
        self.seed = "sdk-rpm-alma10-x86_64-compatible"
        self.key = "sdk-rpm-alma10-x86_64-source-a-2026-43"
        self.path = self.root / "buildroot.tar.zst"

    def tearDown(self):
        self.temporary.cleanup()

    def store(self, key=None, seed=None, body=b"compiled library payload"):
        self.path.write_bytes(body)
        return cache.ReleaseCache(self.backend, key or self.key, self.path, self.seed if seed is None else seed).save()

    def restore(self, key=None, seed=None, required=False):
        return cache.ReleaseCache(self.backend, key or self.key, self.path, self.seed if seed is None else seed).restore(required)

    def test_save_uploads_content_addressed_payload_then_exact_receipt_then_seed(self):
        value = self.store()
        events = [event for event in self.backend.events if event[0] == "upload"]
        self.assertEqual(len(events), 3)
        self.assertTrue(events[0][3])
        self.assertIn(hashlib.sha256(self.path.read_bytes()).hexdigest(), events[0][2])
        self.assertEqual(events[1][2], cache.split_key(self.key)[0] + ".json")
        self.assertEqual(events[2][2], self.seed + ".json")
        self.assertEqual(json.loads(self.backend.assets[value["tag"], events[2][2]]), value)

    def test_exact_hit_is_verified_and_current(self):
        self.store()
        self.path.unlink()
        result = self.restore()
        self.assertEqual(result, {"cache-hit": "true", "seed-hit": "false", "cache-current": "true", "restored-key": self.key})
        self.assertEqual(self.path.read_bytes(), b"compiled library payload")

    def test_unchanged_sdk_crosses_week_without_recompilation_and_can_be_promoted(self):
        value = self.store()
        self.path.unlink()
        newer = self.key.replace("2026-43", "2026-44")
        result = self.restore(newer)
        self.assertEqual(result["cache-hit"], "true")
        self.assertEqual(result["cache-current"], "false")
        self.assertEqual(result["restored-key"], self.key)
        saved = cache.ReleaseCache(self.backend, newer, self.path, self.seed).save()
        self.assertEqual(saved["sha256"], value["sha256"])
        self.assertEqual(self.restore(newer)["cache-current"], "true")

    def test_new_source_uses_previous_compatible_sdk_as_seed(self):
        self.store()
        self.path.unlink()
        newer = self.key.replace("source-a", "source-b")
        result = self.restore(newer)
        self.assertEqual(result["cache-hit"], "false")
        self.assertEqual(result["seed-hit"], "true")
        self.assertEqual(result["restored-key"], self.key)
        self.assertEqual(self.path.read_bytes(), b"compiled library payload")

    def test_consumer_never_accepts_seed_for_different_source(self):
        self.store()
        self.backend.events.clear()
        newer = self.key.replace("source-a", "source-b")
        with self.assertRaisesRegex(cache.CacheError, "No exact"):
            self.restore(newer, required=True)
        self.assertFalse(any(event[2] == self.seed + ".json" for event in self.backend.events))

    def test_wrong_compatibility_is_rejected_before_payload_download(self):
        value = self.store()
        value["compatibility"] = "different-toolchain"
        self.backend.assets[value["tag"], self.seed + ".json"] = json.dumps(value).encode()
        self.path.write_bytes(b"keep existing local data")
        with self.assertRaisesRegex(cache.CacheError, "compatibility"):
            self.restore(self.key.replace("source-a", "source-b"))
        self.assertEqual(self.path.read_bytes(), b"keep existing local data")

    def test_corrupt_tarball_is_rejected_without_replacing_local_file(self):
        value = self.store()
        self.backend.assets[value["tag"], value["asset"]] = b"corrupted compiled output"
        self.path.write_bytes(b"keep existing local data")
        with self.assertRaisesRegex(cache.CacheError, "checksum"):
            self.restore()
        self.assertEqual(self.path.read_bytes(), b"keep existing local data")
        self.assertEqual([p.name for p in self.root.iterdir()], [self.path.name])

    def test_pointer_cannot_redirect_to_an_unrelated_asset(self):
        value = self.store()
        value["asset"] = "other-platform.tar.zst"
        self.backend.assets[value["tag"], self.seed + ".json"] = json.dumps(value).encode()
        with self.assertRaisesRegex(cache.CacheError, "exact artifact"):
            self.restore(self.key.replace("source-a", "source-b"))

    def test_miss_preserves_existing_path_and_returns_explicit_false_outputs(self):
        self.path.write_bytes(b"local user file")
        result = self.restore()
        self.assertEqual(result, {"cache-hit": "false", "seed-hit": "false", "cache-current": "false", "restored-key": ""})
        self.assertEqual(self.path.read_bytes(), b"local user file")

    def test_legacy_exact_cache_remains_available_to_existing_consumers(self):
        asset, tag = cache.split_key(self.key)
        self.backend.assets[tag, asset] = b"legacy exact SDK"
        self.assertEqual(self.restore(seed="", required=True)["cache-hit"], "true")
        self.assertEqual(self.path.read_bytes(), b"legacy exact SDK")
        self.assertEqual(self.restore()["cache-hit"], "false")

    def test_exact_receipt_cannot_claim_a_different_source_key(self):
        value = self.store()
        asset, tag = cache.split_key(self.key)
        value["key"] = self.key.replace("source-a", "source-z")
        value["asset"] = cache.split_key(value["key"])[0]
        self.backend.assets[tag, asset + ".json"] = json.dumps(value).encode()
        with self.assertRaisesRegex(cache.CacheError, "identity"):
            self.restore()

    def test_invalid_key_is_rejected(self):
        for key in ["../escape-2026-43", "no-week", "sdk-2026-54", "sdk-2026-00", "s" * 200 + "-2026-43"]:
            with self.subTest(key=key), self.assertRaises(cache.CacheError):
                cache.ReleaseCache(self.backend, key, self.path, self.seed)

    def unavailable(self, code=504):
        error = cache.CacheError("Cache download failed: HTTP " + str(code))
        error.__cause__ = cache.urllib.error.HTTPError("https://github.com/cache", code, "unavailable", {}, None)
        error.__cause__.close()
        return error

    def test_transient_cache_failure_retries_and_restores_verified_payload(self):
        self.store()
        self.path.unlink()
        download = self.backend.download
        attempts = 0

        def flaky(*args):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise self.unavailable()
            return download(*args)

        with mock.patch.object(self.backend, "download", side_effect=flaky), mock.patch.object(cache.time, "sleep") as sleep:
            self.assertEqual(self.restore()["cache-hit"], "true")
            sleep.assert_called_once_with(1)
        self.assertEqual(self.path.read_bytes(), b"compiled library payload")

    def test_optional_cache_outage_is_bounded_and_preserves_local_data(self):
        self.path.write_bytes(b"local user file")
        with mock.patch.object(self.backend, "download", side_effect=self.unavailable()) as download, mock.patch.object(cache.time, "sleep") as sleep:
            result = self.restore()
        self.assertEqual(download.call_count, 3)
        self.assertEqual(sleep.call_args_list, [mock.call(1), mock.call(2)])
        self.assertEqual(result, {"cache-hit": "false", "seed-hit": "false", "cache-current": "false", "restored-key": ""})
        self.assertEqual(self.path.read_bytes(), b"local user file")
        self.assertEqual([p.name for p in self.root.iterdir()], [self.path.name])

    def test_required_cache_outage_remains_fatal_after_bounded_retries(self):
        with mock.patch.object(self.backend, "download", side_effect=self.unavailable()) as download, mock.patch.object(cache.time, "sleep"):
            with self.assertRaises(cache.CacheError):
                self.restore(required=True)
        self.assertEqual(download.call_count, 3)
        self.assertFalse(self.path.exists())

    def test_authentication_errors_remain_fatal_without_retry(self):
        with mock.patch.object(self.backend, "download", side_effect=self.unavailable(403)) as download, mock.patch.object(cache.time, "sleep") as sleep:
            with self.assertRaises(cache.CacheError):
                self.restore()
        self.assertEqual(download.call_count, 1)
        sleep.assert_not_called()

    def test_cache_listing_timeout_can_fall_back_to_a_fresh_build(self):
        with mock.patch.object(self.backend, "tags", side_effect=TimeoutError("timed out")) as tags, mock.patch.object(cache.time, "sleep"):
            self.assertEqual(self.restore()["cache-hit"], "false")
        self.assertEqual(tags.call_count, 3)


class RacingGitHub(cache.GitHub):
    def __init__(self, body, persistent_failure=False):
        super().__init__("owner/repository", "unused-test-token")
        self.body, self.persistent_failure = body, persistent_failure
        self.assets, self.uploads, self.deletions = {}, 0, 0

    def api(self, suffix, **kwargs):
        if kwargs.get("method") == "DELETE":
            self.deletions += 1
            if not kwargs.get("missing"):
                raise AssertionError("Concurrent asset deletion must tolerate 404")
            self.assets.clear()
            return None
        return list(self.assets.values())

    def request(self, url, method="GET", data=None, headers=None, **kwargs):
        self.uploads += 1
        assert hasattr(data, "read"), "Large SDK uploads must stream an open file"
        assert data.read() == self.body
        assert headers["Content-Length"] == str(len(self.body))
        name = cache.urllib.parse.parse_qs(cache.urllib.parse.urlparse(url).query)["name"][0]
        if self.uploads == 1 or self.persistent_failure:
            self.assets[name] = {"id": 17, "name": name, "size": len(self.body)}
            try:
                raise cache.urllib.error.HTTPError(url, 422, "concurrent asset upload", {}, None)
            except cache.urllib.error.HTTPError as error:
                raise cache.CacheError("GitHub cache request failed: HTTP 422") from error
        self.assets[name] = {"id": 18, "name": name, "size": len(self.body)}
        return self.assets[name]


class UploadRaceTests(unittest.TestCase):
    def test_concurrent_immutable_payload_is_accepted(self):
        with tempfile.TemporaryDirectory(prefix="buildroot-cache-race-") as temporary:
            path = Path(temporary) / "sdk"
            path.write_bytes(b"compiled SDK")
            github = RacingGitHub(path.read_bytes())
            github.upload({"id": 1}, "content-addressed.tar.zst", path, immutable=True)
            self.assertEqual(github.uploads, 1)
            self.assertEqual(github.deletions, 0)

    def test_concurrent_pointer_upload_is_retried(self):
        with tempfile.TemporaryDirectory(prefix="buildroot-cache-race-") as temporary:
            path = Path(temporary) / "metadata.json"
            path.write_bytes(b'{"schema":1}')
            github = RacingGitHub(path.read_bytes())
            github.upload({"id": 1}, "seed.json", path)
            self.assertEqual(github.uploads, 2)
            self.assertEqual(github.deletions, 1)

    def test_continued_conflicts_are_bounded(self):
        with tempfile.TemporaryDirectory(prefix="buildroot-cache-race-") as temporary:
            path = Path(temporary) / "metadata.json"
            path.write_bytes(b'{"schema":1}')
            github = RacingGitHub(path.read_bytes(), persistent_failure=True)
            with self.assertRaises(cache.CacheError):
                github.upload({"id": 1}, "seed.json", path)
            self.assertEqual(github.uploads, 4)


if __name__ == "__main__":
    unittest.main()
