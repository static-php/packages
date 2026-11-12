import contextlib
import gzip
import hashlib
import io
import json
import runpy
import shutil
import subprocess
import tarfile
import unittest
from threading import Barrier
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]
helper = runpy.run_path(str(ROOT / 'bin/forgejo-helper'))
publisher = runpy.run_path(str(ROOT / '.github/actions/shared-libraries/publish-forgejo.py'))
helper_globals = helper['upload_verified_archive'].__globals__


def apk_bytes(name, version, arch, signed=False, cutoffs=None):
    def member(filename, content):
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode='w') as archive:
            info = tarfile.TarInfo(filename)
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))
        return gzip.compress(stream.getvalue(), mtime=0)
    data = member('.PKGINFO', f'pkgname = {name}\npkgver = {version}\narch = {arch}\n'.encode())
    if cutoffs is not None:
        data += member(helper['MIGRATION_CUTOFF_PATH'], json.dumps(cutoffs).encode())
    return member('.SIGN.RSA.fixture', b'signature') + data if signed else data


def response(status=200, payload=None, data=b''):
    result = Mock(status_code=status)
    result.json.return_value = payload
    result.iter_content.return_value = [data]
    result.__enter__ = Mock(return_value=result)
    result.__exit__ = Mock(return_value=False)
    return result


