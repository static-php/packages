<?php

declare(strict_types=1);

namespace staticphp\hook;

use Package\Library\ngtcp2 as Ngtcp2Recipe;
use StaticPHP\Attribute\Package\BeforeStage;
use StaticPHP\Attribute\PatchDescription;
use StaticPHP\Exception\WrongUsageException;
use StaticPHP\Package\LibraryPackage;
use StaticPHP\Util\FileSystem;
use StaticPHP\Util\SharedLibraryUtil;

final class Ngtcp2
{
    #[BeforeStage('ngtcp2', [Ngtcp2Recipe::class, 'build'], 'ngtcp2')]
    #[PatchDescription('Retain OpenSSL dependencies in the shared ngtcp2 crypto adapter')]
    public function patchSharedOpenSsl(LibraryPackage $library): bool
    {
        if (!SharedLibraryUtil::buildsShared($library)) {
            return false;
        }
        $path = $library->getSourceDir() . '/crypto/ossl/Makefile.in';
        $source = FileSystem::readFile($path);
        // Libtool discards SPC's absolute .so arguments when linking this adapter.
        $links = '-L$(libdir) -lssl -lcrypto';
        if (str_contains($source, $links)) {
            return false;
        }
        if (!str_contains($source, '@OPENSSL_LIBS@')) {
            throw new WrongUsageException('ngtcp2: OpenSSL link template no longer matches');
        }
        FileSystem::writeFile($path, str_replace('@OPENSSL_LIBS@', $links, $source));
        return true;
    }
}
