<?php

namespace staticphp\package;

use staticphp\package;
use staticphp\step\CreatePackages;

class embed implements package
{
    public function getName(): string
    {
        return CreatePackages::getPrefix() . '-embed';
    }

    public function getFpmConfig(): array
    {
        $sharedLibrarySuffix = getSharedLibrarySuffix(); // e.g., "-zts-85", "-nts-84"
        $libphp = 'libphp' . $sharedLibrarySuffix . '.so';
        $versionedConflicts = CreatePackages::getVersionedConflicts('-embed');
        $provides = [
            $libphp,
            CreatePackages::getPrefix() . '-embedded'
        ];
        if ($this->getName() !== CreatePackages::getPrefix() . '-embed') {
            $provides[] = CreatePackages::getPrefix() . '-embed';
        }
        return [
            'depends' => [
                CreatePackages::getPrefix() . '-cli',
            ],
            'recommends' => CreatePackages::getSapiRecommendations(),
            'provides' => $provides,
            'replaces' => $versionedConflicts,
            'conflicts' => $versionedConflicts,
            'files' => [
                BUILD_LIB_PATH . '/' . $libphp => getLibdir() . '/php-zts/lib/' . $libphp,
                CreatePackages::stageSymlink('php-zts/lib/' . $libphp, $libphp) => getLibdir() . '/' . $libphp,
            ]
        ];
    }

    public function getFpmExtraArgs(): array
    {
        return [];
    }

    public function getDebuginfoFpmConfig(): array
    {
        $sharedLibrarySuffix = getSharedLibrarySuffix();
        // libphp filename with shared library suffix: libphp-zts-85.so, libphp-nts-84.so
        $libName = 'libphp' . $sharedLibrarySuffix . '.so';

        $src = BUILD_ROOT_PATH . '/debug/libphp' . $sharedLibrarySuffix . '.so.debug';
        if (!file_exists($src)) {
            return [];
        }
        $target = '/usr/lib/debug' . getLibdir() . '/php-zts/lib/' . $libName . '.debug';
        return [
            'depends' => [CreatePackages::getPrefix() . '-embed'],
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
        return 'Embed SAPI library for PHP';
    }
}
