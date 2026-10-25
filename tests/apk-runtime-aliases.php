<?php

declare(strict_types=1);

use staticphp\extension;
use staticphp\package;
use staticphp\package\cli;
use staticphp\step\CreatePackages;
use staticphp\util\ApkRuntimeAliases;
use Symfony\Component\Process\Process;

require dirname(__DIR__) . '/vendor/autoload.php';
require_once dirname(__DIR__) . '/src/util/ApkRuntimeAliases.php';

function aliasCheck(bool $condition, string $message): void
{
    if (!$condition) {
        throw new RuntimeException($message);
    }
}

function aliasCommand(array $arguments, ?string $input = null): string
{
    $process = new Process($arguments);
    $process->setInput($input);
    $process->setTimeout(null);
    $process->mustRun();
    return $process->getOutput();
}

function aliasRemove(string $path): void
{
    if (!is_dir($path) || is_link($path)) {
        unlink($path);
        return;
    }
    foreach (new DirectoryIterator($path) as $entry) {
        if (!$entry->isDot()) {
            aliasRemove($entry->getPathname());
        }
    }
    rmdir($path);
}

final class AliasFixturePackage implements package
{
    public function __construct(private readonly string $name) {}
    public function getName(): string { return $this->name; }
    public function getFpmConfig(): array { return []; }
    public function getDebuginfoFpmConfig(): array { return []; }
    public function getFpmExtraArgs(): array { return []; }
    public function getLicense(): string { return 'MIT'; }
    public function getDescription(): string { return 'APK alias regression fixture'; }
}

