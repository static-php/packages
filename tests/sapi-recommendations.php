<?php

declare(strict_types=1);

use staticphp\package;
use staticphp\step\CreatePackages;
use Symfony\Component\Process\Process;

require dirname(__DIR__) . '/vendor/autoload.php';

function runRecommendationCommand(array $arguments, ?string $input = null): string
{
    $process = new Process($arguments);
    $process->setInput($input);
    $process->setTimeout(120);
    $process->mustRun();
    return $process->getOutput();
}

function assertRecommendation(bool $condition, string $message): void
{
    if (!$condition) {
        throw new RuntimeException($message);
    }
}

function removeRecommendationFixture(string $path): void
{
    if (!is_dir($path) || is_link($path)) {
        unlink($path);
        return;
    }
    foreach (new DirectoryIterator($path) as $file) {
        if (!$file->isDot()) {
            removeRecommendationFixture($file->getPathname());
        }
    }
    rmdir($path);
}

$options = getopt('', ['type:']);
if (!isset($options['type'])) {
    foreach (['rpm', 'deb', 'apk'] as $type) {
        echo runRecommendationCommand([PHP_BINARY, __FILE__, '--type=' . $type]);
    }
    exit;
}
$type = $options['type'];
assertRecommendation(in_array($type, ['rpm', 'deb', 'apk'], true), 'Unknown package type');
$root = trim(runRecommendationCommand(['mktemp', '-d', '/tmp/spp-sapi-recommends-XXXXXXXX']));
$previousTmp = getenv('TMPDIR');
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
    file_put_contents(BUILD_BIN_PATH . '/php', "#!/bin/sh\nprintf '8.6.0'\n");
    chmod(BUILD_BIN_PATH . '/php', 0755);
    file_put_contents(BUILD_BIN_PATH . '/frankenphp', "#!/bin/sh\nif [ \"\$1\" = completion ]; then printf '# fixture completion\\n'; else printf 'FrankenPHP v1.13.3 PHP 8.6.0 Caddy v2.11.7\\n'; fi\n");
    chmod(BUILD_BIN_PATH . '/frankenphp', 0755);
    $helpers = strstr(file_get_contents(BASE_PATH . '/bin/spp'), 'function setupGithubToken(');
    assertRecommendation(is_string($helpers), 'Missing package path helpers');
    eval('use staticphp\\step\\CreatePackages;' . $helpers);
    foreach (['prefix' => SPP_PREFIX, 'versionArch' => ['8.6.0', 'x86_64'], 'iterationOverride' => '1', 'packageType' => $type] as $property => $value) {
        (new ReflectionProperty(CreatePackages::class, $property))->setValue(null, $value);
    }
    CreatePackages::bootstrapSpcGlobals();
    $prefix = CreatePackages::getPrefix();
    $expected = [$prefix . '-fileinfo', $prefix . '-curl'];
    sort($expected);
    file_put_contents($root . '/payload', 'SAPI recommendation fixture');
    $frankenFiles = [
        'Caddyfile', 'content/index.php',
        'rhel/preinstall.sh', 'rhel/postinstall.sh', 'rhel/preuninstall.sh', 'rhel/postuninstall.sh', 'rhel/frankenphp.service',
        'debian/postinst.sh', 'debian/prerm.sh', 'debian/postrm.sh', 'debian/frankenphp.service',
        'alpine/post-install.sh', 'alpine/pre-deinstall.sh', 'alpine/post-deinstall.sh', 'alpine/frankenphp.openrc',
    ];
    foreach ($frankenFiles as $relative) {
        $path = DIST_PATH . '/frankenphp/package/' . $relative;
        if (!is_dir(dirname($path))) {
            mkdir(dirname($path), 0755, true);
        }
        file_put_contents($path, str_ends_with($path, '.sh') ? "#!/bin/sh\nexit 0\n" : 'Fixture configuration');
    }
    foreach (['cli', 'cgi', 'fpm', 'embed', 'frankenphp'] as $sapi) {
        $class = 'staticphp\\package\\' . $sapi;
        $sapiPackage = new $class();
        $config = $sapiPackage->getFpmConfig();
        $recommendations = $config['recommends'] ?? [];
        $actual = $recommendations;
        sort($actual);
        assertRecommendation($actual === $expected, $sapi . ' lacks the shared-extension recommendations');
        ob_start();
        try {
            if ($sapi === 'frankenphp') {
                $method = match ($type) { 'rpm' => 'createRpmPackage', 'deb' => 'createDebPackage', 'apk' => 'createApkPackage' };
                $sapiPackage->$method('x86_64', [], '1');
            } else {
                $fixture = new class($sapiPackage->getName(), $root . '/payload', $recommendations) implements package {
                    public function __construct(private string $name, private string $payload, private array $recommendations) {}
                    public function getName(): string { return $this->name; }
                    public function getFpmConfig(): array { return ['recommends' => $this->recommendations, 'files' => [$this->payload => '/usr/share/' . $this->name . '/fixture']]; }
                    public function getDebuginfoFpmConfig(): array { return []; }
                    public function getFpmExtraArgs(): array { return []; }
                    public function getLicense(): string { return 'MIT'; }
                    public function getDescription(): string { return 'SAPI recommendation fixture'; }
                };
                $method = new ReflectionMethod(CreatePackages::class, match ($type) { 'rpm' => 'createRpmPackage', 'deb' => 'createDebPackage', 'apk' => 'createApkPackage' });
                $method->invoke(null, $fixture, '8.6.0', 'x86_64');
            }
        } finally {
            file_put_contents($root . '/' . $sapi . '.log', ob_get_clean());
        }
        $name = $sapiPackage->getName();
        $artifacts = glob(DIST_PATH . '/' . $type . '/' . $name . '*.' . $type);
        assertRecommendation(count($artifacts) === 1, 'Expected one native ' . $sapi . ' package');
        $artifact = $artifacts[0];
        if ($type === 'rpm') {
            $weak = array_values(array_filter(explode("\n", trim(runRecommendationCommand(['rpm', '-qp', '--recommends', $artifact])))));
            sort($weak);
            assertRecommendation($weak === $expected, $sapi . ' RPM has incorrect native Recommends');
            $strong = runRecommendationCommand(['rpm', '-qp', '--requires', $artifact]);
        } elseif ($type === 'deb') {
            $member = current(array_filter(explode("\n", runRecommendationCommand(['ar', 't', $artifact])), static fn (string $name): bool => str_starts_with($name, 'control.tar')));
            $header = runRecommendationCommand(['tar', '-xOJf', '-', './control'], runRecommendationCommand(['ar', 'p', $artifact, $member]));
            preg_match('/^Recommends: (.*)$/m', $header, $match);
            $weak = isset($match[1]) ? array_map('trim', explode(',', $match[1])) : [];
            sort($weak);
            assertRecommendation($weak === $expected, $sapi . ' DEB has incorrect native Recommends');
            preg_match('/^Depends: (.*)$/m', $header, $match);
            $strong = $match[1] ?? '';
        } else {
            $strong = runRecommendationCommand(['tar', '-xOf', '-', '.PKGINFO'], runRecommendationCommand(['gzip', '-dc', $artifact]));
        }
        foreach ($expected as $recommendation) {
            assertRecommendation(!str_contains($strong, $recommendation), $sapi . ' makes ' . $recommendation . ' a hard dependency');
        }
        echo 'Passed ' . $type . ' native ' . $sapi . ' shared-extension ' . ($type === 'apk' ? 'optionality' : 'recommendations') . ".\n";
    }
} finally {
    $previousTmp === false ? putenv('TMPDIR') : putenv('TMPDIR=' . $previousTmp);
    removeRecommendationFixture($root);
}
