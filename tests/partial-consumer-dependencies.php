<?php

declare(strict_types=1);

use StaticPHP\DI\ApplicationContext;
use StaticPHP\Package\Package;
use StaticPHP\Package\PackageInstaller;
use StaticPHP\Package\PhpExtensionPackage;
use staticphp\hook\ReuseConsumerExtensions;
use staticphp\step\RunSPC;
use staticphp\util\BuildPayload;
use staticphp\util\ConsumerSnapshot;
use Symfony\Component\Process\Process;

require dirname(__DIR__) . '/vendor/autoload.php';
require_once dirname(__DIR__) . '/src/step/RunSPC.php';
require_once dirname(__DIR__) . '/src/hook/ReuseConsumerExtensions.php';

function command(array $arguments): string
{
    $process = new Process($arguments);
    $process->mustRun();
    return $process->getOutput();
}
function check(bool $condition, string $message): void
{
    if (!$condition) {
        throw new RuntimeException($message);
    }
}
function fileIdentity(string $path): array
{
    return ['sha256' => hash_file('sha256', $path), 'mode' => fileperms($path) & 0777];
}

final class ResolvedInstaller extends PackageInstaller
{
    public function __construct(private array $resolved) {}
    public function getResolvedPackages(mixed $package_type = Package::class): array
    {
        return array_filter($this->resolved, static fn (Package $package): bool => $package instanceof $package_type);
    }
}

