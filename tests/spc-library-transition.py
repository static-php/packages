#!/usr/bin/env python3
"""Exercise SPC's installer across a selective native SONAME transition."""

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / '.github/actions/shared-libraries'))
spec = importlib.util.spec_from_file_location('incremental', ROOT / '.github/actions/shared-libraries/incremental-build.py')
incremental = importlib.util.module_from_spec(spec)
spec.loader.exec_module(incremental)


RUNNER = r'''<?php
declare(strict_types=1);

use StaticPHP\Artifact\ArtifactCache;
use StaticPHP\Config\ArtifactConfig;
use StaticPHP\Config\PackageConfig;
use StaticPHP\DI\ApplicationContext;
use StaticPHP\Package\LibraryPackage;
use StaticPHP\Package\PackageInstaller;
use StaticPHP\Registry\PackageLoader;

$autoload = $argv[1] . '/vendor/autoload.php';
require is_file($autoload) ? $autoload : dirname($argv[1], 2) . '/autoload.php';
require $argv[1] . '/src/bootstrap.php';
logger()->setLevel('error');
$names = ['base', 'consumer', 'peer', 'other'];
$configs = $artifacts = $packages = [];
foreach ($names as $short) {
    $name = 'transition-' . $short;
    $configs[$name] = ['type' => 'library', 'artifact' => $name,
        'shared-libs' => ['libtransition-' . $short . '.so'],
        'depends' => $short === 'consumer' ? ['transition-base'] : []];
    $artifacts[$name] = ['source' => ['type' => 'local'],
        'metadata' => ['license' => 'MIT', 'license-files' => ['LICENSE']]];
    $package = new LibraryPackage($name, 'library');
    $package->addStage('build', function () use ($short, $name): void {
        $cache = json_decode(file_get_contents(DOWNLOAD_PATH . '/.cache.json'), true, 512, JSON_THROW_ON_ERROR);
        $version = $short === 'base' ? $cache[$name]['source']['version'] : '1';
        $soname = 'libtransition-' . $short . '-' . $version . '.so.1';
        $realname = $soname . '.0';
        $command = [getenv('CC'), '-shared', '-fPIC', '-g', '-gdwarf-4', '-Wl,-soname,' . $soname,
            '-o', BUILD_LIB_PATH . '/' . $realname, WORKING_DIR . '/native-sources/' . $name . '/fixture.c'];
        if ($short === 'consumer') {
            $command[] = '-L' . BUILD_LIB_PATH;
            $command[] = '-ltransition-base';
        }
        $process = proc_open($command, [1 => ['pipe', 'w'], 2 => ['pipe', 'w']], $pipes);
        $output = stream_get_contents($pipes[1]) . stream_get_contents($pipes[2]);
        fclose($pipes[1]);
        fclose($pipes[2]);
        if (proc_close($process) !== 0) {
            throw new RuntimeException('Native fixture compile failed: ' . $output);
        }
        symlink($realname, BUILD_LIB_PATH . '/' . $soname);
        symlink($soname, BUILD_LIB_PATH . '/libtransition-' . $short . '.so');
        // libtool refreshes neighboring SONAME links during installation.
        foreach (glob(BUILD_LIB_PATH . '/libtransition-*.so.1.0') as $dso) {
            $link = substr($dso, 0, -2);
            @unlink($link);
            symlink(basename($dso), $link);
        }
        file_put_contents(WORKING_DIR . '/compiled.jsonl', json_encode($short) . "\n", FILE_APPEND);
    });
    $packages[$name] = $package;
}
(new ReflectionProperty(PackageConfig::class, 'package_configs'))->setValue(null, $configs);
ArtifactConfig::_restoreFromCache($artifacts);
(new ReflectionProperty(PackageLoader::class, 'packages'))->setValue(null, $packages);
ApplicationContext::set(ArtifactCache::class, new ArtifactCache());
$options = ['with-shared-libs' => true, 'with-shared-libdir' => '/usr/lib/php-zts/lib',
    'no-download' => true, 'no-tracker' => true, 'reuse-installed-library-dependencies' => true];
if ($argv[2] === 'reuse') {
    $options['use-existing-libraries'] = true;
}
$installer = new PackageInstaller($options, false);
foreach (explode(',', $argv[3]) as $short) {
    if ($argv[2] === 'reuse') {
        $installer->addInstallPackage($packages['transition-' . $short]);
    } else {
        $installer->addBuildPackage($packages['transition-' . $short]);
    }
}
$installer->run(true);
echo file_get_contents(BUILD_ROOT_PATH . '/shared-libraries.json');
'''


