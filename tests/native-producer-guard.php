<?php

declare(strict_types=1);

use staticphp\step\RunSPC;
use Symfony\Component\Process\Process;

require dirname(__DIR__) . '/vendor/autoload.php';
require_once dirname(__DIR__) . '/src/step/RunSPC.php';
$process = new Process(['mktemp', '-d', '/tmp/spp-native-producer-XXXXXXXX']);
$process->mustRun();
$root = trim($process->getOutput());
define('BASE_PATH', $root);
define('SPP_TYPE', is_file('/etc/alpine-release') ? 'rpm' : 'apk');
try {
    ob_start();
    $result = RunSPC::run();
    $output = ob_get_clean();
    $expected = SPP_TYPE === 'apk' ? 'native Alpine builder' : 'compiled on EL8';
    if ($result !== false || !str_contains($output, $expected) || file_exists($root . '/craft.yml') || is_dir($root . '/buildroot')) {
        throw new RuntimeException('A nonnative producer was allowed to start or modify the build');
    }
    echo "Passed nonnative producer rejection before source/config/build mutation.\n";
} finally {
    @rmdir($root);
}
