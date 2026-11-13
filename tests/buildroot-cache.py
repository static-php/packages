#!/usr/bin/env python3

import hashlib
import copy
import importlib.util
import io
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
        self.ids = {}
        self.next_id = 1
        self.interrupt_after = None

    def event(self, *value):
        self.events.append(value)
        if self.interrupt_after is not None and len(self.events) == self.interrupt_after:
            raise InterruptedError('simulated cancellation')

    def record(self, key):
        if key not in self.assets:
            return None
        if key not in self.ids:
            self.ids[key] = self.next_id
            self.next_id += 1
        return {'id': self.ids[key], 'name': key[1], 'size': len(self.assets[key]),
                'digest': 'sha256:' + hashlib.sha256(self.assets[key]).hexdigest()}

    def list_assets(self, release):
        return [self.record(key) for key in self.assets if key[0] == release['tag_name']]

    def find_asset(self, release, name):
        return self.record((release['tag_name'], name))

    def rename(self, asset_id, name):
        key = next(key for key, value in self.ids.items() if value == asset_id)
        destination = (key[0], name)
        if destination in self.assets:
            raise cache.CacheError('Asset name already exists')
        self.assets[destination] = self.assets.pop(key)
        self.ids[destination] = self.ids.pop(key)
        self.event('rename', asset_id, name)

    def delete(self, asset_id):
        key = next(key for key, value in self.ids.items() if value == asset_id)
        self.assets.pop(key, None)
        self.ids.pop(key)
        self.event('delete', asset_id)

    def download_id(self, asset_id, destination):
        key = next((key for key, value in self.ids.items() if value == asset_id), None)
        if key not in self.assets:
            return False
        Path(destination).write_bytes(self.assets[key])
        return True

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
        if not immutable or key not in self.assets:
            self.assets[key] = Path(path).read_bytes()
            self.ids.pop(key, None)
        result = self.record(key)
        self.event('upload', key[0], name, immutable)
        return result


class ReleaseCacheTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="buildroot-cache-test-")
        self.root = Path(self.temporary.name)
        self.backend = MemoryBackend()
        self.seed = "buildroot-shared-seed-glibc-alma8-x86_64-" + "c" * 32
        self.key = "buildroot-shared-glibc-alma8-x86_64-" + "a" * 32
        self.path = self.root / "buildroot.tar.zst"

    def tearDown(self):
        self.temporary.cleanup()

    def store(self, key=None, seed=None, body=b"compiled library payload"):
        self.path.write_bytes(body)
        return cache.ReleaseCache(self.backend, key or self.key, self.path, self.seed if seed is None else seed).save()

    def restore(self, key=None, seed=None, required=False):
        return cache.ReleaseCache(self.backend, key or self.key, self.path, self.seed if seed is None else seed).restore(required)

    def test_save_uses_one_readable_archive_and_one_receipt(self):
        value = self.store()
        events = [event for event in self.backend.events if event[0] == "upload"]
        self.assertEqual(len(events), 2)
        self.assertFalse(events[0][3])
        self.assertTrue(events[0][2].startswith('buildroot-glibc-alma8-x86_64.tar.zst.upload-'))
        self.assertTrue(events[1][2].startswith('buildroot-glibc-alma8-x86_64.json.upload-'))
        receipt = cache.cache_names(self.key)[1]
        self.assertEqual(json.loads(self.backend.assets[value['tag'], receipt]), value)
        self.assertEqual(set(self.backend.assets), {('cache', value['asset']), ('cache', receipt)})
        self.assertEqual(value['sha256'], hashlib.sha256(self.path.read_bytes()).hexdigest())
        self.assertEqual(value['compatibility'], self.seed)
        self.assertEqual(value['tag'], 'cache')

    def test_asset_names_do_not_expose_fingerprints(self):
        for prefix, expected in [('buildroot-shared-apk-arm64', 'buildroot-apk-arm64'),
                                 ('buildroot-shared-glibc-alma8-arm64', 'buildroot-glibc-alma8-arm64'),
                                 ('php-payload-musl-aarch64-84', 'php-payload-musl-aarch64-84')]:
            key = prefix + '-' + 'a' * 32
            self.assertEqual(cache.cache_names(key), (expected + '.tar.zst', expected + '.json'))

    def test_new_source_replaces_the_same_slot_and_remains_a_compatible_seed(self):
        self.store()
        newer = self.key.replace('a' * 32, 'b' * 32)
        self.store(newer, body=b'updated library payload')
        self.assertEqual(len(self.backend.assets), 2)
        self.assertEqual(self.restore()['seed-hit'], 'true')
        self.assertEqual(self.path.read_bytes(), b'updated library payload')
        with self.assertRaisesRegex(cache.CacheError, 'No exact'):
            self.restore(required=True)

    def test_cancellation_at_each_replacement_boundary_keeps_a_restorable_sdk(self):
        self.store()
        original = copy.deepcopy(self.backend)
        newer = self.key.replace('a' * 32, 'b' * 32)
        for boundary in range(1, 8):
            with self.subTest(boundary=boundary):
                self.backend = copy.deepcopy(original)
                self.backend.events = []
                self.backend.interrupt_after = boundary
                with self.assertRaises(InterruptedError):
                    self.store(newer, body=b'updated library payload')
                self.backend.interrupt_after = None
                result = self.restore(newer)
                self.assertTrue(result['cache-hit'] == 'true' or result['seed-hit'] == 'true')
                self.assertIn(self.path.read_bytes(), [b'compiled library payload', b'updated library payload'])
                self.store(newer, body=b'updated library payload')
                self.assertEqual(len(self.backend.assets), 2)
                self.assertEqual(self.restore(newer)['cache-hit'], 'true')
                self.assertEqual(self.path.read_bytes(), b'updated library payload')

    def test_receipt_without_archive_fails_instead_of_starting_a_cold_rebuild(self):
        value = self.store()
        del self.backend.assets['cache', value['asset']]
        with self.assertRaisesRegex(cache.CacheError, 'missing archive; refusing a cold rebuild'):
            self.restore(self.key.replace('a' * 32, 'b' * 32))

    def test_metadata_only_update_reuses_the_uploaded_archive(self):
        value = self.store()
        self.backend.events = []
        updated = self.store(self.key.replace('a' * 32, 'b' * 32))
        self.assertEqual(updated['asset_id'], value['asset_id'])
        self.assertEqual(len([event for event in self.backend.events if event[0] == 'upload']), 1)
        self.assertEqual(len(self.backend.assets), 2)

    def test_partial_archive_or_metadata_upload_preserves_the_working_cache(self):
        self.store()
        original = copy.deepcopy(self.backend)
        newer = self.key.replace('a' * 32, 'b' * 32)
        for failed_upload in [1, 2]:
            with self.subTest(failed_upload=failed_upload):
                self.backend = copy.deepcopy(original)
                upload = self.backend.upload
                count = 0
                def interrupted(release, name, path, **kwargs):
                    nonlocal count
                    result = upload(release, name, path, **kwargs)
                    count += 1
                    if count == failed_upload:
                        self.backend.assets[release['tag_name'], name] = b'partial upload'
                        raise InterruptedError('connection closed during upload')
                    return result
                with mock.patch.object(self.backend, 'upload', interrupted), self.assertRaises(InterruptedError):
                    self.store(newer, body=b'updated library payload')
                self.assertEqual(self.restore(newer)['seed-hit'], 'true')
                self.assertEqual(self.path.read_bytes(), b'compiled library payload')
                self.store(newer, body=b'updated library payload')
                self.assertEqual(len(self.backend.assets), 2)

    def test_replaced_toolchain_restores_the_library_sdk_as_a_seed(self):
        self.store(seed='other-toolchain')
        result = self.restore(self.key.replace('a' * 32, 'b' * 32))
        self.assertEqual(result['cache-hit'], 'false')
        self.assertEqual(result['seed-hit'], 'true')
        self.assertEqual(self.path.read_bytes(), b'compiled library payload')

    def test_payload_seed_still_requires_matching_compilation_inputs(self):
        key = 'php-payload-glibc-aarch64-84-' + 'a' * 32
        self.store(key=key, seed='old-php-inputs')
        self.path.unlink()
        result = self.restore(key=key.replace('a' * 32, 'b' * 32), seed='new-php-inputs')
        self.assertEqual(result['seed-hit'], 'false')
        self.assertFalse(self.path.exists())

    def test_library_seed_cannot_cross_architectures_or_producers(self):
        self.store()
        self.path.unlink()
        for key in [self.key.replace('x86_64', 'arm64'), self.key.replace('glibc-alma8', 'apk')]:
            with self.subTest(key=key):
                self.assertEqual(self.restore(key=key)['seed-hit'], 'false')
                self.assertFalse(self.path.exists())

    def test_exact_hit_is_verified_and_current(self):
        self.store()
        self.path.unlink()
        result = self.restore()
        self.assertEqual(result, {"cache-hit": "true", "seed-hit": "false", "cache-current": "true", "restored-key": self.key})
        self.assertEqual(self.path.read_bytes(), b"compiled library payload")

    def test_current_receipt_without_asset_id_uses_verified_named_archive(self):
        value = self.store()
        del value['asset_id']
        self.backend.assets['cache', cache.cache_names(self.key)[1]] = json.dumps(value).encode()
        self.path.unlink()
        self.assertEqual(self.restore()['cache-hit'], 'true')
        self.assertEqual(self.path.read_bytes(), b'compiled library payload')
        self.backend.assets['cache', value['asset']] = b'corrupted payload'
        with self.assertRaisesRegex(cache.CacheError, 'checksum'):
            self.restore()

    def test_new_source_uses_previous_compatible_sdk_as_seed(self):
        self.store()
        self.path.unlink()
        newer = self.key.replace("a" * 32, "b" * 32)
        result = self.restore(newer)
        self.assertEqual(result["cache-hit"], "false")
        self.assertEqual(result["seed-hit"], "true")
        self.assertEqual(result["restored-key"], self.key)
        self.assertEqual(self.path.read_bytes(), b"compiled library payload")

    def test_consumer_never_accepts_seed_for_different_source(self):
        self.store()
        self.backend.events.clear()
        newer = self.key.replace("a" * 32, "b" * 32)
        with self.assertRaisesRegex(cache.CacheError, "No exact"):
            self.restore(newer, required=True)
        self.assertFalse(any(event[2] == self.seed + ".json" for event in self.backend.events))

    def test_wrong_compatibility_is_rejected_before_payload_download(self):
        value = self.store()
        value["compatibility"] = "different-toolchain"
        self.backend.assets[value["tag"], cache.cache_names(self.key)[1]] = json.dumps(value).encode()
        self.path.write_bytes(b"keep existing local data")
        with self.assertRaisesRegex(cache.CacheError, "compatibility"):
            self.restore()
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
        self.backend.assets[value["tag"], cache.cache_names(self.key)[1]] = json.dumps(value).encode()
        with self.assertRaisesRegex(cache.CacheError, "exact artifact"):
            self.restore(self.key.replace("a" * 32, "b" * 32))

    def test_miss_preserves_existing_path_and_returns_explicit_false_outputs(self):
        self.path.write_bytes(b"local user file")
        result = self.restore()
        self.assertEqual(result, {"cache-hit": "false", "seed-hit": "false", "cache-current": "false", "restored-key": ""})
        self.assertEqual(self.path.read_bytes(), b"local user file")

    def test_invalid_key_is_rejected(self):
        for key in ["../escape-2026-43", "no-week", "sdk-2026-54", "sdk-2026-00", "s" * 200 + "-2026-43"]:
            with self.subTest(key=key), self.assertRaises(cache.CacheError):
                cache.ReleaseCache(self.backend, key, self.path, self.seed)

    def unavailable(self, code=504):
        error = cache.CacheError("Cache download failed: HTTP " + str(code))
        error.__cause__ = cache.urllib.error.HTTPError("https://github.com/cache", code, "unavailable", {}, io.BytesIO())
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


