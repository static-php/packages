#!/usr/bin/env python3
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest

REPO = Path(__file__).resolve().parents[1]


def module(name, filename):
    spec = importlib.util.spec_from_file_location(name, REPO / '.github/actions/shared-libraries' / filename)
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


repair = module('extension_repair', 'rehydrate-php-extensions.py')
frozen = module('frozen_php', 'php-source.py')


class Rehydration(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='spp-rehydrate-php-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.downloads = self.root / 'downloads'
        self.downloads.mkdir()
        self.source = self.root / 'source/php-src'
        for directory in ['main', 'ext/bcmath/.libs', 'ext/bz2', '.libs', 'include']:
            (self.source / directory).mkdir(parents=True, exist_ok=True)
        self.version = '8.6.0RC4'
        (self.source / 'main/php_version.h').write_text('#define PHP_VERSION "' + self.version + '"\n')
        for name, contents in {'Makefile': 'configured makefile', '.libs/libphp.so': 'core objects',
                               'include/config.h': 'generated core header', 'ext/bz2/config.m4': 'unselected source',
                               'ext/bcmath/LICENSE': 'preserved license', 'ext/bcmath/generated.h': 'local generated header',
                               'ext/bcmath/.libs/module.so': 'existing extension objects'}.items():
            (self.source / name).write_text(contents)
        self.protected = {name: (self.source / name).read_bytes() for name in ['Makefile', '.libs/libphp.so',
            'include/config.h', 'ext/bz2/config.m4', 'ext/bcmath/LICENSE', 'ext/bcmath/generated.h', 'ext/bcmath/.libs/module.so']}
        buildroot = self.root / 'buildroot'
        (buildroot / 'provenance').mkdir(parents=True)
        (buildroot / 'core').write_text('verified binary')
        (buildroot / 'provenance/build-payload.json').write_text(json.dumps({'state': 'sealed', 'source_version': self.version}))
        self.receipt = {'schema': 1, 'state': 'verified', 'rebuild_packages': ['bcmath'],
            'cached_metadata_path': 'provenance/build-payload.json',
            'cached_metadata_sha256': hashlib.sha256((buildroot / 'provenance/build-payload.json').read_bytes()).hexdigest(),
            'core_files': {'core': {'sha256': hashlib.sha256((buildroot / 'core').read_bytes()).hexdigest(),
                                   'mode': (buildroot / 'core').stat().st_mode & 0o777}}}
        (buildroot / 'consumer-cache-seed.json').write_text(json.dumps(self.receipt))
        self.archive()

    def archive(self, extra=None, symlink=False):
        self.path = self.downloads / ('php-' + self.version + '.tar.gz')
        files = {'php-src/main/php_version.h': '#define PHP_VERSION "' + self.version + '"\n',
                 'php-src/ext/bcmath/config.m4': 'exact pinned configuration',
                 'php-src/ext/bcmath/bcmath.c': 'exact pinned source',
                 'php-src/ext/bcmath/generated.h': 'archive generated header',
                 'php-src/ext/bz2/config.m4': 'archive unselected source',
                 'php-src/Makefile': 'archive core makefile'}
        files.update(extra or {})
        with tarfile.open(self.path, 'w:gz') as archive:
            for name, contents in files.items():
                member = tarfile.TarInfo(name)
                if symlink and name.endswith('bcmath/config.m4'):
                    member.type = tarfile.SYMTYPE
                    member.linkname = '../../../escape'
                    archive.addfile(member)
                else:
                    contents = contents.encode()
                    member.size = len(contents)
                    member.mode = 0o644
                    archive.addfile(member, io.BytesIO(contents))
        self.cache = {'php-src': {'source': {'cache_type': 'archive', 'filename': self.path.name,
            'hash': frozen.archive_identity(self.path)[0], 'version': self.version, 'config': {'type': 'php-release'}}}}
        frozen.write(self.downloads / '.cache.json', self.cache)
        frozen.capture(self.root, self.version)

    def run_repair(self):
        return repair.repair(self.root, self.version, ['bcmath'])

    def test_only_missing_selected_bundled_sources_are_created(self):
        result = self.run_repair()
        self.assertEqual(result['selected_bundled_extensions'], ['bcmath'])
        self.assertEqual((self.source / 'ext/bcmath/config.m4').read_text(), 'exact pinned configuration')
        self.assertEqual((self.source / 'ext/bcmath/bcmath.c').read_text(), 'exact pinned source')
        for name, content in self.protected.items():
            self.assertEqual((self.source / name).read_bytes(), content, name)
        self.assertEqual(self.run_repair()['created_files'], {})

    def test_standalone_artifact_source_is_not_rehydrated(self):
        self.cache['ext-bcmath'] = {'source': {'cache_type': 'archive', 'filename': 'separate.tgz'}}
        frozen.write(self.downloads / '.cache.json', self.cache)
        self.assertEqual(self.run_repair()['selected_bundled_extensions'], [])
        self.assertFalse((self.source / 'ext/bcmath/config.m4').exists())

    def test_modified_frozen_archive_rejected_before_writing(self):
        self.path.write_bytes(self.path.read_bytes() + b'changed')
        with self.assertRaisesRegex(RuntimeError, 'SHA1'):
            self.run_repair()
        self.assertFalse((self.source / 'ext/bcmath/config.m4').exists())

    def test_escaping_archive_member_rejected_before_writing(self):
        self.archive({'php-src/ext/bcmath/../../escape': 'invalid'})
        with self.assertRaisesRegex(RuntimeError, 'Escaping'):
            self.run_repair()
        self.assertFalse((self.source / 'ext/bcmath/config.m4').exists())

    def test_archive_symlink_rejected_before_writing(self):
        self.archive(symlink=True)
        with self.assertRaisesRegex(RuntimeError, 'Unsupported selected'):
            self.run_repair()
        self.assertFalse((self.source / 'ext/bcmath/config.m4').exists())

    def test_destination_symlink_rejected(self):
        target = self.root / 'outside'
        target.mkdir()
        (self.source / 'ext/bcmath/link').symlink_to(target, target_is_directory=True)
        self.archive({'php-src/ext/bcmath/link/file.c': 'invalid'})
        with self.assertRaisesRegex(RuntimeError, 'destination contains a symlink'):
            self.run_repair()
        self.assertEqual(list(target.iterdir()), [])

    def test_different_source_lock_rejected(self):
        self.cache['php-src']['source']['hash'] = '0' * 40
        frozen.write(self.downloads / '.cache.json', self.cache)
        with self.assertRaisesRegex(RuntimeError, 'source lock'):
            self.run_repair()

    def test_changed_core_and_unselected_request_rejected(self):
        with self.assertRaisesRegex(RuntimeError, 'matching verified core seed'):
            repair.repair(self.root, self.version, ['bz2'])
        (self.root / 'buildroot/core').write_text('different binary')
        with self.assertRaisesRegex(RuntimeError, 'core changed'):
            self.run_repair()


if __name__ == '__main__':
    unittest.main()
