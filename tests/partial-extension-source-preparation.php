<?php

declare(strict_types=1);

use Package\Extension\grpc;
use Package\Extension\maxminddb;
use Package\Extension\xhprof;
use StaticPHP\Attribute\Package\BeforeStage;
use StaticPHP\Package\PackageInstaller;
use StaticPHP\Package\TargetPackage;
use staticphp\hook\PrepareReusedExtensionSources;
use Symfony\Component\Process\Process;

require dirname(__DIR__) . '/vendor/autoload.php';
require_once dirname(__DIR__) . '/src/hook/PrepareReusedExtensionSources.php';

trait SourceFixture
{
    public function __construct(string $name, private string $source)
    {
        parent::__construct($name, extension_config: ['display-name' => $name]);
    }

    public function getSourceDir(): string
    {
        return $this->source;
    }
}

final class FixtureGrpc extends grpc { use SourceFixture; }
final class FixtureMaxminddb extends maxminddb { use SourceFixture; }
final class FixtureXhprof extends xhprof { use SourceFixture; }

final class FixturePhp extends TargetPackage
{
    public function __construct(private string $source)
    {
        parent::__construct('php', 'target');
    }

    public function getSourceDir(): string
    {
        return $this->source;
    }
}

final class FixtureInstaller extends PackageInstaller
{
    public function __construct(private TargetPackage $php) {}

    public function getTargetPackage(string $package_name): ?TargetPackage
    {
        return $package_name === 'php' ? $this->php : null;
    }
}

function check(bool $condition, string $message): void
{
    if (!$condition) {
        throw new RuntimeException($message);
    }
}

function identities(string $directory): array
{
    $files = [];
    foreach (new RecursiveIteratorIterator(new RecursiveDirectoryIterator($directory, FilesystemIterator::SKIP_DOTS)) as $file) {
        if ($file->isFile() && !$file->isLink()) {
            $files[substr($file->getPathname(), strlen($directory) + 1)] = hash_file('sha256', $file->getPathname());
        }
    }
    ksort($files);
    return $files;
}

function removeFixture(string $path): void
{
    if (is_link($path) || !is_dir($path)) {
        unlink($path);
        return;
    }
    foreach (new DirectoryIterator($path) as $file) {
        if (!$file->isDot()) {
            removeFixture($file->getPathname());
        }
    }
    rmdir($path);
}

