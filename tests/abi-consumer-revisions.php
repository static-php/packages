<?php

declare(strict_types=1);

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
$bump = new ReflectionProperty(CreatePackages::class, 'bump');
$bump->setValue(null, false);
$frankenCounter = new ReflectionMethod(frankenphp::class, 'resolveIteration');
$version = CreatePackages::getTaggedPackageVersion($type, '1.13.3');
$moduleVersion = CreatePackages::getTaggedPackageVersion($type, '3.8.1');
$release = static fn (string $base, int $revision, bool $legacy = false): string => $base . ($type === 'apk' ? '-r' : '-') . $revision . ($type === 'rpm' && $legacy ? '.el8' : '');
$baselinePath = $directory . '/baseline.json';
$planPath = $directory . '/plan.json';

try {
    $baseline = ['schema' => 1, 'linkage' => 'shared', 'type' => $type, 'migration_cutoffs' => ['8.6' => []],
        'consumer_versions' => ['8.6' => ['php-zts-imagick' => $release($moduleVersion, 17, true),
                                         'php-zts-xdebug' => $release($moduleVersion, 23, true),
                                         'frankenphp' => $release($version, 19, true)]]];
    $baseline['runtime_layout'] = ['package_version' => $release('1.0', 7)];
    file_put_contents($baselinePath, json_encode($baseline, JSON_THROW_ON_ERROR));
    $baselinePaths = [$baselinePath];
    if ($type === 'apk') {
        $foreign = $baseline;
        $foreign['architecture'] = 'arm64';
        $foreign['runtime_layout']['package_version'] = $release('1.0', 99);
        $baselinePaths[] = $directory . '/foreign.json';
        file_put_contents(end($baselinePaths), json_encode($foreign, JSON_THROW_ON_ERROR));
    }
    putenv('SPP_BASELINE_MANIFESTS=' . json_encode($baselinePaths, JSON_THROW_ON_ERROR));
    if (CreatePackages::resolveIteration('php-zts-runtime-layout', '1.0', 'x86_64', $type) !== '7') {
        throw new RuntimeException('Regenerated runtime layout changed its captured revision');
    }
    $plan = ['schema' => 1, 'type' => $type, 'abi_break' => true,
        'published_consumers' => ['8.6' => ['php-zts-imagick' => $release($moduleVersion, 17, true), 'frankenphp' => $release($version, 19, true)]],
        'abi_consumer_requests' => ['8.6' => ['full' => false, 'packages' => ['imagick', 'frankenphp'], 'consumers' => ['php-zts-imagick', 'frankenphp']]]];
    file_put_contents($planPath, json_encode($plan, JSON_THROW_ON_ERROR));
    putenv('SPP_SHARED_LIBRARY_PLAN=' . $planPath);
    foreach ([['php-zts-imagick', $moduleVersion, 17], ['frankenphp', $version, 19]] as [$name, $base, $expected]) {
        $revision = $counter->invoke(null, $name, $base, 'x86_64', $type);
        if ($revision !== $expected || CreatePackages::resolveIteration($name, $base, 'x86_64', $type) !== (string) $expected) {
            throw new RuntimeException('ABI consumer changed revision without --bump: ' . $name);
        }
    }
    if ($counter->invoke(null, 'php-zts-xdebug', $moduleVersion, 'x86_64', $type) !== 23) {
        throw new RuntimeException('Packaging changed an unrelated consumer revision without --bump');
    }
    $memo->setValue(null, []);
    if ($frankenCounter->invoke(new frankenphp(), 'frankenphp', $version, 'x86_64', $type, null, false) !== '19') {
        throw new RuntimeException('FrankenPHP did not share the ABI consumer revision floor');
    }
    $override->setValue(null, '4');
    if (CreatePackages::resolveIteration('php-zts-imagick', $moduleVersion, 'x86_64', $type) !== '4') {
        throw new RuntimeException('Explicit user iteration override lost precedence');
    }
    $override->setValue(null, null);
    $plan['abi_break'] = false;
    file_put_contents($planPath, json_encode($plan, JSON_THROW_ON_ERROR));
    if ($counter->invoke(null, 'php-zts-imagick', $moduleVersion, 'x86_64', $type) !== 17) {
        throw new RuntimeException('Ordinary packaging ignored the current published revision');
    }
    putenv('SPP_SHARED_LIBRARY_PLAN');
    if ($counter->invoke(null, 'php-zts-imagick', $moduleVersion, 'x86_64', $type) !== 17) {
        throw new RuntimeException('Captured published revision was ignored without an ABI plan');
    }
    $localName = match ($type) {
        'rpm' => "php-zts-imagick-{$moduleVersion}-29.x86_64.rpm",
        'deb' => "php-zts-imagick_{$moduleVersion}-29_amd64.deb",
        'apk' => "php-zts-imagick-{$moduleVersion}-r29.x86_64.apk",
    };
    touch($directory . '/' . $localName);
    $memo->setValue(null, []);
    foreach (['php-zts-imagick', 'php-zts-imagick-debuginfo'] as $name) {
        if (CreatePackages::resolveIteration($name, $moduleVersion, $type === 'deb' ? 'amd64' : 'x86_64', $type) !== '29') {
            throw new RuntimeException('Existing local outputs implicitly bumped a package or its debug companion');
        }
    }
    if (CreatePackages::resolveIteration('php-zts-imagick', '9.9.9', 'x86_64', $type) !== ($type === 'apk' ? '0' : '1')) {
        throw new RuntimeException('New upstream release inherited a previous revision');
    }
    putenv('SPP_RPM_REPO_URL=https://fixture.invalid');
    putenv('SPP_FORGEJO_HOST=https://fixture.invalid');
    putenv('SPP_FORGEJO_OWNER=86');
    if ($type === 'rpm') {
        $pages = ['https://fixture.invalid/x86_64/el8/' => [200, "php-zts-imagick-{$moduleVersion}-29.x86_64.rpm"]];
    } else {
        $kind = $type === 'deb' ? 'debian' : 'alpine';
        $url = "https://fixture.invalid/api/v1/packages/86?type={$kind}&q=php-zts-imagick&limit=1000&page=";
        $pages = [$url . '1' => [200, json_encode([['name' => 'php-zts-imagick', 'version' => $release($moduleVersion, 29)]])],
                  $url . '2' => [200, '[]']];
    }
    (new ReflectionProperty(CreatePackages::class, 'httpCache'))->setValue(null, $pages);
    $bump->setValue(null, true);
    $memo->setValue(null, []);
    foreach (['php-zts-imagick', 'php-zts-imagick-debuginfo'] as $name) {
        if (CreatePackages::resolveIteration($name, $moduleVersion, 'x86_64', $type) !== '30') {
            throw new RuntimeException('Explicit --bump failed to advance the published package and debug revision');
        }
    }
    echo 'Passed ' . $type . " revision preservation for ABI consumers, local outputs and FrankenPHP, plus explicit bumps/overrides.\n";
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
