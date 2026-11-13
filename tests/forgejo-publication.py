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

    def mirror(self, cohort, owners):
        with patch.dict('os.environ', {'FORGEJO_PASSWORD': 'test', 'PUBLICATION_PHP_STREAMS': '8.5'}), \
                patch('sys.argv', ['publish-forgejo.py', 'apk', '2', '--cohort', str(cohort), '--directory', str(self.root)]), \
                patch.dict(publisher['main'].__globals__, {'consumer_owners': Mock(return_value=owners)}), \
                patch.object(publisher['main'].__globals__['runpy'], 'run_path', return_value=helper) as loader:
            publisher['main']()
        loader.assert_called_once_with(str(ROOT / 'bin/forgejo-helper'))

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

    def test_verified_headers_are_reused_across_stream_processes_by_checksum(self):
        data = apk_bytes('php-zts-lib-ssl', '3.6.0-r1', 'aarch64', signed=True)
        digest = hashlib.sha256(data).hexdigest()
        files = [{'id': 42, 'name': 'php-zts-lib-ssl-3.6.0-r1.apk', 'sha256': digest}]
        archive = response(data=data)
        archive.raw = io.BytesIO(data)
        cache = self.root / 'verified-headers'
        with patch.dict('os.environ', {'SPP_ARCHIVE_HEADER_CACHE': str(cache)}), \
                patch.object(helper_globals['requests'], 'get', side_effect=[response(payload=files), archive, response(payload=files)]) as get:
            helper['remote_package_archives']('https://example.com', '85', 'test', 'alpine', 'php-zts-lib-ssl', '3.6.0-r1', True)
            helper_globals['ARCHIVE_HEADER_IDENTITIES'].clear()
            mirrored = helper['remote_package_archives']('https://mirror.test', '86', 'test', 'alpine', 'php-zts-lib-ssl', '3.6.0-r1', True)
            self.assertEqual(mirrored[0]['arch'], 'aarch64')
            self.assertEqual(mirrored[0]['owner'], '86')
            self.assertEqual(get.call_count, 3)
            record = cache / (digest + '.json')
            original = record.read_text()
            value = json.loads(original)
            value['name'] = 'another-package'
            record.write_text(json.dumps(value))
            helper_globals['ARCHIVE_HEADER_IDENTITIES'].clear()
            with patch.object(helper_globals['requests'], 'get', return_value=response(payload=files)), \
                    self.assertRaisesRegex(RuntimeError, 'Cached archive header identity differs'):
                helper['remote_package_archives']('https://mirror.test', '86', 'test', 'alpine', 'php-zts-lib-ssl', '3.6.0-r1', True)
            record.write_text(original)
            changed = dict(files[0], sha256='f' * 64)
            archive.raw = io.BytesIO(data)
            with patch.object(helper_globals['requests'], 'get', side_effect=[response(payload=[changed]), archive]) as changed_get:
                helper['remote_package_archives']('https://mirror.test', '86', 'test', 'alpine', 'php-zts-lib-ssl', '3.6.0-r1', True)
            self.assertEqual(changed_get.call_count, 2)

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
                       verify_upload_versions=lambda host, owner, password, kind, paths, remote, **kwargs: paths,
                       upload_verified_archive=upload)
        with contextlib.redirect_stdout(printed):
            publisher['publish']('https://example.com', 'test', 'alpine', {'84': paths}, helpers)
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
                           verify_upload_versions=lambda host, owner, password, kind, paths, remote, **kwargs: paths,
                           upload_verified_archive=upload)
            with self.subTest(mirror=mirror), contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaisesRegex(RuntimeError, '1 package uploads failed; remaining packages were attempted'):
                    publisher['publish']('https://example.com', 'test', 'alpine', {'84': paths}, helpers, mirror=mirror)
            self.assertCountEqual(attempted, paths)

    def test_owner_listing_records_owner_and_reads_every_stream(self):
        listing = Mock(side_effect=lambda host, owner, password, kind: [{'name': 'library', 'version': owner}])
        with patch.dict(helper_globals, {'get_remote_packages': listing}):
            records = helper['get_remote_packages_for_owners']('https://example.com', publisher['STREAM_OWNERS'], 'test', 'alpine')
        self.assertEqual([record['owner'] for record in records], list(publisher['STREAM_OWNERS']))
        self.assertEqual(listing.call_count, 5)

    def test_consumer_ownership_ignores_unselected_archives(self):
        cohort = self.root / 'cohort'
        selected = None
        for stream in ('8.3', '8.6'):
            directory = cohort / ('shared-consumers-apk-x86_64-php' + stream)
            directory.mkdir(parents=True)
            path = self.archive(name='php-zts-ssh2', version='1.0_p' + stream.replace('.', '') + '-r0')
            path.rename(directory / path.name)
            (directory / 'shared-library-consumers.json').write_text(json.dumps({'consumers': {
                'php-zts-ssh2': {'php_version': stream}}}))
            selected = directory / path.name
        reader = Mock(wraps=helper['package_metadata'])
        owners = publisher['consumer_owners'](cohort, 'apk', dict(helper, package_metadata=reader), {selected.name})
        self.assertEqual(owners, {helper['package_metadata'](selected, 'alpine'): '86'})
        reader.assert_called_once_with(selected, 'alpine')

    def test_mixed_mirror_copies_only_selected_packages_without_backfilling_libraries(self):
        names = {'php-zts-runtime-layout', 'php-zts-lib-libaom', 'php-zts-lib-libaom-debuginfo',
                 'php-zts-ssh2', 'php-zts-ssh2-debuginfo'}
        selected = [self.archive(name=name) for name in sorted(names)]
        original = {path.name: path.read_bytes() for path in selected}
        baseline = self.root / 'cohort/shared-libs-apk-x86_64/baseline'
        baseline.mkdir(parents=True)
        unrelated = self.archive(name='php-zts-lib-grpc', version='1.0-r9')
        unrelated.rename(baseline / unrelated.name)
        (baseline / 'shared-libraries.json').write_text(json.dumps({'architecture': 'x86_64', 'libraries': {
            'grpc': {'published_archive': unrelated.name, 'published_version': '1.0-r9',
                     'published_archive_sha256': helper['archive_sha256'](baseline / unrelated.name),
                     'published_debug_archive': {'download_url': 'https://primary.test/unrelated.apk'}}}}))
        owners = {helper['package_metadata'](path, 'alpine'): '85' for path in selected
                  if not publisher['independent'](helper['package_metadata'](path, 'alpine')[0])}
        uploaded = []
        def publish(host, password, kind, groups, helpers, mirror):
            self.assertTrue(mirror)
            self.assertEqual(set(groups), {'85'})
            uploaded.extend(path for paths in groups.values() for path in paths)
        with patch.dict(publisher['publish'].__globals__, {'publish': publish}), \
                patch.dict('os.environ', {'FORGEJO_PASSWORD': 'test'}), \
                contextlib.redirect_stdout(io.StringIO()):
            self.mirror(self.root / 'cohort', owners)
        self.assertEqual({path.name: path.read_bytes() for path in uploaded}, original)
        self.assertEqual(len(uploaded), len(selected))
        self.assertEqual({path.name for path in self.root.glob('*.apk')}, set(original))

    def test_mirror_preflights_runtime_and_debug_together_once(self):
        paths = [self.archive(name=name) for name in ('php-zts-cli', 'php-zts-lib-libaom', 'php-zts-lib-libaom-debuginfo')]
        listing = Mock(return_value=[])
        preflight = Mock(side_effect=lambda host, owner, password, kind, files, remote, **kwargs: files)
        uploaded = []
        helpers = dict(helper, get_remote_candidates=listing, verify_upload_versions=preflight,
                       upload_verified_archive=lambda host, owner, password, kind, path, **kwargs: uploaded.append(path))
        with contextlib.redirect_stdout(io.StringIO()):
            publisher['publish']('https://mirror.test', 'test', 'alpine', {'85': paths}, helpers, mirror=True)
        listing.assert_called_once()
        preflight.assert_called_once_with('https://mirror.test', '85', 'test', 'alpine', paths, [], keep_newer=True)
        self.assertEqual(uploaded[-1].name, paths[-1].name)
        self.assertCountEqual(uploaded, paths)

    def test_library_only_mirror_publishes_only_the_selected_archives(self):
        names = {'php-zts-lib-' + identifier + suffix for identifier in ('libaom', 'libjxl')
                 for suffix in ('', '-debuginfo')}
        for name in sorted(names):
            self.archive(name=name)
        uploaded = []
        def publish(host, password, kind, groups, helpers, mirror):
            uploaded.extend(helpers['package_metadata'](path, kind)[0] for paths in groups.values() for path in paths)
        with patch.dict(publisher['publish'].__globals__, {'publish': publish}), \
                patch.dict('os.environ', {'FORGEJO_PASSWORD': 'test'}):
            self.mirror(self.root, {})
        self.assertEqual(set(uploaded), names)
        self.assertEqual(len(uploaded), len(names))


    def test_candidate_lookup_queries_only_selected_names_and_excludes_related_packages(self):
        paths = [self.archive(name='php-zts-cli'), self.archive(name='php-zts-cli', arch='aarch64'), self.archive(name='frankenphp')]
        listing = Mock(return_value=[{'name': 'frankenphp', 'version': '1'}, {'name': 'php-zts-cli', 'version': '2'},
                                     {'name': 'php-zts-cli', 'version': '3'}, {'name': 'php-zts-cli-unrelated', 'version': '4'}])
        with patch.dict(helper_globals, {'get_remote_packages': listing}):
            records = helper['get_remote_candidates']('https://example.com', '85', 'test', 'alpine', paths)
        self.assertEqual({call.args[-1] for call in listing.call_args_list}, {'frankenphp', 'php-zts-cli'})
        self.assertEqual(listing.call_count, 2)
        self.assertEqual([(record['name'], record['version']) for record in records],
                         [('frankenphp', '1'), ('php-zts-cli', '2'), ('php-zts-cli', '3')])

    def test_selected_candidate_queries_paginate_and_refresh_each_preflight(self):
        files = [self.archive(name='php-zts-cli'), self.archive(name='frankenphp')]
        generations = {}
        def get(url, **kwargs):
            name, page = kwargs['params']['q'], kwargs['params']['page']
            self.assertIn(name, {'php-zts-cli', 'frankenphp'})
            if page == 1:
                generations[name] = generations.get(name, 0) + 1
            if page > 1 or name == 'frankenphp' and generations[name] == 2:
                return response(payload=[])
            version = '2' if name == 'frankenphp' else '1' if generations[name] == 1 else '3'
            return response(payload=[{'name': name, 'version': version}, {'name': name + '-unrelated', 'version': '99'}])
        with patch.object(helper_globals['requests'], 'get', side_effect=get) as listing:
            first = helper['get_remote_candidates']('https://example.com', '85', 'test', 'alpine', files)
            second = helper['get_remote_candidates']('https://example.com', '85', 'test', 'alpine', files)
        self.assertEqual(first, [{'name': 'frankenphp', 'version': '2'}, {'name': 'php-zts-cli', 'version': '1'}])
        self.assertEqual(second, [{'name': 'php-zts-cli', 'version': '3'}])
        self.assertEqual(listing.call_count, 7)

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

    def test_primary_and_mirror_reuse_timestamp_only_deb_archives(self):
        fixture = runpy.run_path(str(ROOT / 'tests/package-content.py'))['PackageContent']()
        published = self.root / 'published.deb'
        candidate = self.root / 'candidate.deb'
        fixture.archive(published, 'Thu, 12 Nov 2026 11:55:27 +0000')
        data = published.read_bytes()
        downloaded = response(data=data)
        downloaded.content = data
        upload = Mock()
        helpers = dict(helper, package_metadata=lambda *args: ('pie-zts', '1.5.5+php86-1', 'amd64'),
            get_remote_candidates=lambda *args: [{'name': 'pie-zts', 'version': '1.5.5+php86-1'}],
            remote_package_archives=lambda *args: [{'arch': 'amd64', 'sha256': hashlib.sha256(data).hexdigest(), 'url': 'https://registry.test/pie.deb'}],
            registry_request=lambda method, url, **kwargs: kwargs['read'](downloaded),
            verify_upload_versions=lambda host, owner, password, kind, paths, remote, **kwargs: paths,
            upload_verified_archive=upload)
        for mirror in (False, True):
            fixture.archive(candidate, 'Fri, 13 Nov 2026 11:37:50 +0000')
            self.assertNotEqual(candidate.read_bytes(), data)
            with contextlib.redirect_stdout(io.StringIO()):
                publisher['publish']('https://registry.test', 'test', 'debian', {'86': [candidate]}, helpers, mirror=mirror)
            self.assertEqual(candidate.read_bytes(), data)
        self.assertEqual(upload.call_count, 2)

    def test_unchanged_archive_is_checked_once_without_upload(self):
        path = self.archive()
        record = {'arch': 'x86_64', 'sha256': helper['archive_sha256'](path)}
        archives = Mock(side_effect=[[record]])
        with patch.dict(helper_globals, {'remote_package_archives': archives}), \
                patch.object(helper_globals['requests'], 'put') as upload, contextlib.redirect_stdout(io.StringIO()):
            helper['upload_verified_archive']('https://example.com', '86', 'test', 'alpine', path, preflighted=True)
        archives.assert_called_once()
        upload.assert_not_called()

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
                       verify_upload_versions=lambda host, owner, password, kind, paths, remote, **kwargs: [] if owner == '86' else paths,
                       upload_verified_archive=upload)
        groups = {owner: [path] for owner in publisher['STREAM_OWNERS']}
        with self.assertRaisesRegex(RuntimeError, 'owner 86'):
            publisher['publish']('https://example.com', 'test', 'alpine', groups, helpers)
        upload.assert_not_called()

    def test_mirror_retry_keeps_newer_runtime_and_debug_in_one_version_check(self):
        cli = self.archive(name='php-zts-cli', version='8.5.12-r1')
        debug = self.archive(name='php-zts-cli-debuginfo', version='8.5.12-r1')
        library = self.archive()
        paths = [cli, debug, library]
        remote = [{'name': 'php-zts-cli', 'version': '8.5.12-r2'}]
        uploaded = []
        architectures = Mock(return_value={'x86_64'})
        helpers = dict(helper, get_remote_candidates=lambda *args: remote,
                       upload_verified_archive=lambda host, owner, password, kind, path, **kwargs: uploaded.append(path))
        with patch.dict(helper_globals, {'version_is_newer': lambda kind, left, right: left > right,
                                         'get_remote_package_archs': architectures}), contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(RuntimeError, 'guard rejected'):
                publisher['publish']('https://primary.test', 'test', 'alpine', {'85': paths}, helpers)
            self.assertEqual(uploaded, [])
            architectures.reset_mock()
            publisher['publish']('https://mirror.test', 'test', 'alpine', {'85': paths}, helpers, mirror=True)
            self.assertEqual(uploaded, [library])
            architectures.assert_called_once()
            conflicting_debug = remote + [{'name': 'php-zts-cli-debuginfo', 'version': '8.5.12-r1'}]
            with patch.dict(helper_globals, {'remote_package_archives': lambda *args: [{'arch': 'x86_64', 'sha256': '0' * 64}]}):
                self.assertEqual(helper['verify_upload_versions']('https://mirror.test', '85', 'test', 'alpine', paths,
                    conflicting_debug, keep_newer=True), [library])
            architectures.return_value = {'aarch64'}
            self.assertEqual(helper['verify_upload_versions']('https://mirror.test', '85', 'test', 'alpine', paths, remote, keep_newer=True), paths)
            architectures.return_value = set()
            with self.assertRaisesRegex(RuntimeError, 'Cannot determine published architectures'):
                helper['verify_upload_versions']('https://mirror.test', '85', 'test', 'alpine', paths, remote, keep_newer=True)

    def test_mirror_retry_does_not_bypass_equal_version_identity_errors(self):
        path = self.archive()
        remote = [{'name': 'php-zts-lib-ssl', 'version': '3.6.0-r1'}]
        upload = Mock()
        helpers = dict(helper, get_remote_candidates=lambda *args: remote, upload_verified_archive=upload)
        with patch.dict(helper_globals, {'version_is_newer': lambda *args: False,
                'remote_package_archives': lambda *args: [{'arch': 'x86_64', 'sha256': '0' * 64}]}), \
                self.assertRaisesRegex(RuntimeError, 'Equal version has different archive bytes'):
            publisher['publish']('https://mirror.test', 'test', 'alpine', {'85': [path]}, helpers, mirror=True)
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
            publisher['publish']('https://mirror.test', 'test', 'alpine', {'82': [path]}, helpers, mirror=True)
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
                       verify_upload_versions=lambda host, owner, password, kind, paths, remote, **kwargs: paths,
                       upload_verified_archive=upload)
        with contextlib.redirect_stdout(io.StringIO()):
            publisher['publish']('https://example.com', 'test', 'alpine', {'85': paths}, helpers, mirror=True)
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
        def preflight(host, owner, password, kind, paths, remote, **kwargs):
            path.write_bytes(path.read_bytes() + b'changed')
            return paths
        upload = Mock()
        helpers = dict(helper, get_remote_candidates=lambda *args: [], verify_upload_versions=preflight, upload_verified_archive=upload)
        with self.assertRaisesRegex(RuntimeError, 'changed during'):
            publisher['publish']('https://example.com', 'test', 'alpine', {'86': [path]}, helpers)
        upload.assert_not_called()

    def test_numeric_rpm_release_is_understood(self):
        self.assertEqual(helper['get_package_info'](Path('php-zts-cli-8.6.0~rc4-42.x86_64.rpm'), 'rpm')[:3], ('php-zts-cli', '8.6.0~rc4-42', 'x86_64'))


ForgejoIndex = runpy.run_path(str(ROOT / 'tests/forgejo-index.py'), run_name='forgejo_index_tests')['ForgejoIndex']


if __name__ == '__main__':
    unittest.main()
