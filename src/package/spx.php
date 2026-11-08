<?php

namespace staticphp\package;

use staticphp\extension;
use staticphp\step\CreatePackages;

class spx extends extension
{
    public function getFpmConfig(): array
    {
        $versionedConflicts = CreatePackages::getVersionedConflicts('-spx');
        $assets = BUILD_ROOT_PATH . '/packaging/spx/web-ui';
        if (!is_dir($assets)) {
            $assets = BUILD_ROOT_PATH . '/share/misc/php-spx/assets/web-ui';
        }
        return [
            'config-files' => [
                getConfdir() . '/conf.d/20-spx.ini',
            ],
            'depends' => [
                CreatePackages::getPrefix() . '-cli'
            ],
            'provides' => [],
            'replaces' => $versionedConflicts,
            'conflicts' => $versionedConflicts,
            'files' => [
                BUILD_MODULES_PATH . '/spx' . getSharedLibrarySuffix() . '.so' => getModuledir() . '/spx' . getSharedLibrarySuffix() . '.so',
                $this->getIniPath() => getConfdir() . '/conf.d/20-spx.ini',
                $assets . '/' => '/usr/share/' . CreatePackages::getPrefix() . '/misc/php-spx/assets/web-ui',
            ]
        ];
    }

    public function getLicense(): string
    {
        return 'GPL-3.0';
    }
}
