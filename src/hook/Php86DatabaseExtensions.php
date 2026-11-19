<?php

declare(strict_types=1);

namespace staticphp\hook;

use Package\Target\php as PhpTarget;
use StaticPHP\Artifact\ArtifactCache;
use StaticPHP\Attribute\Package\BeforeStage;
use StaticPHP\Package\PhpExtensionPackage;
use StaticPHP\Util\SourcePatcher;

final class Php86DatabaseExtensions
{
    #[BeforeStage('ext-mysqlnd_ed25519', 'phpizeForUnix')]
    #[BeforeStage('ext-mysqlnd_parsec', 'phpizeForUnix')]
    #[BeforeStage('ext-pdo_sqlsrv', 'phpizeForUnix')]
    #[BeforeStage('ext-sqlsrv', 'phpizeForUnix')]
    public function patchBeforePhpize(PhpExtensionPackage $extension, ArtifactCache $cache): void
    {
        if (intdiv(PhpTarget::getPHPVersionID(), 100) !== 806) {
            return;
        }
        $directory = dirname(__DIR__, 2) . '/config/patches/php86-extensions';
        $patches = json_decode(file_get_contents($directory . '/patches.json'), true, flags: JSON_THROW_ON_ERROR);
        $name = $extension->getName();
        $version = $cache->getSourceInfo($name)['version'] ?? '';
        foreach ($patches[$name][$version] ?? [] as $patch) {
            SourcePatcher::patchFile($directory . '/' . $patch, $extension->getSourceRoot());
        }
    }
}
