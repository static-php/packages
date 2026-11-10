<?php

use staticphp\step\CreatePackages;

require dirname(__DIR__) . '/vendor/autoload.php';

putenv('SPP_FORGEJO_HOST=https://example.com');
putenv('SPP_FORGEJO_OWNER=86');
$cache = new ReflectionProperty(CreatePackages::class, 'httpCache');
$version = '3.6.0~alpha1+php86.0~rc3';

foreach (['', 'xdebug'] as $selection) {
    putenv('PACKAGES=' . $selection);
    foreach (['deb' => 'debian', 'apk' => 'alpine'] as $type => $registryType) {
        $suffix = $type === 'deb' ? '-9' : '-r9';
        $query = '&q=php-zts-xdebug';
        $url = "https://example.com/api/v1/packages/86?type={$registryType}{$query}&limit=1000&page=";
        $cache->setValue(null, [
            $url . '1' => [200, json_encode([['name' => 'unrelated', 'version' => '1.0-99']])],
            $url . '2' => [200, json_encode([['name' => 'php-zts-xdebug', 'version' => $version . $suffix]])],
            $url . '3' => [200, '[]'],
        ]);
        if (CreatePackages::getRemoteNextIteration('php-zts-xdebug', $version, 'amd64', $type) !== 10) {
            throw new RuntimeException("Missed {$type} revision on a later registry page");
        }
    }
}
putenv('PACKAGES');

$indexes = [];
foreach (['82', '83', '84', '85', '86'] as $owner) {
    $url = "https://example.com/api/v1/packages/{$owner}?type=alpine&q=php-zts-lib-zlib&limit=1000&page=";
    $indexes[$url . '1'] = [200, json_encode([['name' => 'php-zts-lib-zlib', 'version' => '1.3.1-r2']])];
    $indexes[$url . '2'] = [200, $owner === '83' ? json_encode([['name' => 'php-zts-lib-zlib', 'version' => '1.3.1-r11']]) : '[]'];
    $indexes[$url . '3'] = [200, '[]'];
}
$cache->setValue(null, $indexes);
if (CreatePackages::getRemoteNextIteration('php-zts-lib-zlib', '1.3.1', 'aarch64', 'apk') !== 12) {
    throw new RuntimeException('Missed a library revision on another stream owner or later filtered page');
}

echo "Passed remote revision pagination checks.\n";

putenv('SPP_RPM_REPO_URL=https://example.com');
foreach (['php-zts-lib-fixture', 'php-zts-xdebug', 'frankenphp'] as $name) {
    $cache->setValue(null, [
        'https://example.com/x86_64/el8/' => [200, "{$name}-1.2.3-4.x86_64.rpm"],
        'https://example.com/x86_64/el9/' => [500, null],
        'https://example.com/x86_64/el10/' => [500, null],
    ]);
    if (CreatePackages::getRemoteNextIteration($name, '1.2.3', 'x86_64', 'rpm') !== 5) {
        throw new RuntimeException('RPM revision selection must use EL8 without querying retired repositories');
    }
}
putenv('SPP_RPM_REPO_URL');
echo "Passed library, extension and SAPI revision selection with unavailable retired repositories.\n";

