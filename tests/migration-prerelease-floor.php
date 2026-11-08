<?php

declare(strict_types=1);

use staticphp\Command\TestCommand;
use staticphp\step\CreatePackages;

require dirname(__DIR__) . '/vendor/autoload.php';

$options = getopt('', ['type:']);
$type = $options['type'] ?? 'rpm';
if (!in_array($type, ['rpm', 'deb', 'apk'], true)) {
    throw new RuntimeException('Expected --type=rpm|deb|apk');
}
$temporary = trim((string) shell_exec('mktemp -d /tmp/spp-prerelease-floor-XXXXXXXX'));
if ($temporary === '' || !is_dir($temporary)) {
    throw new RuntimeException('Cannot create prerelease floor fixture');
}
$previous = getenv('SPP_BASELINE_MANIFESTS');
$runtime = new ReflectionProperty(CreatePackages::class, 'versionArch');
$previousRuntime = $runtime->getValue();
$runtime->setValue(null, ['8.6.0~rc4', 'x86_64']);
$counter = new ReflectionMethod(CreatePackages::class, 'migrationNextIteration');
$native = new ReflectionMethod(TestCommand::class, 'comparePackageVersions');
$command = (new ReflectionClass(TestCommand::class))->newInstanceWithoutConstructor();
$legacy = match ($type) {
    'rpm' => ['5.1.29_86.0~rc4+ext~~dev', '1.1.4_86.0~rc4+ext~~dev', '3.6.0_86.0~rc4+ext~alpha1'],
    'deb' => ['5.1.29+php86.0~rc4+ext~~dev', '1.1.4+php86.0~rc4+ext~~dev', '3.6.0+php86.0~rc4+ext~alpha1'],
    'apk' => ['5.1.29p86.0_rc4_p0_alpha0_alpha0', '1.1.4p86.0_rc4_p0_alpha0_alpha0', '3.6.0p86.0_rc4_p0_alpha1'],
};

