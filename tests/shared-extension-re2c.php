<?php

declare(strict_types=1);

use StaticPHP\Package\PhpExtensionPackage;

require dirname(__DIR__) . '/vendor/autoload.php';

define('SOURCE_PATH', sys_get_temp_dir() . '/spp-re2c-' . bin2hex(random_bytes(8)));
$directory = SOURCE_PATH . '/php-src';
mkdir($directory, 0755, true);
$makefile = $directory . '/Makefile';
$pdo = new PhpExtensionPackage('pdo', extension_config: ['builtin' => true]);
try {
    foreach ([
        ["RE2C = re2c\nRE2C_FLAGS =\nSHLIB_SUFFIX_NAME = so\n", 're2c', '--no-generation-date -W'],
        ["RE2C =\r\nRE2C_FLAGS = \t\r\nSHLIB_SUFFIX_NAME = so\r\n", 're2c', '--no-generation-date -W'],
        ["RE2C = /opt/bin/re2c\nRE2C_FLAGS\t=\t-W -g\n", '/opt/bin/re2c', '-W -g'],
    ] as [$contents, $tool, $flags]) {
        file_put_contents($makefile, $contents);
        $expected = ['RE2C=' . escapeshellarg($tool), 'RE2C_FLAGS=' . escapeshellarg($flags)];
        if ($pdo->re2cMakeVars() !== $expected) {
            throw new RuntimeException('Incorrect re2c Makefile arguments: ' . implode(' ', $pdo->re2cMakeVars()));
        }
    }
    echo "Passed shared extension re2c Makefile argument parsing.\n";
} finally {
    unlink($makefile);
    rmdir($directory);
    rmdir(SOURCE_PATH);
}
