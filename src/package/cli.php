<?php

namespace staticphp\package;

use staticphp\package;
use staticphp\CraftConfig;
use staticphp\step\CreatePackages;
use staticphp\util\TwigRenderer;

class cli implements package
{
    public function getName(): string
    {
        return CreatePackages::getPrefix() . '-cli';
    }

    public function getFpmConfig(): array
    {
        $config = CraftConfig::getInstance();
        $staticExtensions = $config->getStaticExtensions();
        $prefix = CreatePackages::getPrefix();

        $contents = file_get_contents(INI_PATH . '/php.ini');
        $contents = str_replace('@libdir@', getPhpLibdir(), $contents);
        $contents = str_replace('@varlib@', getVarLibdir(), $contents);
        // Replace ALL hardcoded /etc/php* paths with prefix-based conf dir
        $contents = preg_replace('#/etc/php[^/]*#', getConfdir(), $contents);
        file_put_contents(TEMP_DIR . '/php.ini', $contents);
        $provides = [$prefix];
        $versionedConflicts = CreatePackages::getVersionedConflicts('-cli');
        $replaces = $versionedConflicts;
        $conflicts = $versionedConflicts;
        $configFiles = [
            getConfdir(),
            getConfdir() . '/php.ini'
        ];
        // Generate phpmod script from template (works as both phpenmod and phpdismod)
        $binarySuffix = getBinarySuffix();
        $phpmodPath = TEMP_DIR . '/phpenmod' . $binarySuffix;
        $phpmodPath2 = TEMP_DIR . '/phpdismod' . $binarySuffix;

        $phpmodContents = TwigRenderer::render('phpmod.twig', [
            'binary_suffix' => $binarySuffix,
            'confdir' => getConfdir(),
        ]);
        file_put_contents($phpmodPath, $phpmodContents);
        file_put_contents($phpmodPath2, $phpmodContents);
        chmod($phpmodPath, 0755);
        chmod($phpmodPath2, 0755);

        $files = [
            TEMP_DIR . '/php.ini' => getConfdir() . '/php.ini',
            BUILD_BIN_PATH . '/php' => '/usr/bin/php' . getBinarySuffix(),
            $phpmodPath => '/usr/sbin/phpenmod' . $binarySuffix,
            $phpmodPath2 => '/usr/sbin/phpdismod' . $binarySuffix,
        ];

        foreach ($staticExtensions as $ext) {
            $provides[] = CreatePackages::getPrefix() . "-{$ext}";
            $replaces[] = CreatePackages::getPrefix() . "-{$ext}";
            $conflicts[] = CreatePackages::getPrefix() . "-{$ext}";

            // Add .ini files for statically compiled extensions
            $iniFile = INI_PATH . "/extension/{$ext}.ini";
            if (file_exists($iniFile)) {
                // Process the .ini file to replace ALL hardcoded php paths with prefix-based paths
                $iniContents = TwigRenderer::renderFile($iniFile, [
                    'type' => defined('SPP_TYPE') ? SPP_TYPE : 'rpm',
                    'binary_suffix' => getBinarySuffix(),
                    'shared_library_suffix' => getSharedLibrarySuffix(),
                    'is_shared' => false, // These are static extensions
                ]);

                $iniContents = str_replace('@varlibdir@', getVarLibdir(), $iniContents);
                $iniContents = preg_replace(
                    [
                        '#/usr/share/php[^/]*/#',
                        '#/usr/local/share/php[^/]*/#',
                        '#/var/lib/php[^/]*/#',
                    ],
                    [
                        getSharedir() . '/',
                        '/usr/local/share/' . $prefix . '/',
                        getVarLibdir() . '/',
                    ],
                    $iniContents
                );
                $tempIniPath = TEMP_DIR . "/{$ext}.ini";
                file_put_contents($tempIniPath, $iniContents);

                $files[$tempIniPath] = getConfdir() . "/conf.d/{$ext}.ini";
                $configFiles[] = getConfdir() . "/conf.d/{$ext}.ini";
            }
        }

        if (!file_exists(BUILD_ROOT_PATH . '/license/LICENSE')) {
            copy(BASE_PATH . '/LICENSE', BUILD_ROOT_PATH . '/license/LICENSE');
        }
        $files[BUILD_ROOT_PATH . '/license/'] = '/usr/share/licenses/' . CreatePackages::getPrefix() . '/';

        return [
            'config-files' => $configFiles,
            'empty_directories' => [
                getSharedir() . '/preload',
                getVarLibdir() . '/session',
                getVarLibdir() . '/wsdlcache',
                getVarLibdir() . '/opcache',
            ],
            'directories' => [
                getSharedir() . '/preload',
                getVarLibdir() . '/session',
                getVarLibdir() . '/wsdlcache',
                getVarLibdir() . '/opcache',
            ],
            'rpm_attrs' => [
                '0770,root,frankenphp:' . getVarLibdir() . '/session',
                '0770,root,frankenphp:' . getVarLibdir() . '/wsdlcache',
                '0770,root,frankenphp:' . getVarLibdir() . '/opcache',
            ],
            'apk_file_info' => [
                getVarLibdir() . '/session' => ['mode' => '0770', 'owner' => 'root', 'group' => 'frankenphp'],
                getVarLibdir() . '/wsdlcache' => ['mode' => '0770', 'owner' => 'root', 'group' => 'frankenphp'],
                getVarLibdir() . '/opcache' => ['mode' => '0770', 'owner' => 'root', 'group' => 'frankenphp'],
            ],
            'provides' => $provides,
            'recommends' => [$prefix . '-fileinfo'],
            'replaces' => $replaces,
            'conflicts' => $conflicts,
            'files' => $files
        ];
    }