$mktemp = new Process(['mktemp', '-d', '/tmp/spp-partial-source-preparation-XXXXXXXX']);
$mktemp->mustRun();
$temporary = trim($mktemp->getOutput());
define('BUILD_ROOT_PATH', $temporary . '/buildroot');
define('BUILD_LIB_PATH', BUILD_ROOT_PATH . '/lib');
define('BUILD_BIN_PATH', BUILD_ROOT_PATH . '/bin');
define('BUILD_INCLUDE_PATH', BUILD_ROOT_PATH . '/include');
try {
    foreach (['buildroot/lib', 'buildroot/bin', 'buildroot/include', 'php/ext/grpc/src/php/ext/grpc', 'php/ext/maxminddb/ext', 'php/ext/xhprof-src/extension'] as $directory) {
        mkdir($temporary . '/' . $directory, 0755, true);
    }
    file_put_contents(BUILD_ROOT_PATH . '/bin/php', 'immutable core fixture');
    $coreHash = hash_file('sha256', BUILD_ROOT_PATH . '/bin/php');
    $grpcDirectory = $temporary . '/php/ext/grpc';
    foreach (['call.c' => 'zend_exception_get_default(TSRMLS_C),', 'php_grpc.h' => '#include "version.h"', 'php_grpc.c' => '#include "version.h"', 'version.h' => '#define PHP_GRPC_VERSION "1.84.1"', 'channel.c' => 'void grpc_channel(void) {}'] as $file => $content) {
        file_put_contents($grpcDirectory . '/src/php/ext/grpc/' . $file, $content);
    }
    file_put_contents($grpcDirectory . '/config.m4', 'PHP_NEW_EXTENSION(grpc, bundled_grpc.cc, $ext_shared)');
    $grpc = new FixtureGrpc('grpc', $grpcDirectory);
    $installer = new FixtureInstaller(new FixturePhp($temporary . '/php'));
    $before = identities($grpcDirectory);
    (new PrepareReusedExtensionSources())->prepareBeforePhpize($grpc, $installer);
    check(identities($grpcDirectory) === $before, 'A full build unexpectedly replayed core-stage source hooks');
    PrepareReusedExtensionSources::prepare($grpc, $installer);
    $config = (string) file_get_contents($grpcDirectory . '/config.m4');
    check(str_contains($config, 'GRPC_LIBDIR=' . BUILD_LIB_PATH), 'grpc does not link the retained SDK libdir');
    check(str_contains($config, 'PHP_ADD_LIBRARY(grpc,,GRPC_SHARED_LIBADD)'), 'grpc shared provider linkage is missing');
    check(str_contains($config, 'src/php/ext/grpc/channel.c') && !str_contains($config, 'bundled_grpc.cc'), 'grpc still compiles its bundled library');
    check(!str_contains($config, '@@build_lib_path@@'), 'grpc shared provider path was left unresolved');
    check(is_file($grpcDirectory . '/php_grpc.h') && !file_exists($grpcDirectory . '/src/php/ext/grpc/version.h'), 'grpc header conflict preparation was skipped');
    check(str_contains((string) file_get_contents($grpcDirectory . '/src/php/ext/grpc/call.c'), 'zend_ce_exception,'), 'grpc PHP API preparation was skipped');
    $after = identities($grpcDirectory);
    PrepareReusedExtensionSources::prepare($grpc, $installer);
    check(identities($grpcDirectory) === $after, 'grpc source preparation is not idempotent');

    $maxmindDirectory = $temporary . '/php/ext/maxminddb';
    file_put_contents($maxmindDirectory . '/ext/config.m4', 'PHP_NEW_EXTENSION(maxminddb, maxminddb.c, $ext_shared)');
    file_put_contents($maxmindDirectory . '/ext/maxminddb.c', 'void maxminddb(void) {}');
    $maxmind = new FixtureMaxminddb('maxminddb', $maxmindDirectory);
    PrepareReusedExtensionSources::prepare($maxmind, $installer);
    check(is_file($maxmindDirectory . '/config.m4') && is_file($maxmindDirectory . '/maxminddb.c'), 'Fresh maxminddb source was not normalized before phpize');
    $after = identities($maxmindDirectory);
    PrepareReusedExtensionSources::prepare($maxmind, $installer);
    check(identities($maxmindDirectory) === $after, 'maxminddb source normalization is not idempotent');

    $xhprofDirectory = $temporary . '/php/ext/xhprof-src';
    symlink('xhprof-src/extension', $temporary . '/php/ext/xhprof');
    file_put_contents($xhprofDirectory . '/extension/config.m4', 'if test -f $phpincludedir/ext/pcre/php_pcre.h; then');
    $xhprof = new FixtureXhprof('xhprof', $xhprofDirectory);
    PrepareReusedExtensionSources::prepare($xhprof, $installer);
    check(file_get_contents($xhprofDirectory . '/extension/config.m4') === 'if test -f $abs_srcdir/ext/pcre/php_pcre.h; then', 'An existing xhprof symlink hid fresh source configuration');
    $after = identities($xhprofDirectory);
    PrepareReusedExtensionSources::prepare($xhprof, $installer);
    check(identities($xhprofDirectory) === $after, 'xhprof source preparation is not idempotent');
    check(hash_file('sha256', BUILD_ROOT_PATH . '/bin/php') === $coreHash, 'Source preparation changed the retained PHP core');

    $attributes = (new ReflectionMethod(PrepareReusedExtensionSources::class, 'prepareBeforePhpize'))->getAttributes(BeforeStage::class);
    $packages = array_map(static fn (ReflectionAttribute $attribute): string => $attribute->newInstance()->package_name, $attributes);
    foreach (['ext-grpc', 'ext-maxminddb', 'ext-swoole', 'ext-pdo_sqlsrv'] as $package) {
        check(in_array($package, $packages, true), 'Missing real extension stage hook: ' . $package);
    }
    echo "Passed fresh grpc shared linkage, maxminddb layout, xhprof existing-link preparation, idempotency and full-build isolation.\n";
} finally {
    removeFixture($temporary);
}