class InstallerTransitionTests(unittest.TestCase):
    def test_native_soname_transition_runs_root_then_consumer_in_separate_installers(self):
        if not all(shutil.which(command) for command in ['php', 'cc', 'g++', 'patchelf', 'readelf']):
            self.skipTest('Native fixture requires PHP, C/C++ compilers, patchelf and readelf')
        with tempfile.TemporaryDirectory(prefix='spc-installer-transition-') as temporary:
            root = Path(temporary)
            (root / 'downloads').mkdir()
            (root / 'runner.php').write_text(RUNNER)
            cache = {}
            bodies = {'base': 'int transition_base(void) { return 1; }',
                      'consumer': 'extern int transition_base(void); int transition_consumer(void) { return transition_base(); }',
                      'peer': 'int transition_peer(void) { return 55; }',
                      'other': 'int transition_other(void) { return 77; }'}
            for short, body in bodies.items():
                directory = root / 'native-sources' / ('transition-' + short)
                directory.mkdir(parents=True)
                (directory / 'fixture.c').write_text(body)
                (directory / 'LICENSE').write_text('MIT fixture license')
                cache['transition-' + short] = {'source': {'cache_type': 'local', 'dirname': str(directory),
                    'hash': hashlib.sha256(body.encode()).hexdigest(), 'version': '1'}}

            def save_cache():
                (root / 'downloads/.cache.json').write_text(json.dumps(cache))

            def run(action, selected, success=True):
                if action == 'build':
                    incremental.order_owned_outputs(root / 'buildroot', ['transition-' + name for name in selected.split(',')])
                environment = dict(os.environ, SPC_LIBC='musl' if Path('/etc/alpine-release').is_file() else 'glibc', SPC_TARGET='', SPC_TOOLCHAIN='StaticPHP\\Toolchain\\GccNativeToolchain',
                                   SPC_SKIP_TOOLCHAIN_CHECK='yes', SPC_ENABLE_LOG_FILE='0',
                                   CC='/usr/bin/gcc' if Path('/usr/bin/gcc').is_file() else shutil.which('cc'),
                                   CXX='/usr/bin/g++' if Path('/usr/bin/g++').is_file() else shutil.which('g++'),
                                   BUILD_ROOT_PATH=str(root / 'buildroot'), SOURCE_PATH=str(root / 'source'),
                                   DOWNLOAD_PATH=str(root / 'downloads'), PKG_ROOT_PATH=str(root / 'pkgroot'))
                for name in ['BUILD_LIB_PATH', 'BUILD_BIN_PATH', 'BUILD_INCLUDE_PATH']:
                    environment.pop(name, None)
                result = subprocess.run(['php', str(root / 'runner.php'), str(SPC_ROOT), action, selected],
                                        cwd=root, env=environment, capture_output=True, text=True)
                if success:
                    self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
                    return json.loads(result.stdout)
                self.assertNotEqual(result.returncode, 0, result.stderr + result.stdout)
                return result.stderr + result.stdout

            def signature(relative):
                path = root / 'buildroot' / relative
                return hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns

            def compiled():
                return [json.loads(line) for line in (root / 'compiled.jsonl').read_text().splitlines()]

            save_cache()
            initial = run('build', 'base,consumer,peer,other')
            self.assertNotIn('pending_rebuilds', initial)
            self.assertEqual(compiled(), ['base', 'consumer', 'peer', 'other'])
            consumer = signature('lib/libtransition-consumer-1.so.1')
            other = signature('lib/libtransition-other-1.so.1')
            source = root / 'native-sources/transition-base/fixture.c'
            source.write_text('int transition_base(void) { return 2; }')
            cache['transition-base']['source'].update(version='2', hash=hashlib.sha256(source.read_bytes()).hexdigest())
            save_cache()

            transition = run('build', 'base')
            self.assertEqual(compiled(), ['base', 'consumer', 'peer', 'other', 'base'])
            self.assertFalse((root / 'buildroot/lib/libtransition-base-1.so.1').exists())
            self.assertEqual(transition['pending_rebuilds'], {'transition-consumer': {'libtransition-base-1.so.1': 'transition-base'}})
            self.assertEqual(signature('lib/libtransition-consumer-1.so.1'), consumer)
            self.assertEqual(signature('lib/libtransition-other-1.so.1'), other)
            rejection = run('reuse', 'consumer', success=False)
            self.assertRegex(rejection, r'(?:incomplete|pending|LIBRARY_REUSE|invalid)')
            self.assertEqual(compiled(), ['base', 'consumer', 'peer', 'other', 'base'])

            final = run('build', 'consumer,peer')
            self.assertNotIn('pending_rebuilds', final)
            self.assertEqual(compiled(), ['base', 'consumer', 'peer', 'other', 'base', 'consumer', 'peer'])
            self.assertEqual(signature('lib/libtransition-other-1.so.1'), other)
            self.assertEqual(final['libraries']['transition-consumer']['depends'], ['transition-base'])
            consumer_elf = final['libraries']['transition-consumer']['elf']['lib/libtransition-consumer-1.so.1.0']
            self.assertIn('libtransition-base-2.so.1', consumer_elf['needed'])
            self.assertNotIn('libtransition-base-1.so.1', consumer_elf['needed'])
            run('reuse', 'consumer')
            self.assertEqual(compiled(), ['base', 'consumer', 'peer', 'other', 'base', 'consumer', 'peer'])
            incremental.write(root / 'buildroot/shared-library-build-inputs.json', {'libraries': {
                name: {'artifact': name, 'sources': {name: record}} for name, record in cache.items()}})
            cache['transition-other']['source']['hash'] = 'new-unselected-snapshot-source'
            save_cache()
            self.assertIn('LIBRARY_REUSE invalid', run('reuse', 'other', success=False))
            pipeline = incremental.helper_module('glibc.py', 'transition_pipeline')
            pipeline.bind_library_sources(root / 'buildroot', root / 'downloads')
            run('reuse', 'base,consumer,peer,other')
            self.assertEqual(compiled(), ['base', 'consumer', 'peer', 'other', 'base', 'consumer', 'peer'])
            self.assertEqual(signature('lib/libtransition-other-1.so.1'), other)
            (root / 'check.c').write_text('extern int transition_consumer(void); int main(void) { return transition_consumer() == 2 ? 0 : 1; }')
            compiler = '/usr/bin/gcc' if Path('/usr/bin/gcc').is_file() else shutil.which('cc')
            subprocess.run([compiler, str(root / 'check.c'), '-L' + str(root / 'buildroot/lib'),
                            '-ltransition-consumer', '-Wl,-rpath-link,' + str(root / 'buildroot/lib'),
                            '-o', str(root / 'check')], check=True, capture_output=True)
            subprocess.run([str(root / 'check')], env=dict(os.environ, LD_LIBRARY_PATH=str(root / 'buildroot/lib')), check=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--spc-root', type=Path, default=Path(__file__).resolve().parents[1] / 'vendor/crazywhalecc/static-php-cli')
    args, remaining = parser.parse_known_args()
    SPC_ROOT = args.spc_root.resolve()
    unittest.main(argv=[__file__] + remaining)
