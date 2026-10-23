#!/usr/bin/env php
<?php

declare(strict_types=1);

use StaticPHP\Config\ArtifactConfig;
use StaticPHP\Config\PackageConfig;
use StaticPHP\Package\LibraryPackage;
use StaticPHP\Package\PhpExtensionPackage;
use StaticPHP\Registry\PackageLoader;
use StaticPHP\Registry\Registry;
use StaticPHP\Util\SharedLibraryUtil;

$root = $argv[1] ?? dirname(__DIR__, 3);
require $root . '/vendor/autoload.php';
putenv('SPC_ENABLE_LOG_FILE=0');
require_once ROOT_DIR . '/src/bootstrap.php';
Registry::resolve();

$packages = iterator_to_array(PackageLoader::getPackages());
$managed = static fn ($package): bool => class_exists(SharedLibraryUtil::class)
    ? SharedLibraryUtil::isManaged($package)
    : $package instanceof LibraryPackage && ($package->getType() === 'library'
        || PackageConfig::get($package->getName(), 'static-libs', []) !== []
        || PackageConfig::get($package->getName(), 'shared-libs', []) !== []);
$libraries = [];
$tools = [];
$irrelevant = [];
$visited = [];
$visit = function (string $name) use (&$visit, &$visited, &$libraries, &$tools, $packages, $managed): void {
    if (isset($visited[$name]) || !isset($packages[$name])) {
        return;
    }
    $visited[$name] = true;
    $package = $packages[$name];
    if (($artifact = $package->getArtifact()) !== null) {
        if ($managed($package)) {
            $libraries[$artifact->getName()] = true;
        } else {
            $tools[$artifact->getName()] = true;
        }
    }
    foreach (['depends', 'suggests'] as $field) {
        foreach (PackageConfig::get($name, $field, []) as $dependency) {
            $visit($dependency);
        }
    }
};
foreach ($packages as $name => $package) {
    if ($managed($package)) {
        $visit($name);
    } elseif ($package instanceof PhpExtensionPackage || in_array($name, ['php', 'frankenphp', 'go-xcaddy'], true)) {
        if (($artifact = $package->getArtifact()) !== null) {
            $irrelevant[$artifact->getName()] = true;
        }
    }
}
$visit('pkg-config');
if (ArtifactConfig::get('php-micro') !== null) {
    $irrelevant['php-micro'] = true;
}
$irrelevant = array_diff_key($irrelevant, $libraries, $tools);
$inventory = ['schema' => 1, 'managed_library_artifacts' => array_keys($libraries), 'library_tool_artifacts' => array_keys($tools), 'irrelevant_artifacts' => array_keys($irrelevant)];
foreach (['managed_library_artifacts', 'library_tool_artifacts', 'irrelevant_artifacts'] as $field) {
    sort($inventory[$field]);
}
echo json_encode($inventory, JSON_THROW_ON_ERROR | JSON_UNESCAPED_SLASHES), "\n";
