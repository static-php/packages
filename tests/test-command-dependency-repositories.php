<?php

declare(strict_types=1);

use staticphp\Command\TestCommand;
use Symfony\Component\Console\Output\BufferedOutput;

require dirname(__DIR__) . '/vendor/autoload.php';

define('SPP_PHP_VERSION', '8.6');
$command = (new ReflectionClass(TestCommand::class))->newInstanceWithoutConstructor();
$classify = new ReflectionMethod(TestCommand::class, 'isLibraryOnly');
foreach ([
    [[], false],
    [['php-zts-runtime-layout'], true],
    [['php-zts-lib-zlib', 'php-zts-runtime-layout'], true],
    [['php-zts-lib-zlib', 'php-zts-lib-gcc-runtime', 'php-zts-runtime-layout'], true],
    [['php-zts-lib-zlib', 'php-zts-runtime-layout', 'php-zts-cli'], false],
    [['php-zts-runtime-layout', 'php-zts-imagick'], false],
] as [$names, $expected]) {
    if ($classify->invoke(null, $names) !== $expected) {
        throw new RuntimeException('Incorrect PHP-independent package classification: ' . json_encode($names));
    }
}

$repository = new ReflectionMethod(TestCommand::class, 'dependencyRepository');
foreach (['deb' => 'debian', 'apk' => 'alpine/main/php-zts'] as $type => $suffix) {
    if ($repository->invoke(null, $type, 'https://example.invalid/', '86') !== 'https://example.invalid/api/packages/86/' . $suffix) {
        throw new RuntimeException('Dependency source does not use the existing selected stream');
    }
}

$previousHost = getenv('SPP_FORGEJO_HOST');
$previousOwner = getenv('SPP_FORGEJO_OWNER');
$previousLibraries = getenv('SPP_FORGEJO_LIBS_OWNER');
try {
    putenv('SPP_FORGEJO_HOST=https://example.invalid');
    putenv('SPP_FORGEJO_OWNER=85');
    putenv('SPP_FORGEJO_LIBS_OWNER=unavailable-library-owner');
    $arguments = (new ReflectionMethod(TestCommand::class, 'baseRepoArgs'))->invoke($command, 'apk', new BufferedOutput());
    if ($arguments !== ['--repository', 'https://example.invalid/api/packages/85/alpine/main/php-zts']) {
        throw new RuntimeException('Partial APK validation still requires an additional library repository');
    }
} finally {
    $previousHost === false ? putenv('SPP_FORGEJO_HOST') : putenv('SPP_FORGEJO_HOST=' . $previousHost);
    $previousOwner === false ? putenv('SPP_FORGEJO_OWNER') : putenv('SPP_FORGEJO_OWNER=' . $previousOwner);
    $previousLibraries === false ? putenv('SPP_FORGEJO_LIBS_OWNER') : putenv('SPP_FORGEJO_LIBS_OWNER=' . $previousLibraries);
}

echo "Passed independent layout/library classification and existing-owner dependency repository checks.\n";