try {
    $checks = 0;
    foreach ([['php-zts-apcu', '5.1.29-dev'], ['php-zts-ast', '1.1.4-dev'], ['php-zts-xdebug', '3.6.0alpha1']] as $index => [$name, $upstream]) {
        $old = $legacy[$index] . ($type === 'apk' ? '-r7' : '-7' . ($type === 'rpm' ? '.el10' : ''));
        $path = $temporary . '/baseline.json';
        file_put_contents($path, json_encode(['schema' => 1, 'type' => $type, 'packages' => [
            ['name' => $name, 'version' => $old, 'architecture' => $type === 'deb' ? 'amd64' : 'x86_64'],
        ]], JSON_THROW_ON_ERROR));
        putenv('SPP_BASELINE_MANIFESTS=' . json_encode([$path], JSON_THROW_ON_ERROR));
        $proposed = CreatePackages::getTaggedPackageVersion($type, $upstream);
        $floor = CreatePackages::migrationPackageVersion($type, $name, $proposed, 'x86_64');
        if ($floor !== $legacy[$index] || CreatePackages::migrationUpstreamVersion($type, $old) !== CreatePackages::migrationUpstreamVersion($type, $proposed)) {
            throw new RuntimeException($type . ' failed equivalent prerelease identity for ' . $name);
        }
        $revision = $counter->invoke(null, $name, $floor, 'x86_64', $type);
        $candidate = $floor . ($type === 'apk' ? '-r' : '-') . $revision;
        if ($revision !== 8 || $native->invoke($command, $type, $candidate, $old) <= 0) {
            throw new RuntimeException($type . ' first shared prerelease does not advance the native published package');
        }
        foreach ([$name, $name . '-debuginfo'] as $packageName) {
            if (CreatePackages::migrationPackageVersion($type, $packageName, $proposed, 'x86_64') !== $floor) {
                throw new RuntimeException('Runtime/debug prerelease floors differ');
            }
        }
        $changed = CreatePackages::getTaggedPackageVersion($type, $index === 2 ? '3.6.0alpha2' : str_replace('-dev', 'alpha1', $upstream));
        $changedFloor = CreatePackages::migrationPackageVersion($type, $name, $changed, 'x86_64');
        if (CreatePackages::migrationUpstreamVersion($type, $changedFloor) !== CreatePackages::migrationUpstreamVersion($type, $changed)
            || $native->invoke($command, $type, $changedFloor . ($type === 'apk' ? '-r0' : '-1'), $old) <= 0) {
            throw new RuntimeException('Legacy format migration masks or holds a newer upstream phase');
        }
        $runtime->setValue(null, ['8.6.0~rc3', 'x86_64']);
        $olderPhp = CreatePackages::getTaggedPackageVersion($type, $upstream);
        $olderFloor = CreatePackages::migrationPackageVersion($type, $name, $olderPhp, 'x86_64');
        if ($olderFloor !== str_replace('rc4', 'rc3', $floor)
            || $native->invoke($command, $type, $olderFloor . ($type === 'apk' ? '-r8' : '-8'), $old) >= 0) {
            throw new RuntimeException('Equivalent-format migration masks a lower PHP phase');
        }
        $runtime->setValue(null, ['8.6.0~rc4', 'x86_64']);
        if ($index === 2) {
            foreach (['alpha2', 'beta1', 'rc1', ''] as $phase) {
                $actualSource = '3.6.0' . $phase;
                $proposal = CreatePackages::getTaggedPackageVersion($type, $actualSource);
                $advanced = CreatePackages::migrationPackageVersion($type, $name, $proposal, 'x86_64');
                if (CreatePackages::migrationUpstreamVersion($type, $advanced) !== CreatePackages::migrationUpstreamVersion($type, $proposal)
                    || $native->invoke($command, $type, $advanced . ($type === 'apk' ? '-r0' : '-1'), $old) <= 0) {
                    throw new RuntimeException('Actual extension phase progress is held by the legacy PHP tag layout');
                }
            }
            $runtime->setValue(null, ['8.6.1', 'x86_64']);
            $rollback = CreatePackages::getTaggedPackageVersion($type, '3.6.0alpha0');
            if (CreatePackages::migrationPackageVersion($type, $name, $rollback, 'x86_64') !== $rollback) {
                throw new RuntimeException('A newer PHP tag hid a genuine extension upstream phase rollback');
            }
            $progress = CreatePackages::getTaggedPackageVersion($type, '3.6.0alpha2');
            $advanced = CreatePackages::migrationPackageVersion($type, $name, $progress, 'x86_64');
            if (CreatePackages::migrationUpstreamVersion($type, $advanced) !== CreatePackages::migrationUpstreamVersion($type, $progress)
                || $native->invoke($command, $type, $advanced . ($type === 'apk' ? '-r0' : '-1'), $old) <= 0) {
                throw new RuntimeException('PHP patch and actual upstream phase progress failed together');
            }
            $runtime->setValue(null, ['8.6.0~rc4', 'x86_64']);
        }
        $checks++;
    }
    if ($type === 'apk') {
        foreach ([
            ['php-zts-igbinary', '3.2.17RC1', '8.2.35', '3.2.17~rc1_82-r1'],
            ['php-zts-igbinary', '3.2.17RC1', '8.2.35', '3.2.17~rc1p82-r2'],
            ['php-zts-amqp', '2.2.0', '8.5.12', '2.2.0~dev_85-r1'],
        ] as [$name, $upstream, $php, $published]) {
            $runtime->setValue(null, [$php, 'x86_64']);
            $path = $temporary . '/baseline.json';
            file_put_contents($path, json_encode(['schema' => 1, 'type' => 'apk', 'migration_cutoffs' => [substr($php, 0, 3) => []],
                'packages' => [['name' => $name, 'version' => $published, 'architecture' => 'x86_64']]], JSON_THROW_ON_ERROR));
            putenv('SPP_BASELINE_MANIFESTS=' . json_encode([$path], JSON_THROW_ON_ERROR));
            $proposed = CreatePackages::getTaggedPackageVersion('apk', $upstream);
            $floor = CreatePackages::migrationPackageVersion('apk', $name, $proposed, 'x86_64');
            if ($floor !== $proposed) {
                throw new RuntimeException('An unorderable legacy APK stem was used as a false version floor');
            }
            $revision = $counter->invoke(null, $name, $floor, 'x86_64', 'apk');
            if ($native->invoke($command, 'apk', $floor . '-r' . $revision, $published) >= 0) {
                throw new RuntimeException('Unresolved legacy APK migration no longer triggers the version guard');
            }
            $checks++;
        }
    }
    echo 'Passed ' . $checks . ' native ' . $type . " migration format, revision and rollback guard checks.\n";
} finally {
    $previous === false ? putenv('SPP_BASELINE_MANIFESTS') : putenv('SPP_BASELINE_MANIFESTS=' . $previous);
    $runtime->setValue(null, $previousRuntime);
    foreach (glob($temporary . '/*') as $path) {
        unlink($path);
    }
    rmdir($temporary);
}
