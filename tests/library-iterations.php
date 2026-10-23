<?php

use staticphp\step\CreatePackages;

require dirname(__DIR__) . '/vendor/autoload.php';

putenv('SPP_FORGEJO_HOSTS=https://first.invalid,https://second.invalid');
putenv('SPP_FORGEJO_LIBS_OWNER=common');
putenv('SPP_FORGEJO_OWNER=86');
$cache = new ReflectionProperty(CreatePackages::class, 'httpCache');
foreach (['deb' => 'debian', 'apk' => 'alpine'] as $type => $registryType) {
    $pages = [];
    foreach (['first' => 2, 'second' => 7] as $host => $revision) {
        $url = "https://{$host}.invalid/api/v1/packages/common?type={$registryType}&limit=1000&page=";
        $suffix = $type === 'deb' ? '-' . $revision : '-r' . $revision;
        $pages[$url . '1'] = [200, json_encode([['name' => 'unrelated', 'version' => '1.0-999']])];
        $pages[$url . '2'] = [200, json_encode([['name' => 'php-zts-lib-openssl', 'version' => '3.6.0' . $suffix]])];
        $pages[$url . '3'] = [200, '[]'];
    }
    $cache->setValue(null, $pages);
    if (CreatePackages::getRemoteNextIteration('php-zts-lib-openssl', '3.6.0', 'x86_64', $type) !== 8) {
        throw new RuntimeException("Missed {$type} library revisions across hosts or used a PHP owner");
    }
}
echo "Passed shared library revision lookup across both registries without PHP.\n";
