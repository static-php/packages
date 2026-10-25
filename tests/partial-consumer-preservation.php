<?php

declare(strict_types=1);

use staticphp\step\RunSPC;
use staticphp\util\BuildPayload;
use staticphp\util\ConsumerSnapshot;
use Symfony\Component\Process\Process;

require dirname(__DIR__) . '/vendor/autoload.php';
require_once dirname(__DIR__) . '/src/step/RunSPC.php';

function run(array $arguments): void
{
    $process = new Process($arguments);
    $process->mustRun();
}
function check(bool $condition, string $message): void
{
    if (!$condition) {
        throw new RuntimeException($message);
    }
}
function identity(string $path): string { return hash_file('sha256', $path); }
function rejected(callable $operation, string $message): void
{
    try {
        $operation();
    } catch (Throwable $error) {
        check(str_contains($error->getMessage(), $message), 'Unexpected failure: ' . $error->getMessage());
        return;
    }
    throw new RuntimeException('Expected rejection: ' . $message);
}

$temporary = new Process(['mktemp', '-d', '/tmp/spp-partial-consumers-XXXXXXXX']);
$temporary->mustRun();
$temporary = trim($temporary->getOutput());
$execute = new ReflectionMethod(RunSPC::class, 'executeProcess');
$publish = new ReflectionMethod(RunSPC::class, 'publishBuiltFiles');
try {
    foreach ([0, 23] as $exitCode) {
        $case = $temporary . '/' . $exitCode;
        $root = $case . '/buildroot';
        $previous = $case . '/previous';
        foreach (['bin', 'lib', 'modules', 'debug', 'include/php/Zend', 'include/php/ext/selected', 'lib/php/build'] as $directory) {
            mkdir($root . '/' . $directory, 0755, true);
        }
        file_put_contents($case . '/core.c', 'int main(void) { return 0; }');
        file_put_contents($case . '/module.c', 'int fixture(void) { return 42; }');
        file_put_contents($case . '/new.c', 'int fixture(void) { return 43; }');
        run(['cc', '-g', $case . '/core.c', '-o', $root . '/bin/php']);
        run(['cc', '-g', '-shared', '-fPIC', $case . '/module.c', '-o', $root . '/lib/libphp-zts-86.so']);
        foreach (['kept', 'selected'] as $extension) {
            copy($root . '/lib/libphp-zts-86.so', $root . '/modules/' . $extension . '-zts-86.so');
            symlink($extension . '-zts-86.so', $root . '/modules/' . $extension . '.so');
            run(['objcopy', '--only-keep-debug', $root . '/modules/' . $extension . '-zts-86.so', $root . '/debug/' . $extension . '-zts-86.so.debug']);
            run(['objcopy', '--add-gnu-debuglink=' . $root . '/debug/' . $extension . '-zts-86.so.debug', $root . '/modules/' . $extension . '-zts-86.so']);
        }
        symlink('libphp-zts-86.so', $root . '/lib/libphp.so');
        file_put_contents($root . '/bin/phpize', '#!/bin/sh');
        file_put_contents($root . '/bin/php-config', '#!/bin/sh');
        file_put_contents($root . '/include/php/Zend/zend_modules.h', "#define ZEND_MODULE_API_NO 20260922\n");
        file_put_contents($root . '/include/php/ext/selected/header.h', 'selected header');
        file_put_contents($root . '/lib/php/build/phpize.m4', 'core build asset');
        run(['cp', '-a', $root, $previous]);
        mkdir($previous . '/packaging/frankenphp/package', 0755, true);
        file_put_contents($previous . '/packaging/frankenphp/package/Caddyfile', 'pinned source asset');
        $configuration = ['php-version' => '8.6.0RC4', 'sapi' => 'cli', 'extensions' => [], 'shared-extensions' => 'kept,selected', 'build-options' => ['with-shared-libdir' => '/usr/lib/php-zts/lib']];
        BuildPayload::record($previous, $configuration, '8.6.0RC4', php_uname('m'));
        $metadata = BuildPayload::seal($previous)->metadata();
        $hashes = [];
        foreach (['bin/php', 'lib/libphp-zts-86.so', 'modules/kept-zts-86.so', 'debug/kept-zts-86.so.debug', 'include/php/Zend/zend_modules.h'] as $relative) {
            $hashes[$relative] = identity($root . '/' . $relative);
        }
        $snapshot = ConsumerSnapshot::capture($root, $metadata, ['selected'], $previous);
        run(['cc', '-g', '-shared', '-fPIC', $case . '/new.c', '-o', $case . '/new.so']);
        $process = new Process(['python3', '-c', <<<'PY'
import os, shutil, sys
root, new, status = sys.argv[1:]
for directory in ['modules', 'debug', 'packaging']:
    shutil.rmtree(os.path.join(root, directory), ignore_errors=True)
os.unlink(os.path.join(root, 'bin/php'))
os.makedirs(os.path.join(root, 'modules'))
shutil.copyfile(new, os.path.join(root, 'modules/selected-zts-86.so'))
os.symlink('selected-zts-86.so', os.path.join(root, 'modules/selected.so'))
sys.exit(int(status))
PY, $root, $case . '/new.so', (string) $exitCode]);
        check($execute->invoke(null, $process, $snapshot) === $exitCode, 'The actual SPC exit status changed');
        foreach ($hashes as $relative => $hash) {
            check(identity($root . '/' . $relative) === $hash, 'An unselected consumer was lost or changed: ' . $relative);
        }
        check(is_link($root . '/modules/kept.so') && readlink($root . '/modules/kept.so') === 'kept-zts-86.so', 'Unselected extension alias was lost');
        check(identity($root . '/modules/selected-zts-86.so') === identity($case . '/new.so'), 'Selected output was overwritten by preservation');
        check(!file_exists($root . '/debug/selected-zts-86.so.debug'), 'An old selected debug file was restored');
        check(file_get_contents($root . '/packaging/frankenphp/package/Caddyfile') === 'pinned source asset', 'Pinned cached package assets were lost');
        check(glob($case . '/.consumer-snapshot-*') === [], 'Successful preservation left a disposable snapshot');
        if ($exitCode === 0) {
            $candidate = $case . '/candidate';
            run(['cp', '-a', $root, $candidate]);
            BuildPayload::record($candidate, $configuration, '8.6.0RC4', php_uname('m'));
            rejected(static fn () => $publish->invoke(null, $candidate, $previous), 'unsealed');
            check(identity($previous . '/modules/selected-zts-86.so') === $metadata['files']['modules/selected-zts-86.so']['sha256'], 'A rejected candidate replaced the prior payload');
            BuildPayload::seal($candidate);
            $publish->invoke(null, $candidate, $previous);
            check(identity($previous . '/modules/selected-zts-86.so') === identity($case . '/new.so'), 'Validated candidate was not atomically published');
            check(identity($candidate . '/modules/selected-zts-86.so') === $metadata['files']['modules/selected-zts-86.so']['sha256'], 'The old payload was not retained until atomic exchange completed');
        }
        unlink($root . '/packaging/frankenphp/package/Caddyfile');
        unlink($previous . '/packaging/frankenphp/package/Caddyfile');
        rejected(static fn () => ConsumerSnapshot::capture($root, $metadata, ['selected'], $previous), 'Missing or changed retained consumer');
    }
    echo "Passed SPC cleanup preservation on success/failure, cached package assets, preflight rejection and validated atomic payload replacement.\n";
} finally {
    run(['rm', '-rf', $temporary]);
}
