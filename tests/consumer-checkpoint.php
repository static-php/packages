<?php

declare(strict_types=1);

use staticphp\util\BuildPayload;
use Symfony\Component\Process\Process;
use Symfony\Component\Yaml\Yaml;

require dirname(__DIR__) . '/vendor/autoload.php';
require_once dirname(__DIR__) . '/src/util/BuildPayload.php';

function execute(array $arguments): string
{
    $process = new Process($arguments);
    $process->mustRun();
    return $process->getOutput();
}

$root = trim(execute(['mktemp', '-d', '/tmp/spp-consumer-checkpoint-XXXXXXXX']));
try {
    foreach (['bin', 'lib', 'include/php/Zend', 'downloads'] as $directory) {
        mkdir($root . '/' . $directory, 0755, true);
    }
    file_put_contents($root . '/main.c', '#include <stdio.h>' . "\n" . 'int main(void) { printf("8.6.0RC4"); return 0; }');
    file_put_contents($root . '/embed.c', 'int fixture(void) { return 42; }');
    execute(['cc', '-g', $root . '/main.c', '-o', $root . '/bin/php']);
    execute(['cc', '-g', '-shared', '-fPIC', $root . '/embed.c', '-o', $root . '/lib/libphp-zts-86.so']);
    symlink('libphp-zts-86.so', $root . '/lib/libphp.so');
    file_put_contents($root . '/bin/phpize', '#!/bin/sh');
    file_put_contents($root . '/bin/php-config', '#!/bin/sh');
    file_put_contents($root . '/include/php/Zend/zend_modules.h', '#define ZEND_MODULE_API_NO 20260922');
    file_put_contents($root . '/downloads/.cache.json', '{}');
    $configuration = ['php-version' => '8.6.0RC4', 'extensions' => 'openssl',
        'shared-extensions' => 'curl,fileinfo', 'sapi' => 'cli,cgi,fpm,embed,frankenphp',
        'build-options' => ['with-shared-libdir' => '/usr/lib/php-zts/lib'], 'extra-env' => ['SPC_LIBC' => 'glibc']];
    file_put_contents($root . '/craft.yml', Yaml::dump($configuration));
    $before = hash_file('sha256', $root . '/bin/php');
    execute(['php', dirname(__DIR__) . '/bin/build-payload', 'checkpoint', '--root', $root,
        '--config', $root . '/craft.yml', '--php-version', '8.6.0RC4', '--arch', php_uname('m'), '--downloads', $root . '/downloads']);
    $metadata = BuildPayload::load($root, true)->metadata();
    if ($metadata['configuration'] !== $configuration || $metadata['build_context']['coverage'] !== 'incomplete'
        || $metadata['build_context']['kind'] !== 'intermediate-consumer-build'
        || hash_file('sha256', $root . '/bin/php') !== $before || isset($metadata['frankenphp_version'])
        || count($metadata['files']) !== 2) {
        throw new RuntimeException('Checkpoint changed core/configuration or claimed unavailable consumer outputs');
    }
    echo "Passed honest intermediate consumer checkpoint, full configuration preservation and unchanged core bytes.\n";
} finally {
    execute(['rm', '-rf', '--', $root]);
}
