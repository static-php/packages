<?php

namespace staticphp\package;

use staticphp\package;
use staticphp\step\CreatePackages;

class cgi implements package
{
    public function getName(): string
    {
        return CreatePackages::getPrefix() . '-cgi';
    }

    public function getFpmConfig(): array
    {
        $versionedConflicts = CreatePackages::getVersionedConflicts('-cgi');
        return [
            'depends' => [
                CreatePackages::getPrefix() . '-cli',
            ],
            'recommends' => CreatePackages::getSapiRecommendations(),
            'provides' => [],
            'replaces' => $versionedConflicts,
            'conflicts' => $versionedConflicts,
            'files' => [
                BUILD_BIN_PATH . '/php-cgi' => '/usr/bin/php-cgi' . getBinarySuffix(),
            ]
        ];
    }

    public function getFpmExtraArgs(): array
    {
        return [];
    }

    public function getDebuginfoFpmConfig(): array
    {
        $binarySuffix = getBinarySuffix();
        $src = BUILD_ROOT_PATH . '/debug/php-cgi.debug';
        if (!file_exists($src)) {
            return [];
        }
        return [
            'depends' => [CreatePackages::getPrefix() . '-cgi'],
            'files' => [
                $src => '/usr/lib/debug/usr/bin/php-cgi' . $binarySuffix . '.debug',
            ],
        ];
    }

    public function getLicense(): string
    {
        return 'PHP-3.01';
    }

    public function getDescription(): string
    {
        return 'CGI SAPI for PHP';
    }
}