$baseline = tempnam(sys_get_temp_dir(), 'spp-revision-floor-');
putenv('SPP_BASELINE_MANIFESTS=' . json_encode([$baseline]));
putenv('SPP_RPM_REPO_URL=https://example.com');
try {
    foreach (['rpm', 'deb', 'apk'] as $type) {
        $version = $type === 'apk' ? '2.2.0.0_p83_p36_p0' : '2.2.0+php83.36';
        $cutoff = $type === 'apk' ? '2.2.0p83.36_p0-r1' : $version . '-2' . ($type === 'rpm' ? '.el8' : '');
        file_put_contents($baseline, json_encode(['schema' => 1, 'type' => $type, 'architecture' => 'x86_64',
            'migration_cutoffs' => ['8.3' => ['php-zts-amqp' => $cutoff]]], JSON_THROW_ON_ERROR));
        $pages = [];
        foreach (['x86_64', 'aarch64'] as $arch) {
            $pages["https://example.com/{$arch}/el8/"] = [200, '[]'];
        }
        foreach (['debian', 'alpine'] as $kind) {
            $pages["https://example.com/api/v1/packages/86?type={$kind}&q=php-zts-amqp&limit=1000&page=1"] = [200, '[]'];
        }
        $cache->setValue(null, $pages);
        foreach (['php-zts-amqp', 'php-zts-amqp-debuginfo'] as $name) {
            $actual = CreatePackages::getRemoteNextIteration($name, $version, 'x86_64', $type);
            if ($actual !== ($type === 'apk' ? 1 : 2)) {
                throw new RuntimeException("{$type} automatic bump violates the CLI migration minimum for {$name}");
            }
        }
        if (CreatePackages::getRemoteNextIteration('php-zts-amqp', $version, 'aarch64', $type) !== ($type === 'apk' ? 0 : 1)) {
            throw new RuntimeException('Migration minimum leaked between architectures');
        }
        if (CreatePackages::getRemoteNextIteration('php-zts-amqp', '9.0.0', 'x86_64', $type) !== ($type === 'apk' ? 0 : 1)) {
            throw new RuntimeException('Migration minimum leaked into a different upstream version');
        }
    }
} finally {
    unlink($baseline);
    putenv('SPP_BASELINE_MANIFESTS');
    putenv('SPP_RPM_REPO_URL');
}
echo "Passed RPM/DEB/APK automatic revision bumps above inherited migration cutoffs.\n";

$directory = sys_get_temp_dir() . '/spp-cutoff-revisions-' . bin2hex(random_bytes(6));
mkdir($directory);
define('DIST_APK_PATH', $directory);
$baseline = $directory . '/baseline.json';
file_put_contents($baseline, json_encode(['schema' => 1, 'type' => 'apk', 'migration_cutoffs' => ['8.3' => [
    'php-zts-amqp' => '2.2.0p83.36_p0-r1',
    'frankenphp' => '1.13.5p83.36-r2',
]]], JSON_THROW_ON_ERROR));
putenv('SPP_BASELINE_MANIFESTS=' . json_encode([$baseline], JSON_THROW_ON_ERROR));
putenv('SPP_FORGEJO_OWNER=83');
(new ReflectionProperty(CreatePackages::class, 'versionArch'))->setValue(null, ['8.3.36', 'x86_64']);
$memo = new ReflectionProperty(CreatePackages::class, 'resolvedIterations');
$query = 'https://example.com/api/v1/packages/83?type=alpine&q=';
$cache->setValue(null, [
    $query . 'php-zts-amqp&limit=1000&page=1' => [200, '[]'],
    $query . 'frankenphp&limit=1000&page=1' => [200, '[]'],
]);
try {
    foreach ([false, true] as $bump) {
        $memo->setValue(null, []);
        (new ReflectionProperty(CreatePackages::class, 'bump'))->setValue(null, $bump);
        foreach (['php-zts-amqp', 'php-zts-amqp-debuginfo'] as $name) {
            if (CreatePackages::resolveIteration($name, '2.2.0.0_p83_p36_p0', 'x86_64', 'apk') !== '1') {
                throw new RuntimeException('Normalized replacement was rejected by its own CLI migration cutoff');
            }
        }
        $franken = new ReflectionMethod(\staticphp\package\frankenphp::class, 'resolveIteration');
        if ($franken->invoke(new \staticphp\package\frankenphp(), 'frankenphp', '1.13.5.0_p83_p36', 'x86_64', 'apk', null, $bump) !== '2') {
            throw new RuntimeException('FrankenPHP replacement lost its migration cutoff revision');
        }
        if (CreatePackages::resolveIteration('php-zts-amqp', '2.2.1.0_p83', 'x86_64', 'apk') !== '0') {
            throw new RuntimeException('Historical cutoff changed a newer upstream release revision');
        }
    }
    $memo->setValue(null, []);
    $cache->setValue(null, [$query . 'php-zts-amqp&limit=1000&page=1' => [200, json_encode([
        ['name' => 'php-zts-amqp', 'version' => '2.2.0.0_p83_p36_p0-r5'],
    ])], $query . 'php-zts-amqp&limit=1000&page=2' => [200, '[]']]);
    if (CreatePackages::resolveIteration('php-zts-amqp', '2.2.0.0_p83_p36_p0', 'x86_64', 'apk') !== '6') {
        throw new RuntimeException('Migration cutoff overrode a higher published revision');
    }
    echo "Passed replacement cutoff revision floors and package-scoped bumps.\n";
} finally {
    putenv('SPP_BASELINE_MANIFESTS');
    unlink($baseline);
    rmdir($directory);
}

