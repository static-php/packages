<?php

declare(strict_types=1);

use staticphp\package;
use staticphp\package\frankenphp;
use staticphp\package\pie;
use staticphp\step\CreatePackages;
use staticphp\util\SharedLibraries;
use Symfony\Component\Process\Process;

require dirname(__DIR__) . '/vendor/autoload.php';

function abiCommand(array $arguments, ?string $input = null): string
{
    $process = new Process($arguments);
    $process->setInput($input);
    $process->setTimeout(120);
    $process->mustRun();
    return $process->getOutput();
}

function abiCheck(bool $condition, string $message): void
{
    if (!$condition) {
        throw new RuntimeException($message);
    }
}

function abiRemove(string $path): void
{
    if (!is_dir($path) || is_link($path)) {
        unlink($path);
        return;
    }
    foreach (new DirectoryIterator($path) as $entry) {
        if (!$entry->isDot()) {
            abiRemove($entry->getPathname());
        }
    }
    rmdir($path);
}

final class AbiPackage implements package
{
    public function __construct(private readonly string $name) {}
    public function getName(): string { return $this->name; }
    public function getFpmConfig(): array { return ['files' => [BUILD_ROOT_PATH . '/fixture' => '/usr/share/' . $this->name . '/fixture']]; }
    public function getDebuginfoFpmConfig(): array { return []; }
    public function getFpmExtraArgs(): array { return []; }
    public function getLicense(): string { return 'MIT'; }
    public function getDescription(): string { return 'PHP prerelease ABI fixture'; }
}

final class AbiPie extends pie
{
    public function getFpmConfig(): array
    {
        return ['depends' => ['php-zts-cli'], 'files' => [BUILD_ROOT_PATH . '/fixture' => '/usr/share/pie-zts/fixture']];
    }
}

