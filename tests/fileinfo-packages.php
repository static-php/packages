<?php

declare(strict_types=1);

use staticphp\extension;
use staticphp\package\cli;
use staticphp\step\CreatePackages;
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

$options = getopt('', ['type:', 'native', 'keep']);
if (!isset($options['type'])) {
    foreach (['rpm', 'deb', 'apk'] as $type) {
        $arguments = [PHP_BINARY, __FILE__, '--type=' . $type];
        foreach (['native', 'keep'] as $flag) {
            if (isset($options[$flag])) {
                $arguments[] = '--' . $flag;
            }
        }
        echo command($arguments);
    }
    exit;
}
$type = $options['type'];
check(in_array($type, ['rpm', 'deb', 'apk'], true), 'Unknown package type');
$root = trim(command(['mktemp', '-d', '/tmp/spp-fileinfo-packages-XXXXXXXX']));
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
    foreach ([BUILD_ROOT_PATH . '/license', BUILD_MODULES_PATH, TEMP_DIR, DIST_RPM_PATH, DIST_DEB_PATH, DIST_APK_PATH] as $directory) {
        mkdir($directory, 0755, true);
    }
    $helpers = strstr(file_get_contents(BASE_PATH . '/bin/spp'), 'function setupGithubToken(');
    check(is_string($helpers), 'Cannot load CLI package path helpers');
    eval('use staticphp\\step\\CreatePackages;' . $helpers);
    (new ReflectionProperty(CreatePackages::class, 'prefix'))->setValue(null, SPP_PREFIX);

    foreach (['8.2', '8.3', '8.4', '8.5', '8.6'] as $version) {
        foreach ([null, ['cli'], ['fileinfo'], ['devel']] as $packages) {
            $config = Yaml::parse(TwigRenderer::renderCraftTemplate($version, 'x86_64', $packages));
            $static = explode(',', (string) ($config['extensions'] ?? ''));
            $shared = explode(',', (string) ($config['shared-extensions'] ?? ''));
            check(!in_array('fileinfo', $static, true), "{$version} still builds Fileinfo into PHP");
            $expected = $packages !== ['cli'];
            check(in_array('fileinfo', $shared, true) === $expected, "{$version} shared Fileinfo selection is wrong");
        }
        $libraries = Yaml::parse(TwigRenderer::renderCraftTemplate($version, 'x86_64', null, null, ['zlib']));
        check(empty($libraries['extensions']) && empty($libraries['shared-extensions']), "{$version} library-only build selects extensions");
    }

    $prefix = CreatePackages::getPrefix();
    $config = (new cli())->getFpmConfig();
    check($config['recommends'] === [$prefix . '-fileinfo'], 'CLI does not recommend the matching Fileinfo package');
    foreach (['depends', 'provides', 'replaces', 'conflicts'] as $field) {
        check(!in_array($prefix . '-fileinfo', $config[$field] ?? [], true), "CLI still has a strong/builtin Fileinfo relationship in {$field}");
    }
    check(!in_array(getConfdir() . '/conf.d/fileinfo.ini', $config['config-files'], true), 'CLI still ships Fileinfo configuration');

    CreatePackages::bootstrapSpcGlobals();
    $fileinfo = new extension('fileinfo');
    check($fileinfo->isSharedExtension(), 'Fileinfo is not a shared extension');
    $extensionConfig = $fileinfo->getFpmConfig();
    $module = BUILD_MODULES_PATH . '/fileinfo' . getSharedLibrarySuffix() . '.so';
    check(isset($extensionConfig['files'][$module]), 'Fileinfo package does not own its shared module');
    $ini = current(array_filter(array_keys($extensionConfig['files']), static fn (string $path): bool => str_ends_with($path, '-fileinfo.ini')));
    check(is_string($ini) && preg_match('/^extension=fileinfo' . preg_quote(getSharedLibrarySuffix(), '/') . '$/m', file_get_contents($ini)) === 1, 'Fileinfo drop-in does not load the shared module');
    echo "Passed {$type} Fileinfo shared selection for PHP 8.2–8.6, CLI recommendation and extension configuration.\n";

    if (isset($options['native'])) {
        file_put_contents($root . '/payload', 'Recommendation fixture');
        $fixture = new class($prefix, $root . '/payload', $config['recommends']) implements staticphp\package {
            public function __construct(private string $prefix, private string $payload, private array $recommends) {}
            public function getName(): string { return $this->prefix . '-cli'; }
            public function getFpmConfig(): array { return ['recommends' => $this->recommends, 'files' => [$this->payload => '/usr/share/' . $this->prefix . '/recommendation-fixture']]; }
            public function getDebuginfoFpmConfig(): array { return []; }
            public function getFpmExtraArgs(): array { return []; }
            public function getLicense(): string { return 'MIT'; }
            public function getDescription(): string { return 'Fileinfo recommendation fixture'; }
        };
        (new ReflectionProperty(CreatePackages::class, 'versionArch'))->setValue(null, ['8.6.0', 'x86_64']);
        (new ReflectionProperty(CreatePackages::class, 'iterationOverride'))->setValue(null, '1');
        (new ReflectionProperty(CreatePackages::class, 'packageType'))->setValue(null, $type);
        $method = new ReflectionMethod(CreatePackages::class, match ($type) {
            'rpm' => 'createRpmPackage', 'deb' => 'createDebPackage', 'apk' => 'createApkPackage',
        });
        ob_start();
        try {
            $method->invoke(null, $fixture, '8.6.0', 'x86_64');
        } finally {
            file_put_contents($root . '/package.log', ob_get_clean());
        }
        $artifacts = glob(DIST_PATH . '/' . $type . '/*.' . $type);
        check(count($artifacts) === 1, 'Native recommendation fixture package was not created');
        $artifact = $artifacts[0];
        if ($type === 'rpm') {
            $recommendations = command(['rpm', '-qp', '--recommends', $artifact]);
            check(trim($recommendations) === $prefix . '-fileinfo', 'RPM lacks native Recommends');
            check(!str_contains(command(['rpm', '-qp', '--requires', $artifact]), $prefix . '-fileinfo'), 'RPM turned recommendation into a hard requirement');
            check(!str_contains(command(['rpm', '-qp', '--provides', $artifact]), $prefix . '-fileinfo'), 'RPM still provides builtin Fileinfo');
            $header = $recommendations;
        } elseif ($type === 'deb') {
            $member = current(array_filter(explode("\n", command(['ar', 't', $artifact])), static fn (string $name): bool => str_starts_with($name, 'control.tar')));
            $header = command(['tar', '-xOJf', '-', './control'], command(['ar', 'p', $artifact, $member]));
            check(preg_match('/^Recommends: ' . preg_quote($prefix . '-fileinfo', '/') . '$/m', $header) === 1, 'DEB lacks native Recommends');
            check(preg_match('/^Depends:.*' . preg_quote($prefix . '-fileinfo', '/') . '/m', $header) === 0, 'DEB turned recommendation into a hard dependency');
        } else {
            $header = command(['tar', '-xOf', '-', '.PKGINFO'], command(['gzip', '-dc', $artifact]));
            check(!str_contains($header, 'depend = ' . $prefix . '-fileinfo'), 'APK turned recommendation into a hard dependency');
        }
        file_put_contents($root . '/native-header.txt', $header);
        echo $type === 'apk'
            ? "Passed APK optional Fileinfo metadata without a hard dependency.\n"
            : "Passed {$type} native recommendation metadata.\n";
    }
} finally {
    $oldTmp === false ? putenv('TMPDIR') : putenv('TMPDIR=' . $oldTmp);
    if (isset($options['keep'])) {
        echo "Retained fixture: {$root}\n";
    } else {
        removeFixture($root);
    }
}
