<?php

declare(strict_types=1);

namespace staticphp\hook;

use StaticPHP\Attribute\Package\BeforeStage;
use StaticPHP\Attribute\Package\Extension;
use StaticPHP\Attribute\PatchDescription;
use StaticPHP\Exception\WrongUsageException;
use StaticPHP\Package\PhpExtensionPackage;
use StaticPHP\Util\FileSystem;

#[Extension('ds')]
class Ds
{
    private const string OLD = 'zend_parse_parameter(ZEND_PARSE_PARAMS_QUIET, 1, offset, "l", &index) == FAILURE';
    private const string NEW = '!zend_parse_arg_long(offset, &index, NULL, false, 1)';

    #[BeforeStage('ext-ds', 'phpizeForUnix')]
    #[PatchDescription('Use the supported Zend integer parser for sequence offsets')]
    public function patchBeforeBuild(PhpExtensionPackage $extension): bool
    {
        $file = $extension->getSourceDir() . '/src/php/handlers/php_seq_handlers.c';
        $source = FileSystem::readFile($file);
        if (str_contains($source, self::NEW) || !str_contains($source, 'zend_parse_parameter(')) {
            return false;
        }
        if (substr_count($source, self::OLD) !== 1) {
            throw new WrongUsageException('ext-ds: sequence integer parser no longer matches the PHP 8.6 patch');
        }
        FileSystem::writeFile($file, str_replace(self::OLD, self::NEW, $source));
        return true;
    }
}
