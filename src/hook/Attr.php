<?php

declare(strict_types=1);

namespace staticphp\hook;

use Package\Library\attr as AttrRecipe;
use StaticPHP\Attribute\Package\BeforeStage;
use StaticPHP\Attribute\PatchDescription;
use StaticPHP\Package\LibraryPackage;
use StaticPHP\Util\FileSystem;

final class Attr
{
    #[BeforeStage('attr', [AttrRecipe::class, 'build'], 'attr')]
    #[PatchDescription('Serialize overlapping attr header installation targets')]
    public function serializeInstallation(LibraryPackage $library): bool
    {
        $path = $library->getSourceDir() . '/Makefile.am';
        $source = FileSystem::readFile($path);
        if (preg_match('/^\.NOTPARALLEL:[ \t]*$/m', $source)) {
            return false;
        }
        // SPC's explicit header install targets also run through recursive install-data.
        FileSystem::writeFile($path, $source . "\n.NOTPARALLEL:\n");
        return true;
    }
}
