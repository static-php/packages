<?php

declare(strict_types=1);

namespace staticphp\hook;

use StaticPHP\Attribute\Package\Extension;
use StaticPHP\Attribute\Package\PatchBeforeBuild;
use StaticPHP\Attribute\PatchDescription;
use StaticPHP\Package\PhpExtensionPackage;
use StaticPHP\Util\FileSystem;
use StaticPHP\Util\SourcePatcher;

#[Extension('zip')]
class Zip
{
    #[PatchBeforeBuild]
    #[PatchDescription('Let pecl/zip configure and compile against PHP 8.6')]
    public function patchBeforeBuild(PhpExtensionPackage $ext): bool
    {
        // config.m4 aborts with "PHP version 80600 is not supported yet" for anything past 8.5.
        $config = FileSystem::readFile($ext->getSourceDir() . '/config.m4');
        if (!str_contains($config, 'elif test $PHP_VERSION -lt 80600; then')) {
            return false;
        }

        return SourcePatcher::patchFile(dirname(__DIR__, 2) . '/config/patches/php-zip-8.6.patch', $ext->getSourceDir());
    }
}
