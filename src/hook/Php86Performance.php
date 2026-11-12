<?php

declare(strict_types=1);

namespace staticphp\hook;

use Package\Target\php as PhpTarget;
use StaticPHP\Attribute\Package\BeforeStage;
use StaticPHP\Attribute\PatchDescription;
use StaticPHP\Package\TargetPackage;
use StaticPHP\Util\SourcePatcher;

class Php86Performance
{
    #[BeforeStage('php', [PhpTarget::class, 'buildconfForUnix'], 'php')]
    #[PatchDescription('Apply php-src performance PRs #22729, #22728, #22722 and #23604 to PHP 8.6')]
    public function patchBeforeBuildconf(TargetPackage $package): bool
    {
        $version = PhpTarget::getPHPVersion($package->getSourceDir());
        if (!str_starts_with($version, '8.6.') || version_compare($version, '8.6.0RC2', '<')) {
            return false;
        }

        return SourcePatcher::patchFile(dirname(__DIR__, 2) . '/config/patches/php86-performance-prs.patch', $package->getSourceDir());
    }

    #[BeforeStage('php', [PhpTarget::class, 'buildconfForUnix'], 'php')]
    #[PatchDescription('Select SSE4.1 float formatting at runtime on baseline x86-64')]
    public function patchDtoaBeforeBuildconf(TargetPackage $package): bool
    {
        $version = PhpTarget::getPHPVersion($package->getSourceDir());
        if (!str_starts_with($version, '8.6.') || version_compare($version, '8.6.0RC5', '<')) {
            return false;
        }

        return SourcePatcher::patchFile(dirname(__DIR__, 2) . '/config/patches/php86-dtoa-runtime-trunc.patch', $package->getSourceDir());
    }
}
