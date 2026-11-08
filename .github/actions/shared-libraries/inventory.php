#!/usr/bin/env php
<?php

declare(strict_types=1);

use StaticPHP\Config\ArtifactConfig;
use StaticPHP\Config\PackageConfig;
use StaticPHP\Package\LibraryPackage;
use StaticPHP\Package\PhpExtensionPackage;
use StaticPHP\Registry\PackageLoader;
use StaticPHP\Registry\Registry;
use StaticPHP\Util\DependencyResolver;
use StaticPHP\Util\SharedLibraryUtil;
use Symfony\Component\Yaml\Yaml;
use staticphp\util\SpcAutoload;
use staticphp\util\TwigRenderer;

$root = $argv[1] ?? dirname(__DIR__, 3);
$loader = require $root . '/vendor/autoload.php';
require_once $root . '/src/util/SpcAutoload.php';
SpcAutoload::configure($loader);
putenv('SPC_ENABLE_LOG_FILE=0');
require_once ROOT_DIR . '/src/bootstrap.php';
Registry::resolve();
require_once __DIR__ . '/recipe-inputs.php';
$patchRoots = [ROOT_DIR . '/src/globals/patch', $root . '/config/patches'];

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
$libraryPackages = [];
foreach ($packages as $name => $package) {
    if (!$managed($package)) {
        continue;
    }
    $dependencies = [];
    $inputs = [];
    $seen = [];
    $collect = function (string $dependency) use (&$collect, &$seen, &$dependencies, &$inputs, $packages, $managed, $name): void {
        if (isset($seen[$dependency]) || !isset($packages[$dependency])) {
            return;
        }
        $seen[$dependency] = true;
        $related = $packages[$dependency];
        if ($dependency !== $name && $managed($related)) {
            $dependencies[$dependency] = true;
        }
        if (($artifact = $related->getArtifact()) !== null) {
            $inputs[$artifact->getName()] = true;
        }
        foreach (['depends', 'suggests'] as $field) {
            foreach (PackageConfig::get($dependency, $field, []) as $child) {
                $collect($child);
            }
        }
    };
    $collect($name);
    $recipe = ['config' => PackageConfig::get($name), 'code' => [], 'patches' => []];
    foreach ($package->getStages() as $callback) {
        if (is_array($callback)) {
            $method = new ReflectionMethod($callback[0], $callback[1]);
            if (($file = $method->getFileName()) !== false) {
                $recipe['code'][$method->getDeclaringClass()->getName()] = hash_file('sha256', $file);
                libraryRecipePatches($recipe, file_get_contents($file), $file, $patchRoots);
            }
        }
    }
    $metadata = PackageLoader::getPackageAnnotationInfo($name);
    foreach (['before_stages', 'after_stages'] as $field) {
        foreach ($metadata[$field] ?? [] as $callbacks) {
            foreach ($callbacks as $callback) {
                libraryRecipeCallback($recipe, [$callback['class'], $callback['method']], $patchRoots);
            }
        }
    }
    foreach (array_keys($seen) as $dependency) {
        $related = $packages[$dependency];
        if ($dependency !== $name && $managed($related)) {
            continue;
        }
        if ($dependency !== $name) {
            $recipe['tools'][$dependency] = PackageConfig::get($dependency);
            foreach ($related->getStages() as $callback) {
                libraryRecipeCallback($recipe, $callback, $patchRoots);
            }
        }
        if (($artifact = $related->getArtifact()) !== null) {
            libraryRecipeArtifact($recipe, $artifact, $patchRoots);
        }
    }
    ksort($recipe['code']);
    ksort($recipe['patches']);
    if (isset($recipe['tools'])) {
        ksort($recipe['tools']);
    }
    $dependencyNames = array_keys($dependencies);
    $inputNames = array_keys($inputs);
    sort($dependencyNames);
    sort($inputNames);
    $libraryPackages[$name] = [
        'artifact' => $package->getArtifact()?->getName(),
        'depends' => $dependencyNames,
        'inputs' => $inputNames,
        'recipe_hash' => hash('sha256', json_encode($recipe, JSON_THROW_ON_ERROR)),
    ];
}
ksort($libraryPackages);
$inventory['libraries'] = $libraryPackages;
$craft = Yaml::parse(ltrim(TwigRenderer::renderFile($root . '/config/templates/craft.yml.twig', [
    'php_version' => '8.4', 'php_source_version' => '8.4', 'target' => null,
    'arch' => 'x86_64', 'os' => '8', 'type' => 'rpm', 'prefix' => 'php-zts',
    'release_suffix' => 'zts-84', 'confdir' => '/etc/php-zts',
    'moduledir' => '/usr/lib/php-zts/modules', 'shared_libdir' => '/usr/lib/php-zts/lib',
    'filter_libraries' => null, 'filter_packages' => null, 'ci' => true,
    'allow_shared_ext_failure' => false,
])));
$list = static fn ($value): array => is_array($value) ? $value : array_values(array_filter(array_map('trim', explode(',', (string) $value))));
$roots = array_map(static fn ($extension): string => 'ext-' . $extension,
    array_merge($list($craft['extensions'] ?? []), $list($craft['shared-extensions'] ?? [])));
$roots = array_merge($roots, $list($craft['libs'] ?? []));
foreach ($list($craft['sapi'] ?? []) as $sapi) {
    foreach ([$sapi, 'php-' . $sapi] as $candidate) {
        if (PackageLoader::hasPackage($candidate)) {
            $roots[] = $candidate;
            break;
        }
    }
}
$inventory['required_libraries'] = array_values(array_intersect(
    DependencyResolver::resolve($roots, include_suggests: (bool) ($craft['build-options']['with-suggests'] ?? false)),
    array_keys($libraryPackages),
));
sort($inventory['required_libraries']);
echo json_encode($inventory, JSON_THROW_ON_ERROR | JSON_UNESCAPED_SLASHES), "\n";
