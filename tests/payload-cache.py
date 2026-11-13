#!/usr/bin/env python3
"""Sealed PHP reuse must preserve the current SDK and reject stale consumers."""

import argparse
import hashlib
import importlib.util
import io
import json
import os
import platform
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / '.github/actions/shared-libraries'))
spec = importlib.util.spec_from_file_location('php_payload_cache', ROOT / '.github/actions/shared-libraries/payload-cache.py')
cache = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cache)


class PayloadCache(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory(prefix='spp-payload-cache-test-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / 'downloads').mkdir()
        (self.root / 'bin').mkdir()
        (self.root / 'bin/build-payload').symlink_to(ROOT / 'bin/build-payload')
        self.request = {'payload_core_inputs': {'8.6': 'a' * 64}, 'payload_component_inputs': {'8.6': {'imagick': 'old'}}}
        (self.root / 'downloads/.spc-meta.json').write_text(json.dumps({'php_versions': {'8.6': '8.6.0RC4'}}))
        template = self.root / 'config/templates/craft.yml.twig'
        template.parent.mkdir(parents=True)
        template.write_text("{% set base_sapi_str = 'cli,embed' %}\n{% set base_shared_str = 'imagick' %}\n# -O2\n")
        self.save_request()
        self.compiler = self.root / 'compiler'
        self.compiler.write_text('unchanged ELF8 compiler')
        self.architecture = {'arm64': 'aarch64', 'amd64': 'x86_64'}.get(platform.machine(), platform.machine())
        self.manifest = {'schema': 1, 'linkage': 'shared', 'architecture': self.architecture, 'libc': 'glibc',
                         'runtime_libdir': '/usr/lib/php-zts/lib',
                         'libraries': {'fixture': {'version': '1', 'source_hash': 'old',
                                      'files': ['lib/libfixture.so.1'],
                                      'elf': {'lib/libfixture.so.1': {'soname': 'libfixture.so.1'}}}}}

    def save_request(self):
        (self.root / 'downloads/build-plan.json').write_text(json.dumps(self.request))
        (self.root / 'downloads/.cache.json').write_text(json.dumps({'ext-imagick': {'source': {'hash': self.request['payload_component_inputs']['8.6']['imagick']}}}))

    def state(self):
        return cache.keys(self.root, '8.6', self.manifest, self.compiler.read_text().strip())

    def test_compatible_library_source_changes_keep_exact_php_cache_key(self):
        previous = self.state()
        self.manifest['libraries']['fixture']['version'] = '2'
        self.manifest['libraries']['fixture']['source_hash'] = 'new'
        self.assertEqual(self.state()['key'], previous['key'])
        self.manifest['libraries']['fixture']['elf']['lib/libfixture.so.1']['soname'] = 'libfixture.so.2'
        current = self.state()
        self.assertNotEqual(current['key'], previous['key'])
        self.assertEqual(current['seed-key'], previous['seed-key'])
        self.assertFalse(cache.compatible(previous['library_abi'], current['library_abi']))

    def test_library_compatibility_uses_only_exact_runtime_names(self):
        current = cache.abi(self.manifest)
        self.assertEqual(current, {'fixture': {'sonames': ['libfixture.so.1']}})
        self.assertTrue(cache.compatible({'fixture': {'sonames': ['libfixture.so.1'], 'epoch': 99}}, current))
        for names in [[], ['libfixture.so.2'], ['librenamed.so.1'], ['libfixture.so.1', 'libextra.so.1']]:
            with self.subTest(names=names):
                self.assertFalse(cache.compatible(current, {'fixture': {'sonames': names}}))
        self.manifest['libraries']['fixture']['elf'] = {'lib/plugin.so': {}, 'bin/helper': {}}
        self.assertEqual(cache.abi(self.manifest)['fixture'], {'sonames': ['plugin.so']})
        self.manifest['libraries']['consumer'] = {'elf': {}, 'depends': ['plugin.so']}
        self.assertEqual(cache.library_graph(self.manifest), {'fixture': [], 'consumer': ['fixture']})

    def test_extension_source_update_keeps_core_seed_only(self):
        previous = self.state()
        self.request['payload_component_inputs']['8.6']['imagick'] = 'new'
        self.save_request()
        current = self.state()
        self.assertEqual(current['key'], previous['key'])
        self.assertEqual(current['seed-key'], previous['seed-key'])

    def test_flags_or_compiler_changes_preserve_the_cache_slot(self):
        previous = self.state()
        template = self.root / 'config/templates/craft.yml.twig'
        previous_template = template.read_text()
        template.write_text(previous_template.replace('-O2', '-O3'))
        self.assertEqual(self.state()['seed-key'], previous['seed-key'])
        template.write_text(previous_template)
        self.compiler.write_text('another compiler')
        self.assertEqual(self.state()['seed-key'], previous['seed-key'])

    def test_old_snapshot_recipe_fingerprints_cannot_override_current_checkout(self):
        previous = self.state()
        self.request['payload_core_inputs']['8.6'] = 'stale snapshot configuration'
        self.save_request()
        self.assertEqual(self.state()['key'], previous['key'])
        template = self.root / 'config/templates/craft.yml.twig'
        template.write_text(template.read_text().replace('-O2', '-O3'))
        self.assertEqual(self.state()['seed-key'], previous['seed-key'])

    def test_streamed_archive_rejects_corruption_and_inventory_changes(self):
        content = b'cached executable'
        manifest = {'files': {'bin/php': {'sha256': hashlib.sha256(content).hexdigest(), 'mode': 0o755}}}
        metadata = json.dumps(manifest).encode()
        valid = [('cache.json', metadata), ('files/bin/php', content)]
        for name, members in [('valid', valid), ('duplicate', valid + [valid[1]]),
                              ('missing', valid[:1]), ('extra', valid + [('files/bin/other', content)]),
                              ('corrupt', [valid[0], ('files/bin/php', b'changed')])]:
            with self.subTest(name=name):
                raw = io.BytesIO()
                with tarfile.open(fileobj=raw, mode='w') as archive:
                    for path, data in members:
                        header = tarfile.TarInfo(path)
                        header.size, header.mode = len(data), 0o755
                        archive.addfile(header, io.BytesIO(data))
                compressed = self.root / (name + '.tar.zst')
                compressed.write_bytes(subprocess.check_output(['zstd', '-q', '-c'], input=raw.getvalue()))
                destination = self.root / name
                if name == 'valid':
                    self.assertEqual(cache.extract(compressed, destination), manifest)
                    self.assertEqual((destination / 'bin/php').read_bytes(), content)
                    compressed.write_bytes(compressed.read_bytes()[:-4])
                    with self.assertRaises((subprocess.CalledProcessError, tarfile.TarError, RuntimeError)):
                        cache.extract(compressed, self.root / 'truncated')
                else:
                    with self.assertRaises(RuntimeError):
                        cache.extract(compressed, destination)

    def fixture(self, directory, value, php=False):
        (directory / 'lib').mkdir(parents=True)
        source = self.root / ('fixture-' + str(value) + '.c')
        source.write_text('int exported_function(void) { return ' + str(value) + '; }\n')
        subprocess.run(['gcc', '-shared', '-fPIC', '-g', '-Wl,-soname,libfixture.so.1', str(source), '-o', str(directory / 'lib/libfixture.so.1')], check=True)
        (directory / 'shared-libraries.json').write_text(json.dumps(self.manifest))
        config = self.root / 'craft.yml'
        config.write_text('php-version: 8.6.0RC4\nsapi: [cli, embed]\nshared-extensions: [imagick]\nextra-env:\n  SPC_LIBC: glibc\n  SPC_CMD_VAR_PHP_MAKE_EXTRA_LDFLAGS: "-release zts-86"\nbuild-options:\n  with-shared-libdir: /usr/lib/php-zts/lib\n  with-config-file-path: /etc/php-zts\n')
        if php:
            (directory / 'bin').mkdir()
            program = self.root / 'core.c'
            program.write_text('int main(void) { return 0; }\n')
            subprocess.run(['gcc', '-g', str(program), '-o', str(directory / 'bin/php')], check=True)
            shutil.copy2(directory / 'lib/libfixture.so.1', directory / 'lib/libphp-zts-86.so')
            (directory / 'lib/libphp.so').symlink_to('libphp-zts-86.so')
            (directory / 'modules').mkdir()
            shutil.copy2(directory / 'lib/libfixture.so.1', directory / 'modules/imagick-zts-86.so')
            for helper in ['phpize', 'php-config']:
                (directory / 'bin' / helper).write_text('#!/bin/sh\nexit 0\n')
                (directory / 'bin' / helper).chmod(0o755)
            header = directory / 'include/php/Zend/zend_modules.h'
            header.parent.mkdir(parents=True)
            header.write_text('#define ZEND_MODULE_API_NO 20260924\n')
        for command, extras in [('record', ['--config', str(config), '--php-version', '8.6.0RC4', '--arch', self.architecture]), ('seal', [])]:
            subprocess.run(['php', str(ROOT / 'bin/build-payload'), command, '--root', str(directory), *extras], check=True, stdout=subprocess.DEVNULL)

    @unittest.skipUnless(all(shutil.which(command) for command in ['gcc', 'php', 'zstd', 'readelf', 'strip', 'objcopy']), 'native payload tools required')
    def test_validation_hosts_follow_the_requested_runtime(self):
        self.assertEqual(cache.validation_hosts({'fpm'}), {'cli'})
        self.assertEqual(cache.validation_hosts({'cli'}), set())
        self.assertEqual(cache.validation_hosts({'curl'}), {'cli', 'frankenphp', 'embed'})
        self.assertEqual(cache.validation_hosts({'curl', 'frankenphp'}), {'cli', 'embed'})

    def test_selected_packaging_archive_keeps_sealed_bytes_and_omits_unrelated_sdk_outputs(self):
        payload = self.root / 'build/8.6'
        self.fixture(payload, 1, php=True)
        shutil.copy2(payload / 'modules/imagick-zts-86.so', payload / 'modules/unselected-zts-86.so')
        (payload / 'lib/unneeded.a').write_bytes(b'compiler archive')
        for command in ['record', 'seal']:
            extra = ['--config', self.root / 'craft.yml', '--php-version', '8.6.0RC4', '--arch', self.architecture] if command == 'record' else []
            subprocess.run(['php', ROOT / 'bin/build-payload', command, '--root', payload, *extra], check=True, stdout=subprocess.DEVNULL)
        original = (payload / 'build-payload.json').read_bytes()
        cache.write(self.root / 'stage/payload-provenance.json', {'source_run_id': '1234'})
        archive = self.root / 'package.tar.zst'
        cache.package_archive(argparse.Namespace(root=self.root, payload=payload, packages='imagick', output=archive))
        restored = self.root / 'restored'
        restored.mkdir()
        with subprocess.Popen(['zstd', '-dc', archive], stdout=subprocess.PIPE) as stream:
            subprocess.run(['tar', '-xpf', '-', '-C', restored], stdin=stream.stdout, check=True)
            stream.stdout.close()
            self.assertEqual(stream.wait(), 0)
        selected = restored / 'build/8.6'
        self.assertFalse((selected / 'modules/unselected-zts-86.so').exists())
        self.assertFalse((selected / 'lib/libfixture.so.1').exists())
        self.assertFalse((selected / 'lib/unneeded.a').exists())
        self.assertTrue((selected / 'lib/libphp-zts-86.so').is_file())
        self.assertEqual((payload / 'build-payload.json').read_bytes(), original)
        self.assertEqual(cache.read(selected / 'build-payload.json')['prepared']['modules/imagick-zts-86.so'],
                         json.loads(original)['prepared']['modules/imagick-zts-86.so'])
        verify = ['php', ROOT / 'bin/build-payload', 'verify', '--root', selected]
        subprocess.run(verify, check=True, stdout=subprocess.DEVNULL)
        with self.assertRaisesRegex(RuntimeError, 'cannot replace the complete PHP cache'):
            cache.export(argparse.Namespace(root=self.root, payload=selected))
        recorded = self.root / 'selected-before-sealing'
        shutil.copytree(payload, recorded, symlinks=True)
        script = '''define('BASE_PATH', $argv[1]); require BASE_PATH . '/vendor/autoload.php';
            $payload = \\staticphp\\util\\BuildPayload::load($argv[2], true)->metadata();
            \\staticphp\\util\\BuildPayload::record($argv[2], $payload['configuration'], $payload['php_version'],
                $payload['architecture'], $payload['source_version'], ['kind' => 'partial-consumer-build']);'''
        subprocess.run(['php', '-r', script, ROOT, recorded], env=dict(os.environ, SPP_PACKAGING_SELECTION='imagick'), check=True)
        self.assertEqual(cache.read(recorded / 'build-payload.json')['packaging_selection']['packages'], ['imagick'])
        self.assertFalse((recorded / 'modules/unselected-zts-86.so').exists())
        subprocess.run(['php', ROOT / 'bin/build-payload', 'seal', '--root', recorded], check=True, stdout=subprocess.DEVNULL)
        subprocess.run(['php', ROOT / 'bin/build-payload', 'verify', '--root', recorded], check=True, stdout=subprocess.DEVNULL)
        cache.project_payload(payload, {'imagick'})
        self.assertEqual(cache.read(payload / 'build-payload.json'), cache.read(selected / 'build-payload.json'))
        self.assertFalse((payload / 'modules/unselected-zts-86.so').exists())
        self.assertFalse((payload / 'lib/unneeded.a').exists())
        (payload / 'modules/imagick-zts-86.so').write_bytes(b'changed')
        self.assertNotEqual(subprocess.run(['php', ROOT / 'bin/build-payload', 'verify', '--root', payload], capture_output=True).returncode, 0)
        (selected / 'modules/imagick-zts-86.so').write_bytes(b'changed')
        self.assertNotEqual(subprocess.run(verify, capture_output=True).returncode, 0)

    def test_library_projection_keeps_sealed_bytes_without_php_consumers(self):
        for sealed in [True, False]:
            payload = self.root / ('sdk-' + str(sealed))
            self.fixture(payload, 1, php=True)
            cache.write(payload / 'shared-libraries.json', self.manifest)
            original = cache.read(payload / 'build-payload.json')
            if not sealed:
                unprepared = dict(original, state='recorded')
                unprepared.pop('prepared', None)
                cache.write(payload / 'build-payload.json', unprepared)
            record, manifest, excluded = cache.packaging_projection(payload, cache.read(payload / 'build-payload.json'),
                cache.read(payload / 'shared-libraries.json'), set(), library_selection={'fixture'})
            selected = self.root / ('selected-' + str(sealed))
            def ignore(directory, names):
                return [name for name in names if str((Path(directory) / name).relative_to(payload)) in excluded]
            shutil.copytree(payload, selected, symlinks=True, ignore=ignore)
            cache.write(selected / 'build-payload.json', record)
            cache.write(selected / 'shared-libraries.json', manifest)
            self.assertFalse((selected / 'bin/php').exists())
            self.assertFalse((selected / 'modules/imagick-zts-86.so').exists())
            if sealed:
                self.assertEqual(record['prepared']['lib/libfixture.so.1'], original['prepared']['lib/libfixture.so.1'])
            else:
                subprocess.run(['php', ROOT / 'bin/build-payload', 'seal', '--root', selected], check=True, stdout=subprocess.DEVNULL)
            verify = ['php', ROOT / 'bin/build-payload', 'verify', '--root', selected]
            subprocess.run(verify, check=True, stdout=subprocess.DEVNULL)
            (selected / 'lib/libfixture.so.1').write_bytes(b'tampered library')
            self.assertNotEqual(subprocess.run(verify, capture_output=True).returncode, 0)

    def test_packaging_retains_transitive_php_modules_without_publishing_them(self):
        payload = self.root / 'build/8.6'
        self.fixture(payload, 1, php=True)
        modules = ['mysqlnd_parsec', 'mysqlnd', 'pdo_pgsql', 'pdo', 'swoole', 'swoole-hook-mysql', 'pdo_mysql', 'mysqli', 'unselected']
        for name in modules:
            if name != 'swoole-hook-mysql':
                shutil.copy2(payload / 'modules/imagick-zts-86.so', payload / ('modules/' + name + '-zts-86.so'))
        config = self.root / 'craft.yml'
        config.write_text(config.read_text().replace('shared-extensions: [imagick]', 'shared-extensions: [' + ','.join(modules) + ']'))
        for command in ['record', 'seal']:
            extra = ['--config', config, '--php-version', '8.6.0RC4', '--arch', self.architecture] if command == 'record' else []
            subprocess.run(['php', ROOT / 'bin/build-payload', command, '--root', payload, *extra], check=True, stdout=subprocess.DEVNULL)
        original = cache.read(payload / 'build-payload.json')
        selected = {'mysqlnd_parsec', 'pdo_pgsql', 'swoole'}
        record, manifest, excluded = cache.packaging_projection(payload, cache.read(payload / 'build-payload.json'),
            cache.read(payload / 'shared-libraries.json'), selected)
        for name in ['mysqlnd', 'pdo', 'pdo_mysql', 'mysqli']:
            path = 'modules/' + name + '-zts-86.so'
            self.assertNotIn(path, excluded)
            self.assertEqual(record['files'][path], original['files'][path])
            self.assertEqual(record['prepared'][path], original['prepared'][path])
        self.assertIn('modules/unselected-zts-86.so', excluded)
        self.assertEqual(record['packaging_selection']['packages'], sorted(selected))
        self.assertEqual(cache.read(payload / 'build-payload.json'), original)

    @unittest.skipUnless(all(shutil.which(command) for command in ['gcc', 'php', 'zstd', 'readelf', 'strip', 'objcopy']), 'native payload tools required')
    def test_unchanged_package_restore_is_small_but_changed_sources_keep_the_full_sdk(self):
        old = self.root / 'old'
        self.fixture(old, 1, php=True)
        state = self.state()
        archive = self.root / 'cache.tar.zst'
        cache.export(argparse.Namespace(root=self.root, payload=old, compatibility=self.compiler, key=state['key'],
                                        seed_key=state['seed-key'], source_run_id='1234', output=archive))
        sdk = self.root / 'sdk'
        self.fixture(sdk, 2)
        original_sdk = (sdk / 'build-payload.json').read_bytes()
        args = argparse.Namespace(root=self.root, stream='8.6', sdk=sdk, payload=self.root / 'selected',
                                  compatibility=self.compiler, key=state['key'], seed_key=state['seed-key'],
                                  archive=archive, compile_packages='', package_selection='imagick')
        cache.restore(args)
        self.assertFalse((args.payload / 'lib/libfixture.so.1').exists())
        self.assertTrue((args.payload / 'lib/libphp-zts-86.so').is_file())
        self.assertEqual((args.payload / 'modules/imagick-zts-86.so').read_bytes(), (old / 'modules/imagick-zts-86.so').read_bytes())
        self.assertEqual((sdk / 'build-payload.json').read_bytes(), original_sdk)
        subprocess.run(['php', ROOT / 'bin/build-payload', 'verify', '--root', args.payload], check=True, stdout=subprocess.DEVNULL)
        with self.assertRaisesRegex(RuntimeError, 'cannot replace the complete PHP cache'):
            cache.export(argparse.Namespace(root=self.root, payload=args.payload))
        self.request['payload_component_inputs']['8.6']['imagick'] = 'new'
        self.save_request()
        args.key = self.state()['key']
        args.payload = self.root / 'changed'; args.compile_packages = 'imagick'
        cache.restore(args)
        self.assertTrue((args.payload / 'lib/libfixture.so.1').exists())
        self.assertNotIn('packaging_selection', cache.read(args.payload / 'build-payload.json'))
        self.assertEqual(cache.read(args.payload / 'consumer-cache-seed.json')['rebuild_packages'], ['imagick'])

    @unittest.skipUnless(all(shutil.which(command) for command in ['gcc', 'php', 'zstd', 'readelf', 'strip', 'objcopy']), 'native payload tools required')
    def test_native_graft_keeps_new_sdk_and_identical_cached_php(self):
        old = self.root / 'old'
        self.fixture(old, 1, php=True)
        state = self.state()
        archive = self.root / 'cache.tar.zst'
        cache.export(argparse.Namespace(root=self.root, payload=old, compatibility=self.compiler, key=state['key'],
                                        seed_key=state['seed-key'], source_run_id='1234', output=archive))
        sdk = self.root / 'sdk'
        self.fixture(sdk, 2)
        new_library = hashlib.sha256((sdk / 'lib/libfixture.so.1').read_bytes()).hexdigest()
        target = self.root / 'restored'
        args = argparse.Namespace(root=self.root, stream='8.6', sdk=sdk, payload=target, compatibility=self.compiler,
                                  key=state['key'], seed_key=state['seed-key'], archive=archive, compile_packages='')
        cache.restore(args)
        self.assertEqual((target / 'bin/php').read_bytes(), (old / 'bin/php').read_bytes())
        self.assertEqual((target / 'payload/runtime/bin/php').read_bytes(), (old / 'payload/runtime/bin/php').read_bytes())
        self.assertEqual(hashlib.sha256((target / 'lib/libfixture.so.1').read_bytes()).hexdigest(), new_library)
        self.assertEqual(cache.read(target / 'consumer-cache-seed.json')['compiled_source_run_id'], '1234')
        args.payload = self.root / 'retry'
        args.compile_packages = 'imagick'
        cache.restore(args)
        retry = cache.read(args.payload / 'consumer-cache-seed.json')
        self.assertTrue(retry['cache_exact'])
        self.assertEqual(retry['rebuild_packages'], ['imagick'])


    def test_abi_rebuild_reuses_core_only_when_all_changed_library_consumers_are_selected(self):
        old = self.root / 'old'
        self.fixture(old, 1, php=True)
        source = self.root / 'bound-module.c'
        source.write_text('extern int exported_function(void); int addon(void) { return exported_function(); }\n')
        subprocess.run(['gcc', '-shared', '-fPIC', '-g', str(source), '-L' + str(old / 'lib'),
                        '-l:libfixture.so.1', '-o', str(old / 'modules/imagick-zts-86.so')], check=True)
        for command, extras in [('record', ['--config', str(self.root / 'craft.yml'), '--php-version', '8.6.0RC4', '--arch', self.architecture]), ('seal', [])]:
            subprocess.run(['php', str(ROOT / 'bin/build-payload'), command, '--root', str(old), *extras], check=True, stdout=subprocess.DEVNULL)
        state = self.state()
        archive = self.root / 'cache.tar.zst'
        cache.export(argparse.Namespace(root=self.root, payload=old, compatibility=self.compiler, key=state['key'],
                                        seed_key=state['seed-key'], source_run_id='1234', output=archive))
        self.manifest['libraries']['fixture']['elf']['lib/libfixture.so.1']['soname'] = 'libfixture.so.2'
        current = self.state()
        sdk = self.root / 'sdk'
        self.fixture(sdk, 2)
        args = argparse.Namespace(root=self.root, stream='8.6', sdk=sdk, payload=self.root / 'unselected', compatibility=self.compiler,
                                 key=current['key'], seed_key=current['seed-key'], archive=archive,
                                 compile_packages='', abi_rebuild_libraries='fixture')
        with self.assertRaisesRegex(RuntimeError, 'outside the selected packages: modules/imagick'):
            cache.restore(args)
        self.assertFalse(args.payload.exists())

        args.payload = self.root / 'target'
        args.compile_packages = 'imagick'
        cache.restore(args)
        self.assertEqual((args.payload / 'bin/php').read_bytes(), (old / 'bin/php').read_bytes())
        self.assertFalse((args.payload / 'modules/imagick-zts-86.so').exists())
        self.assertFalse((args.payload / 'payload/runtime/modules/imagick-zts-86.so').exists())
        receipt = cache.read(args.payload / 'consumer-cache-seed.json')
        self.assertEqual(receipt['rebuild_packages'], ['imagick'])
        self.assertTrue(receipt['abi_rebuild_required'])
        self.assertEqual(receipt['abi_rebuild_libraries'], ['fixture'])
        self.assertIn('modules/imagick-zts-86.so', cache.read(args.payload / receipt['cached_metadata_path'])['files'])
        self.assertEqual(cache.abi(cache.read(args.payload / 'shared-libraries.json'))['fixture'], {'sonames': ['libfixture.so.2']})
        args.payload = self.root / 'derived-abi'
        args.compile_packages = ''
        args.allow_all_components = True
        cache.restore(args)
        self.assertEqual(cache.read(args.payload / 'consumer-cache-seed.json')['rebuild_packages'], ['imagick'])

        source.write_text('extern int exported_function(void); int main(void) { return exported_function() == 1 ? 0 : 1; }\n')
        subprocess.run(['gcc', '-g', str(source), '-L' + str(old / 'lib'), '-l:libfixture.so.1',
                        '-Wl,-rpath,' + str(old / 'lib'), '-o', str(old / 'bin/php')], check=True)
        self.manifest['libraries']['fixture']['elf']['lib/libfixture.so.1']['soname'] = 'libfixture.so.1'
        for command, extras in [('record', ['--config', str(self.root / 'craft.yml'), '--php-version', '8.6.0RC4', '--arch', self.architecture]), ('seal', [])]:
            subprocess.run(['php', str(ROOT / 'bin/build-payload'), command, '--root', str(old), *extras], check=True, stdout=subprocess.DEVNULL)
        cache.export(argparse.Namespace(root=self.root, payload=old, compatibility=self.compiler, key=state['key'],
                                        seed_key=state['seed-key'], source_run_id='1234', output=archive))
        self.manifest['libraries']['fixture']['elf']['lib/libfixture.so.1']['soname'] = 'libfixture.so.2'
        args.payload = self.root / 'unsafe-core'
        with self.assertRaisesRegex(RuntimeError, 'outside the selected packages: bin/php'):
            cache.restore(args)
        self.assertFalse(args.payload.exists())

    @unittest.skipUnless(all(shutil.which(command) for command in ['gcc', 'php', 'zstd', 'readelf', 'strip', 'objcopy']), 'native payload tools required')
    def test_extension_update_rebuilds_requested_extension_with_unchanged_source_hash(self):
        old = self.root / 'old'
        self.fixture(old, 1, php=True)
        state = self.state()
        archive = self.root / 'cache.tar.zst'
        cache.export(argparse.Namespace(root=self.root, payload=old, compatibility=self.compiler, key=state['key'],
                                        seed_key=state['seed-key'], source_run_id='1234', output=archive))
        current = self.state()
        sdk = self.root / 'sdk'
        self.fixture(sdk, 2)
        target = self.root / 'target'
        cache.restore(argparse.Namespace(root=self.root, stream='8.6', sdk=sdk, payload=target, compatibility=self.compiler,
                                         key=current['key'], seed_key=current['seed-key'], archive=archive,
                                         compile_packages='imagick', abi_rebuild_libraries='fixture'))
        receipt = cache.read(target / 'consumer-cache-seed.json')
        self.assertEqual(receipt['rebuild_packages'], ['imagick'])
        self.assertFalse(receipt['abi_rebuild_required'])
        self.assertEqual((target / 'modules/imagick-zts-86.so').read_bytes(), (old / 'modules/imagick-zts-86.so').read_bytes())
        cache.write(self.root / 'downloads/.spc-meta.json', {'php_versions': {'8.6': '8.6.0RC5'}})
        args = argparse.Namespace(root=self.root, stream='8.6', sdk=sdk, payload=self.root / 'new-core',
                                  compatibility=self.compiler, key=current['key'], seed_key=current['seed-key'],
                                  archive=archive, compile_packages='imagick')
        with self.assertRaises(SystemExit) as changed:
            cache.restore(args)
        self.assertEqual(changed.exception.code, 3)
        self.assertFalse(args.payload.exists())

if __name__ == '__main__':
    unittest.main()
