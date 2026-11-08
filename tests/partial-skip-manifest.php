<?php

declare(strict_types=1);

use staticphp\step\RunSPC;
use Symfony\Component\Process\Process;

require dirname(__DIR__) . '/vendor/autoload.php';
require_once dirname(__DIR__) . '/src/step/RunSPC.php';
$process = new Process(['mktemp', '-d', '/tmp/spp-partial-skips-XXXXXXXX']);
$process->mustRun();
$root = trim($process->getOutput());
define('BASE_PATH', $root);
try {
    mkdir($root . '/buildroot');
    $file = $root . '/buildroot/skipped-shared-extensions.json';
    $previous = ['schema' => 1, 'skipped' => [
        ['extension' => 'redis', 'message' => 'Prior unrelated PHP 8.6 failure'],
        ['extension' => 'imagick', 'message' => 'Previous selected extension failure'],
    ]];
    $merge = new ReflectionMethod(RunSPC::class, 'mergeSkippedExtensions');
    file_put_contents($file, json_encode(['schema' => 1, 'skipped' => []]));
    $merge->invoke(null, $previous, ['imagick']);
    $actual = json_decode(file_get_contents($file), true);
    if (array_column($actual['skipped'], 'extension') !== ['redis']) {
        throw new RuntimeException('A successful partial rebuild lost an unrelated skip or retained its own old failure');
    }
    file_put_contents($file, json_encode(['schema' => 1, 'skipped' => [['extension' => 'imagick', 'message' => 'New build failure']]]));
    $merge->invoke(null, $previous, ['imagick']);
    $actual = json_decode(file_get_contents($file), true);
    if (array_column($actual['skipped'], 'extension') !== ['redis', 'imagick'] || $actual['skipped'][1]['message'] !== 'New build failure') {
        throw new RuntimeException('A failed partial rebuild did not retain the current failure and unrelated prior skip');
    }
    echo "Passed partial rebuild skip preservation and selected-extension success/failure transitions.\n";
} finally {
    @unlink($root . '/buildroot/skipped-shared-extensions.json');
    @rmdir($root . '/buildroot');
    @rmdir($root);
}
