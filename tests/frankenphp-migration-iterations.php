<?php

declare(strict_types=1);

use staticphp\package\frankenphp;
use staticphp\step\CreatePackages;

require dirname(__DIR__) . '/vendor/autoload.php';

$root = trim((string) shell_exec('mktemp -d /tmp/spp-franken-migration-XXXXXXXX'));
if ($root === '' || !is_dir($root)) {
    throw new RuntimeException('Cannot create FrankenPHP migration fixture');
}
define('DIST_RPM_PATH', $root);
define('DIST_DEB_PATH', $root);
define('DIST_APK_PATH', $root);
$previous = getenv('SPP_BASELINE_MANIFESTS');
$runtime = new ReflectionProperty(CreatePackages::class, 'versionArch');
$runtime->setValue(null, ['8.6.0~rc4', 'x86_64']);
$resolve = new ReflectionMethod(frankenphp::class, 'resolveIteration');
$options = getopt('', ['baseline-root:']);

try {
    foreach ([
        ['rpm', '1.13.3_86.0~rc4', '1.13.3_86.0~rc4-1.el8', 'x86_64', 2, 'rpm/el8/x86_64/8.6/baseline.json'],
        ['deb', '1.13.3+php86.0~rc4', '1.13.3+php86.0~rc4-1', 'amd64', 2, 'deb/x86_64/8.6/baseline.json'],
        ['apk', '1.13.3p86.0_rc4', '1.13.3p86.0_rc4-r0', 'x86_64', 1, 'apk/x86_64/8.6/baseline.json'],
    ] as [$type, $version, $published, $arch, $expected, $relative]) {
        $path = $root . '/' . $type . '.json';
        if (isset($options['baseline-root'])) {
            $path = rtrim($options['baseline-root'], '/') . '/' . $relative;
            $data = json_decode((string) file_get_contents($path), true, 512, JSON_THROW_ON_ERROR);
            $matching = array_filter(array_merge($data['packages'] ?? [], $data['published_packages'] ?? []), static fn (array $package): bool => $package['name'] === 'frankenphp' && $package['version'] === $published);
            if ($matching === []) {
                throw new RuntimeException('Missing captured FrankenPHP baseline ' . $path);
            }
        } else {
            file_put_contents($path, json_encode(['type' => $type, 'packages' => [['name' => 'frankenphp', 'version' => $published, 'architecture' => $arch]]], JSON_THROW_ON_ERROR));
        }
        putenv('SPP_BASELINE_MANIFESTS=' . json_encode([$path], JSON_THROW_ON_ERROR));
        $iteration = $resolve->invoke(new frankenphp(), 'frankenphp', $version, 'x86_64', $type, null, false);
        if ($iteration !== (string) $expected) {
            throw new RuntimeException($type . ' FrankenPHP bump=false did not advance the captured static revision');
        }
        if ($resolve->invoke(new frankenphp(), 'frankenphp', $version, 'x86_64', $type, '17', false) !== '17') {
            throw new RuntimeException($type . ' FrankenPHP lost the explicit revision override');
        }
    }
    echo "Passed FrankenPHP RPM/DEB/APK first shared migration with bump=false and explicit overrides.\n";
} finally {
    $previous === false ? putenv('SPP_BASELINE_MANIFESTS') : putenv('SPP_BASELINE_MANIFESTS=' . $previous);
    foreach (glob($root . '/*') as $path) {
        unlink($path);
    }
    rmdir($root);
}
