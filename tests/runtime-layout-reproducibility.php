<?php

declare(strict_types=1);

use staticphp\package\runtime_layout;
use staticphp\step\CreatePackages;
use Symfony\Component\Process\Process;

require dirname(__DIR__) . '/vendor/autoload.php';

if (($argv[1] ?? '') === '--worker') {
    define('BASE_PATH', dirname(__DIR__));
    define('TEMP_DIR', $argv[2] . '/stage');
    define('BUILD_ROOT_PATH', $argv[2] . '/buildroot');
    define('BUILD_BIN_PATH', BUILD_ROOT_PATH . '/bin');
    define('BUILD_LIB_PATH', BUILD_ROOT_PATH . '/lib');
    define('DIST_PATH', $argv[2]);
    define('DIST_DEB_PATH', $argv[2] . '/deb');
    define('SPP_TYPE', 'deb');
    foreach ([TEMP_DIR, BUILD_ROOT_PATH, DIST_DEB_PATH] as $directory) {
        mkdir($directory, 0755, true);
    }
    (new ReflectionProperty(CreatePackages::class, 'packageType'))->setValue(null, 'deb');
    (new ReflectionProperty(CreatePackages::class, 'iterationOverride'))->setValue(null, '1');
    $layout = new runtime_layout();
    (new ReflectionMethod(CreatePackages::class, 'createDebPackage'))->invoke(null, $layout, $layout->getVersion(), 'all');
    exit;
}

$root = sys_get_temp_dir() . '/spp-layout-repro-' . bin2hex(random_bytes(8));
try {
    foreach (['first', 'second'] as $run) {
        (new Process([PHP_BINARY, __FILE__, '--worker', $root . '/' . $run]))->setTimeout(120)->mustRun();
        if ($run === 'first') {
            sleep(2);
        }
    }
    $version = (new runtime_layout())->getVersion();
    $first = $root . '/first/deb/php-zts-runtime-layout_' . $version . '-1_all.deb';
    $second = $root . '/second/deb/php-zts-runtime-layout_' . $version . '-1_all.deb';
    if (hash_file('sha256', $first) !== hash_file('sha256', $second)) {
        throw new RuntimeException('Independent layout builds produced different archive bytes');
    }
    echo "Passed byte-identical native DEB layout packaging across independent workspaces and timestamps.\n";
} finally {
    if (is_dir($root)) {
        $files = new RecursiveIteratorIterator(new RecursiveDirectoryIterator($root, FilesystemIterator::SKIP_DOTS), RecursiveIteratorIterator::CHILD_FIRST);
        foreach ($files as $file) {
            $file->isDir() && !$file->isLink() ? rmdir($file->getPathname()) : unlink($file->getPathname());
        }
        rmdir($root);
    }
}