$retryDirectory = sys_get_temp_dir() . '/spp-revision-http-' . bin2hex(random_bytes(6));
mkdir($retryDirectory);
$originalPath = getenv('PATH');
$originalProcessEnvironment = [$_ENV, $_SERVER];
$retryState = $retryDirectory . '/state.json';
$http = new ReflectionMethod(CreatePackages::class, 'httpGet');
file_put_contents($retryDirectory . '/curl', <<<'PHP'
#!/usr/bin/env php
<?php
$path = __DIR__ . '/state.json';
$state = json_decode(file_get_contents($path), true, 512, JSON_THROW_ON_ERROR);
$index = $state['attempts']++;
$state['urls'][] = end($argv);
file_put_contents($path, json_encode($state, JSON_THROW_ON_ERROR));
$response = $state['responses'][min($index, count($state['responses']) - 1)];
if ($response[0] === 0) {
    exit(7);
}
echo $response[1] . "\n" . $response[0];
PHP);
chmod($retryDirectory . '/curl', 0755);
putenv('PATH=' . $retryDirectory . ':' . $originalPath);
$_ENV['PATH'] = $_SERVER['PATH'] = $retryDirectory . ':' . $originalPath;
try {
    foreach ([
        'temporary-server' => [[[500, 'first error'], [502, 'second error'], [200, 'final body']], 3, [200, 'final body']],
        'temporary-connection' => [[[0, ''], [200, 'connected']], 2, [200, 'connected']],
        'permanent-error' => [[[403, 'forbidden']], 1, [403, 'forbidden']],
        'exhausted-retries' => [[[503, 'unavailable']], 3, [503, 'unavailable']],
    ] as $name => [$responses, $attempts, $expected]) {
        $cache->setValue(null, []);
        $url = 'https://fixture.invalid/' . $name;
        file_put_contents($retryState, json_encode(['attempts' => 0, 'urls' => [], 'responses' => $responses], JSON_THROW_ON_ERROR));
        if ($http->invoke(null, $url) !== $expected) {
            throw new RuntimeException('Incorrect revision HTTP result after ' . $name);
        }
        $state = json_decode(file_get_contents($retryState), true, 512, JSON_THROW_ON_ERROR);
        if ($state['attempts'] !== $attempts || $state['urls'] !== array_fill(0, $attempts, $url)) {
            throw new RuntimeException('Incorrect bounded revision HTTP attempts for ' . $name);
        }
        if ($expected[0] === 503) {
            file_put_contents($retryState, json_encode(['attempts' => 0, 'urls' => [], 'responses' => [[200, 'recovered']]], JSON_THROW_ON_ERROR));
            if ($http->invoke(null, $url) !== [200, 'recovered']) {
                throw new RuntimeException('Transient revision HTTP failure was cached');
            }
        } else {
            $http->invoke(null, $url);
            if (json_decode(file_get_contents($retryState), true, 512, JSON_THROW_ON_ERROR)['attempts'] !== $attempts) {
                throw new RuntimeException('Stable revision HTTP result was not reused');
            }
        }
    }
} finally {
    putenv('PATH=' . $originalPath);
    [$_ENV, $_SERVER] = $originalProcessEnvironment;
    unlink($retryDirectory . '/curl');
    unlink($retryState);
    rmdir($retryDirectory);
    $cache->setValue(null, []);
}
echo "Passed bounded revision lookup retries, successful body isolation and transient-error cache handling.\n";
