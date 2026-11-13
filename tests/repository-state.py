#!/usr/bin/env python3
import copy
import gzip
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('repository_state', ROOT / '.github/actions/shared-libraries/repository-state.py')
state = importlib.util.module_from_spec(spec)
spec.loader.exec_module(state)


class Store:
    def __init__(self):
        self.assets = {}
        self.downloads = []
        self.counter = 0
        self.fail_commit = False

    def download(self, tag, name, destination):
        self.downloads.append(name)
        if name not in self.assets:
            return False
        Path(destination).write_bytes(self.assets[name]['data'])
        return True

    def release(self, tag):
        return {'id': 1}

    def upload(self, release, name, path):
        self.counter += 1
        self.assets[name] = dict(id=self.counter, name=name, data=Path(path).read_bytes())
        return self.assets[name]

    def find_asset(self, release, name):
        return self.assets.get(name)

    def delete(self, identity):
        name = next(key for key, value in self.assets.items() if value['id'] == identity)
        del self.assets[name]

    def rename(self, identity, name):
        if self.fail_commit and not name.endswith('.previous'):
            raise RuntimeError('interrupted state write')
        old = next(key for key, value in self.assets.items() if value['id'] == identity)
        self.assets[name] = dict(self.assets.pop(old), name=name)


class RepositoryState(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.previous = Path.cwd()
        os.chdir(self.temporary.name)
        self.addCleanup(os.chdir, self.previous)
        self.store = Store()
        env = patch.dict(os.environ, GITHUB_ACTIONS='true', GITHUB_REPOSITORY='static-php/packages')
        env.start()
        self.addCleanup(env.stop)
        backend = patch.object(state, '_backend', self.store)
        backend.start()
        self.addCleanup(backend.stop)

    def fixture(self, kind='deb', arch='arm64', stream='8.6'):
        return dict(schema=1, type=kind, architecture=arch, php_stream=stream,
            baseline=dict(schema=1, linkage='shared', architecture='aarch64', libraries={
                'zlib': {'version': '1.3.1', 'source_hash': 'a', 'elf': {}, 'download_url': 'https://packages.example/zlib.deb',
                         'published_archive': 'archives/zlib.deb', 'published_archive_sha256': 'a' * 64}},
                consumer_inventory_complete=True), snapshots={stream: {'inventory': [{'name': 'php-zts-cli'}]}})

    def test_small_state_roundtrip_is_scoped_and_downloaded_once(self):
        value = self.fixture()
        self.store.upload({}, state.name('deb', 'arm64', '8.6'), self.json_file(value))
        self.assertEqual(state.load('deb', 'arm64', '8.6'), value)
        self.assertEqual(state.load('deb', 'arm64', '8.6'), value)
        self.assertEqual(len(self.store.downloads), 1)
        self.assertIsNone(state.load('deb', 'arm64', '8.5'))
        self.assertIsNone(state.load('deb', 'x86_64', '8.6'))
        self.assertNotEqual(state.name('rpm', 'arm64', '8.6'), state.name('deb', 'arm64', '8.6'))
        self.assertEqual(state.name('rpm', 'arm64', '8.2'), state.name('rpm', 'arm64', '8.6'))

    def json_file(self, value):
        path = Path('state.gz')
        path.write_bytes(gzip.compress(json.dumps(value).encode()))
        return path

    def test_interrupted_commit_keeps_previous_state_readable(self):
        original = self.fixture()
        state.save(original)
        changed = dict(original, publication_run_id='new')
        self.store.fail_commit = True
        with self.assertRaisesRegex(RuntimeError, 'interrupted'):
            state.save(changed)
        for path in Path('stage/repository-state').glob('*'):
            path.unlink()
        self.assertEqual(state.load('deb', 'arm64', '8.6'), original)

    def test_restore_uses_metadata_without_fetching_any_package(self):
        value = self.fixture()
        state.save(value)
        destination = Path('baseline')
        self.assertTrue(state.restore_baseline('deb', 'arm64', ['8.6'], destination, True))
        restored = json.loads((destination / 'shared-libraries.json').read_text())
        self.assertEqual(restored['libraries']['zlib']['download_url'], 'https://packages.example/zlib.deb')
        self.assertFalse(list(destination.rglob('*.deb')))
        self.assertEqual(self.store.downloads, [])

    def test_partial_state_does_not_claim_full_repository_coverage(self):
        value = self.fixture()
        value['baseline']['metadata_scope'] = ['zlib']
        state.save(value)
        self.assertFalse(state.restore_baseline('deb', 'arm64', ['8.6'], Path('full')))
        self.assertTrue(state.restore_baseline('deb', 'arm64', ['8.6'], Path('partial'), libraries=['zlib']))

    def test_selected_archive_is_fetched_once_and_its_checksum_is_enforced(self):
        content = b'published archive'
        entry = {'published_archive': 'archives/zlib.deb', 'download_url': 'https://example.com/zlib.deb',
                 'published_archive_sha256': hashlib.sha256(content).hexdigest()}
        fetches = []
        def fetch(url):
            fetches.append(url)
            return content
        with patch.object(state.runpy, 'run_path', return_value={'fetch': fetch}):
            path = state.materialize(Path('baseline'), entry)
            self.assertEqual(state.materialize(Path('baseline'), entry), path)
            self.assertEqual(fetches, [entry['download_url']])
            path.write_bytes(b'corrupt')
            with self.assertRaisesRegex(RuntimeError, 'checksum'):
                state.materialize(Path('baseline'), entry)

    def test_corrupt_or_foreign_state_is_rejected(self):
        value = self.fixture()
        value['architecture'] = 'x86_64'
        self.store.upload({}, state.name('deb', 'arm64', '8.6'), self.json_file(value))
        with self.assertRaisesRegex(RuntimeError, 'target'):
            state.load('deb', 'arm64', '8.6')

    def test_failed_upload_invalidates_only_its_repository(self):
        state.save(self.fixture())
        other = self.fixture(stream='8.5')
        state.save(other)
        state.invalidate('deb', 'arm64', '8.6')
        self.assertIsNone(state.load('deb', 'arm64', '8.6'))
        self.assertEqual(state.load('deb', 'arm64', '8.5'), other)

    def test_publication_updates_only_its_owner_and_requires_matching_native_bytes(self):
        before = self.fixture()
        before['baseline']['libraries']['zlib']['published_version'] = '1-r0'
        state.save(before)
        other = self.fixture(stream='8.5')
        state.save(other)
        source = Path('cohort/shared-libs-deb-alma8-arm64')
        (source / 'baseline').mkdir(parents=True)
        (source / 'baseline/shared-libraries.json').write_text(json.dumps(before['baseline']))
        archive = Path('dist/deb/zlib.deb')
        archive.parent.mkdir(parents=True)
        archive.write_bytes(b'new published library')
        debug_archive = archive.with_name('zlib-debuginfo.deb')
        debug_archive.write_bytes(b'new published debuginfo')
        package = {'name': 'php-zts-lib-zlib', 'version': '2-r0', 'architecture': 'arm64',
                   'publication_url': 'https://example.com/new-zlib.deb',
                   'published_checksum': {'algorithm': 'sha256', 'value': hashlib.sha256(archive.read_bytes()).hexdigest()}}
        debug_package = dict(package, name='php-zts-lib-zlib-debuginfo', publication_url='https://example.com/new-zlib-debuginfo.deb',
                             published_checksum={'algorithm': 'sha256', 'value': hashlib.sha256(debug_archive.read_bytes()).hexdigest()})
        planner = {'archive_identity': lambda path, kind: tuple((package if path == archive else debug_package)[field] for field in ('name', 'version', 'architecture')),
                   'native_compare': lambda kind, left, right: (left > right) - (left < right),
                   'archive_runtime_json': lambda *_: {'version': '1.3.2', 'source_hash': 'new-source', 'elf': {}},
                   'METADATA': 'usr/share/php-zts/libraries'}
        def published_inventory(args):
            return None
        freezer = {'published_inventory': published_inventory, 'fetch': lambda *_: None,
                   'snapshot_inventory': lambda args, refresh=False: {'inventory': [package, debug_package]}}
        with patch.dict(os.environ, MATRIX_ARCH='arm64'), patch.object(state.runpy, 'run_path', side_effect=[planner, freezer]):
            state.record_publication(Path('cohort'), 'deb', 'alma8-arm64', '8.6')
        self.assertEqual(state.load('deb', 'arm64', '8.6')['baseline']['libraries']['zlib']['source_hash'], 'new-source')
        self.assertTrue(state.load('deb', 'arm64', '8.6')['baseline']['libraries']['zlib']['published_debug_required'])
        self.assertEqual(state.load('deb', 'arm64', '8.5'), other)
        debug_archive.unlink()
        freezer['snapshot_inventory'] = lambda args, refresh=False: {'inventory': [package]}
        package['published_checksum']['value'] = '0' * 64
        with patch.dict(os.environ, MATRIX_ARCH='arm64'), patch.object(state.runpy, 'run_path', side_effect=[planner, freezer]):
            with self.assertRaisesRegex(RuntimeError, 'index differs'):
                state.record_publication(Path('cohort'), 'deb', 'alma8-arm64', '8.6')
        package.update(name='php-zts-cli', version='8.6.0~rc7-1')
        package['published_checksum']['value'] = hashlib.sha256(archive.read_bytes()).hexdigest()
        planner['archive_runtime_json'] = lambda *_: None
        planner['archive_migration_cutoffs'] = lambda *_: {'php_stream': '8.6', 'formats': {'deb': {'legacy': 5}}}
        with patch.dict(os.environ, MATRIX_ARCH='arm64'), patch.object(state.runpy, 'run_path', side_effect=[planner, freezer]):
            state.record_publication(Path('cohort'), 'deb', 'alma8-arm64', '8.6')
        self.assertEqual(state.load('deb', 'arm64', '8.6')['baseline']['migration_cutoffs'], {'8.6': {'legacy': 5}})
        old = dict(package, version='8.6.0~rc1-1')
        freezer['snapshot_inventory'] = lambda args, refresh=False: {'inventory': [old, package]}
        planner['native_compare'] = lambda kind, left, right: 0 if left == right else (_ for _ in ()).throw(AssertionError('Unchanged historical packages must not be rescanned'))
        with patch.dict(os.environ, MATRIX_ARCH='arm64'), patch.object(state.runpy, 'run_path', side_effect=[planner, freezer]):
            state.record_publication(Path('cohort'), 'deb', 'alma8-arm64', '8.6')
        self.assertEqual(state.load('deb', 'arm64', '8.6')['baseline']['consumer_versions']['8.6'], {'php-zts-cli': package['version']})


if __name__ == '__main__':
    unittest.main()
