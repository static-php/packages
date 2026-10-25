#!/usr/bin/env python3
"""Sealed PHP reuse must preserve the current SDK and reject stale consumers."""

import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest


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
        self.manifest = {'schema': 1, 'linkage': 'shared', 'architecture': 'x86_64', 'libc': 'glibc',
                         'runtime_libdir': '/usr/lib/php-zts/lib',
                         'libraries': {'fixture': {'version': '1', 'source_hash': 'old', 'abi_epoch': 1,
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
        self.manifest['libraries']['fixture']['abi_epoch'] = 2
        current = self.state()
        self.assertNotEqual(current['key'], previous['key'])
        self.assertEqual(current['seed-key'], previous['seed-key'])
        self.assertFalse(cache.compatible(previous['library_abi'], current['library_abi']))

    def test_extension_source_update_keeps_core_seed_only(self):
        previous = self.state()
        self.request['payload_component_inputs']['8.6']['imagick'] = 'new'
        self.save_request()
        current = self.state()
        self.assertNotEqual(current['key'], previous['key'])
        self.assertEqual(current['seed-key'], previous['seed-key'])
        self.assertEqual(cache.changed_components({'go': 'old'}, {'go': 'new'}), {'frankenphp'})

    def test_core_source_flags_or_compiler_changes_invalidate_seed(self):
        previous = self.state()
        template = self.root / 'config/templates/craft.yml.twig'
        previous_template = template.read_text()
        template.write_text(previous_template.replace('-O2', '-O3'))
        self.assertNotEqual(self.state()['seed-key'], previous['seed-key'])
        template.write_text(previous_template)
        self.compiler.write_text('another compiler')
        self.assertNotEqual(self.state()['seed-key'], previous['seed-key'])

    def test_old_snapshot_recipe_fingerprints_cannot_override_current_checkout(self):
        previous = self.state()
        self.request['payload_core_inputs']['8.6'] = 'stale snapshot configuration'
        self.save_request()
        self.assertEqual(self.state()['key'], previous['key'])
        template = self.root / 'config/templates/craft.yml.twig'
        template.write_text(template.read_text().replace('-O2', '-O3'))
        self.assertNotEqual(self.state()['seed-key'], previous['seed-key'])

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
        for command, extras in [('record', ['--config', str(config), '--php-version', '8.6.0RC4', '--arch', 'x86_64']), ('seal', [])]:
            subprocess.run(['php', str(ROOT / 'bin/build-payload'), command, '--root', str(directory), *extras], check=True, stdout=subprocess.DEVNULL)

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
        self.assertEqual(retry['rebuild_packages'], [])
        runtime_path = self.root / 'stage/payload-compiler-runtime.json'
        original_runtime = cache.read(runtime_path)
        changed_runtime = json.loads(json.dumps(original_runtime))
        changed_runtime['semantic']['headers_sha256'] = 'b' * 64
        changed_runtime['fingerprint'] = cache.digest(changed_runtime['semantic'])
        cache.write(runtime_path, changed_runtime)
        args.key = self.state()['key']
        args.payload = self.root / 'compatible-nonexact-retry'
        cache.restore(args)
        retry = cache.read(args.payload / 'consumer-cache-seed.json')
        self.assertFalse(retry['cache_exact'])
        self.assertEqual(retry['rebuild_packages'], [])
        cache.write(runtime_path, original_runtime)
        args.compile_packages = ''
        self.request['payload_component_inputs']['8.6']['imagick'] = 'new'
        self.save_request()
        changed = self.state()
        args.key = changed['key']
        args.payload = self.root / 'refused'
        with self.assertRaisesRegex(RuntimeError, 'outside the requested compile packages'):
            cache.restore(args)
        self.assertFalse(args.payload.exists())
        args.compile_packages = 'imagick'
        args.payload = self.root / 'partial'
        cache.restore(args)
        self.assertEqual(cache.read(args.payload / 'consumer-cache-seed.json')['rebuild_packages'], ['imagick'])
        with self.assertRaisesRegex(RuntimeError, 'still require their selected'):
            cache.export(argparse.Namespace(root=self.root, payload=args.payload, compatibility=self.compiler,
                                            key=changed['key'], seed_key=changed['seed-key'], source_run_id='5678',
                                            output=self.root / 'pending.tar.zst'))
        args.compile_packages = ''
        args.allow_all_components = True
        args.payload = self.root / 'derived-partial'
        cache.restore(args)
        self.assertEqual(cache.read(args.payload / 'consumer-cache-seed.json')['rebuild_packages'], ['imagick'])

    @unittest.skipUnless(all(shutil.which(command) for command in ['gcc', 'g++', 'php', 'zstd', 'readelf', 'strip', 'objcopy']), 'native C++ payload tools required')
    def test_changed_cxx_input_rebuilds_only_actual_cxx_consumer(self):
        old = self.root / 'old'
        self.fixture(old, 1, php=True)
        module = old / 'modules/imagick-zts-86.so'
        module.parent.mkdir(exist_ok=True)
        source = self.root / 'consumer.cc'
        source.write_text('#include <stdexcept>\nextern "C" int addon(int v) { if (v) throw std::runtime_error("test"); return 0; }\n')
        subprocess.run(['g++', '-shared', '-fPIC', '-g', str(source), '-o', str(module)], check=True)
        config = self.root / 'craft.yml'
        for command, extras in [('record', ['--config', str(self.root / 'craft.yml'), '--php-version', '8.6.0RC4', '--arch', 'x86_64']), ('seal', [])]:
            subprocess.run(['php', str(ROOT / 'bin/build-payload'), command, '--root', str(old), *extras], check=True, stdout=subprocess.DEVNULL)
        state = self.state()
        archive = self.root / 'cache.tar.zst'
        cache.export(argparse.Namespace(root=self.root, payload=old, compatibility=self.compiler, key=state['key'],
                                        seed_key=state['seed-key'], source_run_id='1234', output=archive))
        runtime_path = self.root / 'stage/payload-compiler-runtime.json'
        runtime = cache.read(runtime_path)
        runtime['semantic']['headers_sha256'] = 'b' * 64
        runtime['fingerprint'] = cache.digest(runtime['semantic'])
        cache.write(runtime_path, runtime)
        current = self.state()
        self.assertEqual(current['seed-key'], state['seed-key'])
        self.assertNotEqual(current['key'], state['key'])
        sdk = self.root / 'sdk'
        self.fixture(sdk, 2)
        restored = self.root / 'restored'
        cache.restore(argparse.Namespace(root=self.root, stream='8.6', sdk=sdk, payload=restored, compatibility=self.compiler,
                                         key=current['key'], seed_key=current['seed-key'], archive=archive, compile_packages=''))
        receipt = cache.read(restored / 'consumer-cache-seed.json')
        self.assertEqual(receipt['runtime_rebuild_packages'], ['imagick'])
        self.assertEqual(receipt['rebuild_packages'], ['imagick'])
        self.assertEqual((restored / 'bin/php').read_bytes(), (old / 'bin/php').read_bytes())

    @unittest.skipUnless(all(shutil.which(command) for command in ['gcc', 'php', 'zstd', 'readelf', 'strip', 'objcopy']), 'native payload tools required')
    def test_abi_rebuild_reuses_core_only_when_all_changed_library_consumers_are_selected(self):
        old = self.root / 'old'
        self.fixture(old, 1, php=True)
        source = self.root / 'bound-module.c'
        source.write_text('extern int exported_function(void); int addon(void) { return exported_function(); }\n')
        subprocess.run(['gcc', '-shared', '-fPIC', '-g', str(source), '-L' + str(old / 'lib'),
                        '-l:libfixture.so.1', '-o', str(old / 'modules/imagick-zts-86.so')], check=True)
        for command, extras in [('record', ['--config', str(self.root / 'craft.yml'), '--php-version', '8.6.0RC4', '--arch', 'x86_64']), ('seal', [])]:
            subprocess.run(['php', str(ROOT / 'bin/build-payload'), command, '--root', str(old), *extras], check=True, stdout=subprocess.DEVNULL)
        state = self.state()
        archive = self.root / 'cache.tar.zst'
        cache.export(argparse.Namespace(root=self.root, payload=old, compatibility=self.compiler, key=state['key'],
                                        seed_key=state['seed-key'], source_run_id='1234', output=archive))
        self.manifest['libraries']['fixture']['abi_epoch'] = 2
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
        self.assertEqual(cache.read(args.payload / 'shared-libraries.json')['libraries']['fixture']['abi_epoch'], 2)
        args.payload = self.root / 'derived-abi'
        args.compile_packages = ''
        args.allow_all_components = True
        cache.restore(args)
        self.assertEqual(cache.read(args.payload / 'consumer-cache-seed.json')['rebuild_packages'], ['imagick'])

        source.write_text('extern int exported_function(void); int main(void) { return exported_function() == 1 ? 0 : 1; }\n')
        subprocess.run(['gcc', '-g', str(source), '-L' + str(old / 'lib'), '-l:libfixture.so.1',
                        '-Wl,-rpath,' + str(old / 'lib'), '-o', str(old / 'bin/php')], check=True)
        self.manifest['libraries']['fixture']['abi_epoch'] = 1
        for command, extras in [('record', ['--config', str(self.root / 'craft.yml'), '--php-version', '8.6.0RC4', '--arch', 'x86_64']), ('seal', [])]:
            subprocess.run(['php', str(ROOT / 'bin/build-payload'), command, '--root', str(old), *extras], check=True, stdout=subprocess.DEVNULL)
        cache.export(argparse.Namespace(root=self.root, payload=old, compatibility=self.compiler, key=state['key'],
                                        seed_key=state['seed-key'], source_run_id='1234', output=archive))
        self.manifest['libraries']['fixture']['abi_epoch'] = 2
        args.payload = self.root / 'unsafe-core'
        with self.assertRaisesRegex(RuntimeError, 'outside the selected packages: bin/php'):
            cache.restore(args)
        self.assertFalse(args.payload.exists())

    @unittest.skipUnless(all(shutil.which(command) for command in ['gcc', 'php', 'zstd', 'readelf', 'strip', 'objcopy']), 'native payload tools required')
    def test_abi_change_unused_by_php_does_not_recompile_requested_source_retry(self):
        old = self.root / 'old'
        self.fixture(old, 1, php=True)
        state = self.state()
        archive = self.root / 'cache.tar.zst'
        cache.export(argparse.Namespace(root=self.root, payload=old, compatibility=self.compiler, key=state['key'],
                                        seed_key=state['seed-key'], source_run_id='1234', output=archive))
        self.manifest['libraries']['fixture']['abi_epoch'] = 2
        current = self.state()
        sdk = self.root / 'sdk'
        self.fixture(sdk, 2)
        target = self.root / 'target'
        cache.restore(argparse.Namespace(root=self.root, stream='8.6', sdk=sdk, payload=target, compatibility=self.compiler,
                                         key=current['key'], seed_key=current['seed-key'], archive=archive,
                                         compile_packages='imagick', abi_rebuild_libraries='fixture'))
        receipt = cache.read(target / 'consumer-cache-seed.json')
        self.assertEqual(receipt['rebuild_packages'], [])
        self.assertFalse(receipt['abi_rebuild_required'])
        self.assertEqual((target / 'modules/imagick-zts-86.so').read_bytes(), (old / 'modules/imagick-zts-86.so').read_bytes())

if __name__ == '__main__':
    unittest.main()
