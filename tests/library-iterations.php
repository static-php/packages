<?php

use staticphp\step\CreatePackages;

require dirname(__DIR__) . '/vendor/autoload.php';

putenv('SPP_FORGEJO_HOST=https://first.invalid');
putenv('SPP_FORGEJO_HOSTS=https://first.invalid,https://second.invalid');
$cache = new ReflectionProperty(CreatePackages::class, 'httpCache');
$names = ['php-zts-lib-openssl', 'php-zts-lib-openssl-debuginfo', 'php-zts-runtime-layout', 'php-zts-runtime-layout-debuginfo'];
foreach (['deb' => 'debian', 'apk' => 'alpine'] as $type => $registryType) {
    $pages = [];
    foreach (['first' => 2, 'second' => 7] as $host => $revision) {
        foreach (['82', '83', '84', '85', '86'] as $owner) {
            $ownerRevision = $owner === '85' ? $revision + 4 : $revision;
            $suffix = $type === 'deb' ? '-' . $ownerRevision : '-r' . $ownerRevision;
            foreach (['php-zts-lib-openssl', 'php-zts-runtime-layout'] as $query) {
                $url = "https://{$host}.invalid/api/v1/packages/{$owner}?type={$registryType}&q={$query}&limit=1000&page=";
                $pages[$url . '1'] = [200, json_encode([['name' => 'unrelated', 'version' => '1.0-999']])];
                $pages[$url . '2'] = [200, json_encode(array_map(static fn (string $name): array => ['name' => $name, 'version' => '3.6.0' . $suffix], $names))];
                $pages[$url . '3'] = [200, '[]'];
            }
        }
    }
    $cache->setValue(null, $pages);
    foreach ($names as $name) {
        if (CreatePackages::getRemoteNextIteration($name, '3.6.0', 'x86_64', $type) !== 7) {
            throw new RuntimeException("Missed {$type} {$name} revisions across primary stream owners");
        }
    }
}
echo "Passed shared library revision lookup using only the primary registry across stream owners.\n";

$allLibraries = new ReflectionProperty(CreatePackages::class, 'allLibraryRevisions');
$allLibraries->setValue(null, true);
try {
    $pages = [];
    foreach (['82', '83', '84', '85', '86'] as $owner) {
        $url = "https://first.invalid/api/v1/packages/{$owner}?type=alpine&q=php-zts-lib-&limit=1000&page=";
        $pages[$url . '1'] = [200, json_encode([
            ['name' => 'php-zts-lib-openssl', 'version' => '3.6.0-r5'],
            ['name' => 'php-zts-lib-openssl-debuginfo', 'version' => '3.6.0-r7'],
            ['name' => 'php-zts-lib-zlib', 'version' => '1.4-r2'],
        ])];
        $pages[$url . '2'] = [200, '[]'];
    }
    $cache->setValue(null, $pages);
    foreach ([['php-zts-lib-openssl', '3.6.0', 6], ['php-zts-lib-openssl-debuginfo', '3.6.0', 8], ['php-zts-lib-zlib', '1.4', 3]] as [$name, $version, $expected]) {
        if (CreatePackages::getRemoteNextIteration($name, $version, 'aarch64', 'apk') !== $expected) {
            throw new RuntimeException('Full library bump lost a revision in the shared filtered listing');
        }
    }
} finally {
    $allLibraries->setValue(null, false);
}
echo "Passed batched full-library revision lookup without a full-registry scan.\n";

$directory = sys_get_temp_dir() . '/spp-library-revisions-' . bin2hex(random_bytes(6));
mkdir($directory);
foreach (['DIST_RPM_PATH', 'DIST_DEB_PATH', 'DIST_APK_PATH'] as $constant) {
    define($constant, $directory);
}
$previousPlan = getenv('SPP_SHARED_LIBRARY_PLAN');
$previousPublish = getenv('PUBLISH');
$override = new ReflectionProperty(CreatePackages::class, 'iterationOverride');
try {
    foreach (['rpm' => '0:1.26.0-7.el8', 'deb' => '1.26.0-7', 'apk' => '1.26.0-r7'] as $type => $published) {
        $path = $directory . '/' . $type . '.json';
        file_put_contents($path, json_encode(['schema' => 1, 'type' => $type, 'published_versions' => ['ngtcp2' => $published]], JSON_THROW_ON_ERROR));
        putenv('SPP_SHARED_LIBRARY_PLAN=' . $path);
        foreach (['php-zts-lib-ngtcp2', 'php-zts-lib-ngtcp2-debuginfo'] as $name) {
            if (CreatePackages::resolveIteration($name, '1.26.0', 'aarch64', $type) !== '8') {
                throw new RuntimeException("Repacked {$type} library did not advance past its captured revision");
            }
        }
        if (CreatePackages::resolveIteration('php-zts-lib-ngtcp2', '1.27.0', 'aarch64', $type) !== ($type === 'apk' ? '0' : '1')) {
            throw new RuntimeException('A new upstream release inherited an older release revision');
        }
        $override->setValue(null, '3');
        if (CreatePackages::resolveIteration('php-zts-lib-ngtcp2', '1.26.0', 'aarch64', $type) !== '3') {
            throw new RuntimeException('Explicit library revision override lost precedence');
        }
        $override->setValue(null, null);
    }
    $path = $directory . '/publication.json';
    file_put_contents($path, json_encode(['schema' => 1, 'type' => 'apk', 'libraries' => ['ngtcp2'],
        'published_versions' => ['ngtcp2' => '1.26.0-r1']], JSON_THROW_ON_ERROR));
    putenv('SPP_SHARED_LIBRARY_PLAN=' . $path);
    putenv('PUBLISH=true');
    $pages = [];
    foreach (['82', '83', '84', '85', '86'] as $owner) {
        $url = "https://first.invalid/api/v1/packages/{$owner}?type=alpine&q=php-zts-lib-ngtcp2&limit=1000&page=";
        $pages[$url . '1'] = [200, json_encode([
            ['name' => 'php-zts-lib-ngtcp2', 'version' => '1.26.0-r' . ($owner === '82' ? '6' : '1')],
            ['name' => 'php-zts-lib-ngtcp2-debuginfo', 'version' => '1.26.0-r' . ($owner === '82' ? '7' : '1')],
        ])];
        $pages[$url . '2'] = [200, '[]'];
    }
    $cache->setValue(null, $pages);
    (new ReflectionProperty(CreatePackages::class, 'resolvedIterations'))->setValue(null, []);
    (new ReflectionProperty(CreatePackages::class, 'debuginfo'))->setValue(null, true);
    foreach (['php-zts-lib-ngtcp2', 'php-zts-lib-ngtcp2-debuginfo'] as $name) {
        if (CreatePackages::resolveIteration($name, '1.26.0', 'aarch64', 'apk') !== '8') {
            throw new RuntimeException('Selected library publication ignored a higher revision in another PHP stream');
        }
    }
} finally {
    putenv($previousPlan === false ? 'SPP_SHARED_LIBRARY_PLAN' : 'SPP_SHARED_LIBRARY_PLAN=' . $previousPlan);
    putenv($previousPublish === false ? 'PUBLISH' : 'PUBLISH=' . $previousPublish);
    foreach (glob($directory . '/*') as $path) {
        unlink($path);
    }
    rmdir($directory);
}
echo "Passed library repack revision floors and runtime/debuginfo pairing without remote queries.\n";
echo "Passed selected library publication above every primary stream's runtime and debug revisions.\n";
