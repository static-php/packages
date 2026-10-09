<?php

use staticphp\step\CreatePackages;

require dirname(__DIR__) . '/vendor/autoload.php';

putenv('SPP_FORGEJO_HOST=https://example.com');
putenv('SPP_FORGEJO_OWNER=86');
$cache = new ReflectionProperty(CreatePackages::class, 'httpCache');
$version = '3.6.0+php86.0~rc3+ext~alpha1';

foreach (['deb' => 'debian', 'apk' => 'alpine'] as $type => $registryType) {
    $suffix = $type === 'deb' ? '-9' : '-r9';
    $url = "https://example.com/api/v1/packages/86?type={$registryType}&limit=1000&page=";
    $cache->setValue(null, [
        $url . '1' => [200, json_encode([['name' => 'unrelated', 'version' => '1.0-99']])],
        $url . '2' => [200, json_encode([['name' => 'php-zts-xdebug', 'version' => $version . $suffix]])],
        $url . '3' => [200, '[]'],
    ]);
    if (CreatePackages::getRemoteNextIteration('php-zts-xdebug', $version, 'amd64', $type) !== 10) {
        throw new RuntimeException("Missed {$type} revision on a later registry page");
    }
}

echo "Passed remote revision pagination checks.\n";
