<?php

declare(strict_types=1);

use staticphp\Command\TestCommand;
use staticphp\package\frankenphp;
use staticphp\step\CreatePackages;

require dirname(__DIR__) . '/vendor/autoload.php';

$options = getopt('', ['type:']);
$type = $options['type'] ?? 'rpm';
if (!in_array($type, ['rpm', 'deb', 'apk'], true)) {
    throw new RuntimeException('Expected --type=rpm|deb|apk');
}
$directory = trim((string) shell_exec('mktemp -d /tmp/spp-abi-revision-XXXXXXXX'));
if ($directory === '' || !is_dir($directory)) {
    throw new RuntimeException('Cannot create ABI revision fixture');
}
foreach (['DIST_RPM_PATH', 'DIST_DEB_PATH', 'DIST_APK_PATH'] as $constant) {
    define($constant, $directory);
}
$previousBaseline = getenv('SPP_BASELINE_MANIFESTS');
$previousPlan = getenv('SPP_SHARED_LIBRARY_PLAN');
$runtime = new ReflectionProperty(CreatePackages::class, 'versionArch');
$previousRuntime = $runtime->getValue();
$runtime->setValue(null, ['8.6.0~rc4', 'x86_64']);
$memo = new ReflectionProperty(CreatePackages::class, 'resolvedIterations');
$override = new ReflectionProperty(CreatePackages::class, 'iterationOverride');
$previousOverride = $override->getValue();
$override->setValue(null, null);
$counter = new ReflectionMethod(CreatePackages::class, 'migrationNextIteration');
$native = new ReflectionMethod(TestCommand::class, 'comparePackageVersions');
$command = (new ReflectionClass(TestCommand::class))->newInstanceWithoutConstructor();
$frankenCounter = new ReflectionMethod(frankenphp::class, 'resolveIteration');
$version = CreatePackages::getTaggedPackageVersion($type, '1.13.3');
$moduleVersion = CreatePackages::getTaggedPackageVersion($type, '3.8.1');
$release = static fn (string $base, int $revision, bool $legacy = false): string => $base . ($type === 'apk' ? '-r' : '-') . $revision . ($type === 'rpm' && $legacy ? '.el10' : '');
$baselinePath = $directory . '/baseline.json';
$planPath = $directory . '/plan.json';

try {
    $baseline = ['schema' => 1, 'linkage' => 'shared', 'type' => $type, 'migration_cutoffs' => ['8.6' => []],
        'consumer_versions' => ['8.6' => ['php-zts-imagick' => $release($moduleVersion, 17, true),
                                         'php-zts-xdebug' => $release($moduleVersion, 23, true),
                                         'frankenphp' => $release($version, 19, true)]]];
    file_put_contents($baselinePath, json_encode($baseline, JSON_THROW_ON_ERROR));
    putenv('SPP_BASELINE_MANIFESTS=' . json_encode([$baselinePath], JSON_THROW_ON_ERROR));
    $plan = ['schema' => 1, 'type' => $type, 'abi_break' => true,
        'published_consumers' => ['8.6' => ['php-zts-imagick' => $release($moduleVersion, 17, true), 'frankenphp' => $release($version, 19, true)]],
        'abi_consumer_requests' => ['8.6' => ['full' => false, 'packages' => ['imagick', 'frankenphp'], 'consumers' => ['php-zts-imagick', 'frankenphp']]]];
    file_put_contents($planPath, json_encode($plan, JSON_THROW_ON_ERROR));
    putenv('SPP_SHARED_LIBRARY_PLAN=' . $planPath);
    foreach ([['php-zts-imagick', $moduleVersion, 18], ['frankenphp', $version, 20]] as [$name, $base, $expected]) {
        $revision = $counter->invoke(null, $name, $base, 'x86_64', $type);
        if ($revision !== $expected || $native->invoke($command, $type, $release($base, $revision), $baseline['consumer_versions']['8.6'][$name]) <= 0) {
            throw new RuntimeException('Affected ABI consumer failed to advance its native release: ' . $name);
        }
    }
    $first = $type === 'apk' ? 0 : 1;
    if ($counter->invoke(null, 'php-zts-xdebug', $moduleVersion, 'x86_64', $type) !== $first) {
        throw new RuntimeException('Unchanged unrelated consumer received a forced revision bump');
    }
    $memo->setValue(null, []);
    if ($frankenCounter->invoke(new frankenphp(), 'frankenphp', $version, 'x86_64', $type, null, false) !== '20') {
        throw new RuntimeException('FrankenPHP did not share the ABI consumer revision floor');
    }
    $override->setValue(null, '4');
    if (CreatePackages::resolveIteration('php-zts-imagick', $moduleVersion, 'x86_64', $type) !== '4') {
        throw new RuntimeException('Explicit user iteration override lost precedence');
    }
    $override->setValue(null, null);
    $plan['abi_break'] = false;
    file_put_contents($planPath, json_encode($plan, JSON_THROW_ON_ERROR));
    if ($counter->invoke(null, 'php-zts-imagick', $moduleVersion, 'x86_64', $type) !== $first) {
        throw new RuntimeException('Compatible library update changed the default consumer revision policy');
    }
    putenv('SPP_SHARED_LIBRARY_PLAN');
    if ($counter->invoke(null, 'php-zts-imagick', $moduleVersion, 'x86_64', $type) !== $first) {
        throw new RuntimeException('Ordinary unchanged packaging changed bump=false behavior');
    }
    echo 'Passed ' . $type . " ABI consumer native revision floors, unaffected/noop defaults, FrankenPHP and explicit overrides.\n";
} finally {
    $previousBaseline === false ? putenv('SPP_BASELINE_MANIFESTS') : putenv('SPP_BASELINE_MANIFESTS=' . $previousBaseline);
    $previousPlan === false ? putenv('SPP_SHARED_LIBRARY_PLAN') : putenv('SPP_SHARED_LIBRARY_PLAN=' . $previousPlan);
    $runtime->setValue(null, $previousRuntime);
    $override->setValue(null, $previousOverride);
    $memo->setValue(null, []);
    foreach (glob($directory . '/*') as $path) {
        unlink($path);
    }
    rmdir($directory);
}
