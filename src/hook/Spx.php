<?php

declare(strict_types=1);

namespace staticphp\hook;

use StaticPHP\Attribute\Package\BeforeStage;
use StaticPHP\Package\PhpExtensionPackage;
use StaticPHP\Util\FileSystem;

final class Spx
{
    #[BeforeStage('ext-spx', 'configureForUnix')]
    public function configureSharedExtension(PhpExtensionPackage $extension): void
    {
        if (!str_contains(FileSystem::readFile($extension->getSourceRoot() . '/config.m4'), 'PHP_ARG_WITH(spx-zstd,')) {
            return;
        }
        $sdk = escapeshellarg(BUILD_ROOT_PATH);
        $assets = escapeshellarg(BUILD_ROOT_PATH . '/share/misc/php-spx/assets');
        $extension->addCustomPhpConfigureArgCallback('Linux', static fn (): string =>
            "--enable-spx=shared --with-zlib-dir={$sdk} --with-spx-zstd={$sdk} --with-spx-assets-dir={$assets} CI=true");
    }
}
