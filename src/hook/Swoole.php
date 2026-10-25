<?php

declare(strict_types=1);

namespace staticphp\hook;

use StaticPHP\Attribute\Package\BeforeStage;
use StaticPHP\Attribute\Package\Extension;
use StaticPHP\Attribute\PatchDescription;
use StaticPHP\Exception\WrongUsageException;
use StaticPHP\Package\PhpExtensionPackage;
use StaticPHP\Util\FileSystem;

#[Extension('swoole')]
final class Swoole
{
    #[BeforeStage('ext-swoole', 'phpizeForUnix')]
    #[PatchDescription('Link the explicit Brotli prefix into the shared Swoole module')]
    public function patchSharedBrotli(PhpExtensionPackage $extension): bool
    {
        if (!$extension->isBuildShared()) {
            return false;
        }
        $path = $extension->getSourceDir() . '/config.m4';
        $source = FileSystem::readFile($path);
        $changed = false;
        foreach (['brotlienc', 'brotlidec'] as $library) {
            $old = 'PHP_ADD_LIBRARY_WITH_PATH(' . $library . ', "${PHP_BROTLI_DIR}/${PHP_LIBDIR}")';
            $new = 'PHP_ADD_LIBRARY_WITH_PATH(' . $library . ', "${PHP_BROTLI_DIR}/${PHP_LIBDIR}", SWOOLE_SHARED_LIBADD)';
            if (str_contains($source, $new)) {
                continue;
            }
            if (substr_count($source, $old) !== 1) {
                throw new WrongUsageException("ext-swoole: {$library} configuration no longer matches the shared-link patch");
            }
            $source = str_replace($old, $new, $source);
            $changed = true;
        }
        if ($changed) {
            FileSystem::writeFile($path, $source);
        }
        return $changed;
    }
}