$options = getopt('', ['type:', 'native']);
if (!isset($options['type'])) {
    foreach (['rpm', 'deb', 'apk'] as $type) {
        echo aliasCommand([PHP_BINARY, '-d', 'memory_limit=384M', __FILE__, '--type=' . $type,
            ...(isset($options['native']) && $type === 'apk' ? ['--native'] : [])]);
    }
    exit;
}
$type = $options['type'];
aliasCheck(in_array($type, ['rpm', 'deb', 'apk'], true), 'Unknown package type');
$root = trim(aliasCommand(['mktemp', '-d', '/tmp/spp-apk-runtime-aliases-XXXXXXXX']));
$oldTmp = getenv('TMPDIR');
$oldPath = getenv('PATH');
try {
    putenv('TMPDIR=' . $root);
    if (($nfpm = getenv('SPP_NFPM_BINARY')) !== false) {
        putenv('PATH=' . dirname($nfpm) . PATH_SEPARATOR . $oldPath);
    }
    define('BASE_PATH', dirname(__DIR__));
    define('INI_PATH', BASE_PATH . '/src/ini');
    define('SPP_TYPE', $type);
    define('SPP_PHP_VERSION', '8.6');
    define('SPP_PREFIX', '-zts86');
    define('SPP_TARGET', null);
    define('BUILD_ROOT_PATH', $root . '/buildroot');
    define('BUILD_BIN_PATH', BUILD_ROOT_PATH . '/bin');
    define('BUILD_LIB_PATH', BUILD_ROOT_PATH . '/lib');
    define('BUILD_MODULES_PATH', BUILD_ROOT_PATH . '/modules');
    define('DIST_PATH', $root . '/dist');
    define('DIST_APK_PATH', DIST_PATH . '/apk');
    define('TEMP_DIR', $root . '/tmp');
    foreach ([BUILD_BIN_PATH, BUILD_LIB_PATH, BUILD_MODULES_PATH, BUILD_ROOT_PATH . '/debug',
        BUILD_ROOT_PATH . '/license', DIST_APK_PATH, TEMP_DIR, $root . '/repo/x86_64'] as $directory) {
        mkdir($directory, 0755, true);
    }
    file_put_contents(BUILD_BIN_PATH . '/php', 'fixture PHP executable');
    $helpers = strstr(file_get_contents(BASE_PATH . '/bin/spp'), 'function setupGithubToken(');
    aliasCheck(is_string($helpers), 'Cannot load package path helpers');
    eval('use staticphp\\step\\CreatePackages;' . $helpers);
    (new ReflectionProperty(CreatePackages::class, 'prefix'))->setValue(null, SPP_PREFIX);
    (new ReflectionProperty(CreatePackages::class, 'versionArch'))->setValue(null, ['8.6.0', 'x86_64']);
    (new ReflectionProperty(CreatePackages::class, 'packageType'))->setValue(null, $type);
    CreatePackages::bootstrapSpcGlobals();
    $prefix = CreatePackages::getPrefix();
    aliasCheck(array_keys(ApkRuntimeAliases::extensions()) === ['amqp', 'igbinary'], 'Unexpected APK alias scope');
    foreach (['82', '83', '84', '85', '86'] as $stream) {
        foreach (['amqp', 'igbinary'] as $name) {
            foreach (['', '-debuginfo'] as $debug) {
                $logical = 'php-zts' . $stream . '-' . $name . $debug;
                $physical = 'php-zts' . $stream . '-' . $name . '-shared' . $debug;
                aliasCheck(ApkRuntimeAliases::physicalName($logical) === $physical, 'Physical alias lost stream or debug identity');
                aliasCheck(ApkRuntimeAliases::logicalName($physical) === $logical, 'Logical alias does not round-trip');
                aliasCheck(ApkRuntimeAliases::extension($physical) === $name, 'Physical name lost extension identity');
                aliasCheck(ApkRuntimeAliases::metadata($physical) === ['logical_name' => $logical, 'extension' => $name, 'component' => $name], 'Publication identity uses the physical suffix');
            }
        }
    }
    foreach (['php-zts86-curl', 'php-zts86-fileinfo', 'php-zts-lib-amqp', 'example-amqp', 'php-zts-runtime-layout'] as $name) {
        aliasCheck(ApkRuntimeAliases::physicalName($name) === $name, 'An unrelated package was aliased: ' . $name);
    }

    $configs = [];
    $packages = [];
    foreach (['amqp', 'igbinary'] as $name) {
        $logical = $prefix . '-' . $name;
        $physical = $logical . '-shared';
        $package = new extension($name);
        $expected = $type === 'apk' ? $physical : $logical;
        aliasCheck($package->getName() === $expected, $type . ' extension package name is wrong');
        $config = $package->getFpmConfig();
        $module = BUILD_MODULES_PATH . '/' . $name . getSharedLibrarySuffix() . '.so';
        file_put_contents($module, 'fake ' . $name . ' module');
        aliasCheck(($config['files'][$module] ?? null) === getModuledir() . '/' . $name . getSharedLibrarySuffix() . '.so', 'Physical package alias leaked into module identity');
        $ini = current(array_filter($config['config-files'], static fn (string $path): bool => preg_match('#/conf\.d/\d+-' . preg_quote($name, '#') . '\.ini$#', $path) === 1));
        aliasCheck(is_string($ini) && str_starts_with($ini, getConfdir() . '/conf.d/'), 'Physical package alias leaked into INI identity: ' . json_encode($config['config-files']));
        foreach ($config['files'] as $source => $destination) {
            if ($destination === $ini) {
                aliasCheck(str_contains(file_get_contents($source), 'extension=' . $name . getSharedLibrarySuffix()), 'Drop-in uses physical package name');
            }
        }
        foreach ($package->getFpmExtraArgs() as $argument) {
            if (is_file($argument)) {
                aliasCheck(!str_contains(file_get_contents($argument), $physical), 'Disable-state script uses physical package identity');
            }
        }
        if ($type === 'apk') {
            aliasCheck(in_array($logical, $config['provides_unversioned'] ?? [], true), 'Runtime alias lacks its versionless logical provide');
            aliasCheck(in_array($logical, $config['replaces'] ?? [], true), 'Runtime alias does not replace the old real package');
            aliasCheck(in_array($logical . '>0', $config['conflicts'] ?? [], true), 'Runtime alias permits an old real package');
            aliasCheck(!in_array($logical, $config['conflicts'] ?? [], true), 'Runtime alias conflicts with its own versionless provide');
            $metadata = (new ReflectionMethod(CreatePackages::class, 'consumerAliasMetadata'))->invoke(null, $physical, $config['files']);
            aliasCheck(($metadata['logical_name'] ?? null) === $logical && ($metadata['extension'] ?? null) === $name
                && ($metadata['module_sha256'] ?? null) === hash_file('sha256', $module), 'Runtime consumer alias does not bind its logical module bytes');
        } else {
            aliasCheck(!in_array($logical, $config['provides_unversioned'] ?? [], true), 'APK-only relationship escaped into ' . $type);
        }
        $debugSource = BUILD_ROOT_PATH . '/debug/' . $name . getSharedLibrarySuffix() . '.so.debug';
        file_put_contents($debugSource, 'fake ' . $name . ' debug data');
        $debugConfig = $package->getDebuginfoFpmConfig();
        if ($type === 'apk') {
            aliasCheck(in_array($physical, $debugConfig['depends'] ?? [], true), 'Debug alias does not require its physical runtime package');
            aliasCheck(in_array($logical . '-debuginfo', $debugConfig['provides_unversioned'] ?? [], true), 'Debug alias lacks its logical provide');
            aliasCheck(in_array($logical . '-debuginfo', $debugConfig['replaces'] ?? [], true), 'Debug alias does not replace the old debug package');
            aliasCheck(in_array($logical . '-debuginfo>0', $debugConfig['conflicts'] ?? [], true), 'Debug alias permits an old real debug package');
        }
        $configs[$name] = $config;
        $configs[$name . '-debuginfo'] = $debugConfig;
        $packages[$name] = $package;
    }
    $cliConfig = (new cli())->getFpmConfig();
    foreach (['amqp', 'igbinary'] as $name) {
        aliasCheck(in_array($prefix . '-' . $name . '>0', $cliConfig['conflicts'] ?? [], true) === ($type === 'apk'), 'CLI old real-package gate is wrong for ' . $type);
    }
    echo "Passed {$type} APK runtime/debug alias identity, logical module/config state, and old-package conflict metadata.\n";

    if (isset($options['native'])) {
        $create = new ReflectionMethod(CreatePackages::class, 'createApkWithNfpm');
        $archive = static function (string $name, string $version, string $revision, array $config, bool $debug = false) use ($create, $root): string {
            ob_start();
            try {
                $create->invoke(null, new AliasFixturePackage(preg_replace('/-debuginfo$/', '', $name)), $name,
                    $version, 'x86_64', $revision, $config, $debug);
            } finally {
                file_put_contents($root . '/package.log', ob_get_clean(), FILE_APPEND);
            }
            return DIST_APK_PATH . '/' . $name . '-' . $version . '-r' . $revision . '.x86_64.apk';
        };
        $header = static fn (string $path): string => aliasCommand(['tar', '-xOf', '-', '.PKGINFO'], aliasCommand(['gzip', '-dc', $path]));
        $oldCli = $archive($prefix . '-cli', '8.6.0', '1', ['files' => [BUILD_BIN_PATH . '/php' => '/usr/bin/alias-fixture-php']]);
        $newCli = $archive($prefix . '-cli', '8.6.0', '2', ['conflicts' => $cliConfig['conflicts'], 'files' => [BUILD_BIN_PATH . '/php' => '/usr/bin/alias-fixture-php']]);
        copy($newCli, $root . '/repo/x86_64/' . basename($newCli));
        $old = [];
        foreach (['amqp', 'igbinary'] as $name) {
            $logical = $prefix . '-' . $name;
            $physical = $logical . '-shared';
            $nativeConfig = $configs[$name];
            $nativeConfig['depends'] = [$prefix . '-cli'];
            $new = $archive($physical, '1.0.0', '1', $nativeConfig);
            copy($new, $root . '/repo/x86_64/' . basename($new));
            $info = $header($new);
            aliasCheck(preg_match('/^pkgname = ' . preg_quote($physical, '/') . '$/m', $info) === 1, 'Native archive has the logical runtime name');
            aliasCheck(preg_match('/^provides = ' . preg_quote($logical, '/') . '$/m', $info) === 1, 'Native runtime provide is versioned or missing');
            aliasCheck(str_contains($info, 'replaces = ' . $logical . "\n") && str_contains($info, 'depend = !' . $logical . ">0\n"), 'Native runtime replacement/conflict headers are wrong: ' . $info);
            $debug = $archive($physical . '-debuginfo', '1.0.0', '1', $configs[$name . '-debuginfo'], true);
            $debugInfo = $header($debug);
            aliasCheck(str_contains($debugInfo, 'depend = ' . $physical . "=1.0.0-r1\n"), 'Native debug archive does not pin its physical runtime');
            aliasCheck(str_contains($debugInfo, 'provides = ' . $logical . "-debuginfo\n") && str_contains($debugInfo, 'depend = !' . $logical . "-debuginfo>0\n"), 'Native debug logical alias metadata is wrong');
            $old[$name] = $archive($logical, '99.0.0', '1', ['files' => $configs[$name]['files'], 'config-files' => $configs[$name]['config-files']]);
        }
        echo "Passed native APK runtime/debug alias headers and exact physical debug dependencies.\n";
        $container = 'spp-apk-aliases-' . bin2hex(random_bytes(6));
        $script = <<<'SH'
set -eu
apk index --allow-untrusted -o /task/repo/x86_64/APKINDEX.tar.gz /task/repo/x86_64/*.apk >/dev/null
printf '%s\n' /task/repo > /etc/apk/repositories
apk add --allow-untrusted /task/dist/apk/php-zts86-cli-8.6.0-r1.x86_64.apk /task/dist/apk/php-zts86-amqp-99.0.0-r1.x86_64.apk /task/dist/apk/php-zts86-igbinary-99.0.0-r1.x86_64.apk
sed -i 's/^\(php-zts86-[a-z-]*\).*/\1/' /etc/apk/world
apk upgrade --allow-untrusted
apk info -e php-zts86-amqp-shared
apk info -e php-zts86-igbinary-shared
! apk info -e php-zts86-amqp
! apk info -e php-zts86-igbinary
apk info -v php-zts86-cli | grep -qx php-zts86-cli-8.6.0-r2
apk upgrade --allow-untrusted
apk del php-zts86-amqp-shared php-zts86-igbinary-shared
apk add --allow-untrusted php-zts86-amqp php-zts86-igbinary
apk info -e php-zts86-amqp-shared
apk info -e php-zts86-igbinary-shared
SH;
        try {
            aliasCommand(['docker', 'run', '--rm', '--memory', '384m', '--name', $container,
                '--mount', 'type=bind,src=' . $root . ',dst=/task', 'alpine:3.21', 'sh', '-ec', $script]);
        } finally {
            (new Process(['docker', 'rm', '-f', $container]))->run();
        }
        $container = 'spp-apk-alias-hold-' . bin2hex(random_bytes(6));
        $script = <<<'SH'
set -eu
printf '%s\n' /task/repo > /etc/apk/repositories
apk add --allow-untrusted /task/dist/apk/php-zts86-cli-8.6.0-r1.x86_64.apk /task/dist/apk/php-zts86-amqp-99.0.0-r1.x86_64.apk
sed -i -e 's/^php-zts86-cli.*/php-zts86-cli/' -e 's/^php-zts86-amqp.*/php-zts86-amqp=99.0.0-r1/' /etc/apk/world
if apk upgrade --allow-untrusted >/tmp/held.log 2>&1; then cat /tmp/held.log; exit 1; fi
apk info -v php-zts86-cli | grep -qx php-zts86-cli-8.6.0-r1
apk info -e php-zts86-amqp
! apk info -e php-zts86-amqp-shared
SH;
        try {
            aliasCommand(['docker', 'run', '--rm', '--memory', '384m', '--name', $container,
                '--mount', 'type=bind,src=' . $root . ',dst=/task,readonly', 'alpine:3.21', 'sh', '-ec', $script]);
        } finally {
            (new Process(['docker', 'rm', '-f', $container]))->run();
        }
        echo "Passed native Alpine ordinary upgrade, logical-name installation, repeat upgrade, and held-old-extension CLI gating.\n";
    }
} finally {
    $oldTmp === false ? putenv('TMPDIR') : putenv('TMPDIR=' . $oldTmp);
    $oldPath === false ? putenv('PATH') : putenv('PATH=' . $oldPath);
    aliasRemove($root);
}
