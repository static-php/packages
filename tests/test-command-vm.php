<?php

declare(strict_types=1);

use staticphp\Command\TestCommand;
use Symfony\Component\Console\Output\BufferedOutput;
use Symfony\Component\Process\Process;

require dirname(__DIR__) . '/vendor/autoload.php';

if (!isset($argv[1])) {
    foreach (['8.5', '8.6'] as $version) {
        $process = new Process([PHP_BINARY, __FILE__, $version]);
        $process->mustRun();
        echo $process->getOutput();
    }
    exit(0);
}

define('SPP_PHP_VERSION', $argv[1]);
$command = (new ReflectionClass(TestCommand::class))->newInstanceWithoutConstructor();
$verify = new ReflectionMethod(TestCommand::class, 'verifyVmKind');
$checks = 0;
$check = static function (string $kind, string $sapi, ?string $expected, ?string $failure = null) use ($command, $verify, &$checks): void {
    $output = new BufferedOutput();
    $result = $verify->invoke($command, $kind, $sapi, $output, $expected);
    $text = $output->fetch();
    if ($failure === null ? $result !== null : $result !== 1 || !str_contains($text, $failure)) {
        throw new RuntimeException("Unexpected VM result for {$sapi}/{$kind}: " . var_export($result, true) . " {$text}");
    }
    if (version_compare(SPP_PHP_VERSION, '8.6', '<') && $text !== '') {
        throw new RuntimeException('PHP <= 8.5 VM validation changed');
    }
    $checks++;
};

if (version_compare(SPP_PHP_VERSION, '8.6', '<')) {
    $check('unknown', 'CLI', null);
    $check('unknown', 'frankenphp', 'ZEND_VM_KIND_CALL');
    $check('ZEND_VM_KIND_TAILCALL', 'frankenphp', 'ZEND_VM_KIND_CALL');
} else {
    foreach (['CALL', 'SWITCH', 'GOTO', 'HYBRID', 'TAILCALL'] as $name) {
        $kind = 'ZEND_VM_KIND_' . $name;
        $check($kind, 'CLI', null);
        $check($kind, 'frankenphp', $kind);
    }
    foreach (['unknown', '', 'ZEND_VM_KIND_JIT', '4'] as $kind) {
        $check($kind, 'CLI', null, 'unknown VM kind');
        $check($kind, 'frankenphp', 'ZEND_VM_KIND_CALL', 'unknown VM kind');
    }
    $check('ZEND_VM_KIND_CALL', 'frankenphp', 'ZEND_VM_KIND_TAILCALL', 'expected CLI VM kind ZEND_VM_KIND_TAILCALL');
    $check('ZEND_VM_KIND_TAILCALL', 'frankenphp', 'ZEND_VM_KIND_CALL', 'expected CLI VM kind ZEND_VM_KIND_CALL');
    foreach (['CALL', 'TAILCALL', 'CALL'] as $name) {
        $kind = 'ZEND_VM_KIND_' . $name;
        $check($kind, 'CLI', null);
        $check($kind, 'frankenphp', $kind);
    }
}

echo 'Passed ' . $checks . ' TestCommand VM checks for PHP ' . SPP_PHP_VERSION . ".\n";
