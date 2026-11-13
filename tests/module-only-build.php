<?php

declare(strict_types=1);

use staticphp\util\ModuleOnlyBuild;
use Symfony\Component\Process\Process;

require dirname(__DIR__) . '/vendor/autoload.php';
require dirname(__DIR__) . '/src/util/ModuleOnlyBuild.php';

function moduleWrite(string $path, string $content): void
{
    if (!is_dir(dirname($path))) {
        mkdir(dirname($path), 0755, true);
    }
    file_put_contents($path, $content);
}

function moduleRemove(string $path): void
{
    if (is_link($path) || !is_dir($path)) {
        unlink($path);
        return;
    }
    foreach (new DirectoryIterator($path) as $file) {
        if (!$file->isDot()) {
            moduleRemove($file->getPathname());
        }
    }
    rmdir($path);
}

$directory = trim((new Process(['mktemp', '-d', sys_get_temp_dir() . '/spp-module-only-XXXXXX']))->mustRun()->getOutput());
try {
    $source = $directory . '/producer/source/php-src';
    $root = $directory . '/producer/buildroot';
    foreach (['main/php_config.h', 'Zend/zend_config.h', 'main/php_version.h', 'main/build-defs.h'] as $header) {
        moduleWrite($root . '/include/php/' . $header, "/* same verified PHP header */\n");
        moduleWrite($source . '/' . $header, "/* same verified PHP header */\n");
    }
    moduleWrite($root . '/bin/php', "verified core bytes\n");
    moduleWrite($root . '/lib/libexample.so', "verified library bytes\n");
    moduleWrite($root . '/modules/soap-zts-86.so', "unselected module bytes\n");
    moduleWrite($root . '/shared-libraries.json', json_encode(['libraries' => ['example' => ['files' => ['lib/libexample.so']]]], JSON_THROW_ON_ERROR));
    moduleWrite($source . '/libtool', "#!/bin/sh\nexit 99\n");
    moduleWrite($source . '/ext/xsl/php_xsl.c', "int selected_module(void) { return 42; }\n");
    $make = <<<'MAKE'
top_srcdir = @SOURCE@
shared_objects_xsl = ext/xsl/php_xsl.lo
all: libphp.la sapi/cli/php
libphp.la sapi/cli/php:
	@echo CORE_TARGET_INVOKED >&2; exit 97
ext/xsl/php_xsl.lo: @SOURCE@/ext/xsl/php_xsl.c
	cc -I$(top_srcdir)/main -fPIC -g -c $< -o $@
ext/xsl/xsl.la: $(shared_objects_xsl) $(XSL_SHARED_DEPENDENCIES)
	mkdir -p ext/xsl/.libs
	cc -shared -o ext/xsl/.libs/xsl-zts-86.so $(shared_objects_xsl)
	printf "dlname='xsl-zts-86.so'\n" > $@
MAKE;
    moduleWrite($source . '/Makefile', str_replace('@SOURCE@', $source, $make));
    ModuleOnlyBuild::capture($source, $root, ['shared-extensions' => 'xsl']);
    $restored = $directory . '/consumer/source/php-src';
    $currentRoot = $directory . '/consumer/buildroot';
    mkdir(dirname($currentRoot), 0755, true);
    (new Process(['cp', '-a', $root, $currentRoot]))->mustRun();
    moduleWrite($restored . '/ext/xsl/php_xsl.c', "#include <build-defs.h>\nint selected_module(void) { return 43; }\n");
    ModuleOnlyBuild::restore($restored, $currentRoot, 'xsl');
    $arguments = ModuleOnlyBuild::command($restored, 'xsl', 2);
    if (end($arguments) !== 'ext/xsl/xsl.la' || !in_array($restored . '/ext/xsl/php_xsl.c', $arguments, true)
        || array_intersect(['all', 'clean', 'build-modules', 'install-modules', 'libphp.la', 'sapi/cli/php'], $arguments)) {
        throw new RuntimeException('Module-only request includes an aggregate/core target');
    }
    $protected = [];
    foreach (['bin/php', 'lib/libexample.so', 'modules/soap-zts-86.so'] as $file) {
        $protected[$file] = hash_file('sha256', $currentRoot . '/' . $file);
    }
    $deploys = 0;
    $deploy = static function (string $input, string $output) use (&$deploys): void {
        copy($input, $output);
        $deploys++;
    };
    ModuleOnlyBuild::build($restored, $currentRoot, 'xsl', 2, [], $deploy);
    $module = $currentRoot . '/modules/xsl-zts-86.so';
    $first = hash_file('sha256', $module);
    moduleWrite($restored . '/ext/xsl/php_xsl.c', "int selected_module(void) { return 44; }\n");
    ModuleOnlyBuild::build($restored, $currentRoot, 'xsl', 2, [], $deploy);
    if ($deploys !== 2 || hash_file('sha256', $module) === $first || !is_link($currentRoot . '/modules/xsl.so')) {
        throw new RuntimeException('Selected module was not rebuilt/deployed on a subsequent request');
    }
    foreach ($protected as $file => $identity) {
        if (hash_file('sha256', $currentRoot . '/' . $file) !== $identity) {
            throw new RuntimeException('Unselected core/library/module changed: ' . $file);
        }
    }
    moduleWrite($restored . '/Makefile', str_replace('$(shared_objects_xsl) $(XSL_SHARED_DEPENDENCIES)', '$(shared_objects_xsl) libphp.la', (string) file_get_contents($restored . '/Makefile')));
    try {
        ModuleOnlyBuild::command($restored, 'xsl', 2);
        throw new RuntimeException('Unexpected core prerequisite was accepted');
    } catch (RuntimeException $error) {
        if (!str_contains($error->getMessage(), 'unexpected dependencies')) {
            throw $error;
        }
    }
    echo "Passed native selected-module compilation, relocated recipe restore, repeated rebuild and unchanged core/library/unselected hashes.\n";
} finally {
    moduleRemove($directory);
}
