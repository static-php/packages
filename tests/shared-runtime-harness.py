"""Local regression checks for the fresh-container harness (no Docker required)."""

import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import runpy
import shutil
import subprocess
import sys
import tarfile
import threading
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from types import SimpleNamespace


harness = runpy.run_path(str(Path(__file__).with_name('shared-runtime-containers.py')))
scope = harness['cell'].__globals__
matrix = runpy.run_path(str(Path(__file__).with_name('build-shared-matrix.py')))


class RuntimeHarness(unittest.TestCase):
    def test_local_repository_refresh_preserves_official_sources_and_full_solver(self):
        refresh = {
            'deb': ['apt-get', '-o', 'Dir::Etc::sourcelist=/etc/apt/sources.list.d/spp-runtime.list',
                    '-o', 'Dir::Etc::sourceparts=-', '-o', 'APT::Get::List-Cleanup=0', 'update'],
            'rpm': ['dnf', '--disablerepo=*', '--enablerepo=spp-runtime', 'makecache', '--refresh'],
        }
        for kind in ['deb', 'rpm']:
            with self.subTest(kind=kind), TemporaryDirectory(prefix='spp-local-refresh-unit-') as scratch:
                root = Path(scratch)
                config = root / ('etc/apt/sources.list.d' if kind == 'deb' else 'etc/yum.repos.d')
                config.mkdir(parents=True)
                official = config / ('official.list' if kind == 'deb' else 'official.repo')
                official.write_text('Official OS repository configuration')
                cache = root / ('var/lib/apt/lists/official_Packages' if kind == 'deb' else 'var/cache/dnf/baseos/repodata/repomd.xml')
                cache.parent.mkdir(parents=True)
                cache.write_text('Prepared official repository metadata')
                commands = []
                def mapped(value):
                    path = Path(value)
                    return root / str(path).lstrip('/') if str(path).startswith('/etc/') else path
                with patch.dict(scope, {'Path': mapped, 'run': lambda command, **kwargs: commands.append(command)}):
                    harness['configure_repo'](kind, '/spp/fixtures/static')
                    harness['configure_repo'](kind, '/spp/fixtures/shared')
                    harness['install'](kind, ['native-os-dependency'])
                self.assertEqual(commands[:2], [refresh[kind], refresh[kind]])
                self.assertEqual(commands[2], ['apt-get', 'install', '-y', '--no-remove', '--no-install-recommends', 'native-os-dependency']
                                 if kind == 'deb' else ['dnf', 'install', '-y', '--setopt=install_weak_deps=False', 'native-os-dependency'])
                source = config / ('spp-runtime.list' if kind == 'deb' else 'spp-runtime.repo')
                self.assertIn('/spp/fixtures/shared', source.read_text())
                self.assertNotIn('/spp/fixtures/static', source.read_text())
                self.assertEqual(official.read_text(), 'Official OS repository configuration')
                self.assertEqual(cache.read_text(), 'Prepared official repository metadata')

    @unittest.skipUnless(shutil.which('php'), 'Native PHP required')
    def test_real_php_probe_trims_scanned_ini_filenames(self):
        with TemporaryDirectory(prefix='spp-native-ini-probe-') as scratch:
            root = Path(scratch)
            binary = root / 'php-zts'
            shutil.copy2(shutil.which('php'), binary)
            scanned = root / 'conf.d'
            scanned.mkdir()
            (scanned / '00-first.ini').write_text('; native scan fixture\n')
            last = scanned / '99-last.ini'
            last.write_text('; native scan fixture\n')
            modules = root / 'modules'
            modules.mkdir()
            primary = root / 'php.ini'
            primary.write_text('extension_dir="' + str(modules) + '"\n')
            environment = dict(os.environ, PHP_INI_SCAN_DIR=str(scanned))
            raw = harness['run']([binary, '-c', primary, '-r', 'echo json_encode(php_ini_scanned_files());'], env=environment)
            self.assertTrue(json.loads(raw.stdout).endswith(str(last) + '\n'))
            probe = harness['run']([binary, '-c', primary, Path(__file__).with_name('shared-runtime.php')], env=environment)
            data = json.loads(probe.stdout)
            self.assertIn(str(primary), data['ini_files'])
            self.assertIn(str(last), data['ini_files'])
            self.assertTrue(all(Path(filename).is_file() for filename in data['ini_files']))
            harness['extension_inventory'](data, 'CLI')

    def test_native_provides_formats_extract_only_cli_builtin_features(self):
        expected = ['curl', 'mbregex', 'opcache', 'openssl', 'password-argon2', 'swoole-hook-mysql', 'zlib']
        capabilities = ['php-zts', 'php-zts-cli', 'php-zts-devel', 'php-zts-lib-openssl',
                        *['php-zts-' + feature for feature in expected]]
        formats = {
            'rpm': '\n'.join(name + ' = 8.4.24-1' for name in capabilities) + '\nphp-zts-cli(x86-64) = 8.4.24-1\n',
            'deb': ', '.join(name + ' (= 8.4.24-1)' for name in capabilities),
            'apk': 'php-zts-cli-8.4.24-r0 provides:\n' + '\n'.join(name + '=8.4.24-r0' for name in capabilities),
        }
        for kind, output in formats.items():
            self.assertEqual(harness['parse_builtin_provides'](kind, output), expected, kind)

    def test_advertised_builtins_are_required_without_any_extension_dsos(self):
        with TemporaryDirectory(prefix='spp-builtin-inventory-') as scratch:
            base = {'sapi': 'cli', 'extension_dir': scratch, 'ini_files': [],
                    'extensions': ['Core', 'date', 'pcre', 'json', 'standard'],
                    'zend_extensions': [], 'loaded_objects': [], 'features': {}}
            for sapi, name, module in (('CLI', 'cli', None), ('CGI', 'cgi-fcgi', 'cgi-fcgi'),
                                       ('FPM', 'fpm-fcgi', 'cgi-fcgi'), ('FrankenPHP', 'frankenphp', 'frankenphp')):
                data = dict(base, sapi=name, extensions=base['extensions'] + ([module] if module else []))
                with self.assertRaisesRegex(RuntimeError, 'advertised builtin modules missing'):
                    harness['extension_inventory'](data, sapi, builtins=['curl', 'openssl', 'mbstring', 'zlib'])

    def test_advertised_addon_features_use_explicit_modules_and_function_flags(self):
        with TemporaryDirectory(prefix='spp-builtin-inventory-') as scratch:
            data = {'sapi': 'cli', 'extension_dir': scratch, 'ini_files': [],
                    'extensions': ['standard', 'mbstring', 'Zend OPcache', 'swoole'],
                    'zend_extensions': ['Zend OPcache'], 'loaded_objects': [],
                    'features': {'mbregex': True, 'password-argon2': True}}
            features = ['mbregex', 'password-argon2', 'opcache', 'swoole-hook-mysql']
            harness['extension_inventory'](data, 'CLI', builtins=features)
            for feature in ('mbregex', 'password-argon2'):
                flags = dict(data['features'], **{feature: False})
                with self.assertRaisesRegex(RuntimeError, 'builtin feature unavailable: ' + feature):
                    harness['extension_inventory'](dict(data, features=flags), 'CLI', builtins=features)
            with self.assertRaisesRegex(RuntimeError, 'builtin modules missing'):
                harness['extension_inventory'](dict(data, extensions=['standard', 'mbstring', 'Zend OPcache']), 'CLI', builtins=['swoole-hook-mysql'])

    def test_skip_fixtures_avoids_compilers_and_fixture_generation(self):
        with TemporaryDirectory(prefix='spp-fixture-skip-') as scratch:
            root = Path(scratch)
            resources = harness['DockerRun'](root)
            for kind in ('rpm', 'deb', 'apk'):
                commands = []
                with patch.object(resources, 'container', return_value='helper'), patch.object(resources, 'exec', side_effect=lambda name, command: commands.append(command)):
                    resources.prepare('base', kind, helper=True, fixtures=False)
                self.assertNotIn('gcc', ' '.join(commands[0]))
                self.assertNotIn('rpm-build', commands[0])
                self.assertIn('--skip-fixtures', commands[1])
            (root / 'spp/repo').mkdir(parents=True)
            def mapped(value):
                return root / str(value).lstrip('/') if str(value).startswith('/spp/') else Path(value)
            with patch.dict(scope, {'Path': mapped}), patch.dict(scope, {'build_fixtures': lambda *args: self.fail('Skipped fixtures were generated')}):
                harness['inside'](SimpleNamespace(type='apk', inside='prepare', skip_fixtures=True))
            self.assertEqual((root / 'spp/package-names.json').read_text(), '[]')

    def test_installed_extensions_require_active_scanned_directives_and_registration(self):
        with TemporaryDirectory(prefix='spp-extension-inventory-') as scratch:
            root = Path(scratch)
            modules = root / 'modules'
            modules.mkdir()
            extension = modules / 'curl-zts-84.so'
            extension.touch()
            ini = root / 'loaded.ini'
            ignored = root / 'not-scanned.ini'
            ignored.write_text('extension=curl-zts-84\n')
            data = {'sapi': 'cli', 'extension_dir': str(modules), 'ini_files': [str(ini)],
                    'extensions': ['curl'], 'zend_extensions': [], 'loaded_objects': [str(extension)]}
            for text in (';extension=curl-zts-84\n', '#extension=curl-zts-84\n', ';See not-scanned.ini\n'):
                ini.write_text(text)
                with self.assertRaisesRegex(RuntimeError, 'no active directive'):
                    harness['extension_inventory'](data, 'CLI')
            ini.write_text('extension="curl-zts-84" ; active\n')
            harness['extension_inventory'](data, 'CLI')
            with self.assertRaisesRegex(RuntimeError, 'did not register curl'):
                harness['extension_inventory'](dict(data, extensions=[]), 'CLI')
            with self.assertRaisesRegex(RuntimeError, 'not mapped'):
                harness['extension_inventory'](dict(data, loaded_objects=[]), 'CLI')

    def test_intentionally_disabled_shared_module_is_absent_in_every_sapi(self):
        with TemporaryDirectory(prefix='spp-disabled-inventory-') as directory:
            root = Path(directory)
            module = root / 'imagick-zts-86.so'
            module.touch()
            ini = root / '30-imagick.ini.disabled'
            ini.write_text('extension=imagick-zts-86\n; user choice\n')
            data = {'sapi': 'cli', 'extension_dir': str(root), 'ini_files': [],
                    'extensions': ['standard'], 'zend_extensions': [], 'loaded_objects': []}
            result = harness['extension_inventory'](data, 'CLI', disabled=['imagick'])
            self.assertEqual(result['files'], set())
            for changed in [dict(data, extensions=['standard', 'imagick']),
                            dict(data, loaded_objects=[str(module)]),
                            dict(data, ini_files=[str(ini)])]:
                with self.assertRaisesRegex(RuntimeError, 'intentionally disabled'):
                    harness['extension_inventory'](changed, 'CLI', disabled=['imagick'])
            with self.assertRaisesRegex(RuntimeError, 'no active directive'):
                harness['extension_inventory'](data, 'CLI')

    def test_zend_aliases_and_mysqlnd_plugins_need_real_registered_modules(self):
        with TemporaryDirectory(prefix='spp-extension-inventory-') as scratch:
            root = Path(scratch)
            files = [root / name for name in ('opcache-zts-84.so', 'pdo-zts-84.so', 'mysqlnd_ed25519-zts-84.so')]
            for path in files:
                path.touch()
            ini = root / 'loaded.ini'
            ini.write_text('zend_extension=opcache-zts-84\nextension=pdo-zts-84\nextension=mysqlnd_ed25519-zts-84\n')
            data = {'sapi': 'cli', 'extension_dir': str(root), 'ini_files': [str(ini)],
                    'extensions': ['Zend OPcache', 'PDO', 'mysqlnd_ed25519'],
                    'zend_extensions': ['Zend OPcache'], 'loaded_objects': [str(path) for path in files]}
            inventory = harness['extension_inventory'](data, 'CLI')
            self.assertIn('zend opcache', inventory['zend_extensions'])
            with self.assertRaisesRegex(RuntimeError, 'did not register zend opcache'):
                harness['extension_inventory'](dict(data, zend_extensions=[]), 'CLI')
            with self.assertRaisesRegex(RuntimeError, 'did not register mysqlnd_ed25519'):
                harness['extension_inventory'](dict(data, extensions=['Zend OPcache', 'PDO']), 'CLI')

    def test_every_web_sapi_must_match_cli_extension_set(self):
        with TemporaryDirectory(prefix='spp-extension-inventory-') as scratch:
            data = {'sapi': 'cli', 'extension_dir': scratch, 'ini_files': [], 'extensions': ['json', 'swoole'],
                    'zend_extensions': [], 'loaded_objects': []}
            reference = harness['extension_inventory'](data, 'CLI')
            for sapi, name, module in (('CGI', 'cgi-fcgi', 'cgi-fcgi'), ('FPM', 'fpm-fcgi', 'cgi-fcgi'),
                                       ('FrankenPHP', 'frankenphp', 'frankenphp')):
                with self.subTest(sapi=sapi):
                    valid = dict(data, sapi=name, extensions=['json', 'swoole', module])
                    self.assertEqual(harness['extension_inventory'](valid, sapi, reference), reference)
                    with self.assertRaisesRegex(RuntimeError, 'module.*register'):
                        harness['extension_inventory'](dict(data, sapi=name), sapi, reference)
                    wrong_module = 'frankenphp' if module == 'cgi-fcgi' else 'cgi-fcgi'
                    with self.assertRaisesRegex(RuntimeError, 'module.*register'):
                        harness['extension_inventory'](dict(data, sapi=name, extensions=['json', 'swoole', wrong_module]), sapi)
                    for extensions in (['json', module], ['json', 'swoole', module, 'unexpected'],
                                       ['json', 'swoole', module, wrong_module]):
                        with self.assertRaisesRegex(RuntimeError, 'extensions differ from CLI'):
                            harness['extension_inventory'](dict(data, sapi=name, extensions=extensions), sapi, reference)
                    with self.assertRaisesRegex(RuntimeError, 'zend_extensions differ from CLI'):
                        harness['extension_inventory'](dict(valid, zend_extensions=['unexpected']), sapi, reference)
                    with self.assertRaisesRegex(RuntimeError, 'expected SAPI ' + name):
                        harness['extension_inventory'](dict(valid, sapi='cli'), sapi, reference)

    def test_named_consumers_require_their_payloads_and_extensions_require_cli(self):
        with TemporaryDirectory(prefix='spp-required-payload-') as scratch:
            root = Path(scratch)
            def mapped(value):
                return root / str(value).lstrip('/') if str(value).startswith('/usr/') else Path(value)
            with patch.dict(scope, {'Path': mapped}):
                for name in ('php-zts-cli', 'php-zts-cgi', 'php-zts-fpm', 'frankenphp', 'php-zts-devel', 'php-zts-embed'):
                    with self.assertRaisesRegex(RuntimeError, 'required .*payload missing'):
                        harness['required_payloads']('8.2', [name], 'deb')
                with self.assertRaisesRegex(RuntimeError, 'did not resolve php-zts-cli'):
                    harness['required_payloads']('8.4', ['php-zts-curl'], 'deb')
                library = root / 'usr/lib/libphp-zts-84.so'
                library.parent.mkdir(parents=True)
                library.touch()
                with self.assertRaisesRegex(RuntimeError, 'payload missing for PHP 8.2'):
                    harness['required_payloads']('8.2', ['php-zts-embed'], 'deb')

    def test_individual_extension_without_cli_fails_before_recording_success(self):
        with TemporaryDirectory(prefix='spp-individual-runtime-') as scratch:
            root = Path(scratch)
            (root / 'spp').mkdir()
            (root / 'spp/package-names.json').write_text('["php-zts-curl"]')
            def mapped(value):
                return root / str(value).lstrip('/') if str(value).startswith(('/usr/', '/spp/')) else Path(value)
            with patch.dict(os.environ, {'LD_LIBRARY_PATH': ''}), patch.dict(scope, {'Path': mapped, 'configure_repo': lambda *args: None,
                                    'install': lambda *args: None, 'installed': lambda *args: '1',
                                    'installed_packages': lambda *args: {'php-zts-curl'},
                                    'assert_elf': lambda *args: {}}):
                args = SimpleNamespace(type='deb', inside='individual', package='php-zts-curl', php='8.4')
                with self.assertRaisesRegex(RuntimeError, 'did not resolve php-zts-cli'):
                    harness['inside'](args)

    def test_elf_dependency_queries_are_cached_only_within_one_validation(self):
        with TemporaryDirectory(prefix='spp-elf-dependencies-') as scratch:
            root = Path(scratch)
            files = [root / ('consumer-' + str(number) + '.so') for number in range(3)]
            for path in files:
                path.write_bytes(b'\x7fELFfixture')
            owner = 'php-zts-consumer'
            providers = {'first': 'libfirst.so.1', 'second': 'libsecond.so.2'}
            manifest = root / 'manifest.json'
            manifest.write_text(json.dumps({'libraries': {
                name: {'sonames': [soname], 'elf': {soname: {'soname': soname}}}
                for name, soname in providers.items()
            }}))
            queries = []
            required = []
            for name in providers:
                required.extend(['php-zts-lib-' + name, 'php-zts-lib-' + name + '-abi-1'])
            answer = ['']
            def dependency_text(kind, entries):
                floor = {'deb': ' (>= 1.0)', 'rpm': ' >= 1.0', 'apk': '>=1.0'}[kind]
                lines = [name + ('' if '-abi-' in name else floor) for name in entries]
                return ', '.join(lines) if kind == 'deb' else (owner + ' depends on:\n' if kind == 'apk' else '') + '\n'.join(lines)
            def dependencies(kind, name):
                queries.append((kind, name))
                return answer[0]
            def inspected(command, **kwargs):
                self.assertIn(Path(command[-1]), files)
                self.assertIn(command[0], ('readelf', 'ldd'))
                return subprocess.CompletedProcess(command, 0, dynamic if command[0] == 'readelf' else resolved, '')
            with patch.dict(scope, {'payload': lambda kind, names: files, 'run': inspected, 'dependencies': dependencies}):
                for kind in ('deb', 'rpm', 'apk'):
                    private = '/usr/lib/php-zts/lib'
                    dynamic = '\n'.join('(NEEDED) Shared library: [' + soname + ']' for soname in providers.values())
                    dynamic += '\n(RUNPATH) Library runpath: [' + private + ']\n'
                    resolved_root = '/usr/lib64/php-zts/lib' if kind == 'rpm' else private
                    resolved = '\n'.join(soname + ' => ' + resolved_root + '/' + soname + ' (0x1)' for soname in providers.values())
                    answer[0] = dependency_text(kind, required)
                    before = len(queries)
                    self.assertEqual(harness['assert_elf'](kind, [owner], manifest),
                                     {'elf_objects': 3, 'private_runpaths': 3})
                    self.assertEqual(queries[before:], [(kind, owner)])
                    for name in providers:
                        for missing in ('provider', 'abi', 'prefixed', 'suffixed'):
                            prefix = 'php-zts-lib-' + name
                            entries = [value for value in required if value != (prefix + '-abi-1' if missing == 'abi' else prefix)]
                            if missing in ('prefixed', 'suffixed'):
                                entries.append('other-' + prefix if missing == 'prefixed' else prefix + '-other')
                            with self.subTest(kind=kind, provider=name, missing=missing):
                                answer[0] = dependency_text(kind, entries)
                                before = len(queries)
                                with self.assertRaisesRegex(RuntimeError, 'lacks its private package and ABI dependencies on ' + prefix):
                                    harness['assert_elf'](kind, [owner], manifest)
                                self.assertEqual(queries[before:], [(kind, owner)])
                    answer[0] = dependency_text(kind, required)
                    before = len(queries)
                    self.assertEqual(harness['assert_elf'](kind, [owner], manifest)['elf_objects'], 3)
                    self.assertEqual(queries[before:], [(kind, owner)])

    @unittest.skipUnless(shutil.which('php-cgi-zts') or shutil.which('php-cgi'), 'Native PHP CGI required')
    def test_cgi_get_request_executes_script_with_explicit_filename(self):
        cgi = shutil.which('php-cgi-zts') or shutil.which('php-cgi')
        with TemporaryDirectory(prefix='spp-cgi-runtime-') as scratch:
            root = Path(scratch)
            script = root / 'spp/shared-runtime.php'
            script.parent.mkdir()
            modules = root / 'modules'
            modules.mkdir()
            scanned = root / 'conf.d'
            scanned.mkdir()
            script.write_text(
                '<?php if (($_SERVER["SCRIPT_FILENAME"] ?? "") !== __FILE__ || '
                '($_SERVER["SCRIPT_NAME"] ?? "") !== "/shared-runtime.php" || '
                '($_SERVER["REQUEST_METHOD"] ?? "") !== "GET" || '
                '($_SERVER["REDIRECT_STATUS"] ?? "") !== "200") { exit(2); } '
                'echo json_encode(["ok"=>true,"version"=>"8.4.0","sapi"=>PHP_SAPI,'
                '"extensions"=>get_loaded_extensions(),"zend_extensions"=>get_loaded_extensions(true),'
                '"ini_files"=>[],"loaded_objects"=>[],"extension_dir"=>' + json.dumps(str(modules)) + ']);')
            environment = dict(os.environ, PHPRC='/dev/null', PHP_INI_SCAN_DIR=str(scanned),
                               REDIRECT_STATUS='200', REQUEST_METHOD='GET')
            for name in ('SCRIPT_FILENAME', 'SCRIPT_NAME', 'PATH_TRANSLATED', 'PATH_INFO'):
                environment.pop(name, None)
            old = harness['run']([cgi, '-f', script], env=environment, check=False)
            self.assertNotEqual(old.returncode, 0)
            self.assertIn('No input file specified', old.stdout)
            def mapped(value):
                if str(value) == '/usr/bin/php-cgi-zts':
                    return Path(cgi)
                return root / str(value).lstrip('/') if str(value).startswith(('/usr/', '/spp/')) else Path(value)
            with patch.dict(scope, {'Path': mapped, 'required_payloads': lambda *args: []}), patch.dict(os.environ, environment, clear=True):
                result = harness['sapi_checks']('8.4', ['php-zts-cgi'], 'deb')
            self.assertEqual(result['sapis'], ['cgi'])

    @unittest.skipUnless(sys.platform.startswith('linux') and shutil.which('cc'), 'Native ELF compiler required')
    def test_standalone_embed_runs_isolated_startup_loaded_api_without_cli(self):
        with TemporaryDirectory(prefix='spp-embed-runtime-') as scratch:
            root = Path(scratch)
            library = root / 'usr/lib/libphp-zts-84.so'
            library.parent.mkdir(parents=True)
            source = root / 'embed.c'
            source.write_text(r'''
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <dirent.h>

static __thread volatile unsigned char state[65536] __attribute__((tls_model("initial-exec")));

static int mode(const char *value) {
    const char *current = getenv("SPP_EMBED_MODE");
    return current && strcmp(current, value) == 0;
}

static void audit(const char *event) {
    FILE *file = fopen(getenv("SPP_EMBED_AUDIT"), "a");
    if (!file) abort();
    fprintf(file, "%s:%ld\n", event, (long)getpid());
    fclose(file);
}

int php_embed_init(int argc, char **argv) {
    audit("init");
    if (argc != 2 || strcmp(argv[0], "spp-embed") || strcmp(argv[1], "-n") || argv[2]) return 7;
    if (getenv("LD_LIBRARY_PATH") || !getenv("LD_PRELOAD")) return 8;
    const char *phprc = getenv("PHPRC"), *scan = getenv("PHP_INI_SCAN_DIR");
    if (!phprc || strcmp(phprc, "/dev/null") || !scan || !*scan) return 12;
    DIR *directory = opendir(scan);
    if (!directory) return 13;
    struct dirent *entry;
    while ((entry = readdir(directory))) {
        if (strcmp(entry->d_name, ".") && strcmp(entry->d_name, "..")) {
            closedir(directory);
            return 14;
        }
    }
    closedir(directory);
    if (mode("init-fail")) return 9;
    state[0] = 1;
    state[65535] = 2;
    if (mode("warning")) fputs("PHP Startup: fixture warning\n", stderr);
    return 0;
}

#ifndef SPP_OMIT_EVAL
int zend_eval_string(const char *code, void *result, const char *filename) {
    audit("eval");
    if (state[0] != 1 || state[65535] != 2 || result || !filename || strcmp(code, "echo \"SPP_EMBED_OK\";")) return 10;
    if (mode("eval-fail")) return 11;
    if (!mode("no-marker")) fputs("SPP_EMBED_OK", stdout);
    return 0;
}
#endif

void php_embed_shutdown(void) {
    audit("shutdown");
    state[0] = 0;
}
''')
            harness['run'](['cc', '-shared', '-fPIC', source, '-o', library])
            audit = root / 'lifecycle'
            inherited = root / 'inherited-conf.d'
            inherited.mkdir()
            (inherited / 'fixture.ini').write_text('extension=must-not-load\n')
            environment = {'SPP_EMBED_AUDIT': str(audit), 'SPP_EMBED_MODE': '', 'LD_LIBRARY_PATH': '/spp/untrusted',
                           'PHPRC': str(inherited / 'fixture.ini'), 'PHP_INI_SCAN_DIR': str(inherited)}
            def mapped(value):
                return root / str(value).lstrip('/') if str(value).startswith('/usr/') else Path(value)
            with patch.dict(scope, {'Path': mapped}), patch.dict(os.environ, environment):
                result = harness['sapi_checks']('8.4', ['php-zts-embed'], 'deb')
            self.assertEqual(result['sapis'], [])
            self.assertEqual(result['embed_libraries'], [str(library)])
            events = [line.split(':') for line in audit.read_text().splitlines()]
            self.assertEqual([event for event, _ in events], ['init', 'eval', 'shutdown'])
            self.assertEqual(len({pid for _, pid in events}), 1)
            self.assertNotEqual(int(events[0][1]), os.getpid())
            for mode, error, expected in (
                ('init-fail', 'php_embed_init failed: 9', ['init']),
                ('eval-fail', 'zend_eval_string failed: 11', ['init', 'eval', 'shutdown']),
                ('no-marker', 'Installed embed API probe failed', ['init', 'eval', 'shutdown']),
                ('warning', 'fixture warning', ['init', 'eval', 'shutdown']),
            ):
                with self.subTest(mode=mode):
                    audit.unlink()
                    with patch.dict(os.environ, dict(environment, SPP_EMBED_MODE=mode)):
                        with self.assertRaisesRegex(RuntimeError, error):
                            harness['embed_check'](library)
                    self.assertEqual([line.split(':')[0] for line in audit.read_text().splitlines()], expected)
            missing = root / 'missing-api.so'
            harness['run'](['cc', '-shared', '-fPIC', '-DSPP_OMIT_EVAL', source, '-o', missing])
            with patch.dict(os.environ, environment):
                with self.assertRaisesRegex(RuntimeError, 'zend_eval_string'):
                    harness['embed_check'](missing)

    def test_interruption_reaps_client_before_container_cleanup(self):
        original = subprocess.Popen
        clients = []
        class InterruptedClient:
            def __init__(self, *args, **kwargs):
                self.process = original(*args, **kwargs)
                self.interrupted = False
                clients.append(self.process)
            def communicate(self, *args, **kwargs):
                if not self.interrupted:
                    self.interrupted = True
                    raise KeyboardInterrupt()
                return self.process.communicate(*args, **kwargs)
            def __getattr__(self, name):
                return getattr(self.process, name)
        with TemporaryDirectory(prefix='spp-runtime-interrupt-') as scratch:
            with patch.object(subprocess, 'Popen', InterruptedClient):
                with self.assertRaises(KeyboardInterrupt):
                    harness['run']([sys.executable, '-c', 'import time; time.sleep(30)'], cwd=scratch)
        self.assertEqual(len(clients), 1)
        self.assertIsNotNone(clients[0].poll())

    def test_apk_control_and_data_members(self):
        with TemporaryDirectory(prefix='spp-runtime-unit-') as scratch:
            root = Path(scratch)
            source = root / 'consumer'
            source.write_text('temporary payload')
            harness['fixture_package']('apk', root / 'repo', 'php-zts-fixture', '1.0.0',
                                       {'/usr/bin/consumer': source},
                                       depends=['php-zts-lib-fixture (>= 1.0.0)', 'php-zts-lib-fixture-abi-1'])
            package = next((root / 'repo').glob('*.apk'))
            self.assertEqual(harness['metadata']('apk', package), 'php-zts-fixture')
            with tarfile.open(fileobj=io.BytesIO(gzip.decompress(package.read_bytes())), mode='r:') as control:
                info = control.extractfile('.PKGINFO').read().decode()
            self.assertIn('depend = php-zts-lib-fixture>=1.0.0', info)
            self.assertIn('depend = php-zts-lib-fixture-abi-1', info)
            # Locate the second gzip member by consuming the first with zlib.
            inflater = harness['gzip'].zlib.decompressobj(31)
            inflater.decompress(package.read_bytes())
            data = inflater.unused_data
            self.assertIn('datahash = ' + hashlib.sha256(data).hexdigest(), info)
            with tarfile.open(fileobj=io.BytesIO(gzip.decompress(data)), mode='r:') as payload:
                self.assertEqual(payload.extractfile('usr/bin/consumer').read(), b'temporary payload')

    def test_apk_repository_uses_header_names_for_architecture_suffixed_artifacts(self):
        with TemporaryDirectory(prefix='spp-apk-canonical-') as scratch:
            root = Path(scratch)
            source = root / 'payload'
            source.write_text('temporary payload')
            repo = root / 'repo'
            harness['fixture_package']('apk', repo, 'php-zts-lib-fixture', '1.0.0', {'/usr/share/fixture': source})
            canonical = repo / 'php-zts-lib-fixture-1.0.0-r0.apk'
            artifact = canonical.with_name('misleading-name-9.9-r9.x86_64.apk')
            canonical.rename(artifact)
            artifact.write_bytes(harness['tar_gz']({'.SIGN.RSA.fixture.rsa.pub': b'fixture signature'}) + artifact.read_bytes())
            original = artifact.read_bytes()
            commands = []
            with patch.dict(scope, {'run': lambda command, **kwargs: commands.append(command)}):
                harness['index_repo']('apk', repo)
            indexed = repo / 'x86_64' / canonical.name
            self.assertEqual(indexed.read_bytes(), original)
            self.assertEqual(artifact.read_bytes(), original)
            self.assertFalse((repo / 'x86_64' / artifact.name).exists())
            self.assertEqual(commands, [['apk', 'index', '--allow-untrusted', '-o', repo / 'x86_64/APKINDEX.tar.gz', indexed]])
            self.assertEqual(harness['metadata']('apk', indexed), 'php-zts-lib-fixture')

    @unittest.skipUnless(shutil.which('apk'), 'Native Alpine tools required')
    def test_native_apk_installs_architecture_suffixed_artifact_from_local_repository(self):
        with TemporaryDirectory(prefix='spp-apk-native-repo-') as scratch:
            root = Path(scratch)
            source = root / 'payload'
            source.write_text('temporary native payload')
            repo = root / 'repo'
            harness['fixture_package']('apk', repo, 'php-zts-lib-fixture', '1.0.0', {'/usr/share/fixture': source})
            package = repo / 'php-zts-lib-fixture-1.0.0-r0.apk'
            package.rename(package.with_name('php-zts-lib-fixture-1.0.0-r0.x86_64.apk'))
            harness['index_repo']('apk', repo)
            installed = root / 'installed'
            installed.mkdir()
            harness['run'](['apk', '--root', installed, '--initdb', '--no-network', '--allow-untrusted',
                            '--repository', repo, 'add', 'php-zts-lib-fixture'])
            self.assertEqual((installed / 'usr/share/fixture').read_text(), 'temporary native payload')

    @unittest.skipUnless(shutil.which('rpmbuild') and shutil.which('rpm'), 'RPM tools required')
    def test_native_rpm_private_capability(self):
        with TemporaryDirectory(prefix='spp-runtime-unit-') as scratch:
            root = Path(scratch)
            source = root / 'consumer'
            source.write_text('temporary payload')
            harness['fixture_package']('rpm', root / 'repo', 'php-zts-fixture', '1.0.0',
                                       {'/usr/share/fixture': source},
                                       depends=['php-zts-lib-fixture (>= 1.0.0)', 'php-zts-lib-fixture-abi-1'])
            package = next((root / 'repo').glob('*.rpm'))
            self.assertEqual(harness['metadata']('rpm', package), 'php-zts-fixture')
            requires = harness['run'](['rpm', '-qpR', '--nosignature', package]).stdout
            self.assertIn('php-zts-lib-fixture >= 1.0.0', requires)
            self.assertIn('php-zts-lib-fixture-abi-1', requires)

    @unittest.skipUnless(shutil.which('dpkg-deb'), 'Debian tools required')
    def test_native_debian_private_capability(self):
        with TemporaryDirectory(prefix='spp-runtime-unit-') as scratch:
            root = Path(scratch)
            source = root / 'consumer'
            source.write_text('temporary payload')
            harness['fixture_package']('deb', root / 'repo', 'php-zts-fixture', '1.0.0',
                                       {'/usr/share/fixture': source},
                                       depends=['php-zts-lib-fixture (>= 1.0.0)', 'php-zts-lib-fixture-abi-1'])
            package = next((root / 'repo').glob('*.deb'))
            self.assertEqual(harness['metadata']('deb', package), 'php-zts-fixture')
            requires = harness['run'](['dpkg-deb', '-f', package, 'Depends']).stdout
            self.assertIn('php-zts-lib-fixture (>= 1.0.0)', requires)
            self.assertIn('php-zts-lib-fixture-abi-1', requires)

    def test_runtime_reports_startup_warnings_and_wrong_vm(self):
        with self.assertRaisesRegex(RuntimeError, 'startup'):
            harness['result_json']('PHP Startup: missing dependency\n{}', '8.4')
        with self.assertRaisesRegex(RuntimeError, 'invalid Zend VM'):
            harness['result_json']('{"ok":true,"version":"8.6.0","vm":"unknown"}', '8.6')
        with self.assertRaisesRegex(RuntimeError, 'startup'):
            harness['result_json']('Cannot load Xdebug - it was already loaded\n{}', '8.4')
        self.assertTrue(harness['result_json']('{"ok":true,"version":"8.4.1"}', '8.4')['ok'])

    def test_installed_inventory_parses_native_state_without_virtual_capabilities(self):
        cases = {
            'rpm': ('php-zts-cli\nphp-zts-lib-base\nphp-zts-cli\n', {'php-zts-cli', 'php-zts-lib-base'}),
            'apk': ('php-zts-cli\nphp-zts-lib-base\n.spp-build-deps\n',
                    {'php-zts-cli', 'php-zts-lib-base', '.spp-build-deps'}),
            'deb': ('php-zts-cli\tii \nphp-zts-lib-base\tii \nphp-zts-removed\trc \n'
                    'php-zts-unpacked\tiU \nphp-zts-held\thi \nphp-zts-remove-requested\tri \nphp-zts-purge-requested\tpi \n'
                    'php-zts-broken\tiiR\nphp-zts-cli\tii \n',
                    {'php-zts-cli', 'php-zts-lib-base', 'php-zts-held', 'php-zts-remove-requested', 'php-zts-purge-requested'}),
        }
        for kind, (output, expected) in cases.items():
            commands = []
            def native(command, **kwargs):
                commands.append(command)
                return SimpleNamespace(stdout=output)
            with self.subTest(kind=kind), patch.dict(scope, {'run': native}):
                self.assertEqual(harness['installed_packages'](kind), expected)
                self.assertEqual(len(commands), 1)
                self.assertNotIn('php-zts-lib-base-abi-1', harness['installed_packages'](kind))

    def test_installed_inventory_rejects_malformed_or_failed_native_queries(self):
        cases = [('rpm', ''), ('rpm', 'php-zts-cli unexpected\n'),
                 ('apk', 'Available packages:\n'), ('apk', 'php-zts-cli\n\n'),
                 ('deb', 'php-zts-cli\tii\n'), ('deb', 'php-zts-cli\txx \n'),
                 ('deb', 'php-zts-cli\tii \textra\n')]
        for kind, output in cases:
            with self.subTest(kind=kind, output=output), patch.dict(scope, {'run': lambda *args: SimpleNamespace(stdout=output)}):
                with self.assertRaisesRegex(RuntimeError, 'inventory|package name'):
                    harness['installed_packages'](kind)
        with patch.dict(scope, {'run': lambda *args: (_ for _ in ()).throw(FileNotFoundError('missing native tool'))}):
            with self.assertRaisesRegex(FileNotFoundError, 'missing native tool'):
                harness['installed_packages']('rpm')

    def test_php86_accepts_supported_compiled_vm_kinds(self):
        for kind in ('CALL', 'SWITCH', 'GOTO', 'HYBRID', 'TAILCALL'):
            value = 'ZEND_VM_KIND_' + kind
            data = {'ok': True, 'version': '8.6.0beta1', 'vm': value}
            with self.subTest(kind=kind):
                self.assertEqual(harness['result_json'](json.dumps(data), '8.6')['vm'], value)
        for value in (None, '', 'unknown', 'CALL', 'ZEND_VM_KIND_FUTURE'):
            with self.subTest(value=value), self.assertRaisesRegex(RuntimeError, 'invalid Zend VM'):
                harness['result_json'](json.dumps({'ok': True, 'version': '8.6.0beta1', 'vm': value}), '8.6')

    def test_php86_sapi_vm_kind_must_match_cli(self):
        with TemporaryDirectory(prefix='spp-vm-kind-') as scratch:
            modules = Path(scratch)
            for vm in ('ZEND_VM_KIND_CALL', 'ZEND_VM_KIND_TAILCALL'):
                data = {'ok': True, 'version': '8.6.0beta1', 'sapi': 'cli', 'vm': vm,
                        'extension_dir': str(modules), 'ini_files': [], 'extensions': ['json'],
                        'zend_extensions': [], 'loaded_objects': []}
                reference = harness['extension_inventory'](data, 'CLI')
                for sapi, name, module in [('CGI', 'cgi-fcgi', 'cgi-fcgi'), ('FPM', 'fpm-fcgi', 'cgi-fcgi'),
                                           ('FrankenPHP', 'frankenphp', 'frankenphp')]:
                    valid = dict(data, sapi=name, extensions=['json', module])
                    with self.subTest(vm=vm, sapi=sapi):
                        self.assertEqual(harness['extension_inventory'](valid, sapi, reference), reference)
                        other = 'ZEND_VM_KIND_TAILCALL' if vm == 'ZEND_VM_KIND_CALL' else 'ZEND_VM_KIND_CALL'
                        with self.assertRaisesRegex(RuntimeError, 'Zend VM differs from CLI'):
                            harness['extension_inventory'](dict(valid, vm=other), sapi, reference)

    def test_failed_preparation_cleans_scratch_directory(self):
        created = []
        def fail(resources, *args, **kwargs):
            created.append(resources.directory)
            (resources.directory / 'partial-output').write_text('partial build')
            raise RuntimeError('injected preparation failure')
        with TemporaryDirectory(prefix='spp-runtime-unit-') as artifacts:
            (Path(artifacts) / 'fixture.apk').touch()
            with patch.object(harness['DockerRun'], 'prepare', fail):
                with self.assertRaisesRegex(RuntimeError, 'injected'):
                    harness['cell']('alpine', '8.4', artifacts)
        self.assertEqual(len(created), 1)
        self.assertFalse(created[0].exists())

    def test_apk_installed_version_uses_package_listing(self):
        def native(command, **kwargs):
            if command == ['apk', 'info', '--from', 'installed', '-e', 'php-zts-lib-fixture']:
                return SimpleNamespace(returncode=0, stdout='php-zts-lib-fixture\n')
            self.assertEqual(command, ['apk', 'list', '--from', 'installed', '--installed', 'php-zts-lib-fixture'])
            return SimpleNamespace(returncode=0, stdout='php-zts-lib-fixture-1.1.0-r0 x86_64 {fixture} (MIT) [installed]\n')
        with patch.dict(scope, {'run': native}):
            self.assertEqual(harness['installed']('apk', 'php-zts-lib-fixture'), '1.1.0-r0')

    def test_apk_dependencies_use_installed_metadata_and_fail_for_missing_owner(self):
        current = 'php-zts-fixture-1.0-r0 depends on:\nphp-zts-lib-base>=1.0-r0\nphp-zts-lib-base-abi-1\n\n'
        candidate = 'php-zts-fixture-2.0-r0 depends on:\nphp-zts-lib-other>=2.0-r0\nphp-zts-lib-other-abi-2\n\n'
        def native(command, **kwargs):
            installed_only = '--from' in command and command[command.index('--from') + 1] == 'installed'
            if installed_only and command[-1] == 'php-zts-missing':
                raise RuntimeError('Native installed-package query failed')
            return SimpleNamespace(returncode=0, stdout=current if installed_only else candidate)
        with patch.dict(scope, {'run': native}):
            self.assertEqual(harness['dependencies']('apk', 'php-zts-fixture'), current)
            with self.assertRaisesRegex(RuntimeError, 'installed-package query failed'):
                harness['dependencies']('apk', 'php-zts-missing')

    @unittest.skipUnless(shutil.which('apk'), 'Native Alpine tools required')
    def test_native_apk_queries_ignore_newer_repository_metadata(self):
        with TemporaryDirectory(prefix='spp-apk-installed-queries-') as scratch:
            root = Path(scratch)
            repo, installed = root / 'repo', root / 'installed'
            installed.mkdir()
            source = root / 'payload'
            source.write_text('native installed metadata fixture')
            for name, version, provides in [('base', '1.0.0', 'php-zts-lib-base-abi-1'), ('other', '2.0.0', 'php-zts-lib-other-abi-2')]:
                harness['fixture_package']('apk', repo, 'php-zts-lib-' + name, version,
                                           {'/usr/share/spp-' + name: source}, provides=[provides])
            for version, owner, floor, feature, payload in [('8.4.24', 'base', '1.0.0', 'openssl', 'old'), ('8.4.25', 'other', '2.0.0', 'gmp', 'new')]:
                harness['fixture_package']('apk', repo, 'php-zts-cli', version,
                                           {'/usr/share/spp-cli-' + payload: source},
                                           depends=['php-zts-lib-' + owner + ' (>= ' + floor + ')', 'php-zts-lib-' + owner + '-abi-' + ('1' if owner == 'base' else '2')],
                                           provides=['php-zts-curl', 'php-zts-' + feature])
            harness['index_repo']('apk', repo)
            repositories = root / 'repositories'
            repositories.write_text(str(repo) + '\n')
            command = ['apk', '--root', installed, '--no-network', '--repositories-file', repositories, '--allow-untrusted']
            native_run = harness['run']
            native_run([*command, '--initdb', 'add', repo / 'php-zts-lib-base-1.0.0-r0.apk',
                        repo / 'php-zts-lib-other-2.0.0-r0.apk', repo / 'php-zts-cli-8.4.24-r0.apk'])
            policy = native_run([*command, 'policy', 'php-zts-cli']).stdout
            self.assertIn('8.4.24-r0', policy)
            self.assertIn('8.4.25-r0', policy)
            def rooted(arguments, **kwargs):
                self.assertEqual(arguments[0], 'apk')
                return native_run([*command, *arguments[1:]], **kwargs)
            def mapped(value):
                path = Path(value)
                return installed / str(path).lstrip('/') if str(path).startswith('/usr/share/spp-') else path
            for version, owner, feature, payload, absent_version, absent_owner in [('8.4.24', 'base', 'openssl', 'old', '8.4.25', 'other'), ('8.4.25', 'other', 'gmp', 'new', '8.4.24', 'base')]:
                if version == '8.4.25':
                    native_run([*command, 'add', '--upgrade', repo / 'php-zts-cli-8.4.25-r0.apk'])
                with patch.dict(scope, {'run': rooted, 'Path': mapped}):
                    self.assertEqual(harness['installed']('apk', 'php-zts-cli'), version + '-r0')
                    self.assertEqual(harness['payload']('apk', ['php-zts-cli']), [installed / ('usr/share/spp-cli-' + payload)])
                    self.assertEqual(harness['expected_builtins']('apk', True), ['curl', feature] if feature == 'gmp' else ['curl', 'openssl'])
                    required = harness['dependencies']('apk', 'php-zts-cli')
                    self.assertIn('php-zts-cli-' + version + '-r0 depends on:', required)
                    self.assertIn('php-zts-lib-' + owner + '-abi-', required)
                    self.assertNotIn(absent_version, required)
                    self.assertNotIn('php-zts-lib-' + absent_owner + '-abi-', required)
                    self.assertIsNone(harness['installed']('apk', 'php-zts-missing'))
                    with self.assertRaises(RuntimeError):
                        harness['dependencies']('apk', 'php-zts-missing')
                    with self.assertRaises(RuntimeError):
                        harness['payload']('apk', ['php-zts-missing'])


