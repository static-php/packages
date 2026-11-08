<?php

declare(strict_types=1);

use staticphp\CraftConfig;
use staticphp\step\CreatePackages;
use staticphp\util\ExtMeta;

require dirname(__DIR__) . '/vendor/autoload.php';

$options = getopt('', ['payload:', 'stream:', 'type:']);
$root = realpath($options['payload'] ?? '');
if ($root === false || !is_file($root . '/bin/php') || !preg_match('/^8\.\d+$/D', $options['stream'] ?? '')) {
    throw new RuntimeException('Pass --payload=PATH --stream=8.x --type=rpm|deb|apk to restored PHP files without their source tree.');
}
$stream = $options['stream'];
define('BASE_PATH', dirname(__DIR__));
define('INI_PATH', BASE_PATH . '/src/ini');
define('SPP_PHP_VERSION', $stream);
define('SPP_PREFIX', '-zts');
define('SPP_TYPE', $options['type'] ?? 'rpm');
define('SPP_TARGET', null);
define('BUILD_ROOT_PATH', $root);
define('BUILD_BIN_PATH', $root . '/bin');
define('BUILD_MODULES_PATH', $root . '/modules');
define('BUILD_LIB_PATH', $root . '/lib');
$helpers = strstr(file_get_contents(BASE_PATH . '/bin/spp'), 'function setupGithubToken(');
eval('use staticphp\\step\\CreatePackages;' . $helpers);
CreatePackages::bootstrapSpcGlobals();
$probe = new ReflectionMethod(CreatePackages::class, 'getExtensionVersion');
$checked = [];
foreach (CraftConfig::getInstance()->getSharedExtensions() as $extension) {
    if (ExtMeta::isAddon($extension) || !is_file(BUILD_MODULES_PATH . '/' . $extension . getSharedLibrarySuffix() . '.so')) {
        continue;
    }
    $checked[$extension] = $probe->invoke(null, $extension, $stream);
}
foreach (['amqp', 'mysqli', 'xdebug'] as $extension) {
    if (empty($checked[$extension])) {
        throw new RuntimeException('Required standalone, dependent or Zend extension was not probed: ' . $extension);
    }
}
echo 'Passed native version probes for ' . count($checked) . " restored shared extensions.\n";
