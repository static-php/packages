<?php

declare(strict_types=1);

namespace staticphp\hook;

use RecursiveDirectoryIterator;
use RecursiveIteratorIterator;
use StaticPHP\Attribute\Package\AfterStage;
use StaticPHP\Package\LibraryPackage;
use StaticPHP\Package\Package;
use StaticPHP\Package\PhpExtensionPackage;
use StaticPHP\Package\TargetPackage;
use StaticPHP\Util\FileSystem;

class CleanupSources
{
    #[AfterStage('*', 'build')]
    public function afterBuild(Package $package): void
    {
        if (!getenv('CI')) {
            return;
        }
        if ($package instanceof TargetPackage) {
            // Targets (e.g. php, frankenphp) keep their source; shared ext builds reference php-src/ext/*.
            return;
        }
        if ($package instanceof PhpExtensionPackage) {
            $this->cleanupExtension($package);
            return;
        }
        if ($package instanceof LibraryPackage) {
            $this->cleanupLibrary($package);
        }
    }

    private function cleanupLibrary(LibraryPackage $package): void
    {
        $source = $package->getSourceDir();
        if (is_dir($source)) {
            echo "Cleaning up source directory for library: {$package->getName()}\n";
            FileSystem::removeDir($source);
        }
    }

    private function cleanupExtension(PhpExtensionPackage $package): void
    {
        try {
            $source = $package->getSourceDir();
        } catch (\Throwable) {
            return;
        }
        if (!is_dir($source)) {
            return;
        }
        $phpSource = defined('SOURCE_PATH') ? realpath(SOURCE_PATH . '/php-src') : false;
        $resolved = realpath($source);
        if ($phpSource !== false && $resolved !== false && ($resolved === $phpSource || str_starts_with($resolved, $phpSource . '/'))) {
            return;
        }
        echo "Cleaning up source directory for shared extension: {$package->getExtensionName()} (preserving license files)\n";

        $iterator = new RecursiveIteratorIterator(
            new RecursiveDirectoryIterator($source, RecursiveDirectoryIterator::SKIP_DOTS),
            RecursiveIteratorIterator::CHILD_FIRST,
        );
        foreach ($iterator as $fileInfo) {
            $filename = $fileInfo->getFilename();
            $path = $fileInfo->getPathname();
            // Preserve COPYING/LICENSE/LICENCE/README/NOTICE/AUTHORS/CREDITS/PATENTS/CONTRIBUTORS files
            if (preg_match('/^(COPYING|LICENSE|LICENCE|README|NOTICE|AUTHORS?|CREDITS?|PATENTS?|CONTRIBUTORS?)/i', $filename)) {
                continue;
            }
            if ($fileInfo->isDir()) {
                @rmdir($path);
            } else {
                @unlink($path);
            }
        }
    }
}
