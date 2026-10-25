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

$options = getopt('', ['type:', 'native', 'php-stream:']);
if (isset($options['native']) && !isset($options['php-stream']) && ($options['type'] ?? 'apk') === 'apk') {
    if (!isset($options['type'])) {
        echo aliasCommand([PHP_BINARY, '-d', 'memory_limit=384M', __FILE__]);
    }
    foreach (['8.2', '8.5'] as $stream) {
        echo aliasCommand([PHP_BINARY, '-d', 'memory_limit=384M', __FILE__, '--type=apk', '--native', '--php-stream=' . $stream]);
    }
    exit;
}
$stream = $options['php-stream'] ?? '8.6';
aliasCheck(in_array($stream, ['8.2', '8.5', '8.6'], true), 'Unknown fixture PHP stream');
aliasCheck(!isset($options['native']) || in_array($stream, ['8.2', '8.5'], true), 'Native fixtures cover historical PHP8.2 and8.5');
$fullPhpVersion = ['8.2' => '8.2.35', '8.5' => '8.5.12', '8.6' => '8.6.0'][$stream];
if (!isset($options['type'])) {
    foreach (['rpm', 'deb', 'apk'] as $type) {
        echo aliasCommand([PHP_BINARY, '-d', 'memory_limit=384M', __FILE__, '--type=' . $type,
            ...(isset($options['native']) && $type === 'apk' ? ['--native'] : []),
            ...(isset($options['php-stream']) ? ['--php-stream=' . $stream] : [])]);
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
    define('SPP_PHP_VERSION', $stream);
    define('SPP_PREFIX', isset($options['native']) ? '-zts' : '-zts86');
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
    (new ReflectionProperty(CreatePackages::class, 'versionArch'))->setValue(null, [$fullPhpVersion, 'x86_64']);
    (new ReflectionProperty(CreatePackages::class, 'packageType'))->setValue(null, $type);
    CreatePackages::bootstrapSpcGlobals();
    $prefix = CreatePackages::getPrefix();
    aliasCheck(array_keys(ApkRuntimeAliases::extensions()) === ['amqp', 'igbinary'], 'Unexpected APK alias scope');
    foreach (['82', '83', '84', '85', '86'] as $identityStream) {
        foreach (['amqp', 'igbinary'] as $name) {
            foreach (['', '-debuginfo'] as $debug) {
                $logical = 'php-zts' . $identityStream . '-' . $name . $debug;
                $physical = 'php-zts' . $identityStream . '-' . $name . '-shared' . $debug;
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
            aliasCheck(($config['apk_provider_priority'] ?? null) === 100 && empty($config['apk_install_if']), 'Runtime alias priority or activation policy is wrong');
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
            aliasCheck(($debugConfig['apk_provider_priority'] ?? null) === 100 && empty($debugConfig['apk_install_if']), 'Debug alias priority or activation policy is wrong');
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
        $oldCliRevision = $stream === '8.2' ? '1' : '2';
        $oldCli = $archive($prefix . '-cli', $fullPhpVersion, $oldCliRevision,
            ['files' => [BUILD_BIN_PATH . '/php' => '/usr/bin/alias-fixture-php']]);
        $link = $root . '/libphp-fixture.so';
        symlink('php-zts/lib/libphp-fixture.so', $link);
        $newCli = $archive($prefix . '-cli', $fullPhpVersion, '100',
            ['conflicts' => $cliConfig['conflicts'], 'files' => [BUILD_BIN_PATH . '/php' => '/usr/bin/alias-fixture-php',
                $link => '/usr/lib/libphp-fixture.so']]);
        aliasCheck(str_contains(aliasCommand(['tar', '-tvf', $newCli]), ' -> php-zts/lib/libphp-fixture.so'), 'Native APK lost the staged relative embed symlink');
        foreach ([$oldCli, $newCli] as $path) {
            copy($path, $root . '/repo/x86_64/' . preg_replace('/\.x86_64(?=\.apk$)/', '', basename($path)));
        }
        $historicalVersions = $stream === '8.2' ? [
            'amqp' => ['2.2.0p82-r2', '2.2.0p82.35_p0-r0'],
            'igbinary' => ['3.2.17~rc1_82-r1', '3.2.17~rc1p82-r0', '3.2.17_rc1_p82-r0'],
        ] : [
            'amqp' => ['2.2.0~dev_85-r1', '2.2.0p85-r2'],
            'igbinary' => ['3.2.17~rc1_85-r1', '3.2.17~rc1p85-r2'],
        ];
        $versions = [];
        $old = [];
        $future = [];
        foreach (['amqp', 'igbinary'] as $name) {
            $logical = $prefix . '-' . $name;
            $physical = $logical . '-shared';
            $version = CreatePackages::getTaggedPackageVersion('apk', $name === 'amqp' ? '2.2.0' : '3.2.17~rc1');
            $versions[$name] = $version;
            $nativeConfig = $configs[$name];
            $nativeConfig['depends'] = [$prefix . '-cli=' . $fullPhpVersion . '-r100'];
            $new = $archive($physical, $version, '0', $nativeConfig);
            copy($new, $root . '/repo/x86_64/' . preg_replace('/\.x86_64(?=\.apk$)/', '', basename($new)));
            $info = $header($new);
            aliasCheck(preg_match('/^pkgname = ' . preg_quote($physical, '/') . '$/m', $info) === 1, 'Native archive has the logical runtime name');
            aliasCheck(preg_match('/^provides = ' . preg_quote($logical, '/') . '$/m', $info) === 1, 'Native runtime provide is versioned or missing');
            aliasCheck(str_contains($info, 'replaces = ' . $logical . "\n") && str_contains($info, 'depend = !' . $logical . ">0\n"), 'Native runtime replacement/conflict headers are wrong: ' . $info);
            aliasCheck(str_contains($info, "provider_priority = 100\n") && !str_contains($info, 'install_if ='), 'Native runtime priority is missing or introduces install_if');
            $debug = $archive($physical . '-debuginfo', $version, '0', $configs[$name . '-debuginfo'], true);
            copy($debug, $root . '/repo/x86_64/' . preg_replace('/\.x86_64(?=\.apk$)/', '', basename($debug)));
            $debugInfo = $header($debug);
            aliasCheck(str_contains($debugInfo, 'depend = ' . $physical . '=' . $version . "-r0\n"), 'Native debug archive does not pin its physical runtime');
            aliasCheck(str_contains($debugInfo, 'provides = ' . $logical . "-debuginfo\n") && str_contains($debugInfo, 'depend = !' . $logical . "-debuginfo>0\n"), 'Native debug logical alias metadata is wrong');
            aliasCheck(str_contains($debugInfo, "provider_priority = 100\n") && !str_contains($debugInfo, 'install_if ='), 'Native debug priority is missing or introduces install_if');
            $future[$name] = $archive($physical, $version, '1', $nativeConfig);
            $future[$name . '-debuginfo'] = $archive($physical . '-debuginfo', $version, '1', $configs[$name . '-debuginfo'], true);
            foreach ($historicalVersions[$name] as $historical) {
                preg_match('/^(.*)-r(\d+)$/D', $historical, $parts);
                $path = $archive($logical, $parts[1], $parts[2], [
                    'depends' => [$prefix . '-cli'],
                    'files' => $configs[$name]['files'], 'config-files' => $configs[$name]['config-files'],
                ]);
                $old[$name][] = $path;
                copy($path, $root . '/repo/x86_64/' . preg_replace('/\.x86_64(?=\.apk$)/', '', basename($path)));
            }
        }
        echo "Passed native APK {$stream} runtime/debug headers, provider priority, and exact physical debug dependencies.\n";
        $run = static function (string $script) use ($root): void {
            $container = 'spp-apk-aliases-' . bin2hex(random_bytes(6));
            try {
                aliasCommand(['docker', 'run', '--rm', '--memory', '384m', '--cpus', '1', '--network', 'none', '--name', $container,
                    '--mount', 'type=bind,src=' . $root . ',dst=/task', 'alpine:3.21', 'sh', '-ec', $script]);
            } finally {
                (new Process(['docker', 'rm', '-f', $container]))->run();
            }
        };
        $setup = <<<'SH'
set -eu
apk index --allow-untrusted -o /task/repo/x86_64/APKINDEX.tar.gz /task/repo/x86_64/*.apk >/dev/null
printf '%s\n' /task/repo > /etc/apk/repositories
tar -xOf /task/repo/x86_64/APKINDEX.tar.gz APKINDEX | awk 'BEGIN { RS="" } /P:php-zts-(amqp|igbinary)-shared\n/ { if ($0 !~ /\nk:100\n/) exit 1; count++ } END { if (count != 2) exit 1 }'
SH;
        $assertAliases = <<<'SH'
apk info -e php-zts-amqp-shared
apk info -e php-zts-igbinary-shared
! grep -qx 'P:php-zts-amqp' /lib/apk/db/installed
! grep -qx 'P:php-zts-igbinary' /lib/apk/db/installed
apk info -e php-zts-amqp
apk info -e php-zts-igbinary
SH;
        $newCliVersion = $fullPhpVersion . '-r100';
        $cliCheck = 'apk info -v -e php-zts-cli | grep -qx ' . escapeshellarg('php-zts-cli-' . $newCliVersion) . "\n"
            . "test \"\$(readlink /usr/lib/libphp-fixture.so)\" = php-zts/lib/libphp-fixture.so\n";
        $cases = max(count($old['amqp']), count($old['igbinary']));
        for ($index = 0; $index < $cases; $index++) {
            $amqp = $old['amqp'][$index % count($old['amqp'])];
            $igbinary = $old['igbinary'][$index % count($old['igbinary'])];
            $initial = implode(' ', array_map(static fn (string $path): string => escapeshellarg('/task/dist/apk/' . basename($path)), [$oldCli, $amqp, $igbinary]));
            $run($setup . "\napk add --allow-untrusted {$initial}\n"
                . "sed -i 's/^\\(php-zts-[a-z-]*\\).*/\\1/' /etc/apk/world\n"
                . "cp /etc/apk/world /tmp/world.before\napk upgrade --allow-untrusted\n"
                . $assertAliases . "\n" . $cliCheck
                . "cmp /tmp/world.before /etc/apk/world\ncp /lib/apk/db/installed /tmp/installed.before\napk upgrade --allow-untrusted\ncmp /tmp/installed.before /lib/apk/db/installed\n"
                . "apk del php-zts-amqp php-zts-igbinary\n! apk info -e php-zts-amqp-shared\n! apk info -e php-zts-igbinary-shared\n"
                . "apk add --allow-untrusted php-zts-amqp php-zts-igbinary\n" . $assertAliases);
        }
        $run($setup . "\napk add --allow-untrusted php-zts-amqp php-zts-igbinary php-zts-amqp-debuginfo php-zts-igbinary-debuginfo\n"
            . $assertAliases . "\n" . $cliCheck
            . "apk info -e php-zts-amqp-shared-debuginfo\napk info -e php-zts-igbinary-shared-debuginfo\n");
        $run($setup . "\napk add --allow-untrusted " . escapeshellarg('/task/dist/apk/' . basename($oldCli))
            . "\nsed -i 's/^php-zts-cli.*/php-zts-cli/' /etc/apk/world\napk upgrade --allow-untrusted\n"
            . $cliCheck . "! apk info -e php-zts-amqp-shared\n! apk info -e php-zts-igbinary-shared\n");
        $currentIndex = 'apk index --allow-untrusted -o /task/repo/x86_64/APKINDEX.tar.gz ' . implode(' ', array_map(
            static fn (string $path): string => escapeshellarg('/task/repo/x86_64/' . basename($path)),
            glob($root . '/repo/x86_64/*.apk')));
        foreach ($future as $path) {
            copy($path, $root . '/repo/x86_64/' . preg_replace('/\.x86_64(?=\.apk$)/', '', basename($path)));
        }
        $allIndex = 'apk index --allow-untrusted -o /task/repo/x86_64/APKINDEX.tar.gz /task/repo/x86_64/*.apk';
        $run(str_replace($allIndex, $currentIndex, $setup)
            . "\napk add --allow-untrusted php-zts-amqp php-zts-igbinary php-zts-amqp-debuginfo php-zts-igbinary-debuginfo\n"
            . 'apk info -v -e php-zts-amqp-shared | grep -qx ' . escapeshellarg('php-zts-amqp-shared-' . $versions['amqp'] . '-r0') . "\n"
            . 'apk info -v -e php-zts-igbinary-shared | grep -qx ' . escapeshellarg('php-zts-igbinary-shared-' . $versions['igbinary'] . '-r0') . "\n"
            . "{$allIndex} >/dev/null\napk update --allow-untrusted\napk upgrade --allow-untrusted\n"
            . $assertAliases . "\n"
            . 'apk info -v -e php-zts-amqp-shared | grep -qx ' . escapeshellarg('php-zts-amqp-shared-' . $versions['amqp'] . '-r1') . "\n"
            . 'apk info -v -e php-zts-igbinary-shared | grep -qx ' . escapeshellarg('php-zts-igbinary-shared-' . $versions['igbinary'] . '-r1') . "\n"
            . 'apk info -v -e php-zts-amqp-shared-debuginfo | grep -qx ' . escapeshellarg('php-zts-amqp-shared-debuginfo-' . $versions['amqp'] . '-r1') . "\n"
            . 'apk info -v -e php-zts-igbinary-shared-debuginfo | grep -qx ' . escapeshellarg('php-zts-igbinary-shared-debuginfo-' . $versions['igbinary'] . '-r1') . "\n"
            . "cp /lib/apk/db/installed /tmp/installed.before\napk upgrade --allow-untrusted\ncmp /tmp/installed.before /lib/apk/db/installed\n");
        if ($stream === '8.2') {
            $held = $old['amqp'][0];
            $run(str_replace('if (count != 2)', 'if (count != 4)', $setup) . "\napk add --allow-untrusted " . escapeshellarg('/task/dist/apk/' . basename($oldCli)) . ' '
                . escapeshellarg('/task/dist/apk/' . basename($held)) . "\n"
                . "sed -i -e 's/^php-zts-cli.*/php-zts-cli/' -e 's/^php-zts-amqp.*/php-zts-amqp=2.2.0p82-r2/' /etc/apk/world\n"
                . "apk upgrade --allow-untrusted >/tmp/held.log 2>&1 || :\n"
                . 'apk info -v -e php-zts-cli | grep -qx ' . escapeshellarg('php-zts-cli-' . $fullPhpVersion . '-r' . $oldCliRevision) . "\n"
                . "grep -qx 'P:php-zts-amqp' /lib/apk/db/installed\n! apk info -e php-zts-amqp-shared\n");
        }
        echo "Passed native Alpine {$stream} historical valid/malformed versions, ordinary upgrades, fresh/debug logical installs, absent modules and repeat upgrades. Metadata fixture only; no shared-payload runtime claim.\n";
    }
} finally {
    $oldTmp === false ? putenv('TMPDIR') : putenv('TMPDIR=' . $oldTmp);
    $oldPath === false ? putenv('PATH') : putenv('PATH=' . $oldPath);
    aliasRemove($root);
}
