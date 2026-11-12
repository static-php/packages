<?php

use staticphp\step\CreatePackages;

require dirname(__DIR__) . '/vendor/autoload.php';

$directory = sys_get_temp_dir() . '/spp-package-revisions-' . bin2hex(random_bytes(6));
mkdir($directory);
foreach (['DIST_RPM_PATH', 'DIST_DEB_PATH', 'DIST_APK_PATH'] as $constant) {
    define($constant, $directory);
}
putenv('SPP_FORGEJO_OWNER=83');
(new ReflectionProperty(CreatePackages::class, 'versionArch'))->setValue(null, ['8.3.36', 'x86_64']);
$cache = new ReflectionProperty(CreatePackages::class, 'httpCache');
$memo = new ReflectionProperty(CreatePackages::class, 'resolvedIterations');
$snapshot = static function (string $type, string $arch, array $packages = []) use ($directory): void {
    $path = $directory . '/inventory.json';
    file_put_contents($path, json_encode(['schema' => 1, 'type' => $type, 'php_stream' => '8.3',
        'architecture' => $arch, 'inventory' => $packages], JSON_THROW_ON_ERROR));
    putenv('SPP_NATIVE_INVENTORY=' . $path);
};
try {
    foreach (['deb', 'apk'] as $type) {
        $version = $type === 'apk' ? '3.6.0_alpha1_p83' : '3.6.0~alpha1+php83';
        $suffix = $type === 'apk' ? '-r' : '-';
        $snapshot($type, 'x86_64', [
            ['name' => 'php-zts-pdo-mysql', 'version' => $version . $suffix . '9', 'architecture' => 'amd64'],
            ['name' => 'php-zts-pdo_mysql', 'version' => $version . $suffix . '9', 'architecture' => 'x86_64'],
            ['name' => 'unrelated', 'version' => $version . $suffix . '99', 'architecture' => 'x86_64'],
        ]);
        if (CreatePackages::getRemoteNextIteration('php-zts-pdo_mysql', $version, 'x86_64', $type) !== 10) {
            throw new RuntimeException('Native package name/version matching lost a revision');
        }
    }
    putenv('SPP_NATIVE_INVENTORY');
    putenv('SPP_RPM_REPO_URL=https://example.com');
    foreach (['php-zts-lib-fixture', 'php-zts-xdebug', 'frankenphp'] as $name) {
        $cache->setValue(null, [
            'https://example.com/x86_64/el8/' => [200, "{$name}-1.2.3-4.x86_64.rpm"],
            'https://example.com/x86_64/el9/' => [500, null],
            'https://example.com/x86_64/el10/' => [500, null],
        ]);
        if (CreatePackages::getRemoteNextIteration($name, '1.2.3', 'x86_64', 'rpm') !== 5) {
            throw new RuntimeException('RPM revision selection must use the shared EL8 architecture repository');
        }
    }
    echo "Passed native package naming and shared EL8 RPM revision lookup.\n";

    $baseline = $directory . '/baseline.json';
    putenv('SPP_BASELINE_MANIFESTS=' . json_encode([$baseline]));
    foreach (['rpm', 'deb', 'apk'] as $type) {
        $version = $type === 'apk' ? '2.2.0.0_p83_p36_p0' : '2.2.0+php83.36';
        $cutoff = $type === 'apk' ? '2.2.0p83.36_p0-r1' : $version . '-2' . ($type === 'rpm' ? '.el8' : '');
        file_put_contents($baseline, json_encode(['schema' => 1, 'type' => $type, 'php_stream' => '8.3', 'architecture' => 'x86_64',
            'migration_cutoffs' => ['8.3' => ['php-zts-amqp' => $cutoff]]], JSON_THROW_ON_ERROR));
        $cache->setValue(null, [
            'https://example.com/x86_64/el8/' => [200, '[]'],
            'https://example.com/aarch64/el8/' => [200, '[]'],
        ]);
        $snapshot($type, 'x86_64');
        foreach (['php-zts-amqp', 'php-zts-amqp-debuginfo'] as $name) {
            if (CreatePackages::getRemoteNextIteration($name, $version, 'x86_64', $type) !== ($type === 'apk' ? 1 : 2)) {
                throw new RuntimeException('Automatic bump violates the CLI migration minimum');
            }
        }
        $snapshot($type, 'arm64');
        if (CreatePackages::getRemoteNextIteration('php-zts-amqp', $version, 'aarch64', $type) !== ($type === 'apk' ? 0 : 1)) {
            throw new RuntimeException('Migration minimum leaked between architectures');
        }
        $snapshot($type, 'x86_64');
        if (CreatePackages::getRemoteNextIteration('php-zts-amqp', '9.0.0', 'x86_64', $type) !== ($type === 'apk' ? 0 : 1)) {
            throw new RuntimeException('Migration minimum leaked into a different upstream version');
        }
    }
    file_put_contents($baseline, json_encode(['schema' => 1, 'type' => 'apk', 'migration_cutoffs' => ['8.3' => [
        'php-zts-amqp' => '2.2.0p83.36_p0-r1', 'frankenphp' => '1.13.5p83.36-r2',
    ]]], JSON_THROW_ON_ERROR));
    $snapshot('apk', 'x86_64');
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
    $snapshot('apk', 'x86_64', [['name' => 'php-zts-amqp', 'version' => '2.2.0.0_p83_p36_p0-r5', 'architecture' => 'x86_64']]);
    if (CreatePackages::resolveIteration('php-zts-amqp', '2.2.0.0_p83_p36_p0', 'x86_64', 'apk') !== '6') {
        throw new RuntimeException('Migration cutoff overrode a higher published revision');
    }
    echo "Passed migration cutoff floors and explicit package bumps.\n";
} finally {
    putenv('SPP_NATIVE_INVENTORY');
    putenv('SPP_BASELINE_MANIFESTS');
    putenv('SPP_RPM_REPO_URL');
    foreach (glob($directory . '/*') as $path) {
        unlink($path);
    }
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
