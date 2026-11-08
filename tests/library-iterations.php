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
        foreach (['common', '82', '83', '84', '85', '86'] as $owner) {
            $url = "https://{$host}.invalid/api/v1/packages/{$owner}?type={$registryType}&limit=1000&page=";
            $ownerRevision = $host === 'second' && $owner === '85' ? 11 : $revision;
            $suffix = $type === 'deb' ? '-' . $ownerRevision : '-r' . $ownerRevision;
            $pages[$url . '1'] = [200, json_encode([['name' => 'unrelated', 'version' => '1.0-999']])];
            $pages[$url . '2'] = [200, json_encode([['name' => 'php-zts-lib-openssl', 'version' => '3.6.0' . $suffix]])];
            $pages[$url . '3'] = [200, '[]'];
        }
    }
    $cache->setValue(null, $pages);
    if (CreatePackages::getRemoteNextIteration('php-zts-lib-openssl', '3.6.0', 'x86_64', $type) !== 12) {
        throw new RuntimeException("Missed {$type} library revisions across hosts or stream owners");
    }
}
echo "Passed shared library revision lookup across both registries without PHP.\n";
