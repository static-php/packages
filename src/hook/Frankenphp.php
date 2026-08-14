<?php

declare(strict_types=1);

namespace staticphp\hook;

use StaticPHP\Attribute\Package\PatchBeforeBuild;
use StaticPHP\Attribute\Package\Target;
use StaticPHP\Attribute\PatchDescription;
use StaticPHP\Exception\WrongUsageException;
use StaticPHP\Package\TargetPackage;
use StaticPHP\Registry\ArtifactLoader;
use StaticPHP\Util\FileSystem;
use StaticPHP\Util\SourcePatcher;

#[Target('frankenphp')]
class Frankenphp
{
    private const string OLD_OUTPUT_HANDLER = <<<'C'
            if (PG(output_handler) && PG(output_handler)[0]) {
              zval oh;

              ZVAL_STRING(&oh, PG(output_handler));
        C;

    private const string NEW_OUTPUT_HANDLER = <<<'C'
            /* PG(output_handler) is a char* before PHP 8.6, a zend_string* since */
        #if PHP_VERSION_ID < 80600
            if (PG(output_handler) && PG(output_handler)[0]) {
              zval oh;

              ZVAL_STRING(&oh, PG(output_handler));
        #else
            if (PG(output_handler) && ZSTR_LEN(PG(output_handler))) {
              zval oh;

              ZVAL_STR(&oh, zend_string_dup(PG(output_handler), false));
        #endif
        C;

    #[PatchBeforeBuild]
    #[PatchDescription('Adapt frankenphp.c to the PHP 8.6 PG(output_handler) type change (php/frankenphp#2600)')]
    public function patchBeforeBuild(TargetPackage $package): bool
    {
        // PG(output_handler) became a zend_string* in 8.6.0beta1, so PHP_VERSION_ID alone cannot
        // decide this — 8.6.0alpha1..3 are also 80600 but still declare it char*. Read the type
        // out of the php-src being built instead.
        $globals = FileSystem::readFile(ArtifactLoader::getArtifactInstance('php-src')->getSourceDir() . '/main/php_globals.h');
        if (preg_match('/^\s*(char|zend_string)\s*\*\s*output_handler;/m', $globals, $match) !== 1) {
            throw new WrongUsageException('frankenphp: cannot determine the PG(output_handler) type from php_globals.h');
        }
        if ($match[1] === 'char') {
            return false;
        }

        $file = $package->getSourceDir() . '/frankenphp.c';
        $source = FileSystem::readFile($file);

        if (str_contains($source, 'ZSTR_LEN(PG(output_handler))')) {
            return false;
        }

        if (!str_contains($source, self::OLD_OUTPUT_HANDLER)) {
            throw new WrongUsageException("frankenphp: upstream frankenphp.c no longer matches the PHP 8.6 patch, missing:\n" . self::OLD_OUTPUT_HANDLER);
        }

        FileSystem::writeFile($file, str_replace(self::OLD_OUTPUT_HANDLER, self::NEW_OUTPUT_HANDLER, $source));

        return true;
    }

    #[PatchBeforeBuild]
    #[PatchDescription('Drop frankenphp\'s CLI emulation in favour of php-src do_php_cli() (php/frankenphp#1757)')]
    public function patchCliBeforeBuild(TargetPackage $package): bool
    {
        return SourcePatcher::patchFile(dirname(__DIR__, 2) . '/config/patches/frankenphp-1757.patch', $package->getSourceDir());
    }
}
