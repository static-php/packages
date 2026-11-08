#!/usr/bin/env python3
import copy
import importlib.util
import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest
import subprocess
import sys

HELPER = Path(__file__).resolve().parents[1] / '.github/actions/shared-libraries/php-source.py'
spec = importlib.util.spec_from_file_location('php_source', HELPER)
source = importlib.util.module_from_spec(spec)
spec.loader.exec_module(source)


class PhpSources(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='spp-php-sources-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.downloads = self.root / 'downloads'
        self.downloads.mkdir()
        (self.root / 'config/templates').mkdir(parents=True)
        (self.root / 'config/templates/craft.yml.twig').write_text("download-options:\n    custom-git:\n        - 'ext-apcu:master:https://example.test/apcu.git'\ncraft-options:\n    download: true\n")
        source.write(self.downloads / '.spc-meta.json', {'php_versions': {'8.2': '8.2.35', '8.3': '8.3.36', '8.4': '8.4.27', '8.5': '8.5.12', '8.6': '8.6.0RC4'}})
        self.cache = {'ext-imagick': {'source': {'hash': 'frozen-extension-commit', 'version': 'master'}},
                      'php-src': {'binary': []}}

    def archive(self, version, header=None, extension='gz', extra_header=False):
        path = self.downloads / ('php-' + version + '.tar.' + extension)
        with tarfile.open(path, 'w:' + extension) as archive:
            names = ['php-src/main/php_version.h']
            if extra_header:
                names.append('other/main/php_version.h')
            for name in names:
                contents = ('#define PHP_VERSION "' + (header or version) + '"\n').encode()
                member = tarfile.TarInfo(name)
                member.size = len(contents)
                member.mode = 0o644
                archive.addfile(member, io.BytesIO(contents))
        return path

    def record(self, version, extension='gz'):
        path = self.archive(version, extension=extension)
        self.cache['php-src']['source'] = {'lock_type': 'source', 'cache_type': 'archive', 'filename': path.name,
            'extract': None, 'hash': source.archive_identity(path)[0], 'time': 1, 'version': version,
            'config': {'type': 'php-release', 'domain': 'https://www.php.net'}, 'downloader': 'Actual\\PhpSource'}
        source.write(self.downloads / source.CACHE, self.cache)
        return path

    def stable_extension(self):
        path = self.downloads / 'apcu-5.1.28.tgz'
        path.write_bytes(b'Frozen PECL source bytes')
        record = {'lock_type': 'source', 'cache_type': 'archive', 'filename': path.name,
                  'hash': source.archive_identity(path)[0], 'version': '5.1.28',
                  'extract': 'php-src/ext/apcu', 'config': {'type': 'pecl', 'name': 'apcu'}}
        self.cache['ext-apcu'] = {'source': record, 'binary': []}
        source.write(self.downloads / source.CACHE, self.cache)
        return record

    def custom_extension(self):
        path = self.downloads / 'ext-apcu'
        path.mkdir()
        subprocess.check_call(['git', 'init', '--quiet', '--initial-branch=master', str(path)])
        (path / 'README').write_text('Frozen custom Git source\n')
        subprocess.check_call(['git', '-C', str(path), 'add', 'README'])
        subprocess.check_call(['git', '-c', 'user.email=fixture@example.test', '-c', 'user.name=Fixture', '-C', str(path), 'commit', '--quiet', '-m', 'Frozen fixture'])
        commit = subprocess.check_output(['git', '-C', str(path), 'rev-parse', 'HEAD'], text=True).strip()
        record = {'lock_type': 'source', 'cache_type': 'git', 'dirname': path.name,
                  'hash': commit, 'version': 'dev-master+' + commit[:7], 'extract': None,
                  'config': {'rev': 'master', 'url': 'https://example.test/apcu.git'}}
        self.cache['ext-apcu'] = {'source': record, 'binary': []}
        source.write(self.downloads / source.CACHE, self.cache)
        return record

    def test_capture_five_streams_then_select_without_changing_extensions(self):
        versions = ['8.2.35', '8.3.36', '8.4.27', '8.5.12', '8.6.0RC4']
        for version in versions:
            self.record(version)
            source.capture(self.root, version)
        before = source.read(self.downloads / source.CACHE)
        selected = source.select(self.root, '8.2.35')
        after = source.read(self.downloads / source.CACHE)
        self.assertEqual(set(source.read(self.downloads / source.INDEX)['sources']), set(versions))
        self.assertEqual(after['ext-imagick'], before['ext-imagick'])
        self.assertEqual(after['php-src']['binary'], before['php-src']['binary'])
        self.assertEqual(after['php-src']['source']['version'], '8.2.35')
        self.assertEqual(after['php-src']['source'], selected['source'])
        self.assertEqual(selected['provenance']['kind'], 'download-cache-capture')

    def run_library_source_selection(self):
        repository = HELPER.parents[3]
        (self.root / 'config/templates/craft.yml.twig').write_text((repository / 'config/templates/craft.yml.twig').read_text())
        helper = self.root / '.github/actions/shared-libraries/php-source.py'
        helper.parent.mkdir(parents=True)
        helper.symlink_to(HELPER)
        spc = self.root / 'spc-probe'
        spc.write_text('#!' + sys.executable + '\n' + '''import json, sys
from pathlib import Path
Path('spc-input.json').write_text(json.dumps({
    'arguments': sys.argv[1:],
    'cache': json.loads(Path('downloads/.cache.json').read_text()),
}))
sys.exit(1)
''')
        spc.chmod(0o755)
        command = r'''
namespace staticphp\step {
    function parse_ini_file($filename) {
        return $filename === '/etc/os-release'
            ? ['ID' => 'almalinux', 'VERSION_ID' => '8'] : \parse_ini_file($filename);
    }
}
namespace {
    require $argv[2] . '/vendor/autoload.php';
    define('BASE_PATH', $argv[1]);
    define('SPP_TYPE', 'rpm');
    define('SPP_TARGET', null);
    function getSharedLibrarySuffix(): string { return '-zts-84'; }
    putenv('SPP_SPC_BINARY=' . BASE_PATH . '/spc-probe');
    exit(\staticphp\step\RunSPC::run(phpVersion: '8.4', libsOnly: true) ? 1 : 0);
}
'''
        return subprocess.run(['php', '-r', command, str(self.root), str(repository)],
                              capture_output=True, text=True)

    def test_library_build_selects_pinned_php_before_spc(self):
        self.record('8.4.27', extension='xz')
        stable = source.capture(self.root, '8.4.27')['source']
        self.record('8.6.0RC4')
        source.capture(self.root, '8.6.0RC4')
        result = self.run_library_source_selection()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        probe = source.read(self.root / 'spc-input.json')
        self.assertIsNotNone(probe, result.stdout + result.stderr)
        self.assertEqual(probe['arguments'], ['craft', '--libs-only'])
        self.assertEqual(probe['cache']['php-src']['source'], stable)
        self.assertIn('download: false', (self.root / 'craft.yml').read_text())

    def test_library_build_rejects_changed_php_archive_before_spc(self):
        archive = self.record('8.4.27', extension='xz')
        source.capture(self.root, '8.4.27')
        self.record('8.6.0RC4')
        source.capture(self.root, '8.6.0RC4')
        archive.write_bytes(archive.read_bytes() + b'changed')
        result = self.run_library_source_selection()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('Frozen PHP source SHA1 differs', result.stdout + result.stderr)
        self.assertFalse((self.root / 'spc-input.json').exists())
        self.assertFalse((self.root / 'craft.yml').exists())

    def test_capture_rejects_other_current_release(self):
        self.record('8.6.0RC4')
        with self.assertRaisesRegex(RuntimeError, 'not the requested release'):
            source.capture(self.root, '8.2.35')
        self.assertFalse((self.downloads / source.INDEX).exists())

    def test_capture_verifies_recorded_sha1(self):
        self.record('8.6.0RC4')
        self.cache['php-src']['source']['hash'] = '0' * 40
        source.write(self.downloads / source.CACHE, self.cache)
        with self.assertRaisesRegex(RuntimeError, 'actual source record'):
            source.capture(self.root, '8.6.0RC4')

    def test_select_rejects_modified_archive(self):
        path = self.record('8.6.0RC4')
        source.capture(self.root, '8.6.0RC4')
        path.write_bytes(path.read_bytes() + b'changed')
        with self.assertRaisesRegex(RuntimeError, 'SHA1'):
            source.select(self.root, '8.6.0RC4')

    def test_header_prevents_relabelling_old_tarball(self):
        path = self.archive('8.2.35', header='8.2.34')
        source.write(self.downloads / source.CACHE, self.cache)
        with self.assertRaisesRegex(RuntimeError, 'header differs'):
            source.select(self.root, '8.2.35')
        self.assertFalse((self.downloads / source.INDEX).exists())

    def test_frozen_index_header_rechecked_even_with_new_hashes(self):
        self.record('8.6.0RC4')
        source.capture(self.root, '8.6.0RC4')
        path = self.archive('8.6.0RC4', header='8.6.0RC3')
        index = source.read(self.downloads / source.INDEX)
        entry = index['sources']['8.6.0RC4']
        entry['source']['hash'], entry['archive_sha256'] = source.archive_identity(path)
        entry['archive_sha1'] = entry['source']['hash']
        source.write(self.downloads / source.INDEX, index)
        with self.assertRaisesRegex(RuntimeError, 'header does not match'):
            source.select(self.root, '8.6.0RC4')

    def test_legacy_stable_archive_bootstrap_is_explicit_provenance(self):
        self.record('8.6.0RC4')
        stable = self.archive('8.5.12', extension='xz')
        before = source.read(self.downloads / source.CACHE)
        entry = source.select(self.root, '8.5.12')
        self.assertEqual(entry['source']['filename'], stable.name)
        self.assertEqual(entry['source']['version'], '8.5.12')
        self.assertEqual(entry['provenance'], {'kind': 'verified-legacy-archive', 'download_cache_record_available': False})
        self.assertEqual(source.read(self.downloads / source.CACHE)['ext-imagick'], before['ext-imagick'])
        self.assertNotEqual(entry['source']['hash'], before['php-src']['source']['hash'])
        self.assertEqual(source.read(self.downloads / source.INDEX)['sources']['8.6.0RC4']['source'], before['php-src']['source'])

    def test_matching_legacy_cache_preserves_actual_source_record(self):
        self.record('8.6.0RC4')
        original = copy.deepcopy(self.cache['php-src']['source'])
        entry = source.select(self.root, '8.6.0RC4')
        self.assertEqual(entry['source'], original)
        self.assertEqual(entry['provenance']['kind'], 'download-cache-capture')

    def test_ambiguous_unrecorded_archives_fail(self):
        self.record('8.6.0RC4')
        self.archive('8.2.35', extension='gz')
        self.archive('8.2.35', extension='xz')
        with self.assertRaisesRegex(RuntimeError, 'exactly one frozen archive'):
            source.select(self.root, '8.2.35')

    def test_rejects_source_archive_symlinks(self):
        path = self.record('8.6.0RC4')
        target = path.with_name('other.tar.gz')
        path.rename(target)
        path.symlink_to(target.name)
        with self.assertRaisesRegex(RuntimeError, 'Missing frozen'):
            source.capture(self.root, '8.6.0RC4')

    def test_requires_full_release(self):
        with self.assertRaisesRegex(RuntimeError, 'full pinned PHP'):
            source.select(self.root, '8.6')

    def test_rejects_known_broken_rc1_without_downloader_bypass(self):
        self.record('8.6.0RC1')
        with self.assertRaisesRegex(RuntimeError, 'upstream packaging issue'):
            source.select(self.root, '8.6.0RC1')

    def test_multiple_header_roots_fail(self):
        self.archive('8.2.35', extra_header=True)
        with self.assertRaisesRegex(RuntimeError, 'exactly one version header'):
            source.select(self.root, '8.2.35')

    def test_template_is_the_single_custom_git_recipe_source(self):
        self.assertEqual(source.custom_git(self.root), [('ext-apcu', 'master', 'https://example.test/apcu.git')])

    def test_stable_and_custom_extension_profiles_select_per_stream(self):
        self.record('8.2.35')
        source.capture(self.root, '8.2.35')
        self.record('8.6.0RC4')
        source.capture(self.root, '8.6.0RC4')
        stable = copy.deepcopy(self.stable_extension())
        source.capture_extensions(self.root, 'stable')
        custom = copy.deepcopy(self.custom_extension())
        source.capture_extensions(self.root, 'custom')
        before_common = source.read(self.downloads / source.CACHE)['ext-imagick']
        source.select(self.root, '8.2.35')
        current = source.read(self.downloads / source.CACHE)
        self.assertEqual(current['ext-apcu']['source'], stable)
        self.assertEqual(current['ext-imagick'], before_common)
        self.assertEqual(current['php-src']['source']['version'], '8.2.35')
        source.select(self.root, '8.6.0RC4')
        self.assertEqual(source.read(self.downloads / source.CACHE)['ext-apcu']['source'], custom)
        source.restore_stable(self.root)
        self.assertEqual(source.read(self.downloads / source.CACHE)['ext-apcu']['source'], stable)

    def test_changed_frozen_master_tree_fails_without_head_fetch(self):
        self.record('8.6.0RC4')
        self.custom_extension()
        source.capture_extensions(self.root, 'custom')
        (self.downloads / 'ext-apcu/README').write_text('Unrecorded change\n')
        with self.assertRaisesRegex(RuntimeError, 'Git tree changed'):
            source.select(self.root, '8.6.0RC4')

    def test_custom_capture_rejects_wrong_recipe(self):
        self.custom_extension()
        self.cache['ext-apcu']['source']['config']['rev'] = 'other'
        source.write(self.downloads / source.CACHE, self.cache)
        with self.assertRaisesRegex(RuntimeError, 'requested Git recipe'):
            source.capture_extensions(self.root, 'custom')

    def test_partial_extension_index_does_not_fall_back_to_another_stream(self):
        self.record('8.2.35')
        self.custom_extension()
        source.capture_extensions(self.root, 'custom')
        with self.assertRaisesRegex(RuntimeError, 'missing for PHP 8.2'):
            source.select(self.root, '8.2.35')


if __name__ == '__main__':
    unittest.main()