$options = getopt('', ['type:']);
$type = $options['type'] ?? 'rpm';
abiCheck(in_array($type, ['rpm', 'deb', 'apk'], true), 'Unknown package format');
$root = trim(abiCommand(['mktemp', '-d', '/tmp/spp-php-abi-XXXXXXXX']));
$oldTmp = getenv('TMPDIR');
$oldLogs = getenv('SPC_LOGS_DIR');
try {
    putenv('TMPDIR=' . $root);
    putenv('SPC_LOGS_DIR=' . $root . '/logs');
    define('BASE_PATH', dirname(__DIR__));
    define('SPP_TYPE', $type);
    define('SPP_PREFIX', '-zts');
    define('SPP_PHP_VERSION', '8.6');
    define('SPP_TARGET', null);
    define('BUILD_ROOT_PATH', $root . '/buildroot');
    define('BUILD_BIN_PATH', BUILD_ROOT_PATH . '/bin');
    define('BUILD_LIB_PATH', BUILD_ROOT_PATH . '/lib');
    define('BUILD_MODULES_PATH', BUILD_ROOT_PATH . '/modules');
    define('DIST_PATH', $root . '/dist');
    define('DIST_RPM_PATH', DIST_PATH . '/rpm');
    define('DIST_DEB_PATH', DIST_PATH . '/deb');
    define('DIST_APK_PATH', DIST_PATH . '/apk');
    define('TEMP_DIR', $root . '/tmp');
    foreach ([BUILD_BIN_PATH, BUILD_LIB_PATH, BUILD_MODULES_PATH, BUILD_ROOT_PATH . '/include/php/Zend',
        TEMP_DIR, DIST_RPM_PATH, DIST_DEB_PATH, DIST_APK_PATH] as $directory) {
        mkdir($directory, 0755, true);
    }
    file_put_contents(BUILD_ROOT_PATH . '/fixture', 'Metadata fixture');
    file_put_contents(BUILD_BIN_PATH . '/frankenphp', 'Metadata fixture');
    file_put_contents(BUILD_ROOT_PATH . '/include/php/Zend/zend_modules.h', "#define ZEND_MODULE_API_NO 20260924\n");
    $helpers = strstr(file_get_contents(BASE_PATH . '/bin/spp'), 'function setupGithubToken(');
    abiCheck(is_string($helpers), 'Missing package path helpers');
    eval('use staticphp\\step\\CreatePackages;' . $helpers);
    CreatePackages::bootstrapSpcGlobals();
    file_put_contents(BUILD_ROOT_PATH . '/shared-libraries.json', json_encode([
        'schema' => 1, 'linkage' => 'shared', 'architecture' => 'x86_64',
        'libc' => $type === 'apk' ? 'musl' : 'glibc', 'runtime_libdir' => CreatePackages::runtimeLibdir(), 'libraries' => [],
    ], JSON_THROW_ON_ERROR));
    (new ReflectionProperty(CreatePackages::class, 'sharedLibraries'))->setValue(null, new SharedLibraries(BUILD_ROOT_PATH));
    (new ReflectionProperty(CreatePackages::class, 'packageType'))->setValue(null, $type);
    $runtime = new ReflectionProperty(CreatePackages::class, 'versionArch');
    $config = new ReflectionMethod(CreatePackages::class, 'packageConfig');
    $frankenDependencies = new ReflectionMethod(frankenphp::class, 'sharedDependencies');
    $create = new ReflectionMethod(CreatePackages::class, 'createPackageWithFpm');
    $stable = 'php-zts-shared-runtime-86-abi20260924';
    foreach (['8.6.0-dev' => '-dev0', '8.6.0alpha1' => '-alpha1', '8.6.0beta3' => '-beta3',
        '8.6.0RC2' => '-rc2', '8.6.0RC5' => '-rc5', '8.6.0' => '', '8.6.1RC1' => '', '8.6.1' => ''] as $php => $phase) {
        $runtime->setValue(null, [CreatePackages::normalizeVersion($php), 'x86_64']);
        $capability = $stable . $phase . '-1';
        abiCheck(CreatePackages::getSharedRuntimeCapability() === $capability, 'Incorrect PHP ABI capability for ' . $php);
        abiCheck(in_array($capability, $frankenDependencies->invoke(new frankenphp(), $type), true), 'FrankenPHP lost its PHP ABI requirement');
        foreach ([new AbiPackage('php-zts-cli'), new AbiPackage('php-zts-igbinary'), new AbiPie()] as $package) {
            $metadata = $config->invoke(null, $package, false);
            $provides = $metadata['provides_unversioned'] ?? [];
            $requires = $metadata['depends'] ?? [];
            if ($package->getName() === 'php-zts-cli') {
                abiCheck(in_array($capability, $provides, true), 'CLI does not provide its precise PHP ABI');
                if ($phase !== '') {
                    abiCheck(!in_array($stable . '-1', $provides, true), 'Prerelease CLI also advertises the stable ABI');
                }
            } elseif ($package instanceof pie) {
                abiCheck(array_filter($requires, static fn (string $dependency): bool => str_contains($dependency, '-shared-runtime-')) === [], 'PIE acquired a compiled PHP ABI requirement');
            } else {
                abiCheck(in_array($capability, $requires, true), 'Compiled extension does not require its precise PHP ABI');
            }
        }
    }
    $revision = 0;
    foreach (['8.6.0RC2' => '-rc2', '8.6.0RC5' => '-rc5', '8.6.0' => '', '8.6.1RC1' => ''] as $php => $phase) {
        $runtime->setValue(null, [CreatePackages::normalizeVersion($php), 'x86_64']);
        (new ReflectionProperty(CreatePackages::class, 'iterationOverride'))->setValue(null, (string) ++$revision);
        foreach ([new AbiPackage('php-zts-cli'), new AbiPackage('php-zts-igbinary'), new AbiPie()] as $package) {
            $name = $package->getName();
            $upstream = $name === 'php-zts-cli' ? CreatePackages::normalizeVersion($php) : ($package instanceof pie ? '1.5.5' : '3.2.17~rc1');
            $previousArchives = glob(DIST_PATH . '/' . $type . '/*.' . $type);
            ob_start();
            try {
                $create->invoke(null, $package, $upstream, 'x86_64');
            } finally {
                file_put_contents($root . '/packages.log', ob_get_clean(), FILE_APPEND);
            }
            $artifact = array_values(array_diff(glob(DIST_PATH . '/' . $type . '/*.' . $type), $previousArchives));
            abiCheck(count($artifact) === 1, 'Expected one native archive for ' . $name . ' at ' . $php);
            $header = match ($type) {
                'rpm' => abiCommand(['rpm', '-qp', '--qf', "%{VERSION}-%{RELEASE}\n[%{REQUIRENAME}\n][%{PROVIDENAME}\n]", $artifact[0]]),
                'deb' => abiCommand(['dpkg-deb', '-f', $artifact[0]]),
                'apk' => abiCommand(['tar', '-xOf', '-', '.PKGINFO'], abiCommand(['gzip', '-dc', $artifact[0]])),
            };
            if ($package instanceof pie) {
                $expected = match ($type) {'rpm' => '1.5.5_86', 'deb' => '1.5.5+php86', 'apk' => '1.5.5.0_p86'};
                abiCheck(str_contains($header, $expected . ($type === 'apk' ? '-r' : '-') . $revision), 'PIE archive has a PHP prerelease in its version');
                abiCheck(!str_contains($header, '-shared-runtime-'), 'PIE archive requires a compiled PHP ABI');
            } else {
                abiCheck(str_contains($header, $stable . $phase . '-1'), 'Native archive lost its PHP ABI capability');
                $expected = $name === 'php-zts-cli' ? ($type === 'apk' ? CreatePackages::toApkVersion($upstream) : $upstream)
                    : match ($type) {'rpm' => '3.2.17~rc1_86', 'deb' => '3.2.17~rc1+php86', 'apk' => '3.2.17_rc1_p86'}
                        . ($phase === '' ? '' : ($type === 'apk' ? '_' : '~') . substr($phase, 1));
                abiCheck(str_contains($header, $expected . ($type === 'apk' ? '-r' : '-') . $revision), 'Native archive has the wrong core or compiled extension version');
            }
        }
    }
    echo "Passed {$type} native PHP prerelease ABI providers/requirements, GA and patch-RC compatibility, and stream-only PIE versions/dependencies.\n";
} finally {
    $oldTmp === false ? putenv('TMPDIR') : putenv('TMPDIR=' . $oldTmp);
    $oldLogs === false ? putenv('SPC_LOGS_DIR') : putenv('SPC_LOGS_DIR=' . $oldLogs);
    abiRemove($root);
}