$temporary = trim(command(['mktemp', '-d', '/tmp/spp-partial-dependencies-XXXXXXXX']));
define('BASE_PATH', dirname(__DIR__));
define('BUILD_ROOT_PATH', $temporary . '/buildroot');
define('BUILD_MODULES_PATH', BUILD_ROOT_PATH . '/modules');
$snapshot = null;
try {
    foreach (['bin', 'lib', 'lib/php/build', 'modules', 'include/php/Zend', 'provenance/cached-consumer'] as $directory) {
        mkdir(BUILD_ROOT_PATH . '/' . $directory, 0755, true);
    }
    file_put_contents($temporary . '/core.c', 'int main(void) { return 0; }');
    file_put_contents($temporary . '/module.c', 'int fixture(void) { return 42; }');
    command(['cc', '-g', $temporary . '/core.c', '-o', BUILD_ROOT_PATH . '/bin/php']);
    command(['cc', '-g', '-shared', '-fPIC', $temporary . '/module.c', '-o', BUILD_ROOT_PATH . '/lib/libphp-zts-86.so']);
    symlink('libphp-zts-86.so', BUILD_ROOT_PATH . '/lib/libphp.so');
    file_put_contents(BUILD_ROOT_PATH . '/bin/phpize', '#!/bin/sh');
    $phpConfig = "#!/bin/sh\nextension_dir=\"/usr/lib/php-zts/modules\"\n";
    $phpizeM4 = 'test "[$]$1" = "no" && $1=yes' . "\n";
    file_put_contents(BUILD_ROOT_PATH . '/bin/php-config', $phpConfig);
    file_put_contents(BUILD_ROOT_PATH . '/lib/php/build/phpize.m4', $phpizeM4);
    file_put_contents(BUILD_ROOT_PATH . '/include/php/Zend/zend_modules.h', "#define ZEND_MODULE_API_NO 20260922\n");
    $names = ['igbinary', 'msgpack', 'memcached', 'yac', 'mysqlnd', 'pdo_mysql'];
    foreach ($names as $name) {
        copy(BUILD_ROOT_PATH . '/lib/libphp-zts-86.so', BUILD_MODULES_PATH . '/' . $name . '-zts-86.so');
        symlink($name . '-zts-86.so', BUILD_MODULES_PATH . '/' . $name . '.so');
    }
    $configuration = ['php-version' => '8.6.0RC4', 'extensions' => [], 'shared-extensions' => implode(',', $names), 'sapi' => 'cli'];
    BuildPayload::record(BUILD_ROOT_PATH, $configuration, '8.6.0RC4', php_uname('m'));
    $metadata = BuildPayload::seal(BUILD_ROOT_PATH)->metadata();
    $metadataPath = BUILD_ROOT_PATH . '/provenance/cached-consumer/build-payload.json';
    file_put_contents($metadataPath, json_encode($metadata));
    $core = [];
    foreach (['bin/php', 'bin/phpize', 'bin/php-config', 'lib/php/build/phpize.m4', 'include/php/Zend/zend_modules.h', 'lib/libphp-zts-86.so'] as $file) {
        $core[$file] = fileIdentity(BUILD_ROOT_PATH . '/' . $file);
    }
    $core['lib/libphp.so'] = ['symlink' => 'libphp-zts-86.so'];
    $receipt = ['schema' => 1, 'state' => 'verified', 'php_stream' => '8.6', 'key' => str_repeat('a', 64), 'seed_key' => str_repeat('b', 64),
        'rebuild_packages' => ['memcached', 'pdo_mysql'], 'cached_metadata_path' => 'provenance/cached-consumer/build-payload.json',
        'cached_metadata_sha256' => hash_file('sha256', $metadataPath), 'core_files' => $core];
    file_put_contents(BUILD_ROOT_PATH . '/consumer-cache-seed.json', json_encode($receipt));
    $snapshot = ConsumerSnapshot::capture(BUILD_ROOT_PATH, $metadata, $receipt['rebuild_packages']);
    $snapshot->activate($receipt);
    command(['rm', '-rf', BUILD_MODULES_PATH]);
    $built = [];
    $extensions = [];
    foreach ($names as $name) {
        $extension = new PhpExtensionPackage($name, extension_config: ['display-name' => $name]);
        $extension->setBuildShared();
        if ($name === 'mysqlnd') {
            $extension->setBuildWithPhp();
        }
        $extension->addStage('build', static function () use (&$built, $name): void { $built[] = $name; });
        $extensions[$name] = $extension;
    }
    $installer = new ResolvedInstaller($extensions);
    ApplicationContext::set(PackageInstaller::class, $installer);
    (new ReuseConsumerExtensions())->preserveResolvedExtensions($installer);
    foreach ($extensions as $extension) {
        $extension->buildShared();
    }
    sort($built);
    check($built === ['memcached', 'pdo_mysql'], 'Resolved dependency providers were rebuilt instead of reusing verified outputs');
    foreach (['igbinary', 'msgpack', 'yac', 'mysqlnd'] as $name) {
        check(is_file(BUILD_MODULES_PATH . '/' . $name . '.so') && $extensions[$name]->isBuildShared(), 'A reused dependency lost shared resolution semantics: ' . $name);
    }
    foreach (['memcached', 'yac', 'pdo_mysql'] as $name) {
        $load = $extensions[$name]->getSharedExtensionLoadString();
        foreach (['igbinary', 'msgpack', 'mysqlnd'] as $provider) {
            check(str_contains($load, 'extension=' . $provider), 'Dependency smoke load omitted a cached provider: ' . $name . ' -> ' . $provider);
        }
    }
    $resolve = new ReflectionMethod(RunSPC::class, 'partialResolutionExtensions');
    $metadata['configuration']['shared-extensions'] .= ',redis,swoole,swoole-hook-pgsql';
    $metadata['skipped_shared_extensions'] = ['skipped' => [['extension' => 'redis'], ['extension' => 'swoole']]];
    $resolved = $resolve->invoke(null, $metadata, ['memcached', 'pdo_mysql']);
    check(in_array('igbinary', $resolved, true) && in_array('msgpack', $resolved, true) && in_array('mysqlnd', $resolved, true), 'Partial resolution lost cached dependency providers');
    check(!in_array('redis', $resolved, true) && !in_array('swoole', $resolved, true) && !in_array('swoole-hook-pgsql', $resolved, true), 'Partial resolution retried unrelated skipped extensions or addons');
    $withFranken = $resolve->invoke(null, $metadata, ['memcached', 'pdo_mysql', 'frankenphp']);
    check(!in_array('frankenphp', $withFranken, true), 'FrankenPHP was incorrectly added as a shared extension');
    $validate = new ReflectionMethod(RunSPC::class, 'validatePartialCompileSelection');
    $validate->invoke(null, ['memcached', 'pdo_mysql', 'frankenphp']);
    $caught = false;
    try {
        $validate->invoke(null, ['mysqlnd']);
    } catch (RuntimeException $error) {
        $caught = str_contains($error->getMessage(), 'module') && str_contains($error->getMessage(), 'recipe');
    }
    check($caught, 'A selected module compiled with core was silently accepted by maintainer skip');
    file_put_contents(BUILD_ROOT_PATH . '/bin/php-config', str_replace('/usr/lib/php-zts/modules', BUILD_MODULES_PATH, $phpConfig));
    file_put_contents(BUILD_ROOT_PATH . '/lib/php/build/phpize.m4', '# ' . $phpizeM4);
    check(ConsumerSnapshot::active(BUILD_ROOT_PATH) !== null, 'Exact SPC temporary script substitutions invalidated selected source hooks');
    $caught = false;
    try {
        BuildPayload::verifiedCoreSeed(BUILD_ROOT_PATH, $receipt['rebuild_packages'], '8.6.0RC4');
    } catch (RuntimeException $error) {
        $caught = str_contains($error->getMessage(), 'Cached core input changed');
    }
    check($caught, 'Normal seed validation accepted temporary script changes without snapshot proof');
    file_put_contents(BUILD_ROOT_PATH . '/bin/php-config', "unexpected modification\n", FILE_APPEND);
    $caught = false;
    try {
        ConsumerSnapshot::active(BUILD_ROOT_PATH);
    } catch (RuntimeException $error) {
        $caught = str_contains($error->getMessage(), 'Unverified temporary build-script change');
    }
    check($caught, 'Source hook validation accepted an unexplained changed core script');
    file_put_contents(BUILD_ROOT_PATH . '/bin/php-config', $phpConfig);
    file_put_contents(BUILD_ROOT_PATH . '/lib/php/build/phpize.m4', $phpizeM4);
    unlink(BUILD_MODULES_PATH . '/igbinary-zts-86.so');
    $caught = false;
    try {
        ConsumerSnapshot::active(BUILD_ROOT_PATH)->verifyExtension('igbinary');
    } catch (RuntimeException $error) {
        $caught = str_contains($error->getMessage(), 'Unverified reused');
    }
    check($caught, 'A missing unselected provider was accepted');
    echo "Passed verified provider reuse, full shared resolution, cached dependency smoke loading, known skip pruning and missing-provider rejection.\n";
} finally {
    $snapshot?->close();
    command(['rm', '-rf', $temporary]);
}
