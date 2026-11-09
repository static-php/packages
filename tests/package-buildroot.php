<?php

use staticphp\Command\PackageCommand;
use Symfony\Component\Console\Input\ArrayInput;
use Symfony\Component\Console\Output\NullOutput;

require dirname(__DIR__) . '/vendor/autoload.php';

$directory = sys_get_temp_dir() . '/spp-package-root-' . bin2hex(random_bytes(6));
define('BASE_PATH', $directory);
define('DIST_PATH', $directory . '/dist');
define('DIST_RPM_PATH', DIST_PATH . '/rpm');
define('DIST_DEB_PATH', DIST_PATH . '/deb');
define('DIST_APK_PATH', DIST_PATH . '/apk');
define('TEMP_DIR', $directory . '/tmp');
$root = $directory . '/format-sdk';
try {
    $command = new PackageCommand();
    $input = new ArrayInput(['--phpv' => '8.5', '--type' => 'rpm', '--buildroot' => $root], $command->getDefinition());
    (new ReflectionMethod($command, 'initialize'))->invoke($command, $input, new NullOutput());
    if (BUILD_ROOT_PATH !== $root || BUILD_BIN_PATH !== $root . '/bin'
        || BUILD_MODULES_PATH !== $root . '/modules' || SPP_PHP_VERSION !== '8.5') {
        throw new RuntimeException('Explicit package payload changed PHP stream or used another buildroot');
    }
    echo "Passed explicit package buildroot selection without changing PHP stream identity.\n";
} finally {
    if (is_dir($directory)) {
        foreach (new RecursiveIteratorIterator(new RecursiveDirectoryIterator($directory, FilesystemIterator::SKIP_DOTS), RecursiveIteratorIterator::CHILD_FIRST) as $path) {
            $path->isDir() ? rmdir($path->getPathname()) : unlink($path->getPathname());
        }
        rmdir($directory);
    }
}