class MatrixDriver(unittest.TestCase):
    def reused_baseline(self, root, os_name='debian'):
        kind = matrix['FORMATS'][os_name]
        directory = root / os_name
        buildroot = directory / 'buildroot'
        (buildroot / 'lib').mkdir(parents=True)
        (buildroot / 'lib/libfixture.so.1').write_text('original library')
        (buildroot / 'lib/libfixture.so').symlink_to('libfixture.so.1')
        (buildroot / 'abi').mkdir()
        (buildroot / 'abi/fixture.xml').write_text('prepared ABI fixture')
        manifest = {'schema': 1, 'linkage': 'shared',
                    'runtime_libdir': '/usr/lib64/php-zts/lib' if kind == 'rpm' else '/usr/lib/php-zts/lib',
                    'libraries': {'fixture': {'abi_epoch': 2, 'abi_capability': 'php-zts-lib-fixture-abi-2',
                                              'package_versions': {kind: '1.0-2'},
                                              'abi_dump': {'libfixture.so.1': 'abi/fixture.xml'}}}}
        (buildroot / 'shared-libraries.json').write_text(json.dumps(manifest))
        dist = directory / 'dist' / kind
        dist.mkdir(parents=True)
        (dist / ('php-zts-lib-fixture_1.0-2.' + kind)).write_text('original package')
        return directory, manifest

    def test_reused_library_inputs_fail_before_source_copies(self):
        for invalid in ['missing-target', 'manifest', 'schema', 'runtime', 'epoch', 'capability', 'revision', 'abi-dump', 'packages', 'consumer-only', 'escaping-symlink']:
            with self.subTest(invalid=invalid), TemporaryDirectory(prefix='spp-library-baseline-') as scratch:
                root = Path(scratch)
                directory, manifest = self.reused_baseline(root)
                manifest_path = directory / 'buildroot/shared-libraries.json'
                if invalid == 'missing-target': shutil.rmtree(directory)
                elif invalid == 'manifest': manifest_path.write_text('invalid json')
                elif invalid == 'schema': manifest['schema'] = 9
                elif invalid == 'runtime': manifest['runtime_libdir'] = '/usr/lib64/php-zts/lib'
                elif invalid == 'epoch': manifest['libraries']['fixture']['abi_epoch'] = 0
                elif invalid == 'capability': manifest['libraries']['fixture']['abi_capability'] = 'php-zts-lib-fixture-abi-1'
                elif invalid == 'revision': manifest['libraries']['fixture']['package_versions'] = {}
                elif invalid == 'abi-dump': (directory / 'buildroot/abi/fixture.xml').unlink()
                elif invalid in ['packages', 'consumer-only']:
                    (directory / 'dist/deb/php-zts-lib-fixture_1.0-2.deb').unlink()
                    if invalid == 'consumer-only':
                        (directory / 'dist/deb/php-zts-cli_8.2-1.deb').write_text('stale consumer')
                elif invalid == 'escaping-symlink': (directory / 'buildroot/lib/external.so').symlink_to(root / 'outside')
                if invalid not in ['missing-target', 'manifest']:
                    manifest_path.write_text(json.dumps(manifest))
                args = SimpleNamespace(image_prefix='fixture-', library_matrix_root=root, php_versions=['8.2'])
                with patch.dict(matrix['Target'].execute.__globals__, {'copy_sources': lambda *args: self.fail('Copied sources before validating baseline')}), patch.object(matrix['Target'], 'builder', lambda *args: self.fail('Started a builder for invalid baseline')):
                    result = matrix['Target'](args, 'debian', threading.Event()).execute()
                self.assertFalse(result['ok'])
                self.assertIn('reuse library baseline', result['error'])
                self.assertFalse(result.get('cleanup_errors'), result)

    def test_default_runtime_matrix_has_one_rpm_set_and_extra_els_are_smokes(self):
        calls = []
        def cell(*arguments, **options):
            calls.append((arguments, options))
            return {'ok': True}
        with patch.dict(scope, {'cell': cell, 'run': lambda *args, **kwargs: subprocess.CompletedProcess([], 0, '', '')}), patch.object(sys, 'argv', ['shared-runtime-containers.py', '--matrix-root', '/unused']), patch('sys.stdout', new_callable=io.StringIO):
            self.assertEqual(harness['main'](), 0)
        self.assertEqual(len(calls), 15)
        self.assertEqual({arguments[0] for arguments, _ in calls}, {'alma8', 'debian', 'alpine'})
        calls.clear()
        with patch.dict(scope, {'cell': cell, 'run': lambda *args, **kwargs: subprocess.CompletedProcess([], 0, '', '')}), patch.object(sys, 'argv', ['shared-runtime-containers.py', '--os', 'alma10', '--php', '8.6', '--artifacts', '/unused']), patch('sys.stdout', new_callable=io.StringIO):
            self.assertEqual(harness['main'](), 0)
        self.assertEqual(calls[0][0][-2:], (False, False))

    def test_retired_multidistro_builder_cannot_start_another_compile(self):
        arguments = ['build-shared-matrix.py', '--os', 'debian,alma9', '--php-versions', '8.2']
        with patch.object(sys, 'argv', arguments), patch.object(subprocess, 'run', side_effect=AssertionError('Retired builder invoked Docker')), patch('sys.stderr', new_callable=io.StringIO) as errors:
            with self.assertRaises(SystemExit) as error:
                matrix['main']()
        self.assertEqual(error.exception.code, 2)
        self.assertIn('legacy multi-distro build matrix is retired', errors.getvalue())
        self.assertIn('single-EL8 producer', errors.getvalue())

    def test_reused_baseline_is_private_and_every_requested_php_cell_runs(self):
        with TemporaryDirectory(prefix='spp-library-reuse-') as scratch:
            root = Path(scratch)
            baseline, _ = self.reused_baseline(root / 'baselines')
            (baseline / 'dist/deb/php-zts-cli_8.2-1.deb').write_text('stale consumer')
            original_manifest = (baseline / 'buildroot/shared-libraries.json').read_bytes()
            for name in ['project', 'spc']:
                (root / name / 'vendor').mkdir(parents=True)
                (root / name / 'vendor/autoload.php').write_text('')
            (root / 'downloads').mkdir()
            (root / 'downloads/.spc-meta.json').write_text(json.dumps({'php_versions': {'8.2': '8.2.33', '8.4': '8.4.24'}}))
            args = SimpleNamespace(image_prefix='fixture-', build_concurrency=4,
                                   project=root / 'project', spc=root / 'spc', downloads=root / 'downloads',
                                   php_versions=['8.2', '8.4'], library_matrix_root=root / 'baselines')
            calls, directories = [], []
            def build(target, name, command):
                calls.append(name)
                directories.append(target.directory)
                if name == 'prepare builder tools':
                    return
                self.assertIn(name, ['build/package PHP 8.2', 'build/package PHP 8.4'])
                buildroot = target.project / 'buildroot'
                dist = target.project / 'dist/deb'
                self.assertEqual((buildroot / 'shared-libraries.json').read_bytes(), original_manifest)
                self.assertEqual((buildroot / 'lib/libfixture.so').read_text(), 'original library')
                self.assertTrue((buildroot / 'lib/libfixture.so').is_symlink())
                self.assertEqual((dist / 'php-zts-lib-fixture_1.0-2.deb').read_text(), 'original package')
                self.assertFalse((dist / 'php-zts-cli_8.2-1.deb').exists())
                (buildroot / 'lib/libfixture.so').write_text('changed private library')
                (buildroot / 'shared-libraries.json').write_text('changed private manifest')
                (dist / 'php-zts-lib-fixture_1.0-2.deb').write_text('changed private package')
                (dist / 'php-zts-cli_8.2-1.deb').write_text('current consumer')
            def validate(target, name, command, **kwargs):
                report = Path(command[command.index('--report') + 1])
                report.write_text(json.dumps({'cells': [{'ok': True}]}))
            with patch.object(matrix['Target'], 'builder', build), patch.object(matrix['Target'], 'stage', validate):
                result = matrix['Target'](args, 'debian', threading.Event()).execute()
            self.assertTrue(result['ok'], result)
            self.assertEqual(calls, ['prepare builder tools', 'build/package PHP 8.2', 'build/package PHP 8.4'])
            self.assertEqual([cell['php'] for cell in result['cells']], ['8.2', '8.4'])
            self.assertEqual((baseline / 'buildroot/shared-libraries.json').read_bytes(), original_manifest)
            self.assertEqual((baseline / 'buildroot/lib/libfixture.so').read_text(), 'original library')
            self.assertEqual((baseline / 'dist/deb/php-zts-lib-fixture_1.0-2.deb').read_text(), 'original package')
            self.assertTrue(all(not directory.exists() for directory in directories))
            report = root / 'report.md'
            prior = {'os': 'alma9', 'libraries': True, 'cells': [{'php': '8.5', 'ok': False, 'stage': 'build', 'error': 'previous failure'}]}
            matrix['write_report'](report, [prior], 'prior-run', 25)
            matrix['write_report'](report, [result], 'targeted-rerun', 2)
            text = report.read_text()
            rerun = text.split('## Real package matrix targeted-rerun', 1)[1]
            self.assertIn('Requested: 2 PHP/distro combinations. Completed reports: 2.', rerun)
            self.assertEqual(rerun.count('| Reused |'), 2)
            self.assertIn('Libraries and ABI data were not rebuilt in this run.', rerun)
            self.assertIn(result['library_manifest_sha256'], rerun)
            self.assertIn('previous failure', text)
            self.assertIn('Requested: 25', text)

    def test_abi_slots_limit_concurrent_stages(self):
        semaphore = threading.Semaphore(2)
        cancelled = threading.Event()
        release = threading.Event()
        reached_limit = threading.Event()
        lock = threading.Lock()
        state = {'active': 0, 'maximum': 0}
        def worker():
            with matrix['abi_slot'](semaphore, cancelled):
                with lock:
                    state['active'] += 1
                    state['maximum'] = max(state['maximum'], state['active'])
                    if state['active'] == 2:
                        reached_limit.set()
                release.wait(timeout=2)
                with lock:
                    state['active'] -= 1
        threads = [threading.Thread(target=worker) for _ in range(6)]
        try:
            for thread in threads:
                thread.start()
            self.assertTrue(reached_limit.wait(timeout=2))
        finally:
            release.set()
            for thread in threads:
                thread.join(timeout=2)
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(state['maximum'], 2)

    def test_cancelled_abi_wait_does_not_start_a_stage(self):
        waiting = threading.Event()
        cancelled = threading.Event()
        started = threading.Event()
        failures = []
        class Occupied:
            def acquire(self, timeout):
                waiting.set()
                cancelled.wait(timeout)
                return False
        def worker():
            try:
                with matrix['abi_slot'](Occupied(), cancelled):
                    started.set()
            except matrix['StageFailure'] as error:
                failures.append(error)
        thread = threading.Thread(target=worker)
        thread.start()
        try:
            self.assertTrue(waiting.wait(timeout=2))
        finally:
            cancelled.set()
            thread.join(timeout=2)
        self.assertFalse(thread.is_alive())
        self.assertFalse(started.is_set())
        self.assertEqual(len(failures), 1)

    def compatibility_snapshot(self, root):
        downloads = root / 'downloads'
        downloads.mkdir(exist_ok=True)
        template = root / 'config/templates/craft.yml.twig'
        template.parent.mkdir(parents=True)
        content = 'download-options:\n    custom-git:\n'
        cache = {}
        for name, url in matrix['CUSTOM_MASTERS'].items():
            repository = downloads / name
            subprocess.check_call(['git', 'init', '-q', str(repository)])
            subprocess.check_call(['git', '-C', str(repository), 'symbolic-ref', 'HEAD', 'refs/heads/master'])
            (repository / 'source').write_text(name)
            subprocess.check_call(['git', '-C', str(repository), 'add', 'source'])
            subprocess.check_call(['git', '-C', str(repository), '-c', 'core.hooksPath=/dev/null', '-c', 'user.name=Snapshot fixture', '-c', 'user.email=fixture@example.invalid', 'commit', '-qm', 'Snapshot fixture'])
            revision = subprocess.check_output(['git', '-C', str(repository), 'rev-parse', 'HEAD'], universal_newlines=True).strip()
            cache[name] = {'source': {'cache_type': 'git', 'lock_type': 'source', 'hash': revision, 'dirname': name,
                                      'version': 'dev-master+' + revision[:7], 'config': {'url': url, 'rev': 'master'}}, 'binary': {}}
            content += "        - '" + name + ':master:' + url + "'\n"
        content += "        - 'ext-xdebug:3.6.0alpha1:https://github.com/xdebug/xdebug.git'\n"
        template.write_text(content)
        (downloads / '.cache.json').write_text(json.dumps(cache))
        return cache, template

    @unittest.skipUnless(shutil.which('git'), 'Git required')
    def test_golden_copy_reuses_exact_three_master_snapshots_and_keeps_xdebug_tag(self):
        with TemporaryDirectory(prefix='spp-snapshot-freeze-') as scratch:
            root = Path(scratch)
            cache, template = self.compatibility_snapshot(root)
            frozen = matrix['freeze_custom_master_sources'](root)
            self.assertEqual(frozen, {name: entry['source']['hash'] for name, entry in cache.items()})
            self.assertNotIn(':master:', template.read_text())
            self.assertIn('ext-xdebug:3.6.0alpha1:', template.read_text())

    @unittest.skipUnless(shutil.which('git'), 'Git required')
    def test_golden_snapshot_rejects_mismatched_and_dirty_inputs_without_rewriting_template(self):
        for invalid in ['hash', 'url', 'rev', 'dirty', 'untracked']:
            with self.subTest(invalid=invalid), TemporaryDirectory(prefix='spp-snapshot-freeze-') as scratch:
                root = Path(scratch)
                cache, template = self.compatibility_snapshot(root)
                original = template.read_text()
                if invalid == 'hash': cache['ext-apcu']['source']['hash'] = '0' * 40
                elif invalid in ['url', 'rev']: cache['ext-apcu']['source']['config'][invalid] = 'mismatched'
                elif invalid == 'dirty': (root / 'downloads/ext-apcu/source').write_text('modified')
                else: (root / 'downloads/ext-apcu/untracked').write_text('untracked')
                (root / 'downloads/.cache.json').write_text(json.dumps(cache))
                with self.assertRaises(matrix['StageFailure']):
                    matrix['freeze_custom_master_sources'](root)
                self.assertEqual(template.read_text(), original)

    @unittest.skipUnless(shutil.which('git') and shutil.which('php'), 'Git and PHP required')
    def test_real_spc_downloader_reuses_validated_master_cache_without_a_download_queue(self):
        spc = Path(os.environ.get('SPP_SPC_ROOT', str(Path(__file__).resolve().parents[2] / 'static-php-cli')))
        if not (spc / 'vendor/autoload.php').is_file():
            self.skipTest('SPC checkout dependencies required')
        with TemporaryDirectory(prefix='spp-snapshot-downloader-') as scratch:
            root = Path(scratch)
            cache, _ = self.compatibility_snapshot(root)
            matrix['freeze_custom_master_sources'](root)
            code = '''require $argv[1] . '/vendor/autoload.php';
putenv('SPC_ENABLE_LOG_FILE=0');
require ROOT_DIR . '/src/bootstrap.php';
\\StaticPHP\\Registry\\Registry::resolve();
$downloader = new \\StaticPHP\\Artifact\\ArtifactDownloader(['source-only' => 'ext-apcu', 'no-alt' => true], false);
$artifact = new \\StaticPHP\\Artifact\\Artifact('ext-apcu');
$queue = new ReflectionMethod($downloader, 'generateQueue');
$cache = \\StaticPHP\\DI\\ApplicationContext::get(\\StaticPHP\\Artifact\\ArtifactCache::class);
echo json_encode(['queue' => $queue->invoke($downloader, $artifact), 'hash' => $cache->getSourceInfo('ext-apcu')['hash']]);'''
            result = subprocess.run(['php', '-r', code, str(spc)], cwd=root, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout), {'queue': [], 'hash': cache['ext-apcu']['source']['hash']})

    def test_failure_report_keeps_actual_error_before_stack_and_compiler_context(self):
        with TemporaryDirectory(prefix='spp-matrix-diagnostics-') as scratch:
            root = Path(scratch)
            console = root / 'stage.log'
            console.write_text('\x1b[31mERROR: failed building fixture: unresolved private library\x1b[0m\n'
                               + ''.join(f'#{index} /spc/src/PackageBuilder.php(42): runStage()\n' for index in range(150)))
            (root / 'log').mkdir()
            output = root / 'log/spc.output.log'
            output.write_text('ERROR: unrelated earlier stage failure\n')
            previous = {output: (output.stat().st_size, output.stat().st_mtime_ns)}
            with output.open('a') as stream:
                stream.write('Compiling fixture.c\n\x1b[31mfixture.c:12:5: error: unknown type name SPP_TYPE\x1b[0m\n'
                             '   12 | SPP_TYPE value;\n      | ^~~~~~~~\n'
                             + ''.join(f'#{index} /spc/src/Executor.php(55): execute()\n' for index in range(100)))
            shell = root / 'log/spc.shell.log'
            shell.write_text('Executing: gcc -shared fixture.o -o libfixture.so\n'
                             '/usr/bin/ld: undefined reference to fixture_symbol\n'
                             'collect2: error: ld returned 1 exit status\n')
            previous[shell] = (0, 0)
            summary = matrix['failure_summary'](console, previous)
            self.assertIn('unresolved private library', summary)
            self.assertIn('fixture.c:12:5: error: unknown type name SPP_TYPE', summary)
            self.assertIn('SPP_TYPE value;', summary)
            self.assertIn('Executing: gcc -shared', summary)
            self.assertIn('undefined reference to fixture_symbol', summary)
            self.assertNotIn('unrelated earlier', summary)
            self.assertNotIn('\x1b', summary)
            self.assertLessEqual(len(summary.splitlines()), 120)
            report = root / 'report.md'
            result = {'os': 'alpine', 'libraries': False, 'cells': [
                {'php': '8.4', 'ok': False, 'stage': 'build libraries', 'error': summary}]}
            matrix['write_report'](report, [result], 'diagnostic-fixture', 1)
            retained = report.read_text()
            self.assertIn('unresolved private library', retained)
            self.assertIn('unknown type name SPP_TYPE', retained)
            self.assertIn('undefined reference to fixture_symbol', retained)

    def test_copies_sources_without_state_or_credentials(self):
        with TemporaryDirectory(prefix='spp-matrix-unit-') as scratch:
            root = Path(scratch)
            source = root / 'source'
            for name in ('vendor/package/build', 'build', '.aws', '.git'):
                (source / name).mkdir(parents=True)
                (source / name / 'file').write_text(name)
            (source / '.env').write_text('SECRET=1')
            matrix['copy_sources'](source, root / 'copy')
            self.assertTrue((root / 'copy/vendor/package/build/file').exists())
            for name in ('build', '.aws', '.git', '.env'):
                self.assertFalse((root / 'copy' / name).exists())

    def test_report_preserves_fixture_section_and_updates_run(self):
        with TemporaryDirectory(prefix='spp-matrix-unit-') as scratch:
            report = Path(scratch) / 'report.md'
            report.write_text('# Native fixtures\n\nFive baselines passed.\n')
            target = {'os': 'alpine', 'libraries': True, 'cells': [{'php': '8.4', 'ok': True}]}
            matrix['write_report'](report, [target], 'run-1', 1)
            target['cells'][0].update({'ok': False, 'stage': 'build', 'error': 'build failure'})
            matrix['write_report'](report, [target], 'run-1', 1)
            text = report.read_text()
            self.assertIn('Five baselines passed.', text)
            self.assertEqual(text.count('## Real package matrix'), 1)
            self.assertIn('Fail: build', text)

    def test_library_build_is_reused_without_php_state_and_scratch_is_cleaned(self):
        calls = []
        directories = []
        with TemporaryDirectory(prefix='spp-matrix-unit-') as scratch:
            root = Path(scratch)
            for name in ('project', 'spc'):
                (root / name / 'vendor').mkdir(parents=True)
                (root / name / 'vendor/autoload.php').write_text('')
            (root / 'downloads').mkdir()
            (root / 'downloads/.spc-meta.json').write_text(json.dumps({'php_versions': {'8.2': '8.2.33', '8.4': '8.4.24', '8.6': '8.6.0RC2'}}))
            args = SimpleNamespace(image_prefix='fixture-', build_concurrency=4,
                                   project=root / 'project', spc=root / 'spc',
                                   downloads=root / 'downloads', php_versions=['8.2', '8.6'])
            def build(target, name, command):
                calls.append(name)
                directories.append(target.directory)
                buildroot = target.project / 'buildroot'
                dist = target.project / 'dist/apk'
                if name == 'prepare builder tools':
                    return
                elif name == 'build libraries':
                    buildroot.mkdir()
                    (buildroot / 'library').write_text('shared library')
                elif name == 'prepare library ABI':
                    (buildroot / 'shared-libraries.json').write_text('{"abi":"prepared"}')
                elif name == 'package libraries':
                    dist.mkdir(parents=True)
                    (dist / 'library.apk').write_text('library')
                else:
                    self.assertFalse((buildroot / 'php').exists())
                    self.assertEqual((buildroot / 'shared-libraries.json').read_text(), '{"abi":"prepared"}')
                    self.assertTrue((dist / 'library.apk').exists())
                    (buildroot / 'php').write_text(name)
            def validate(target, name, command, **kwargs):
                report = Path(command[command.index('--report') + 1])
                report.write_text(json.dumps({'cells': [{'ok': True}]}))
                module_cache = target.project / '.validation/cache/read-only-module'
                if not module_cache.exists():
                    module_cache.mkdir()
                    (module_cache / 'source').write_text('Go module cache fixture')
                    module_cache.chmod(0o555)
            with patch.object(matrix['Target'], 'builder', build), patch.object(matrix['Target'], 'stage', validate), patch.dict(matrix['Target'].execute.__globals__, {'freeze_custom_master_sources': lambda project: {'ext-apcu': 'fixture-commit'}}):
                result = matrix['Target'](args, 'alpine', threading.Event()).execute()
            self.assertTrue(result['ok'], result)
            self.assertEqual(calls.count('build libraries'), 1)
            self.assertEqual(len(result['cells']), 2)
            self.assertTrue(all(not path.exists() for path in directories))


if __name__ == '__main__':
    unittest.main()
