<?php

declare(strict_types=1);

namespace staticphp\util;

use RuntimeException;

final class ApkRuntimeAliases
{
    public static function extensions(): array
    {
        static $extensions;
        if ($extensions === null) {
            $path = dirname(__DIR__, 2) . '/config/apk-runtime-aliases.json';
            $config = json_decode((string) file_get_contents($path), true, 512, JSON_THROW_ON_ERROR);
            if (($config['schema'] ?? null) !== 1 || !is_array($config['extensions'] ?? null)) {
                throw new RuntimeException('Invalid APK runtime alias configuration');
            }
            foreach ($config['extensions'] as $extension => $entry) {
                if (!is_string($extension) || !preg_match('/^[a-z][a-z0-9_]*$/D', $extension)
                    || !is_array($entry) || !is_string($entry['suffix'] ?? null)
                    || !preg_match('/^-[a-z][a-z0-9-]*$/D', $entry['suffix'])) {
                    throw new RuntimeException('Invalid APK runtime alias entry');
                }
            }
            $extensions = $config['extensions'];
        }
        return $extensions;
    }

    public static function physicalName(string $logical): string
    {
        $debug = str_ends_with($logical, '-debuginfo') ? '-debuginfo' : '';
        $base = $debug === '' ? $logical : substr($logical, 0, -strlen($debug));
        foreach (self::extensions() as $extension => $entry) {
            if (self::isLogicalBase($base, $extension)) {
                return $base . $entry['suffix'] . $debug;
            }
        }
        return $logical;
    }

    public static function logicalName(string $physical): string
    {
        $debug = str_ends_with($physical, '-debuginfo') ? '-debuginfo' : '';
        $base = $debug === '' ? $physical : substr($physical, 0, -strlen($debug));
        foreach (self::extensions() as $extension => $entry) {
            if (str_ends_with($base, '-' . $extension . $entry['suffix'])) {
                $logical = substr($base, 0, -strlen($entry['suffix']));
                if (self::isLogicalBase($logical, $extension)) {
                    return $logical . $debug;
                }
            }
        }
        return $physical;
    }

    public static function extension(string $name): ?string
    {
        $logical = self::logicalName($name);
        $base = preg_replace('/-debuginfo$/', '', $logical);
        foreach (self::extensions() as $extension => $_) {
            if (self::isLogicalBase($base, $extension)) {
                return $extension;
            }
        }
        return null;
    }

    private static function isLogicalBase(string $name, string $extension): bool
    {
        $suffix = '-' . $extension;
        return str_ends_with($name, $suffix) && !str_starts_with($name, 'php-zts-lib-')
            && preg_match('/^php(?:-[a-z0-9][a-z0-9.-]*)?$/D', substr($name, 0, -strlen($suffix))) === 1;
    }

    public static function metadata(string $name): array
    {
        $extension = self::extension($name);
        return $extension === null ? [] : [
            'logical_name' => self::logicalName($name),
            'extension' => $extension,
            'component' => $extension,
        ];
    }

    public static function relationships(string $physical): array
    {
        $logical = self::logicalName($physical);
        return $logical === $physical ? [] : [
            'apk_provider_priority' => 100,
            'provides_unversioned' => [$logical],
            'replaces' => [$logical],
            'conflicts' => [$logical . '>0'],
        ];
    }

    public static function conflicts(string $prefix, bool $debuginfo = false): array
    {
        $conflicts = [];
        foreach (self::extensions() as $extension => $_) {
            $conflicts[] = $prefix . '-' . $extension . ($debuginfo ? '-debuginfo' : '') . '>0';
        }
        return $conflicts;
    }
}
