<?php

declare(strict_types=1);

namespace staticphp\hook;

use StaticPHP\Attribute\Package\BeforeStage;
use StaticPHP\Attribute\Package\Extension;
use StaticPHP\Attribute\PatchDescription;
use StaticPHP\Exception\WrongUsageException;
use StaticPHP\Package\PhpExtensionPackage;
use StaticPHP\Util\FileSystem;

#[Extension('rar')]
class Rar
{
    private const string ANCHOR = "#include <ext/standard/file.h>\n";

    private const string MACRO = <<<'C'

        #if PHP_VERSION_ID >= 80600
        # define rar_stream_log_error(wrapper, options, ...) \
        	php_stream_wrapper_log_error((wrapper), NULL, (options), E_WARNING, true, \
        		PHP_STREAM_EC(Generic), __VA_ARGS__)
        #else
        # define rar_stream_log_error(wrapper, options, ...) \
        	php_stream_wrapper_log_error((wrapper), (options) TSRMLS_CC, __VA_ARGS__)
        #endif

        C;

    private const string OLD_CALL = 'php_stream_wrapper_log_error(wrapper, options TSRMLS_CC,';

    private const string NEW_CALL = 'rar_stream_log_error(wrapper, options,';

    private const string OLD_TIDY = <<<'C'
        static void _rar_stream_tidy_wrapper_error_log(php_stream_wrapper *wrapper TSRMLS_DC)
        {
        	if (wrapper && FG(wrapper_errors)) {
        		zend_hash_str_del(FG(wrapper_errors), (const char*)&wrapper, sizeof wrapper);
        	}
        }
        C;

    private const string NEW_TIDY = <<<'C'
        #if PHP_VERSION_ID >= 80600
        static void _rar_stream_tidy_wrapper_error_log(php_stream_wrapper *wrapper)
        {
        	php_stream_tidy_wrapper_error_log(wrapper);
        }
        #else
        static void _rar_stream_tidy_wrapper_error_log(php_stream_wrapper *wrapper TSRMLS_DC)
        {
        	if (wrapper && FG(wrapper_errors)) {
        		zend_hash_str_del(FG(wrapper_errors), (const char*)&wrapper, sizeof wrapper);
        	}
        }
        #endif
        C;

    #[BeforeStage('ext-rar', 'phpizeForUnix')]
    #[PatchDescription('Adapt rar_stream.c to the PHP 8.6 stream error API')]
    public function patchBeforeBuild(PhpExtensionPackage $ext): bool
    {
        $file = $ext->getSourceDir() . '/rar_stream.c';
        $source = FileSystem::readFile($file);

        if (str_contains($source, self::NEW_CALL)) {
            return false;
        }

        foreach ([self::ANCHOR, self::OLD_CALL, self::OLD_TIDY] as $needle) {
            if (!str_contains($source, $needle)) {
                throw new WrongUsageException("ext-rar: upstream rar_stream.c no longer matches the PHP 8.6 patch, missing:\n{$needle}");
            }
        }

        $source = str_replace(self::ANCHOR, self::ANCHOR . self::MACRO, $source);
        $source = str_replace(self::OLD_CALL, self::NEW_CALL, $source);
        $source = str_replace(self::OLD_TIDY, self::NEW_TIDY, $source);
        FileSystem::writeFile($file, $source);

        return true;
    }
}
