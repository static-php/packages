<?php

declare(strict_types=1);

use staticphp\extension;
use staticphp\package\cli;
use staticphp\package\meta;
use staticphp\step\CreatePackages;
use staticphp\util\SharedLibraries;
use staticphp\util\TwigRenderer;
use Symfony\Component\Process\Process;
use Symfony\Component\Yaml\Yaml;

require dirname(__DIR__) . '/vendor/autoload.php';

function command(array $arguments, ?string $input = null): string
{
    $process = new Process($arguments);
    $process->setInput($input);
    $process->setTimeout(null);
    $process->mustRun();
    return $process->getOutput();
}

function check(bool $condition, string $message): void
{
    if (!$condition) {
        throw new RuntimeException($message);
    }
}

function removeFixture(string $path): void
{
    if (!is_dir($path) || is_link($path)) {
        unlink($path);
        return;
    }
    foreach (new DirectoryIterator($path) as $entry) {
        if (!$entry->isDot()) {
            removeFixture($entry->getPathname());
        }
    }
    rmdir($path);
}

$options = getopt('', ['type:', 'native']);
if (!isset($options['type'])) {
    foreach (['rpm', 'deb', 'apk'] as $type) {
        echo command([PHP_BINARY, __FILE__, '--type=' . $type, ...(isset($options['native']) ? ['--native'] : [])]);
    }
    exit;
}
$type = $options['type'];
check(in_array($type, ['rpm', 'deb', 'apk'], true), 'Unknown package type');
$root = trim(command(['mktemp', '-d', '/tmp/spp-curl-packages-XXXXXXXX']));
$oldTmp = getenv('TMPDIR');
try {
    putenv('TMPDIR=' . $root);
    define('BASE_PATH', dirname(__DIR__));
    define('INI_PATH', BASE_PATH . '/src/ini');
    define('SPP_TYPE', $type);
    define('SPP_PHP_VERSION', '8.6');
    define('SPP_TARGET', null);
    define('SPP_PREFIX', $type === 'rpm' ? '-zts' : '-zts86');
    define('BUILD_ROOT_PATH', $root . '/buildroot');
    define('BUILD_BIN_PATH', BUILD_ROOT_PATH . '/bin');
    define('BUILD_LIB_PATH', BUILD_ROOT_PATH . '/lib');
    define('BUILD_MODULES_PATH', BUILD_ROOT_PATH . '/modules');
    define('DIST_PATH', $root . '/dist');
    define('DIST_RPM_PATH', DIST_PATH . '/rpm');
    define('DIST_DEB_PATH', DIST_PATH . '/deb');
    define('DIST_APK_PATH', DIST_PATH . '/apk');
    define('TEMP_DIR', $root . '/tmp');
    foreach ([BUILD_ROOT_PATH . '/license', BUILD_BIN_PATH, BUILD_LIB_PATH, BUILD_MODULES_PATH, TEMP_DIR, DIST_RPM_PATH, DIST_DEB_PATH, DIST_APK_PATH] as $directory) {
        mkdir($directory, 0755, true);
    }
    file_put_contents(BUILD_BIN_PATH . '/php', 'CLI payload without private-library dependencies');
    $helpers = strstr(file_get_contents(BASE_PATH . '/bin/spp'), 'function setupGithubToken(');
    check(is_string($helpers), 'Cannot load package path helpers');
    eval('use staticphp\\step\\CreatePackages;' . $helpers);
    (new ReflectionProperty(CreatePackages::class, 'prefix'))->setValue(null, SPP_PREFIX);
    (new ReflectionProperty(CreatePackages::class, 'versionArch'))->setValue(null, ['8.6.0', 'x86_64']);

    foreach (['8.2', '8.3', '8.4', '8.5', '8.6'] as $version) {
        foreach ([null, ['cli'], ['curl'], ['fileinfo'], ['devel']] as $packages) {
            $config = Yaml::parse(TwigRenderer::renderCraftTemplate($version, 'x86_64', $packages));
            $static = explode(',', (string) ($config['extensions'] ?? ''));
            $shared = explode(',', (string) ($config['shared-extensions'] ?? ''));
            check(!in_array('curl', $static, true), "{$version} still builds cURL into PHP");
            $expected = $packages === null || $packages === ['curl'] || $packages === ['devel'];
            check(in_array('curl', $shared, true) === $expected, "{$version} shared cURL selection is wrong");
        }
        $libraries = Yaml::parse(TwigRenderer::renderCraftTemplate($version, 'x86_64', null, null, ['curl']));
        check(empty($libraries['extensions']) && empty($libraries['shared-extensions']), "{$version} library-only build selects extensions");
    }

    $prefix = CreatePackages::getPrefix();
    $cliConfig = (new cli())->getFpmConfig();
    foreach (['depends', 'provides', 'replaces', 'conflicts'] as $field) {
        check(!in_array($prefix . '-curl', $cliConfig[$field] ?? [], true), "CLI retains a cURL relationship in {$field}");
    }
    check($cliConfig['recommends'] === [$prefix . '-fileinfo', $prefix . '-curl'], 'CLI shared-extension recommendations are wrong');
    foreach (['cli', 'cgi', 'fpm', 'embed', 'frankenphp'] as $sapi) {
        $class = 'staticphp\\package\\' . $sapi;
        $sapiConfig = (new $class())->getFpmConfig();
        check(($sapiConfig['recommends'] ?? []) === [$prefix . '-fileinfo', $prefix . '-curl'], $sapi . ' does not recommend the shared extensions');
        check(!in_array($prefix . '-curl', $sapiConfig['depends'] ?? [], true), $sapi . ' requires cURL instead of recommending it');
    }
    check(!in_array(getConfdir() . '/conf.d/curl.ini', $cliConfig['config-files'], true), 'CLI still owns cURL configuration');
    check((new meta())->getFpmConfig()['depends'] === [$prefix . '-cli'], 'Base metapackage forces cURL installation');

    CreatePackages::bootstrapSpcGlobals();
    $curl = new extension('curl');
    check($curl->isSharedExtension(), 'cURL is not a shared extension');
    $config = $curl->getFpmConfig();
    $module = BUILD_MODULES_PATH . '/curl' . getSharedLibrarySuffix() . '.so';
    check(isset($config['files'][$module]), 'cURL package does not own its module');
    $ini = current(array_filter(array_keys($config['files']), static fn (string $path): bool => str_ends_with($path, '-curl.ini')));
    check(is_string($ini) && preg_match('/^extension=curl' . preg_quote(getSharedLibrarySuffix(), '/') . '$/m', file_get_contents($ini)) === 1, 'cURL drop-in does not load its module');
    $swooleConfig = (new extension('swoole'))->getFpmConfig();
    check(in_array($prefix . '-curl', $swooleConfig['depends'], true), 'Swoole does not require the separate cURL extension package');
    check(in_array(getConfdir() . '/conf.d/20-curl.ini', $config['config-files'], true)
        && in_array(getConfdir() . '/conf.d/30-swoole.ini', $swooleConfig['config-files'], true), 'Swoole is not configured to load after shared cURL');

    file_put_contents($root . '/curl.c', "int fixture_curl(void) { return 42; }\n");
    file_put_contents($root . '/module.c', "extern int fixture_curl(void);\nint fixture_module(void) { return fixture_curl(); }\n");
    file_put_contents(BUILD_ROOT_PATH . '/license/curl', 'Fixture curl license');
    $libdir = getLibdir() . '/php-zts/lib';
    command(['gcc', '-shared', '-nostdlib', '-fPIC', '-Wl,-soname,libcurl.so.4', '-Wl,--enable-new-dtags,-rpath,' . $libdir, '-o', BUILD_LIB_PATH . '/libcurl.so.4', $root . '/curl.c']);
    command(['gcc', '-shared', '-nostdlib', '-fPIC', '-Wl,--enable-new-dtags,-rpath,' . $libdir, '-o', $module, $root . '/module.c', '-L' . BUILD_LIB_PATH, '-l:libcurl.so.4']);
    $manifest = ['schema' => 1, 'linkage' => 'shared', 'architecture' => 'x86_64', 'libc' => 'glibc', 'runtime_libdir' => $libdir, 'libraries' => ['curl' => [
        'version' => '8.21.0', 'source_hash' => str_repeat('c', 64), 'license' => 'curl',
        'license_files' => ['license/curl'], 'files' => ['lib/libcurl.so.4'],
        'runtime_assets' => (object) [], 'dlopen_dependencies' => [], 'depends' => [], 'abi_epoch' => 1,
        'elf' => ['lib/libcurl.so.4' => ['soname' => 'libcurl.so.4', 'needed' => [], 'runpath' => [$libdir]]],
    ]]];
    file_put_contents(BUILD_ROOT_PATH . '/shared-libraries.json', json_encode($manifest, JSON_THROW_ON_ERROR));
    $libraries = new SharedLibraries(BUILD_ROOT_PATH, $libdir);
    $floor = $type === 'apk' ? '8.21.0-r1' : '8.21.0-1';
    $libraries->setPackageVersion('curl', $type, $floor);
    (new ReflectionProperty(CreatePackages::class, 'sharedLibraries'))->setValue(null, $libraries);
    (new ReflectionProperty(CreatePackages::class, 'packageType'))->setValue(null, $type);
    $packageConfig = new ReflectionMethod(CreatePackages::class, 'packageConfig');
    $curlConfig = $packageConfig->invoke(null, $curl, false);
    check(in_array('php-zts-lib-curl-abi-1', $curlConfig['depends'], true), 'cURL module lacks its private-library ABI requirement');
    check(count(array_filter($curlConfig['depends'], static fn (string $dependency): bool => str_starts_with($dependency, 'php-zts-lib-curl ') || str_starts_with($dependency, 'php-zts-lib-curl>='))) === 1, 'cURL module lacks its private-library version floor');
    check(!str_contains(json_encode($packageConfig->invoke(null, new cli(), false)), 'php-zts-lib-curl'), 'CLI acquires cURL library dependencies');
    echo "Passed {$type} cURL shared selection for PHP 8.2–8.6, weak SAPI recommendations, Swoole dependency/loading order, INI ownership and ELF library dependencies.\n";

    if (isset($options['native'])) {
        (new ReflectionProperty(CreatePackages::class, 'versionArch'))->setValue(null, ['8.6.0', 'x86_64']);
        (new ReflectionProperty(CreatePackages::class, 'iterationOverride'))->setValue(null, '1');
        $method = new ReflectionMethod(CreatePackages::class, match ($type) {
            'rpm' => 'createRpmPackage', 'deb' => 'createDebPackage', 'apk' => 'createApkPackage',
        });
        ob_start();
        try {
            $method->invoke(null, $curl, '8.6.0', 'x86_64');
        } finally {
            file_put_contents($root . '/package.log', ob_get_clean());
        }
        $artifacts = glob(DIST_PATH . '/' . $type . '/*.' . $type);
        check(count($artifacts) === 1, 'Native cURL package was not created');
        if ($type === 'rpm') {
            $header = command(['rpm', '-qp', '--requires', $artifacts[0]]);
        } elseif ($type === 'deb') {
            $member = current(array_filter(explode("\n", command(['ar', 't', $artifacts[0]])), static fn (string $name): bool => str_starts_with($name, 'control.tar')));
            $header = command(['tar', '-xOJf', '-', './control'], command(['ar', 'p', $artifacts[0], $member]));
        } else {
            $header = command(['tar', '-xOf', '-', '.PKGINFO'], command(['gzip', '-dc', $artifacts[0]]));
        }
        check(str_contains($header, 'php-zts-lib-curl-abi-1') && str_contains($header, 'php-zts-lib-curl') && str_contains($header, $floor), 'Native cURL package lacks private library dependencies');
        check(!str_contains($header, 'libcurl.so.4'), 'Native cURL package requires a system libcurl SONAME');
        check(str_contains($header, $prefix . '-cli'), 'Native cURL package lacks the PHP CLI dependency');
        echo "Passed {$type} native cURL package dependency metadata.\n";
    }
} finally {
    $oldTmp === false ? putenv('TMPDIR') : putenv('TMPDIR=' . $oldTmp);
    removeFixture($root);
}
