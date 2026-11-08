<?php

declare(strict_types=1);

namespace staticphp\hook;

use StaticPHP\Attribute\Package\BeforeStage;
use StaticPHP\Attribute\Package\Extension;
use StaticPHP\Attribute\PatchDescription;
use StaticPHP\Exception\WrongUsageException;
use StaticPHP\Package\PhpExtensionPackage;
use StaticPHP\Util\FileSystem;

#[Extension('ev')]
class Ev
{
    private const string SAVE = <<<'C'
        #if PHP_VERSION_ID >= 80600
        		zend_object *php_ev_saved_exception = EG(exception);
        		EG(exception) = NULL;
        #else
        		zend_exception_save();
        #endif
        C;

    private const string RESTORE = <<<'C'
        #if PHP_VERSION_ID >= 80600
        		if (php_ev_saved_exception) {
        			if (EG(exception)) {
        				zend_exception_set_previous(EG(exception), php_ev_saved_exception);
        			} else {
        				EG(exception) = php_ev_saved_exception;
        			}
        		}
        #else
        		zend_exception_restore();
        #endif
        C;

    #[BeforeStage('ext-ev', 'phpizeForUnix')]
    #[PatchDescription('Preserve callback exceptions with the PHP 8.6 Zend API')]
    public function patchBeforeBuild(PhpExtensionPackage $extension): bool
    {
        $changed = false;
        foreach (['watcher.c', 'periodic.c'] as $name) {
            $file = $extension->getSourceDir() . '/' . $name;
            $source = FileSystem::readFile($file);
            if (str_contains($source, 'php_ev_saved_exception') || !str_contains($source, 'zend_exception_save();')) {
                continue;
            }
            if (substr_count($source, "\t\tzend_exception_save();") !== 1
                || substr_count($source, "\t\tzend_exception_restore();") !== 1) {
                throw new WrongUsageException("ext-ev: {$name} exception handling no longer matches the PHP 8.6 patch");
            }
            $source = str_replace(["\t\tzend_exception_save();", "\t\tzend_exception_restore();"], [self::SAVE, self::RESTORE], $source);
            FileSystem::writeFile($file, $source);
            $changed = true;
        }
        return $changed;
    }
}