class GitHubDownloadTests(unittest.TestCase):
    def test_missing_release_and_empty_release_do_not_request_download_urls(self):
        for release, calls in [(None, 1), ({"id": 17}, 2)]:
            with self.subTest(release=release):
                github = cache.GitHub("owner/repository")
                with mock.patch.object(github, "api", side_effect=[release, []]) as api, mock.patch.object(cache.urllib.request, "build_opener") as opener:
                    self.assertFalse(github.download("cache", "absent", "unused"))
                    self.assertFalse(github.download("cache", "other", "unused"))
                self.assertEqual(api.call_count, calls)
                opener.assert_not_called()

    def test_paginated_inventory_downloads_by_asset_id_and_is_reused(self):
        github = cache.GitHub("owner/repository", "test-token")
        first = [{"name": str(index), "id": index} for index in range(100)]
        second = [{"name": "sdk.tar.zst", "id": 123}]
        with tempfile.TemporaryDirectory() as temporary, mock.patch.object(github, "api", side_effect=[{"id": 17}, first, second]) as api, mock.patch.object(cache.urllib.request, "build_opener") as opener:
            opener.return_value.open.return_value = io.BytesIO(b"compiled SDK")
            path = Path(temporary) / "sdk"
            self.assertTrue(github.download("cache", "sdk.tar.zst", path))
            self.assertEqual(path.read_bytes(), b"compiled SDK")
            self.assertFalse(github.download("cache", "missing", path))
            request = opener.return_value.open.call_args.args[0]
            self.assertEqual(request.full_url, "https://api.github.com/repos/owner/repository/releases/assets/123")
            self.assertEqual(request.get_header("Accept"), "application/octet-stream")
            self.assertEqual(request.get_header("Authorization"), "Bearer test-token")
            self.assertEqual(api.call_count, 3)

    def test_asset_redirect_does_not_forward_github_token_to_storage(self):
        request = cache.urllib.request.Request("https://api.github.com/repos/owner/repository/releases/assets/123", headers={"Authorization": "Bearer test-token"})
        redirected = cache.AssetRedirectHandler().redirect_request(request, None, 302, "Found", {}, "https://release-assets.githubusercontent.com/asset")
        self.assertIsNone(redirected.get_header("Authorization"))


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
