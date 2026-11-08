<?php

declare(strict_types=1);

use Symfony\Component\Process\Process;

require dirname(__DIR__) . '/vendor/autoload.php';

$repository = dirname(__DIR__);
$temporary = sys_get_temp_dir() . '/spp-builder-tools-' . bin2hex(random_bytes(8));
mkdir($temporary . '/bin', 0755, true);
mkdir($temporary . '/downloads');
copy($repository . '/bin/spp', $temporary . '/bin/spp');
foreach (['vendor', 'src', 'config', 'spc.registry.yml'] as $entry) {
    symlink($repository . '/' . $entry, $temporary . '/' . $entry);
}
$pkgroot = getenv('PKG_ROOT_PATH');
if (!$pkgroot || !is_file($pkgroot . '/.cache.json')) {
    throw new RuntimeException('Run this regression inside a builder image with preinstalled go-xcaddy');
}
$seed = json_decode(file_get_contents($pkgroot . '/.cache.json'), true, flags: JSON_THROW_ON_ERROR);
try {
    foreach (['foreign-platform', 'stale-native'] as $scenario) {
        $snapshot = ['php-src' => ['source' => ['version' => 'pinned-test-release']],
            'go-xcaddy' => ['source' => null, 'binary' => ['foreign-platform' => ['cache_type' => 'archive']]]];
        if ($scenario === 'stale-native') {
            foreach ($seed['go-xcaddy']['binary'] as $platform => $entry) {
                $snapshot['go-xcaddy']['binary'][$platform] = array_replace($entry, ['filename' => 'absent-snapshot-tool.tar.gz', 'hash' => 'stale']);
            }
        }
        file_put_contents($temporary . '/downloads/.cache.json', json_encode($snapshot));
        (new Process([PHP_BINARY, 'bin/spp', '--version'], $temporary))->mustRun();
        $merged = json_decode(file_get_contents($temporary . '/downloads/.cache.json'), true, flags: JSON_THROW_ON_ERROR);
        if ($merged['php-src'] !== $snapshot['php-src']) {
            throw new RuntimeException('Builder metadata changed the pinned source');
        }
        foreach ($seed['go-xcaddy']['binary'] as $platform => $entry) {
            if (($merged['go-xcaddy']['binary'][$platform]['cache_type'] ?? null) !== 'local') {
                throw new RuntimeException('Installed image tool still depends on its download archive');
            }
        }
        $check = new Process([PHP_BINARY, 'vendor/bin/spc', 'dev:is-installed', 'go-xcaddy'], $temporary);
        $check->mustRun();
        echo $scenario . ': ' . $check->getOutput();
    }
    (new Process([$pkgroot . '/go-xcaddy/bin/xcaddy', 'version'], $temporary))->mustRun();
    echo "Passed offline installed-tool detection without changing pinned sources.\n";
} finally {
    (new Process(['rm', '-rf', $temporary]))->mustRun();
}
