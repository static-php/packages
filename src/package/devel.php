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

        $buildPrefixes = [BASE_PATH . '/buildroot', '/app/buildroot', BUILD_ROOT_PATH];
        if (preg_match('/^prefix=([\'\"])(\/[^\r\n\'\"]+)\1\s*$/m', $phpConfigContent, $match)) {
            $buildPrefixes[] = rtrim($match[2], '/');
        }
        $buildPrefixes = array_values(array_unique(array_filter($buildPrefixes, static fn (string $path): bool => $path !== '' && $path !== '/')));
        usort($buildPrefixes, static fn (string $left, string $right): int => strlen($right) <=> strlen($left));
        $phpConfigContent = str_replace($buildPrefixes, '/usr', $phpConfigContent);

        $binarySuffix = getBinarySuffix();
        $sharedLibrarySuffix = getSharedLibrarySuffix();
        $phpConfigContent = preg_replace(
            [
                '/^prefix=.*$/m',
                '/^ldflags=.*$/m',
                '/^libs=.*$/m',
                '/^program_prefix=.*$/m',
                '/^program_suffix=.*$/m',
                '/^lib_dir=.*$/m',
                '/^extension_dir=.*$/m',
            ],
            [
                'prefix="/usr"',
                'ldflags="-lpthread"',
                'libs=""',
                'program_prefix=""',
                'program_suffix="' . $binarySuffix . '"',
                'lib_dir="' . getLibdir() . '"',
                'extension_dir="' . getModuledir() . '"',
            ],
            $phpConfigContent
        );

        // Replace all /php paths with versioned paths
        $phpConfigContent = preg_replace('#/php(?!' . preg_quote($binarySuffix, '#') . ')#', '/' . CreatePackages::getPrefix(), $phpConfigContent);
        // libphp filename with shared library suffix: libphp-zts-85.so, libphp-nts-84.so
        $libName = 'libphp' . $sharedLibrarySuffix . '.so';
        $phpConfigContent = str_replace('libphp.so', $libName, $phpConfigContent);
        $phpConfigContent = preg_replace('/^ldflags=.*$/m', 'ldflags="-lpthread -L' . getLibdir() . '/php-zts/lib -Wl,--enable-new-dtags -Wl,-rpath,' . CreatePackages::runtimeLibdir() . '"', $phpConfigContent);

        // For APK, sed is in /bin/sed instead of /usr/bin/sed
        if (defined('SPP_TYPE') && SPP_TYPE === 'apk') {
            $phpConfigContent = str_replace('/usr/bin/sed', '/bin/sed', $phpConfigContent);
        }

        file_put_contents($modifiedPhpConfigPath, $phpConfigContent);
        chmod($modifiedPhpConfigPath, 0755);

        $phpizePath = BUILD_BIN_PATH . '/phpize';
        $modifiedPhpizePath = TEMP_DIR . '/phpize';

        $phpizeContent = file_get_contents($phpizePath);

        $phpizeContent = str_replace($buildPrefixes, '/usr', $phpizeContent);

        $phpizeContent = preg_replace(
            [
                '/^prefix=.*$/m',
                '/^datarootdir=.*$/m',
                '/^phpdir=.*$/m',
                '/^includedir=.*$/m',
            ],
            [
                'prefix="/usr"',
                'datarootdir="/' . CreatePackages::getPrefix() . '"',
                'phpdir="' . getPhpLibdir() . '/build"',
                'includedir="/usr/include/' . CreatePackages::getPrefix() . '"',
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
# Replace only aliases belonging to this PHP stream; relative targets survive old removal hooks.
if [ -L {$libdir}/libphp.so ] && [ "\$(readlink {$libdir}/libphp.so)" = "{$libdir}/{$libphpVersioned}" ]; then
    ln -sf {$libphpVersioned} {$libdir}/libphp.so
fi
if [ -L {$libdir}/{$libphpWithBinarySuffix} ] && [ "\$(readlink {$libdir}/{$libphpWithBinarySuffix})" = "{$libdir}/{$libphpVersioned}" ]; then
    ln -sf {$libphpVersioned} {$libdir}/{$libphpWithBinarySuffix}
fi
if [ ! -e {$libdir}/libphp.so ]; then
    ln -sf {$libphpVersioned} {$libdir}/libphp.so
fi
if [ ! -e {$libdir}/{$libphpWithBinarySuffix} ]; then
    ln -sf {$libphpVersioned} {$libdir}/{$libphpWithBinarySuffix}
fi
BASH;
        $afterRemoveScript = <<<BASH
#!/bin/sh
case "\${1:-}" in upgrade|failed-upgrade|1|2) exit 0 ;; esac
if [ -L {$libdir}/libphp.so ] && [ "\$(readlink {$libdir}/libphp.so)" = "{$libphpVersioned}" ]; then
    rm -f {$libdir}/libphp.so
fi
if [ -L {$libdir}/{$libphpWithBinarySuffix} ] && [ "\$(readlink {$libdir}/{$libphpWithBinarySuffix})" = "{$libphpVersioned}" ]; then
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
