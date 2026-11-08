<?php

declare(strict_types=1);

namespace staticphp\hook;

use StaticPHP\Attribute\Package\BeforeStage;
use StaticPHP\Package\PackageInstaller;
use StaticPHP\Package\PhpExtensionPackage;
use StaticPHP\Util\FileSystem;
use staticphp\util\ConsumerSnapshot;

require_once dirname(__DIR__) . '/util/ConsumerSnapshot.php';

final class PrepareReusedExtensionSources
{
    #[BeforeStage('ext-decimal', 'phpizeForUnix')]
    #[BeforeStage('ext-dio', 'phpizeForUnix')]
    #[BeforeStage('ext-gd', 'phpizeForUnix')]
    #[BeforeStage('ext-grpc', 'phpizeForUnix')]
    #[BeforeStage('ext-maxminddb', 'phpizeForUnix')]
    #[BeforeStage('ext-memcache', 'phpizeForUnix')]
    #[BeforeStage('ext-mongodb', 'phpizeForUnix')]
    #[BeforeStage('ext-parallel', 'phpizeForUnix')]
    #[BeforeStage('ext-pdo_odbc', 'phpizeForUnix')]
    #[BeforeStage('ext-pdo_sqlsrv', 'phpizeForUnix')]
    #[BeforeStage('ext-rdkafka', 'phpizeForUnix')]
    #[BeforeStage('ext-simdjson', 'phpizeForUnix')]
    #[BeforeStage('ext-spx', 'phpizeForUnix')]
    #[BeforeStage('ext-swoole', 'phpizeForUnix')]
    #[BeforeStage('ext-trader', 'phpizeForUnix')]
    #[BeforeStage('ext-yac', 'phpizeForUnix')]
    public function prepareBeforePhpize(PhpExtensionPackage $extension, PackageInstaller $installer): void
    {
        $snapshot = ConsumerSnapshot::active(BUILD_ROOT_PATH);
        if ($snapshot === null || !$snapshot->isSelected($extension->getExtensionName())) {
            return;
        }
        self::prepare($extension, $installer);
    }

    public static function prepare(PhpExtensionPackage $extension, PackageInstaller $installer): void
    {
        // Fresh consumer sources have not seen the PHP core stages skipped by a verified reuse build.
        switch ($extension->getExtensionName()) {
            case 'decimal':
            case 'dio':
            case 'gd':
            case 'maxminddb':
            case 'memcache':
            case 'parallel':
            case 'pdo_odbc':
                $extension->patchBeforeBuildconf();
                break;
            case 'grpc':
                $extension->patchBeforeBuildconf();
                FileSystem::replaceFileStr($extension->getSourceDir() . '/config.m4', '@@build_lib_path@@', BUILD_LIB_PATH);
                break;
            case 'mongodb':
                $extension->patchBeforeBuildconfUnix();
                break;
            case 'pdo_sqlsrv':
                (new \Package\Extension\pdo_sqlsrv())->patchDirectoryStructureForPhp85();
                break;
            case 'rdkafka':
                $extension->patchBeforeBuildconf();
                $extension->patchBeforeMake();
                break;
            case 'simdjson':
            case 'trader':
            case 'yac':
                $extension->patchBeforeBuildconf($installer);
                break;
            case 'spx':
                $extension->patchBeforeBuildconf();
                $extension->patchBeforeConfigure();
                break;
            case 'swoole':
                $extension->patchBeforeMake();
                $extension->patchBeforeMake2();
                $extension->patchBeforeMake3();
                break;
            case 'xhprof':
                $extension->patchBeforeBuildconf($installer);
                FileSystem::replaceFileStr(
                    $extension->getSourceDir() . '/extension/config.m4',
                    'if test -f $phpincludedir/ext/pcre/php_pcre.h; then',
                    'if test -f $abs_srcdir/ext/pcre/php_pcre.h; then'
                );
                break;
        }
    }
}
