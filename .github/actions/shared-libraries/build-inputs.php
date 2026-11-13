#!/usr/bin/env php
<?php

declare(strict_types=1);

use staticphp\util\SpcAutoload;
use staticphp\util\TwigRenderer;
use Symfony\Component\Yaml\Yaml;

$root = $argv[1] ?? dirname(__DIR__, 3);
$type = $argv[2] ?? '';
$target = $argv[3] ?? '';
if (!in_array($type, ['rpm', 'deb', 'apk'], true)
    || !preg_match('/^(?:alma(?:8|9|10)-)?(?:amd64|x86_64|arm64)$/D', $target)) {
    throw new RuntimeException('Library build inputs require a package type and target');
}
$loader = require $root . '/vendor/autoload.php';
require_once $root . '/src/util/SpcAutoload.php';
SpcAutoload::configure($loader);
define('BASE_PATH', $root);
$arch = str_ends_with($target, 'arm64') ? 'aarch64' : 'x86_64';
$os = $type === 'apk' ? '99' : '8';
$libdir = '/usr/lib';
$config = Yaml::parse(ltrim(TwigRenderer::renderFile($root . '/config/templates/craft.yml.twig', [
    'php_version' => '8.4', 'php_source_version' => '8.4', 'target' => null,
    'arch' => $arch, 'os' => $os, 'type' => $type, 'prefix' => 'php-zts',
    'release_suffix' => 'zts-84', 'confdir' => '/etc/php-zts',
    'moduledir' => $libdir . '/php-zts/modules', 'shared_libdir' => $libdir . '/php-zts/lib',
    'filter_libraries' => ['zlib'], 'filter_packages' => null, 'ci' => true,
    'allow_shared_ext_failure' => false,
])));
$environment = [];
foreach ($config['extra-env'] as $name => $value) {
    if (str_starts_with($name, 'SPC_CMD_VAR_PHP_')
        || str_starts_with($name, 'SPC_CMD_PREFIX_PHP_')
        || str_starts_with($name, 'SPC_CMD_VAR_FRANKENPHP_')
        || str_starts_with($name, 'PHP_BUILD_')
        || in_array($name, ['EXTENSION_DIR', 'SPC_MICRO_PATCHES'], true)) {
        continue;
    }
    $environment[$name] = $value;
}
$options = array_intersect_key($config['build-options'], array_flip([
    'with-shared-libs', 'with-shared-libdir', 'with-suggests',
]));
ksort($environment);
ksort($options);
echo json_encode([
    'schema' => 1, 'type' => $type, 'target' => $target, 'architecture' => $arch,
    'environment' => $environment, 'options' => $options,
], JSON_THROW_ON_ERROR | JSON_UNESCAPED_SLASHES), "\n";
