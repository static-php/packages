<?php

use staticphp\step\CreatePackages;

require dirname(__DIR__) . '/vendor/autoload.php';
define('BASE_PATH', dirname(__DIR__));
$directory = sys_get_temp_dir() . '/spp-library-revisions-' . bin2hex(random_bytes(6));
mkdir($directory);
foreach (['DIST_RPM_PATH', 'DIST_DEB_PATH', 'DIST_APK_PATH'] as $constant) {
    define($constant, $directory);
}
$memo = new ReflectionProperty(CreatePackages::class, 'resolvedIterations');
$bump = new ReflectionProperty(CreatePackages::class, 'bump');
$debug = new ReflectionProperty(CreatePackages::class, 'debuginfo');
$override = new ReflectionProperty(CreatePackages::class, 'iterationOverride');
(new ReflectionProperty(CreatePackages::class, 'versionArch'))->setValue(null, ['8.3.36', 'x86_64']);
$check = static function (bool $condition, string $message): void {
    if (!$condition) {
        throw new RuntimeException($message);
    }
};
$snapshot = static function (string $type, string $stream, string $arch, array $packages, array $extra = []) use ($directory): void {
    $path = $directory . '/inventory.json';
    file_put_contents($path, json_encode(array_replace(['schema' => 1, 'type' => $type, 'php_stream' => $stream,
        'architecture' => $arch, 'inventory' => $packages], $extra), JSON_THROW_ON_ERROR));
    putenv('SPP_NATIVE_INVENTORY=' . $path);
};
try {
    foreach (['deb', 'apk'] as $type) {
        $suffix = $type === 'apk' ? '-r' : '-';
        $arch = $type === 'deb' ? 'amd64' : 'x86_64';
        $otherArch = $type === 'deb' ? 'arm64' : 'aarch64';
        $row = static fn (string $name, int $revision, string $architecture): array =>
            ['name' => $name, 'version' => '1.4' . $suffix . $revision, 'architecture' => $architecture];
        foreach (['php-zts-lib-zlib', 'php-zts-runtime-layout', 'php-zts', 'php-zts-cli', 'frankenphp', 'pie-zts'] as $name) {
            putenv('SPP_FORGEJO_OWNER=83');
            $snapshot($type, '8.3', 'x86_64', [$row($name, 1, $arch), $row($name, 8, $otherArch)]);
            $memo->setValue(null, []);
            $bump->setValue(null, true);
            $debug->setValue(null, false);
            $check(CreatePackages::resolveIteration($name, '1.4', $arch, $type) === '2', "{$type} {$name} r1 must bump to r2 in PHP83 x86");
            putenv('SPP_FORGEJO_OWNER=86');
            $snapshot($type, '8.6', 'arm64', [$row($name, 8, $otherArch), $row($name, 20, $arch)]);
            $check(CreatePackages::resolveIteration($name, '1.4', $otherArch, $type) === '9', "{$type} {$name} PHP86 arm r8 must bump to r9");
            $snapshot($type, '8.6', 'x86_64', [$row($name, 8, $arch)]);
            $check(CreatePackages::resolveIteration($name, '1.4', $arch, $type) === '9', 'Revision memo leaked from a different owner');
            $check(CreatePackages::getRemoteNextIteration($name, '1.5', $arch, $type) === ($type === 'apk' ? 0 : 1), 'New upstream inherited an old revision');

            putenv('SPP_FORGEJO_OWNER=83');
            $snapshot($type, '8.3', 'x86_64', [$row($name, 1, $arch), $row($name . '-debuginfo', 3, $arch), $row($name . '-debuginfo', 50, $otherArch)]);
            $memo->setValue(null, []);
            $debug->setValue(null, true);
            foreach ([$name, $name . '-debuginfo'] as $paired) {
                $check(CreatePackages::resolveIteration($paired, '1.4', $arch, $type) === '4', 'Runtime/debug pair missed its own higher revision');
            }
            $plan = $directory . '/plan.json';
            file_put_contents($plan, json_encode(['schema' => 1, 'type' => $type, 'published_versions' => ['zlib' => '1.4' . $suffix . '8']]));
            putenv('SPP_SHARED_LIBRARY_PLAN=' . $plan);
            $bump->setValue(null, false);
            $memo->setValue(null, []);
            $check(CreatePackages::resolveIteration($name, '1.4', $arch, $type) === '1', 'bump=false inherited another repository revision');
            $override->setValue(null, '6');
            $check(CreatePackages::resolveIteration($name, '1.4', $arch, $type) === '6', 'Explicit revision lost precedence');
            $override->setValue(null, null);
            putenv('SPP_NATIVE_INVENTORY');
            $baselines = [];
            foreach ([['8.3', 'x86_64', $arch, 1], ['8.3', 'arm64', $otherArch, 8], ['8.6', 'x86_64', $arch, 9]] as [$stream, $target, $native, $revision]) {
                $path = $directory . '/baseline-' . count($baselines) . '.json';
                $published = '1.4' . $suffix . $revision;
                file_put_contents($path, json_encode(['type' => $type, 'php_stream' => $stream, 'architecture' => $target,
                    'packages' => [$row($name, $revision, $native)], 'runtime_layout' => ['package_version' => $published],
                    'consumer_versions' => [$stream => [$name => $published]], 'migration_cutoffs' => [$stream => [$name => $published]]]));
                $baselines[] = $path;
            }
            putenv('SPP_BASELINE_MANIFESTS=' . json_encode($baselines));
            $memo->setValue(null, []);
            $check(CreatePackages::resolveIteration($name, '1.4', $arch, $type) === '1', 'Legacy baseline revision leaked across stream or architecture');
            putenv('SPP_BASELINE_MANIFESTS');
        }
        foreach ([['type' => 'rpm'], ['php_stream' => '8.6'], ['architecture' => 'arm64'], ['error' => 'unavailable'], ['inventory' => null]] as $invalid) {
            $snapshot($type, '8.3', 'x86_64', [], $invalid);
            try {
                CreatePackages::getRemoteNextIteration('php-zts-lib-zlib', '1.4', $arch, $type);
                throw new LogicException('Invalid captured inventory was accepted');
            } catch (RuntimeException $error) {
                $check(str_contains($error->getMessage(), 'Published inventory snapshot'), 'Unexpected inventory validation failure');
            }
        }
    }
    define('SPP_PHP_VERSION', '8.3');
    $libsOnly = new ReflectionProperty(CreatePackages::class, 'libsOnly');
    $libsOnly->setValue(null, true);
    (new ReflectionProperty(CreatePackages::class, 'versionArch'))->setValue(null, ['8.4.12', 'x86_64']);
    putenv('SPP_FORGEJO_OWNER');
    $snapshot('deb', '8.3', 'x86_64', [['name' => 'php-zts-lib-zlib', 'version' => '1.4-1', 'architecture' => 'amd64']]);
    $check(CreatePackages::getRemoteNextIteration('php-zts-lib-zlib', '1.4', 'amd64', 'deb') === 2, 'Library repository followed the SDK canonical stream instead of --phpv');
    $check(CreatePackages::getPhpVersionTag('deb', '1.4') === '+php84', 'Consumer version tags stopped following the actual payload');
    putenv('SPP_FORGEJO_OWNER=86');
    $snapshot('deb', '8.6', 'x86_64', [['name' => 'php-zts-lib-zlib', 'version' => '1.4-8', 'architecture' => 'amd64']]);
    $check(CreatePackages::getRemoteNextIteration('php-zts-lib-zlib', '1.4', 'amd64', 'deb') === 9, 'Explicit Forgejo owner did not override the requested library stream');
    $libsOnly->setValue(null, false);
    putenv('SPP_NATIVE_INVENTORY');
    putenv('SPP_SHARED_LIBRARY_PLAN');
    putenv('SPP_FORGEJO_HOST=file://' . $directory);
    foreach (['83' => 1, '86' => 8] as $owner => $revision) {
        $base = $directory . '/api/packages/' . $owner . '/debian/';
        foreach (['amd64' => $revision, 'arm64' => 20, 'all' => 0] as $arch => $value) {
            $path = $base . 'dists/php-zts/main/binary-' . $arch;
            mkdir($path, 0777, true);
            $record = "Package: php-zts-lib-zlib\nVersion: 1.4-{$value}\nArchitecture: {$arch}\nFilename: fixture.deb\nSHA256: " . str_repeat('a', 64) . "\n\n";
            file_put_contents($path . '/Packages', $arch === 'all' ? '' : $record);
        }
        file_put_contents($base . 'repository.key', 'fixture');
    }
    putenv('SPP_FORGEJO_OWNER=83');
    $check(CreatePackages::getRemoteNextIteration('php-zts-lib-zlib', '1.4', 'amd64', 'deb') === 2, 'Native index fallback missed exact owner/arch');
    unlink($directory . '/api/packages/83/debian/dists/php-zts/main/binary-amd64/Packages');
    $check(CreatePackages::getRemoteNextIteration('php-zts-lib-zlib', '1.4', 'x86_64', 'deb') === 2, 'Native index was fetched again for each package');
    $check(CreatePackages::getRemoteNextIteration('php-zts-lib-zlib', '1.4', 'arm64', 'deb') === 21, 'Native index cache mixed architectures');
    putenv('SPP_FORGEJO_OWNER=86');
    $check(CreatePackages::getRemoteNextIteration('php-zts-lib-zlib', '1.4', 'amd64', 'deb') === 9, 'Native index cache mixed owners');
    putenv('SPP_FORGEJO_OWNER=85');
    try {
        CreatePackages::getRemoteNextIteration('php-zts-lib-zlib', '1.4', 'amd64', 'deb');
        throw new LogicException('Unavailable native index silently used an initial revision');
    } catch (\Symfony\Component\Process\Exception\ProcessFailedException) {
    }
} finally {
    foreach (['SPP_NATIVE_INVENTORY', 'SPP_FORGEJO_HOST', 'SPP_FORGEJO_OWNER', 'SPP_SHARED_LIBRARY_PLAN'] as $variable) {
        putenv($variable);
    }
    foreach (new RecursiveIteratorIterator(new RecursiveDirectoryIterator($directory, FilesystemIterator::SKIP_DOTS), RecursiveIteratorIterator::CHILD_FIRST) as $entry) {
        $entry->isDir() ? rmdir($entry->getPathname()) : unlink($entry->getPathname());
    }
    rmdir($directory);
}
echo "Passed exact owner/architecture revisions, runtime/debug pairing, preserved revisions and cached native-index fallback.\n";