    public function getFpmExtraArgs(): array
    {
        $binarySuffix = getBinarySuffix();

        $addGroupCmd = SPP_TYPE === 'apk'
            ? 'addgroup -S'
            : 'groupadd -r';
        $beforeInstallScript = <<<BASH
#!/bin/sh
# Create frankenphp group if it doesn't exist
if ! getent group frankenphp > /dev/null 2>&1; then
    {$addGroupCmd} frankenphp
fi
BASH;
        $afterInstallScript = <<<BASH
#!/bin/sh
if [ ! -e /usr/bin/php ]; then
    ln -sf /usr/bin/php{$binarySuffix} /usr/bin/php
fi
BASH;
        $afterRemoveScript = <<<BASH
#!/bin/sh
if [ -L /usr/bin/php ] && [ "\$(readlink /usr/bin/php)" = "/usr/bin/php{$binarySuffix}" ]; then
    rm -f /usr/bin/php
fi
BASH;

        file_put_contents(TEMP_DIR . '/cli-before-install.sh', $beforeInstallScript);
        file_put_contents(TEMP_DIR . '/cli-after-install.sh', $afterInstallScript);
        file_put_contents(TEMP_DIR . '/cli-after-remove.sh', $afterRemoveScript);
        chmod(TEMP_DIR . '/cli-before-install.sh', 0755);
        chmod(TEMP_DIR . '/cli-after-install.sh', 0755);
        chmod(TEMP_DIR . '/cli-after-remove.sh', 0755);

        return [
            '--before-install', TEMP_DIR . '/cli-before-install.sh',
            '--after-install', TEMP_DIR . '/cli-after-install.sh',
            '--after-remove', TEMP_DIR . '/cli-after-remove.sh'
        ];
    }

    public function getDebExtraArgs(): array
    {
        $binarySuffix = getBinarySuffix();
        $varLibDir = getVarLibdir();

        $this->getFpmExtraArgs();

        $debAfterInstallScript = <<<BASH
#!/bin/sh
set -e
if [ ! -e /usr/bin/php ]; then
    ln -sf /usr/bin/php{$binarySuffix} /usr/bin/php
fi

if getent group frankenphp > /dev/null 2>&1; then
    chgrp frankenphp "{$varLibDir}/session"
    chmod 770 "{$varLibDir}/session"
    chgrp frankenphp "{$varLibDir}/wsdlcache"
    chmod 770 "{$varLibDir}/wsdlcache"
    chgrp frankenphp "{$varLibDir}/opcache"
    chmod 770 "{$varLibDir}/opcache"
fi
BASH;

        file_put_contents(TEMP_DIR . '/cli-deb-after-install.sh', $debAfterInstallScript);
        chmod(TEMP_DIR . '/cli-deb-after-install.sh', 0755);

        return [
            '--before-install', TEMP_DIR . '/cli-before-install.sh',
            '--after-install', TEMP_DIR . '/cli-deb-after-install.sh',
            '--after-remove', TEMP_DIR . '/cli-after-remove.sh'
        ];
    }

    public function getDebuginfoFpmConfig(): array
    {
        $binarySuffix = getBinarySuffix();
        $src = BUILD_ROOT_PATH . '/debug/php.debug';
        if (!file_exists($src)) {
            return [];
        }
        $target = '/usr/lib/debug/usr/bin/php' . $binarySuffix . '.debug';
        return [
            'depends' => [CreatePackages::getPrefix() . '-cli'],
            'files' => [
                $src => $target,
            ],
        ];
    }

    public function getLicense(): string
    {
        return 'PHP-3.01';
    }

    public function getDescription(): string
    {
        return 'CLI SAPI for PHP';
    }
}
