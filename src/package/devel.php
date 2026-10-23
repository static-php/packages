<?php

namespace staticphp\package;

use staticphp\package;
use staticphp\step\CreatePackages;

class devel implements package
{
    public function getName(): string
    {
        return CreatePackages::getPrefix() . '-devel';
    }
    public function getFpmConfig(): array
    {
        $phpConfigPath = BUILD_BIN_PATH . '/php-config';
        $modifiedPhpConfigPath = TEMP_DIR . '/php-config';

        $phpConfigContent = file_get_contents($phpConfigPath);

        $builtDir = BASE_PATH . '/buildroot';
        $phpConfigContent = str_replace([$builtDir, '/app/buildroot', BUILD_ROOT_PATH], '/usr', $phpConfigContent);

        $binarySuffix = getBinarySuffix();
        $sharedLibrarySuffix = getSharedLibrarySuffix();
        $phpConfigContent = preg_replace(
            [
                '/^prefix=.*$/m',
                '/^ldflags=.*$/m',
                '/^libs=.*$/m',
                '/^program_prefix=.*$/m',
                '/^program_suffix=.*$/m',
            ],
            [
                'prefix="/usr"',
                'ldflags="-lpthread"',
                'libs=""',
                'program_prefix=""',
                'program_suffix="' . $binarySuffix . '"',
            ],
            $phpConfigContent
        );

        // Replace all /php paths with versioned paths
        $phpConfigContent = preg_replace('#/php(?!' . preg_quote($binarySuffix, '#') . ')#', '/' . CreatePackages::getPrefix(), $phpConfigContent);
        // libphp filename with shared library suffix: libphp-zts-85.so, libphp-nts-84.so
        $libName = 'libphp' . $sharedLibrarySuffix . '.so';
        $phpConfigContent = str_replace('libphp.so', $libName, $phpConfigContent);
        $phpConfigContent = preg_replace('/^ldflags=.*$/m', 'ldflags="-lpthread -Wl,--enable-new-dtags -Wl,-rpath,' . getLibdir() . '/php-zts/lib"', $phpConfigContent);

        // For APK, sed is in /bin/sed instead of /usr/bin/sed
        if (defined('SPP_TYPE') && SPP_TYPE === 'apk') {
            $phpConfigContent = str_replace('/usr/bin/sed', '/bin/sed', $phpConfigContent);
        }

        file_put_contents($modifiedPhpConfigPath, $phpConfigContent);
        chmod($modifiedPhpConfigPath, 0755);

        $phpizePath = BUILD_BIN_PATH . '/phpize';
        $modifiedPhpizePath = TEMP_DIR . '/phpize';

        $phpizeContent = file_get_contents($phpizePath);

        $phpizeContent = str_replace([$builtDir, '/app/buildroot', BUILD_ROOT_PATH], '/usr', $phpizeContent);

        $phpizeContent = preg_replace(
            [
                '/^prefix=.*$/m',
                '/^datarootdir=.*$/m',
            ],
            [
                'prefix="/usr"',
                'datarootdir="/' . CreatePackages::getPrefix() . '"',
            ],
            $phpizeContent
        );
        $phpizeContent = str_replace(
            [
                'lib/php`',
                '"`eval echo ${prefix}/include`/php"'
            ],
            [
                str_replace('/usr/', '', getPhpLibdir()) . '`',
                '"`eval echo ${prefix}/include`/' . CreatePackages::getPrefix() . '"'
            ],
            $phpizeContent
        );

        // For APK, sed is in /bin/sed instead of /usr/bin/sed
        if (defined('SPP_TYPE') && SPP_TYPE === 'apk') {
            $phpizeContent = str_replace('/usr/bin/sed', '/bin/sed', $phpizeContent);
        }

        file_put_contents($modifiedPhpizePath, $phpizeContent);
        chmod($modifiedPhpizePath, 0755);

        $versionedConflicts = CreatePackages::getVersionedConflicts('-devel');

        // APK needs sed dependency since php-config uses sed
        $depends = [CreatePackages::getPrefix() . '-cli'];
        if (defined('SPP_TYPE') && SPP_TYPE === 'apk') {
            $depends[] = 'sed';
        }

        return [
            'files' => [
                $modifiedPhpConfigPath => '/usr/bin/php-config' . getBinarySuffix(),
                $modifiedPhpizePath => '/usr/bin/phpize' . getBinarySuffix(),
                BUILD_INCLUDE_PATH . '/php/' => '/usr/include/' . CreatePackages::getPrefix(),
                BUILD_LIB_PATH . '/php/build/' => getPhpLibdir() . '/build',
            ],
            'depends' => $depends,
            'provides' => [
                'php-config' . getBinarySuffix(),
                'phpize' . getBinarySuffix(),
            ],
            'replaces' => $versionedConflicts,
            'conflicts' => $versionedConflicts,
        ];
    }

    public function getFpmExtraArgs(): array
    {
        $binarySuffix = getBinarySuffix();
        $sharedLibrarySuffix = getSharedLibrarySuffix();
        $libphpVersioned = 'libphp' . $sharedLibrarySuffix . '.so';  // libphp-zts-85.so
        $libphpWithBinarySuffix = 'libphp' . $binarySuffix . '.so';   // libphp-zts.so
        $libdir = getLibdir();

        // Create TWO symlinks:
        // 1. libphp.so -> libphp-zts-85.so
        // 2. libphp-zts.so -> libphp-zts-85.so
        $afterInstallScript = <<<BASH
#!/bin/sh
# Create libphp.so symlink
if [ ! -e {$libdir}/libphp.so ]; then
    ln -sf {$libdir}/{$libphpVersioned} {$libdir}/libphp.so
fi
if [ ! -e {$libdir}/{$libphpWithBinarySuffix} ]; then
    ln -sf {$libdir}/{$libphpVersioned} {$libdir}/{$libphpWithBinarySuffix}
fi
BASH;
        $afterRemoveScript = <<<BASH
#!/bin/sh
if [ -L {$libdir}/libphp.so ] && [ "\$(readlink {$libdir}/libphp.so)" = "{$libdir}/{$libphpVersioned}" ]; then
    rm -f {$libdir}/libphp.so
fi
if [ -L {$libdir}/{$libphpWithBinarySuffix} ] && [ "\$(readlink {$libdir}/{$libphpWithBinarySuffix})" = "{$libdir}/{$libphpVersioned}" ]; then
    rm -f {$libdir}/{$libphpWithBinarySuffix}
fi
BASH;

        file_put_contents(TEMP_DIR . '/devel-after-install.sh', $afterInstallScript);
        file_put_contents(TEMP_DIR . '/devel-after-remove.sh', $afterRemoveScript);
        chmod(TEMP_DIR . '/devel-after-install.sh', 0755);
        chmod(TEMP_DIR . '/devel-after-remove.sh', 0755);

        return [
            '--after-install', TEMP_DIR . '/devel-after-install.sh',
            '--after-remove', TEMP_DIR . '/devel-after-remove.sh'
        ];
    }

    public function getDebuginfoFpmConfig(): array
    {
        return [];
    }

    public function getLicense(): string
    {
        return 'PHP-3.01';
    }

    public function getDescription(): string
    {
        return 'Development files and tools for PHP (phpize, php-config)';
    }
}
