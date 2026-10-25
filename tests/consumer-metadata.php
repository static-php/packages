<?php

declare(strict_types=1);

use staticphp\package;
use staticphp\step\CreatePackages;
use staticphp\util\SharedLibraries;
use Symfony\Component\Process\Process;

require dirname(__DIR__) . '/vendor/autoload.php';
require_once dirname(__DIR__) . '/src/util/BuildPayload.php';

function consumerCommand(array $arguments, ?string $input = null): string
{
    $process = new Process($arguments);
    $process->setInput($input);
    $process->setTimeout(120);
    $process->mustRun();
    return $process->getOutput();
}

function consumerCheck(bool $condition, string $message): void
{
    if (!$condition) {
        throw new RuntimeException($message);
    }
}

function consumerRemove(string $path): void
{
    if (!is_dir($path) || is_link($path)) {
        unlink($path);
        return;
    }
    foreach (new DirectoryIterator($path) as $entry) {
        if (!$entry->isDot()) {
            consumerRemove($entry->getPathname());
        }
    }
    rmdir($path);
}

$root = trim(consumerCommand(['mktemp', '-d', '/tmp/php-zts-consumer-metadata-XXXXXX']));
try {
    define('BASE_PATH', dirname(__DIR__));
    define('SPP_PHP_VERSION', '8.6.0RC4');
    define('BUILD_ROOT_PATH', $root . '/buildroot');
    define('BUILD_LIB_PATH', BUILD_ROOT_PATH . '/lib');
    define('BUILD_BIN_PATH', BUILD_ROOT_PATH . '/bin');
    define('DIST_PATH', $root . '/dist');
    define('DIST_RPM_PATH', DIST_PATH . '/rpm');
    define('DIST_DEB_PATH', DIST_PATH . '/deb');
    define('DIST_APK_PATH', DIST_PATH . '/apk');
    define('TEMP_DIR', $root . '/tmp');
    foreach ([BUILD_LIB_PATH, BUILD_BIN_PATH, BUILD_ROOT_PATH . '/licenses', DIST_RPM_PATH, DIST_DEB_PATH, DIST_APK_PATH, TEMP_DIR] as $directory) {
        mkdir($directory, 0755, true);
    }
    $libdir = '/usr/lib/php-zts/lib';
    $manifest = ['schema' => 1, 'linkage' => 'shared', 'architecture' => 'amd64', 'libc' => 'glibc', 'runtime_libdir' => $libdir, 'libraries' => []];
    foreach (['base', 'top', 'dynamic', 'unused'] as $id) {
        $source = $id === 'top' ? 'extern int base(void); int top(void) { return base(); }' : "int {$id}(void) { return 42; }";
        file_put_contents($root . '/' . $id . '.c', $source);
        $relative = 'lib/libfixture-' . $id . '.so.1';
        consumerCommand(['gcc', '-shared', '-nostdlib', '-fPIC', '-Wl,-soname,libfixture-' . $id . '.so.1', '-Wl,--enable-new-dtags,-rpath,' . $libdir,
            '-o', BUILD_ROOT_PATH . '/' . $relative, $root . '/' . $id . '.c', ...($id === 'top' ? ['-L' . BUILD_LIB_PATH, '-l:libfixture-base.so.1'] : [])]);
        preg_match_all('/\(NEEDED\).*\[([^\]]+)\]/', consumerCommand(['readelf', '--wide', '--dynamic', BUILD_ROOT_PATH . '/' . $relative]), $needed);
        file_put_contents(BUILD_ROOT_PATH . '/licenses/' . $id, 'MIT fixture');
        $manifest['libraries'][$id] = ['version' => '1.0', 'source_hash' => str_repeat('a', 64), 'license' => 'MIT', 'license_files' => ['licenses/' . $id],
            'files' => [$relative], 'depends' => [], 'dlopen_dependencies' => $id === 'top' ? ['dynamic'] : [], 'abi_epoch' => 1,
            'elf' => [$relative => ['soname' => 'libfixture-' . $id . '.so.1', 'needed' => $needed[1], 'runpath' => [$libdir]]]];
    }
    file_put_contents(BUILD_ROOT_PATH . '/shared-libraries.json', json_encode($manifest, JSON_THROW_ON_ERROR));
    $libraries = new SharedLibraries(BUILD_ROOT_PATH);
    foreach (['base', 'top', 'dynamic', 'unused'] as $id) {
        foreach (['rpm', 'deb', 'apk'] as $type) {
            $libraries->setPackageVersion($id, $type, $type === 'apk' ? '1.0-r1' : '1.0-1');
        }
    }
    (new ReflectionProperty(CreatePackages::class, 'sharedLibraries'))->setValue(null, $libraries);
    (new ReflectionProperty(CreatePackages::class, 'versionArch'))->setValue(null, ['8.6.0~rc4', 'x86_64']);
    (new ReflectionProperty(CreatePackages::class, 'iterationOverride'))->setValue(null, '1');
    file_put_contents($root . '/php.c', 'extern int top(void); int php_fixture(void) { return top(); }');
    consumerCommand(['gcc', '-shared', '-nostdlib', '-fPIC', '-Wl,-soname,libphp-zts-86.so', '-Wl,--enable-new-dtags,-rpath,' . $libdir,
        '-o', BUILD_LIB_PATH . '/libphp-zts-86.so', $root . '/php.c', '-L' . BUILD_LIB_PATH, '-l:libfixture-top.so.1']);
    file_put_contents($root . '/franken.c', 'extern int php_fixture(void); int franken_fixture(void) { return php_fixture(); }');
    consumerCommand(['gcc', '-shared', '-nostdlib', '-fPIC', '-Wl,--enable-new-dtags,-rpath,' . $libdir, '-o', BUILD_BIN_PATH . '/frankenphp',
        $root . '/franken.c', '-L' . BUILD_LIB_PATH, '-l:libphp-zts-86.so']);
    $files = [BUILD_BIN_PATH . '/frankenphp' => '/usr/bin/frankenphp'];
    $metadataFiles = CreatePackages::consumerMetadataFiles('frankenphp', $files);
    $metadata = json_decode(file_get_contents(array_key_first($metadataFiles)), true, flags: JSON_THROW_ON_ERROR);
    consumerCheck($metadata === ['schema' => 1, 'name' => 'frankenphp', 'php_stream' => '8.6', 'architecture' => 'x86_64', 'libraries' => ['base', 'dynamic', 'top']], 'FrankenPHP lost linked libphp/transitive/dlopen dependencies or inherited unrelated libraries');
    $empty = CreatePackages::consumerMetadataFiles('php-zts-devel', []);
    consumerCheck(json_decode(file_get_contents(array_key_first($empty)), true)['libraries'] === [], 'Empty PHP consumer dependencies were not explicit');
    $package = new class($files) implements package {
        public function __construct(private array $files) {}
        public function getName(): string { return 'php-zts-fixture_module'; }
        public function getFpmConfig(): array { return ['files' => $this->files]; }
        public function getDebuginfoFpmConfig(): array { return ['files' => []]; }
        public function getFpmExtraArgs(): array { return []; }
        public function getLicense(): string { return 'MIT'; }
        public function getDescription(): string { return 'Consumer metadata fixture'; }
    };
    $configMethod = new ReflectionMethod(CreatePackages::class, 'packageConfig');
    $build = new ReflectionMethod(CreatePackages::class, 'createPackageWithFpm');
    foreach (['rpm', 'deb', 'apk'] as $type) {
        (new ReflectionProperty(CreatePackages::class, 'packageType'))->setValue(null, $type);
        $config = $configMethod->invoke(null, $package, false);
        $name = $type === 'deb' ? 'php-zts-fixture-module' : $package->getName();
        $destination = '/usr/share/php-zts/consumers/' . $name . '.json';
        consumerCheck(in_array($destination, $config['files'], true), 'PHP consumer metadata was not added before archive creation');
        consumerCheck($configMethod->invoke(null, $package, true)['files'] === [], 'Runtime consumer metadata leaked into debuginfo');
        ob_start();
        try {
            $build->invoke(null, $package, '8.6.0~rc4', 'x86_64');
        } finally {
            ob_end_clean();
        }
        $archive = glob(DIST_PATH . '/' . $type . '/*.' . $type)[0];
        if ($type === 'rpm') {
            $content = consumerCommand(['cpio', '--quiet', '-i', '--to-stdout', '.' . $destination], consumerCommand(['rpm2cpio', $archive]));
            consumerCheck(trim(consumerCommand(['rpm', '-qp', '--qf', '%{NAME}', $archive])) === $name, 'RPM consumer name differs from metadata');
        } elseif ($type === 'deb') {
            $member = current(array_filter(explode("\n", consumerCommand(['ar', 't', $archive])), static fn ($value) => str_starts_with($value, 'data.tar')));
            $content = consumerCommand(['tar', '-xOJf', '-', '.' . $destination], consumerCommand(['ar', 'p', $archive, $member]));
        } else {
            $content = consumerCommand(['tar', '--ignore-zeros', '-xOf', '-', ltrim($destination, '/')], consumerCommand(['gzip', '-dc', $archive]));
        }
        $embedded = json_decode($content, true, flags: JSON_THROW_ON_ERROR);
        consumerCheck($embedded['name'] === $name && $embedded['libraries'] === ['base', 'dynamic', 'top'], 'Actual native archive lost consumer metadata');
    }
    echo "Passed native RPM/DEB/APK embedded consumer metadata, normalized names, linked libphp/transitive/dlopen dependencies, empty consumers and debug exclusion.\n";
} finally {
    consumerRemove($root);
}