class ForgejoPublication(unittest.TestCase):
    def setUp(self):
        helper_globals['VERIFIED_ARCHIVE_IDENTITIES'].clear()
        helper_globals['ARCHIVE_HEADER_IDENTITIES'].clear()
        client = patch.dict(helper_globals, {'registry_session': lambda: helper_globals['requests']})
        client.start()
        self.addCleanup(client.stop)
        indexes = patch.dict(publisher['publish'].__globals__, {'verify_debian_indexes': Mock(return_value=[])})
        indexes.start()
        self.addCleanup(indexes.stop)
        self.temporary = TemporaryDirectory(prefix='forgejo-publication-test-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def archive(self, name='php-zts-lib-ssl', version='3.6.0-r1', arch='x86_64'):
        path = self.root / f'{name}-{version}.{arch}.apk'
        path.write_bytes(apk_bytes(name, version, arch))
        return path

    def test_registry_connections_are_reused_within_each_worker(self):
        from threading import local
        with patch.dict(helper_globals, {'REGISTRY_SESSIONS': local()}), \
                patch.object(helper_globals['requests'], 'Session', side_effect=lambda: object()) as factory:
            first = helper['registry_session']()
            self.assertIs(helper['registry_session'](), first)
            with helper_globals['ThreadPoolExecutor'](max_workers=1) as pool:
                other = pool.submit(helper['registry_session']).result()
                self.assertIs(pool.submit(helper['registry_session']).result(), other)
            self.assertIsNot(first, other)
            self.assertEqual(factory.call_count, 2)

    def test_publication_reads_only_the_other_architecture_header(self):
        import os
        data = apk_bytes('php-zts-lib-ssl', '3.6.0-r1', 'aarch64', signed=True) + gzip.compress(os.urandom(1024 * 1024))
        digest = hashlib.sha256(data).hexdigest()
        stream = io.BytesIO(data)
        archive = response(data=data)
        archive.raw = stream
        files = [{'id': 42, 'name': 'php-zts-lib-ssl-3.6.0-r1.apk', 'sha256': digest}]
        with patch.object(helper_globals['requests'], 'get', side_effect=[response(payload=files), archive, response(payload=files)]) as get, \
                patch.dict(helper_globals, {'archive_embedded_metadata': Mock(side_effect=AssertionError('Full archive read'))}):
            first = helper['remote_package_archives']('https://example.com', '85', 'test', 'alpine', 'php-zts-lib-ssl', '3.6.0-r1', True)
            second = helper['remote_package_archives']('https://example.com', '86', 'test', 'alpine', 'php-zts-lib-ssl', '3.6.0-r1', True)
        self.assertEqual(first[0]['arch'], 'aarch64')
        self.assertEqual(second[0]['sha256'], digest)
        self.assertLess(stream.tell(), 256 * 1024)
        self.assertEqual(get.call_count, 3)

    @unittest.skipUnless(shutil.which('dpkg-deb'), 'native DEB reader unavailable')
    def test_deb_publication_stops_after_the_control_header(self):
        import os
        def tar_member(name, content):
            out = io.BytesIO()
            with tarfile.open(fileobj=out, mode='w:gz') as tar:
                entry = tarfile.TarInfo(name)
                entry.size = len(content)
                tar.addfile(entry, io.BytesIO(content))
            return out.getvalue()
        members = [('debian-binary', b'2.0\n'), ('control.tar.gz', tar_member('control',
            b'Package: header-test\nVersion: 1.0-1\nArchitecture: amd64\nMaintainer: Test <test@example.com>\nDescription: header test\n')),
            ('data.tar.gz', tar_member('usr/share/test', os.urandom(1024 * 1024)))]
        data = b'!<arch>\n'
        for name, body in members:
            data += (f'{name + "/":<16}{0:<12}{0:<6}{0:<6}{"100644":<8}{len(body):<10}`\n').encode() + body
            if len(body) % 2:
                data += b'\n'
        stream = io.BytesIO(data)
        archive = response()
        archive.iter_content.side_effect = lambda size: iter(lambda: stream.read(size), b'')
        self.assertEqual(helper['remote_archive_header'](archive, 'debian'), ('header-test', '1.0-1', 'amd64'))
        self.assertLess(stream.tell(), 256 * 1024)

    def test_disconnected_registry_listing_retries_the_same_page(self):
        requests = helper_globals['requests']
        with patch.object(requests, 'get', side_effect=[requests.ConnectionError('remote disconnected'),
                          response(payload=[{'name': 'library'}]), response(payload=[])]) as get, \
                patch.object(helper_globals['time'], 'sleep') as sleep, contextlib.redirect_stdout(io.StringIO()) as output:
            records = helper['get_remote_packages']('https://example.com', '84', 'test', 'alpine', 'library')
        self.assertEqual(records, [{'name': 'library'}])
        self.assertEqual([call.kwargs['params']['page'] for call in get.call_args_list], [1, 1, 2])
        sleep.assert_called_once_with(2)
        self.assertIn('attempt 2/3', output.getvalue())

    def test_upload_retries_rewind_the_complete_archive_and_verify_conflicts(self):
        path = self.archive()
        record = {'arch': 'x86_64', 'sha256': helper['archive_sha256'](path)}
        sent = []
        def put(url, **kwargs):
            sent.append(kwargs['files']['file'].read())
            if len(sent) == 1:
                raise helper_globals['requests'].ConnectionError('response lost after upload')
            return response(status=409)
        with patch.dict(helper_globals, {'get_remote_packages': lambda *args: [],
                        'remote_package_archives': Mock(side_effect=[[], [record]])}), \
                patch.object(helper_globals['requests'], 'put', side_effect=put), \
                patch.object(helper_globals['time'], 'sleep'), contextlib.redirect_stdout(io.StringIO()):
            helper['upload_verified_archive']('https://example.com', '84', 'test', 'alpine', path)
        self.assertEqual(sent, [path.read_bytes(), path.read_bytes()])

    def test_interrupted_archive_download_restarts_and_checks_full_digest(self):
        path = self.archive()
        data = path.read_bytes()
        broken = response()
        def chunks(size):
            yield data[:32]
            raise helper_globals['requests'].exceptions.ChunkedEncodingError('interrupted body')
        broken.iter_content.side_effect = chunks
        files = [{'id': 45, 'name': path.name, 'sha256': helper['archive_sha256'](path)}]
        with patch.dict(helper_globals, {'read_consumer_sidecar': lambda *args: None}), \
                patch.object(helper_globals['requests'], 'get', side_effect=[response(payload=files), broken, response(data=data)]) as get, \
                patch.object(helper_globals['time'], 'sleep'), contextlib.redirect_stdout(io.StringIO()):
            records = helper['remote_package_archives']('https://example.com', '84', 'test', 'alpine', 'php-zts-lib-ssl', '3.6.0-r1')
        self.assertEqual(records[0]['sha256'], helper['archive_sha256'](path))
        self.assertEqual(get.call_count, 3)

    def test_transient_http_errors_retry_but_authentication_errors_do_not(self):
        requests = helper_globals['requests']
        for status in (429, 500, 502, 503, 504, 401, 403):
            failed = response(status=status)
            failed.raise_for_status.side_effect = requests.HTTPError('HTTP ' + str(status))
            with self.subTest(status=status), patch.object(requests, 'get', side_effect=[failed, response(payload=[])]) as get, \
                    patch.object(helper_globals['time'], 'sleep'), contextlib.redirect_stdout(io.StringIO()):
                if status in (401, 403):
                    with self.assertRaises(requests.HTTPError):
                        helper['get_remote_packages']('https://example.com', '84', 'test', 'alpine')
                    self.assertEqual(get.call_count, 1)
                else:
                    self.assertEqual(helper['get_remote_packages']('https://example.com', '84', 'test', 'alpine'), [])
                    self.assertEqual(get.call_count, 2)

    def test_exhausted_connections_fail_with_request_context(self):
        with patch.object(helper_globals['requests'], 'get', side_effect=helper_globals['requests'].ConnectionError('disconnected')) as get, \
                patch.object(helper_globals['time'], 'sleep') as sleep, contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(RuntimeError, 'GET https://example.com/api/v1/packages/84 failed after 3 attempts'):
                helper['get_remote_packages']('https://example.com', '84', 'test', 'alpine')
        self.assertEqual(get.call_count, 3)
        self.assertEqual(sleep.call_count, 2)

    def test_server_failure_reports_registry_reason_without_dumping_response(self):
        failed = response(status=500, payload={'message': 'database is locked\nretry later', 'stack': 'long internal traceback'})
        failed.raise_for_status.side_effect = helper_globals['requests'].HTTPError('HTTP 500')
        printed = io.StringIO()
        with patch.object(helper_globals['requests'], 'get', side_effect=[failed, response(payload=[])]), \
                patch.object(helper_globals['time'], 'sleep'), contextlib.redirect_stdout(printed):
            helper['get_remote_packages']('https://example.com', '84', 'test', 'alpine')
        self.assertIn('HTTP 500; registry: database is locked retry later', printed.getvalue())
        self.assertNotIn('long internal traceback', printed.getvalue())

    def test_package_progress_is_flushed_before_each_upload(self):
        paths = [self.archive(name=name) for name in ('php-zts-cli', 'php-zts-lib-ssl')]
        printed = io.StringIO()
        seen = []
        def upload(host, owner, password, kind, path, **kwargs):
            seen.append((path, printed.getvalue()))
        helpers = dict(helper, get_remote_candidates=lambda *args: [],
                       verify_upload_versions=lambda host, owner, password, kind, paths, remote: paths,
                       upload_verified_archive=upload)
        with contextlib.redirect_stdout(printed):
            publisher['publish']('upload', 'https://example.com', 'test', 'alpine', {'84': paths}, helpers)
        for index, (path, output) in enumerate(seen, 1):
            self.assertIn(f'Uploading {index}/2: https://example.com 84/{path.name}', output)
            self.assertNotIn(f'Completed {index}/2:', output)
        self.assertIn('Completed 2/2:', printed.getvalue())

    def test_failed_debug_upload_does_not_cancel_remaining_packages_or_phases(self):
        paths = [self.archive(name=name) for name in (
            'php-zts-runtime-layout', 'php-zts-lib-grpc-debuginfo', 'php-zts-lib-zlib',
            'frankenphp', 'php-zts-cli')]
        for mirror in (False, True):
            attempted = []
            def upload(host, owner, password, kind, path, **kwargs):
                attempted.append(path)
                if 'grpc-debuginfo' in path.name:
                    raise RuntimeError('connection closed during upload')
            helpers = dict(helper, get_remote_candidates=lambda *args: [],
                           verify_upload_versions=lambda host, owner, password, kind, paths, remote: paths,
                           upload_verified_archive=upload)
            with self.subTest(mirror=mirror), contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaisesRegex(RuntimeError, '1 package uploads failed; remaining packages were attempted'):
                    publisher['publish']('upload', 'https://example.com', 'test', 'alpine', {'84': paths}, helpers, mirror=mirror)
            self.assertCountEqual(attempted, paths)

    def test_owner_listing_records_owner_and_reads_every_stream(self):
        listing = Mock(side_effect=lambda host, owner, password, kind: [{'name': 'library', 'version': owner}])
        with patch.dict(helper_globals, {'get_remote_packages': listing}):
            records = helper['get_remote_packages_for_owners']('https://example.com', publisher['STREAM_OWNERS'], 'test', 'alpine')
        self.assertEqual([record['owner'] for record in records], list(publisher['STREAM_OWNERS']))
        self.assertEqual(listing.call_count, 5)

    def test_mirror_dependency_versions_remain_scoped_to_the_selected_owner(self):
        cohort = self.root / 'cohort'
        baseline = cohort / 'shared-libs-apk-x86_64/baseline'
        libraries = {}
        for stream, revision in [('8.3', 1), ('8.6', 8)]:
            owner = baseline / 'owners' / stream
            owner.mkdir(parents=True)
            archive = self.archive(name='php-zts-lib-zlib', version='1.3.1-r' + str(revision))
            shutil.copy2(archive, owner / archive.name)
            libraries[stream] = {'zlib': {'published_archive': archive.name, 'published_version': '1.3.1-r' + str(revision),
                                        'published_archive_sha256': helper['archive_sha256'](archive)}}
            (owner / 'shared-libraries.json').write_text(json.dumps({'architecture': 'x86_64', 'libraries': libraries[stream]}))
        (baseline / 'shared-libraries.json').write_text(json.dumps({'architecture': 'x86_64',
            'libraries': libraries['8.6'], 'stream_libraries': libraries}))
        destination = self.root / 'mirror'
        destination.mkdir()
        with contextlib.redirect_stdout(io.StringIO()):
            publisher['stage_mirror_dependencies'](cohort, destination, 'apk', helper, ['83'])
        self.assertEqual({helper['package_metadata'](p, 'alpine')[:2] for p in destination.glob('*.apk')},
                         {('php-zts-lib-zlib', '1.3.1-r1')})
        with self.assertRaisesRegex(RuntimeError, 'Missing retained library owner baseline'):
            publisher['stage_mirror_dependencies'](cohort, destination, 'apk', helper, ['84'])

    def test_mirror_includes_retained_primary_libraries_without_replacing_candidates(self):
        cohort = self.root / 'cohort'
        baseline = cohort / 'shared-libs-apk-x86_64/baseline'
        baseline.mkdir(parents=True)
        destination = self.root / 'mirror'
        destination.mkdir()
        libraries = {}
        for identifier in ('ssl', 'zlib'):
            archive = self.archive(name='php-zts-lib-' + identifier)
            shutil.copy2(archive, baseline / archive.name)
            libraries[identifier] = {'published_archive': archive.name, 'published_version': '3.6.0-r1',
                                     'published_archive_sha256': helper['archive_sha256'](archive)}
        manifest = baseline / 'shared-libraries.json'
        manifest.write_text(json.dumps({'architecture': 'x86_64', 'libraries': libraries}))
        candidate = self.archive(name='php-zts-lib-zlib', version='3.6.0-r2')
        shutil.copy2(candidate, destination / candidate.name)
        with contextlib.redirect_stdout(io.StringIO()):
            publisher['stage_mirror_dependencies'](cohort, destination, 'apk', helper)
        self.assertEqual({helper['package_metadata'](p, 'alpine')[:2] for p in destination.glob('*.apk')},
                         {('php-zts-lib-ssl', '3.6.0-r1'), ('php-zts-lib-zlib', '3.6.0-r2')})
        ssl = destination / libraries['ssl']['published_archive']
        ssl.unlink()
        libraries['ssl']['published_debug_archive'] = {'host': 'https://primary.test', 'owner': '85',
            'name': 'php-zts-lib-ssl-debuginfo', 'package_version': '3.6.0-r1'}
        manifest.write_text(json.dumps({'architecture': 'x86_64', 'libraries': libraries}))
        mirrored_debug = dict(helper,
            remote_package_archives=lambda *_: [{'arch': 'x86_64', 'sha256': 'a' * 64}],
            get_remote_package_files=lambda *_: [{'sha256': 'a' * 64}])
        with patch.dict('os.environ', {'FORGEJO_PASSWORD': 'test'}), contextlib.redirect_stdout(io.StringIO()), \
                patch.object(publisher['stage_mirror_dependencies'].__globals__['runpy'], 'run_path') as loader:
            deferred = publisher['stage_mirror_dependencies'](cohort, destination, 'apk', mirrored_debug, ['85'])
            self.assertEqual(publisher['stage_mirror_debug'](cohort, destination, 'apk', mirrored_debug, ['85'], deferred), [])
        loader.assert_not_called()
        debug_archive = self.archive(name='php-zts-lib-ssl-debuginfo')
        missing_on_mirror = dict(mirrored_debug, get_remote_package_files=lambda *_: [])
        with patch.dict('os.environ', {'FORGEJO_PASSWORD': 'test'}), contextlib.redirect_stdout(io.StringIO()), \
                patch.object(publisher['stage_mirror_debug'].__globals__['runpy'], 'run_path',
                    return_value={'stage_debug_archive': lambda *a, **kw: {'path': str(debug_archive)}}):
            self.assertEqual(publisher['stage_mirror_debug'](cohort, destination, 'apk', missing_on_mirror, ['85'], deferred), [])
        self.assertTrue((destination / debug_archive.name).is_file())
        (destination / debug_archive.name).unlink()
        ssl.unlink()
        missing_debug = dict(mirrored_debug, remote_package_archives=lambda *_: [])
        with contextlib.redirect_stdout(io.StringIO()):
            deferred = publisher['stage_mirror_dependencies'](cohort, destination, 'apk', missing_debug, ['85'])
            failures = publisher['stage_mirror_debug'](cohort, destination, 'apk', missing_debug, ['85'], deferred)
        self.assertEqual(len(failures), 1)
        self.assertTrue(ssl.is_file())
        ssl.unlink()
        (baseline / libraries['ssl']['published_archive']).write_bytes(b'changed captured archive')
        with self.assertRaisesRegex(RuntimeError, 'checksum/path mismatch'):
            publisher['stage_mirror_dependencies'](cohort, destination, 'apk', helper)

    def test_runtime_mirror_upload_precedes_large_retained_debug_downloads(self):
        self.archive(name='php-zts-cli')
        self.archive(name='php-zts-lib-ssl')
        self.archive(name='php-zts-lib-ssl-debuginfo')
        events = []
        def publish(action, host, password, kind, groups, helpers, mirror):
            self.assertTrue(mirror, 'An incomplete mirror must receive libraries before PHP')
            events.append([helper['package_metadata'](path, kind)[0] for paths in groups.values() for path in paths])
        def debug(*args):
            events.append('fetch retained debug')
            return ['debug download failed']
        with patch.dict(publisher['publish_mirror'].__globals__, {'publish': publish,
                'stage_mirror_dependencies': lambda *_: [], 'stage_mirror_debug': debug}), \
                patch.dict('os.environ', {'FORGEJO_PASSWORD': 'test'}), contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(RuntimeError, 'debug download failed'):
                publisher['publish_mirror']('upload', self.root, self.root, 'apk', {('php-zts-cli', '3.6.0-r1', 'x86_64'): '85'}, ['85'], helper)
        self.assertEqual(events, [['php-zts-cli', 'php-zts-lib-ssl'], 'fetch retained debug', ['php-zts-lib-ssl-debuginfo']])

    def test_library_only_mirror_publishes_only_the_selected_archives(self):
        names = {'php-zts-lib-' + identifier + suffix for identifier in ('libaom', 'libjxl')
                 for suffix in ('', '-debuginfo')}
        for name in sorted(names):
            self.archive(name=name)
        staged = Mock(side_effect=AssertionError('Unchanged dependencies must not be staged'))
        uploaded = []
        def publish(action, host, password, kind, groups, helpers, mirror):
            uploaded.extend(helpers['package_metadata'](path, kind)[0] for paths in groups.values() for path in paths)
        with patch.dict(publisher['publish_mirror'].__globals__, {'publish': publish,
                'stage_mirror_dependencies': staged, 'stage_mirror_debug': staged}), \
                patch.dict('os.environ', {'FORGEJO_PASSWORD': 'test'}):
            publisher['publish_mirror']('upload', self.root, self.root, 'apk', {}, ['85'], helper)
        self.assertEqual(set(uploaded), names)
        self.assertEqual(len(uploaded), len(names))
        staged.assert_not_called()


    def test_candidate_lookup_batches_selected_names_and_excludes_unrelated_packages(self):
        first = self.archive(name='php-zts-cli')
        second = self.archive(name='php-zts-cli', arch='aarch64')
        third = self.archive(name='frankenphp')
        listing = Mock(return_value=[{'name': 'frankenphp', 'version': '1'},
                                     {'name': 'php-zts-cli', 'version': '2'},
                                     {'name': 'php-zts-cli', 'version': '3'},
                                     {'name': 'php-zts-cli-unrelated', 'version': '4'}])
        with patch.dict(helper_globals, {'get_remote_packages': listing}):
            records = helper['get_remote_candidates']('https://example.com', '85', 'test', 'alpine', [first, second, third])
        listing.assert_called_once_with('https://example.com', '85', 'test', 'alpine', None)
        self.assertEqual([(record['name'], record['version']) for record in records],
                         [('frankenphp', '1'), ('php-zts-cli', '2'), ('php-zts-cli', '3')])

    def test_candidate_lookup_uses_a_shared_prefix_and_keeps_all_selected_versions(self):
        first = self.archive(name='php-zts-cli')
        second = self.archive(name='php-zts-curl')
        listing = Mock(return_value=[{'name': 'php-zts-cli', 'version': '1'},
                                     {'name': 'php-zts-cli', 'version': '2'},
                                     {'name': 'php-zts-curl', 'version': '1'},
                                     {'name': 'php-zts-cli-extra', 'version': '9'}])
        with patch.dict(helper_globals, {'get_remote_packages': listing}):
            records = helper['get_remote_candidates']('https://example.com', '85', 'test', 'alpine', [first, second])
        listing.assert_called_once_with('https://example.com', '85', 'test', 'alpine', 'php-zts-c')
        self.assertEqual(len(records), 3)

    def test_bulk_candidate_pages_are_read_once_and_next_preflight_refreshes_them(self):
        files = [self.archive(name='php-zts-cli'), self.archive(name='frankenphp')]
        pages = [[{'name': 'php-zts-cli', 'version': '1'}, {'name': 'unrelated', 'version': '99'}],
                 [{'name': 'frankenphp', 'version': '2'}], [],
                 [{'name': 'php-zts-cli', 'version': '3'}], []]
        with patch.object(helper_globals['requests'], 'get', side_effect=[response(payload=page) for page in pages]) as listing:
            first = helper['get_remote_candidates']('https://example.com', '85', 'test', 'alpine', files)
            second = helper['get_remote_candidates']('https://example.com', '85', 'test', 'alpine', files)
        self.assertEqual([(p['name'], p['version']) for p in first], [('php-zts-cli', '1'), ('frankenphp', '2')])
        self.assertEqual(second, [{'name': 'php-zts-cli', 'version': '3'}])
        self.assertEqual([call.kwargs['params']['page'] for call in listing.call_args_list], [1, 2, 3, 1, 2])
        self.assertTrue(all('q' not in call.kwargs['params'] for call in listing.call_args_list))

    def test_signed_apk_header_and_sha_are_read_from_file_id(self):
        data = apk_bytes('php-zts-lib-ssl', '3.6.0-r1', 'aarch64', signed=True)
        calls = []
        def get(url, **kwargs):
            calls.append(url)
            return response(payload=[{'id': 45, 'name': 'php-zts-lib-ssl-3.6.0-r1.apk'}]) if url.endswith('/files') else response(data=data)
        with patch.object(helper_globals['requests'], 'get', side_effect=get):
            records = helper['remote_package_archives']('https://example.com', '86', 'test', 'alpine', 'php-zts-lib-ssl', '3.6.0-r1')
        self.assertEqual(records[0]['arch'], 'aarch64')
        self.assertEqual(records[0]['sha256'], hashlib.sha256(data).hexdigest())
        self.assertTrue(calls[-1].endswith('/files/45'))

    def test_registry_and_archive_header_mismatch_fails(self):
        data = apk_bytes('wrong-name', '3.6.0-r1', 'x86_64')
        with patch.object(helper_globals['requests'], 'get', side_effect=[response(payload=[{'id': 45, 'name': 'library.apk'}]), response(data=data)]):
            with self.assertRaisesRegex(RuntimeError, 'header differs'):
                helper['remote_package_archives']('https://example.com', '86', 'test', 'alpine', 'library', '3.6.0-r1')

    def test_verified_file_is_downloaded_once_but_registry_is_read_again(self):
        data = apk_bytes('php-zts-lib-ssl', '3.6.0-r1', 'x86_64')
        digest = hashlib.sha256(data).hexdigest()
        files = [{'id': 45, 'name': 'library.apk', 'sha256': digest}]
        with patch.dict(helper_globals, {'read_consumer_sidecar': lambda *args: None}), patch.object(helper_globals['requests'], 'get', side_effect=[response(payload=files), response(data=data), response(payload=files), response(payload=[])]) as get:
            first = helper['remote_package_archives']('https://example.com', '86', 'test', 'alpine', 'php-zts-lib-ssl', '3.6.0-r1')
            first[0]['native_metadata']['depends'].append('mutated caller result')
            second = helper['remote_package_archives']('https://example.com', '86', 'test', 'alpine', 'php-zts-lib-ssl', '3.6.0-r1')
            self.assertEqual(second[0]['native_metadata']['depends'], [])
            self.assertEqual(helper['remote_package_archives']('https://example.com', '86', 'test', 'alpine', 'php-zts-lib-ssl', '3.6.0-r1'), [])
        self.assertEqual(get.call_count, 4)

    def test_changed_digest_requires_download_and_cannot_reuse_old_identity(self):
        data = apk_bytes('php-zts-lib-ssl', '3.6.0-r1', 'x86_64')
        files = [{'id': 45, 'name': 'library.apk', 'sha256': hashlib.sha256(data).hexdigest()}]
        changed = [dict(files[0], sha256='0' * 64)]
        with patch.dict(helper_globals, {'read_consumer_sidecar': lambda *args: None}), patch.object(helper_globals['requests'], 'get', side_effect=[response(payload=files), response(data=data), response(payload=changed), response(data=data)]) as get:
            helper['remote_package_archives']('https://example.com', '86', 'test', 'alpine', 'php-zts-lib-ssl', '3.6.0-r1')
            with self.assertRaisesRegex(RuntimeError, 'differs from registry file digest'):
                helper['remote_package_archives']('https://example.com', '86', 'test', 'alpine', 'php-zts-lib-ssl', '3.6.0-r1')
        self.assertEqual(get.call_count, 4)

    def test_identical_registry_digests_reuse_headers_across_streams(self):
        data = apk_bytes('php-zts-lib-ssl', '3.6.0-r1', 'x86_64')
        file = {'id': 45, 'name': 'library.apk', 'sha256': hashlib.sha256(data).hexdigest()}
        replies = [response(payload=[file]), response(data=data), response(payload=[dict(file, id=46)]), response(payload=[file])]
        with patch.dict(helper_globals, {'read_consumer_sidecar': lambda *args: None}), patch.object(helper_globals['requests'], 'get', side_effect=replies) as get:
            for owner in ('85', '85', '86'):
                helper['remote_package_archives']('https://example.com', owner, 'test', 'alpine', 'php-zts-lib-ssl', '3.6.0-r1')
        self.assertEqual(get.call_count, 4)

    def test_cli_apk_retains_optional_cutoffs_during_download(self):
        cutoffs = {'schema': 1, 'php_stream': '8.6', 'architecture': 'x86_64', 'formats': {'apk': {'php-zts-intl': '8.6.0_rc4-r2'}}}
        for metadata in (None, cutoffs):
            with self.subTest(shared=metadata is not None):
                data = apk_bytes('php-zts-cli', '8.6.0_rc4-r2', 'x86_64', signed=True, cutoffs=metadata)
                with patch.object(helper_globals['requests'], 'get', side_effect=[response(payload=[{'id': 45, 'name': 'php-zts-cli-8.6.0_rc4-r2.apk'}]), response(data=data)]):
                    records = helper['remote_package_archives']('https://example.com', '86', 'test', 'alpine', 'php-zts-cli', '8.6.0_rc4-r2')
                self.assertEqual(records[0]['migration_cutoffs'], metadata)

    def test_cli_cutoff_identity_must_match_header(self):
        cutoffs = {'schema': 1, 'php_stream': '8.5', 'architecture': 'aarch64', 'formats': {'apk': {}}}
        data = apk_bytes('php-zts-cli', '8.6.0_rc4-r2', 'x86_64', cutoffs=cutoffs)
        with patch.object(helper_globals['requests'], 'get', side_effect=[response(payload=[{'id': 45, 'name': 'cli.apk'}]), response(data=data)]):
            with self.assertRaisesRegex(RuntimeError, 'identity differs'):
                helper['remote_package_archives']('https://example.com', '86', 'test', 'alpine', 'php-zts-cli', '8.6.0_rc4-r2')

    def test_cli_cutoff_member_is_bounded_and_schema_checked(self):
        for metadata in ([], {'schema': 1, 'formats': {'apk': {'x': 'x' * helper['MAX_MIGRATION_CUTOFF_BYTES']}}}):
            with self.subTest(metadata_type=type(metadata).__name__):
                path = self.root / 'cutoffs.apk'
                path.write_bytes(apk_bytes('php-zts-cli', '8.6.0_rc4-r2', 'x86_64', cutoffs=metadata))
                with self.assertRaisesRegex(RuntimeError, 'Invalid migration cutoff'):
                    helper['archive_migration_cutoffs'](path, 'alpine')

    @unittest.skipUnless(shutil.which('dpkg-deb'), 'dpkg-deb required')
    def test_deb_architecture_comes_from_header_not_filename(self):
        package = self.root / 'package'
        (package / 'DEBIAN').mkdir(parents=True)
        (package / 'DEBIAN/control').write_text('Package: php-zts-lib-ssl\nVersion: 3.6.0-1\nArchitecture: amd64\nMaintainer: Test <test@example.com>\nDescription: archive identity fixture\n')
        archive = self.root / 'fixture.deb'
        subprocess.run(['dpkg-deb', '--build', '--root-owner-group', str(package), str(archive)], check=True, capture_output=True)
        with patch.object(helper_globals['requests'], 'get', side_effect=[response(payload=[{'id': 65, 'name': 'php-zts-lib-ssl_3.6.0-1_arm64.deb'}]), response(data=archive.read_bytes())]):
            records = helper['remote_package_archives']('https://example.com', '86', 'test', 'debian', 'php-zts-lib-ssl', '3.6.0-1')
        self.assertEqual(records[0]['arch'], 'amd64')
        self.assertEqual(records[0]['sha256'], helper['archive_sha256'](archive))

    @unittest.skipUnless(shutil.which('dpkg-deb'), 'dpkg-deb required')
    def test_cli_deb_optional_cutoffs_are_streamed_without_extraction(self):
        package = self.root / 'package'
        (package / 'DEBIAN').mkdir(parents=True)
        (package / 'DEBIAN/control').write_text('Package: php-zts-cli\nVersion: 8.6.0~rc4-2\nArchitecture: amd64\nMaintainer: Test <test@example.com>\nDescription: cutoff fixture\n')
        archive = self.root / 'cli.deb'
        subprocess.run(['dpkg-deb', '--build', '--root-owner-group', str(package), str(archive)], check=True, capture_output=True)
        self.assertIsNone(helper['archive_migration_cutoffs'](archive, 'debian'))
        cutoffs = {'schema': 1, 'php_stream': '8.6', 'architecture': 'amd64', 'formats': {'deb': {'php-zts-intl': '8.6.0~rc4-2'}}}
        metadata = package / helper['MIGRATION_CUTOFF_PATH']
        metadata.parent.mkdir(parents=True)
        metadata.write_text(json.dumps(cutoffs))
        subprocess.run(['dpkg-deb', '--build', '--root-owner-group', str(package), str(archive)], check=True, capture_output=True)
        self.assertEqual(helper['archive_migration_cutoffs'](archive, 'debian'), cutoffs)

    def test_deb_reader_process_is_reaped_on_bad_tar(self):
        process = Mock(stdout=io.BytesIO(b'invalid tar'))
        process.poll.return_value = None
        with patch.object(helper_globals['subprocess'], 'Popen', return_value=process):
            with self.assertRaises(tarfile.ReadError):
                helper['archive_migration_cutoffs'](self.root / 'bad.deb', 'debian')
        process.terminate.assert_called_once()
        process.wait.assert_called_once()

    def test_matching_local_digest_never_downloads_the_remote_archive(self):
        path = self.archive()
        digest = helper['archive_sha256'](path)
        files = [{'id': 45, 'name': path.name, 'sha256': digest}]
        with patch.dict(helper_globals, {'version_is_newer': lambda *args: False}), \
                patch.object(helper_globals['requests'], 'get', return_value=response(payload=files)) as get:
            allowed = helper['verify_upload_versions']('https://example.com', '85', 'test', 'alpine', [path],
                [{'name': 'php-zts-lib-ssl', 'version': '3.6.0-r1'}])
        self.assertEqual(allowed, [path])
        self.assertEqual(get.call_count, 1)
        self.assertTrue(get.call_args.args[0].endswith('/files'))

    def test_equal_version_different_bytes_is_rejected_before_upload(self):
        path = self.archive()
        with patch.dict(helper_globals, {
            'version_is_newer': lambda *args: False,
            'remote_package_archives': lambda *args: [{'arch': 'x86_64', 'sha256': 'different'}],
        }), contextlib.redirect_stdout(io.StringIO()):
            allowed = helper['verify_upload_versions']('https://example.com', '86', 'test', 'alpine', [path], [{'name': 'php-zts-lib-ssl', 'version': '3.6.0-r1'}])
        self.assertEqual(allowed, [])

    def test_idempotent_archive_is_verified_without_upload_or_git_repository_link(self):
        path = self.archive()
        record = {'arch': 'x86_64', 'sha256': helper['archive_sha256'](path)}
        listing = Mock(return_value=[])
        with patch.dict(helper_globals, {
            'get_remote_packages': listing,
            'remote_package_archives': lambda *args: [record],
        }), patch.object(helper_globals['requests'], 'put') as upload, patch.object(helper_globals['requests'], 'post', return_value=response(status=409)) as link, contextlib.redirect_stdout(io.StringIO()):
            helper['upload_verified_archive']('https://example.com', '86', 'test', 'alpine', path)
        listing.assert_called_once_with('https://example.com', '86', 'test', 'alpine', 'php-zts-lib-ssl')
        upload.assert_not_called()
        link.assert_not_called()

    def test_cohort_preflight_is_reused_but_equal_version_bytes_are_still_verified(self):
        path = self.archive()
        listing = Mock(side_effect=AssertionError('version listing repeated'))
        record = {'arch': 'x86_64', 'sha256': helper['archive_sha256'](path)}
        with patch.dict(helper_globals, {'get_remote_packages': listing,
                'remote_package_archives': lambda *args: [record]}), contextlib.redirect_stdout(io.StringIO()):
            helper['upload_verified_archive']('https://example.com', '86', 'test', 'alpine', path, preflighted=True)
            record['sha256'] = '0' * 64
            with self.assertRaisesRegex(RuntimeError, 'Equal version has different archive bytes'):
                helper['upload_verified_archive']('https://example.com', '86', 'test', 'alpine', path, preflighted=True)
        listing.assert_not_called()

    def test_new_archive_is_verified_without_a_git_repository_association(self):
        path = self.archive()
        record = {'arch': 'x86_64', 'sha256': helper['archive_sha256'](path)}
        with patch.dict(helper_globals, {
            'get_remote_packages': lambda *args: [],
            'remote_package_archives': Mock(side_effect=[[], [record]]),
        }), patch.object(helper_globals['requests'], 'put', return_value=response(status=201)) as upload, \
                patch.object(helper_globals['requests'], 'post', return_value=response(status=400)) as link, \
                contextlib.redirect_stdout(io.StringIO()):
            helper['upload_verified_archive']('https://example.com', '86', 'test', 'alpine', path)
        upload.assert_called_once()
        link.assert_not_called()

    def test_second_architecture_cannot_remove_existing_archive(self):
        path = self.archive(arch='aarch64')
        before = {'arch': 'x86_64', 'sha256': 'existing'}
        current = {'arch': 'aarch64', 'sha256': helper['archive_sha256'](path)}
        with patch.dict(helper_globals, {
            'get_remote_packages': lambda *args: [],
            'remote_package_archives': Mock(side_effect=[[before], [current]]),
        }), patch.object(helper_globals['requests'], 'put', return_value=response(status=201)), patch.object(helper_globals['requests'], 'post', return_value=response(status=201)):
            with self.assertRaisesRegex(RuntimeError, 'changed an existing architecture'):
                helper['upload_verified_archive']('https://example.com', '86', 'test', 'alpine', path)

    def test_second_architecture_upload_preserves_both_archive_identities(self):
        path = self.archive(arch='aarch64')
        before = {'arch': 'x86_64', 'sha256': 'existing'}
        current = {'arch': 'aarch64', 'sha256': helper['archive_sha256'](path)}
        with patch.dict(helper_globals, {
            'get_remote_packages': lambda *args: [],
            'remote_package_archives': Mock(side_effect=[[before], [before, current]]),
        }), patch.object(helper_globals['requests'], 'put', return_value=response(status=201)) as upload, patch.object(helper_globals['requests'], 'post', return_value=response(status=201)), contextlib.redirect_stdout(io.StringIO()):
            helper['upload_verified_archive']('https://example.com', '86', 'test', 'alpine', path)
        upload.assert_called_once()

    def test_conflict_response_is_not_success_when_new_architecture_is_missing(self):
        path = self.archive(arch='aarch64')
        old = {'arch': 'x86_64', 'sha256': 'existing'}
        with patch.dict(helper_globals, {
            'get_remote_packages': lambda *args: [],
            'remote_package_archives': lambda *args: [old],
        }), patch.object(helper_globals['requests'], 'put', return_value=response(status=409)), patch.object(helper_globals['requests'], 'post', return_value=response(status=201)):
            with self.assertRaisesRegex(RuntimeError, 'missing after upload'):
                helper['upload_verified_archive']('https://example.com', '86', 'test', 'alpine', path)

    def test_library_and_layout_fan_out_to_existing_stream_owners(self):
        library = self.archive()
        layout = self.archive(name='php-zts-runtime-layout', version='1-r0', arch='noarch')
        cli = self.archive(name='php-zts-cli', version='8.5.12-r1')
        owners = {helper['package_metadata'](cli, 'alpine'): '85'}
        groups = publisher['package_groups'](self.root, 'apk', owners, helper)
        self.assertEqual(set(groups), set(publisher['STREAM_OWNERS']))
        for owner, paths in groups.items():
            self.assertIn(library, paths)
            self.assertIn(layout, paths)
            self.assertEqual(cli in paths, owner == '85')
        scoped = publisher['package_groups'](self.root, 'apk', owners, helper, ['85'])
        self.assertEqual(set(scoped), {'85'})
        self.assertEqual(set(scoped['85']), {library, layout, cli})
        with self.assertRaisesRegex(RuntimeError, 'unselected PHP owner'):
            publisher['package_groups'](self.root, 'apk', owners, helper, ['86'])

    def test_last_owner_preflight_failure_prevents_all_uploads(self):
        path = self.archive()
        upload = Mock()
        helpers = dict(helper, get_remote_candidates=lambda *args: [],
                       verify_upload_versions=lambda host, owner, password, kind, paths, remote: [] if owner == '86' else paths,
                       upload_verified_archive=upload)
        groups = {owner: [path] for owner in publisher['STREAM_OWNERS']}
        with self.assertRaisesRegex(RuntimeError, 'owner 86'):
            publisher['publish']('upload', 'https://example.com', 'test', 'alpine', groups, helpers)
        upload.assert_not_called()

    def test_mirror_retry_keeps_newer_architecture_and_still_uploads_missing_libraries(self):
        cli = self.archive(name='php-zts-cli', version='8.5.12-r1')
        debug = self.archive(name='php-zts-cli-debuginfo', version='8.5.12-r1')
        library = self.archive()
        paths = [cli, debug, library]
        remote = [{'name': 'php-zts-cli', 'version': '8.5.12-r2'}]
        uploaded = []
        helpers = dict(helper, get_remote_candidates=lambda *args: remote,
                       version_is_newer=lambda kind, left, right: left > right,
                       get_remote_package_archs=lambda *args, **kwargs: {'x86_64'},
                       verify_upload_versions=lambda host, owner, password, kind, paths, remote: [p for p in paths if p == library],
                       upload_verified_archive=lambda host, owner, password, kind, path, **kwargs: uploaded.append(path))
        with contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(RuntimeError, 'guard rejected'):
                publisher['publish']('upload', 'https://primary.test', 'test', 'alpine', {'85': paths}, helpers)
            self.assertEqual(uploaded, [])
            publisher['publish']('upload', 'https://mirror.test', 'test', 'alpine', {'85': paths}, helpers, mirror=True)
        self.assertEqual(uploaded, [library])
        helpers['get_remote_package_archs'] = lambda *args, **kwargs: {'aarch64'}
        self.assertEqual(publisher['mirror_candidates'](paths, remote, 'https://mirror.test', '85', 'test', 'alpine', helpers), paths)
        helpers['get_remote_package_archs'] = lambda *args, **kwargs: set()
        with self.assertRaisesRegex(RuntimeError, 'Cannot verify newer mirror package'):
            publisher['mirror_candidates'](paths, remote, 'https://mirror.test', '85', 'test', 'alpine', helpers)

    def test_mirror_retry_does_not_bypass_equal_version_identity_errors(self):
        path = self.archive()
        remote = [{'name': 'php-zts-lib-ssl', 'version': '3.6.0-r1'}]
        upload = Mock()
        helpers = dict(helper, get_remote_candidates=lambda *args: remote,
                       version_is_newer=lambda kind, left, right: left > right,
                       verify_upload_versions=lambda *args: [], upload_verified_archive=upload)
        with self.assertRaisesRegex(RuntimeError, 'guard rejected'):
            publisher['publish']('upload', 'https://mirror.test', 'test', 'alpine', {'85': [path]}, helpers, mirror=True)
        upload.assert_not_called()

    @unittest.skipUnless(shutil.which('apk'), 'native APK version comparator unavailable')
    def test_invalid_old_apk_version_cannot_block_valid_mirror_replacement(self):
        path = self.archive(name='php-zts-igbinary', version='3.2.17_rc1_p82-r5')
        remote = [{'name': 'php-zts-igbinary', 'version': '3.2.17~rc1p82-r0'}]
        self.assertFalse(helper['version_is_newer']('alpine', remote[0]['version'], '3.2.17_rc1_p82-r5'))
        self.assertTrue(helper['version_is_newer']('alpine', '3.2.17_rc1_p82-r5', remote[0]['version']))
        self.assertTrue(helper['version_is_newer']('alpine', '3.2.17_rc1_p82-r6', '3.2.17_rc1_p82-r5'))
        upload = Mock()
        helpers = dict(helper, get_remote_candidates=lambda *args: remote,
                       get_remote_package_archs=Mock(side_effect=AssertionError('Invalid old archive read')),
                       upload_verified_archive=upload)
        with contextlib.redirect_stdout(io.StringIO()):
            publisher['publish']('upload', 'https://mirror.test', 'test', 'alpine', {'82': [path]}, helpers, mirror=True)
        upload.assert_called_once_with('https://mirror.test', '82', 'test', 'alpine', path, preflighted=True)

    def test_parallel_mirror_uploads_finish_dependencies_before_cli(self):
        libraries = {'php-zts-lib-' + str(i) for i in range(4)}
        paths = [self.archive(name=name) for name in sorted(libraries | {'php-zts-cli', 'php-zts-runtime-layout'})]
        completed = set()
        barrier = Barrier(2)
        def upload(host, owner, password, kind, path, **kwargs):
            name = helper['package_metadata'](path, kind)[0]
            if name in libraries:
                self.assertIn('php-zts-runtime-layout', completed)
                barrier.wait(timeout=3)
            if name == 'php-zts-cli':
                self.assertTrue(libraries <= completed)
            completed.add(name)
        helpers = dict(helper, get_remote_candidates=lambda *args: [],
                       verify_upload_versions=lambda host, owner, password, kind, paths, remote: paths,
                       upload_verified_archive=upload)
        with contextlib.redirect_stdout(io.StringIO()):
            publisher['publish']('upload', 'https://example.com', 'test', 'alpine', {'85': paths}, helpers, mirror=True)
        self.assertEqual(len(completed), 6)

    def test_primary_and_mirror_update_order(self):
        paths = [self.archive(name=name) for name in ('php-zts-cli', 'php-zts-intl', 'php-zts-lib-ssl',
                 'php-zts-runtime-layout', 'php-zts-cli-debuginfo', 'php-zts-lib-ssl-debuginfo')]
        groups = {'86': paths}
        def names(mirror):
            return [helper['package_metadata'](path, 'alpine')[0] for _, _, path in publisher['publication_order'](groups, helper, 'alpine', mirror)]
        debug = ['php-zts-cli-debuginfo', 'php-zts-lib-ssl-debuginfo']
        self.assertEqual(names(True), ['php-zts-runtime-layout', 'php-zts-lib-ssl', 'php-zts-intl', 'php-zts-cli'] + debug)
        self.assertEqual(names(False), ['php-zts-runtime-layout', 'php-zts-intl', 'php-zts-cli', 'php-zts-lib-ssl'] + debug)

    def test_archive_mutation_during_preflight_prevents_upload(self):
        path = self.archive()
        def preflight(host, owner, password, kind, paths, remote):
            path.write_bytes(path.read_bytes() + b'changed')
            return paths
        upload = Mock()
        helpers = dict(helper, get_remote_candidates=lambda *args: [], verify_upload_versions=preflight, upload_verified_archive=upload)
        with self.assertRaisesRegex(RuntimeError, 'changed during'):
            publisher['publish']('upload', 'https://example.com', 'test', 'alpine', {'86': [path]}, helpers)
        upload.assert_not_called()

    def test_numeric_rpm_release_is_understood(self):
        self.assertEqual(helper['get_package_info'](Path('php-zts-cli-8.6.0~rc4-42.x86_64.rpm'), 'rpm')[:3], ('php-zts-cli', '8.6.0~rc4-42', 'x86_64'))


ForgejoIndex = runpy.run_path(str(ROOT / 'tests/forgejo-index.py'), run_name='forgejo_index_tests')['ForgejoIndex']


if __name__ == '__main__':
    unittest.main()
