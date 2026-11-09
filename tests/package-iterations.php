<?php

use staticphp\step\CreatePackages;

require dirname(__DIR__) . '/vendor/autoload.php';

putenv('SPP_FORGEJO_HOST=https://example.com');
putenv('SPP_FORGEJO_OWNER=86');
$cache = new ReflectionProperty(CreatePackages::class, 'httpCache');
$version = '3.6.0~alpha1+php86.0~rc3';

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

$indexes = [];
foreach (['82', '83', '84', '85', '86'] as $owner) {
    $url = "https://example.com/api/v1/packages/{$owner}?type=alpine&q=php-zts-lib-&limit=1000&page=";
    $indexes[$url . '1'] = [200, json_encode([['name' => 'php-zts-lib-zlib', 'version' => '1.3.1-r2']])];
    $indexes[$url . '2'] = [200, $owner === '83' ? json_encode([['name' => 'php-zts-lib-zlib', 'version' => '1.3.1-r11']]) : '[]'];
    $indexes[$url . '3'] = [200, '[]'];
}
$cache->setValue(null, $indexes);
if (CreatePackages::getRemoteNextIteration('php-zts-lib-zlib', '1.3.1', 'aarch64', 'apk') !== 12) {
    throw new RuntimeException('Missed a library revision on another stream owner or later filtered page');
}

echo "Passed remote revision pagination checks.\n";
