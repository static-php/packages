<?php

declare(strict_types=1);

namespace staticphp\hook;

use Package\Target\php as PhpTarget;
use StaticPHP\Attribute\Package\BeforeStage;
use StaticPHP\Attribute\PatchDescription;
use StaticPHP\Package\TargetPackage;
use StaticPHP\Registry\ArtifactLoader;
use StaticPHP\Util\FileSystem;
use StaticPHP\Util\SourcePatcher;

class FrankenPhp86
{
    #[BeforeStage('frankenphp', [PhpTarget::class, 'buildFrankenphpForUnix'], 'frankenphp')]
    #[PatchDescription('Adapt FrankenPHP output handlers to the PHP 8.6 zend_string API')]
    public function patchBeforeBuild(TargetPackage $package): bool
    {
        $phpSource = ArtifactLoader::getArtifactInstance('php-src')->getSourceDir();
        if (PhpTarget::getPHPVersionID($phpSource) < 80600
            || !preg_match('/\bzend_string\s*\*\s*output_handler\s*;/', FileSystem::readFile($phpSource . '/main/php_globals.h'))) {
            return false;
        }

        return SourcePatcher::patchFile(dirname(__DIR__, 2) . '/config/patches/frankenphp-php86-output-handler.patch', $package->getSourceDir());
    }
}
