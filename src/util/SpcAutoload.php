<?php

declare(strict_types=1);

namespace staticphp\util;

use Composer\Autoload\ClassLoader;
use RuntimeException;

final class SpcAutoload
{
    public static function configure(ClassLoader $loader, ?string $root = null): void
    {
        $root ??= getenv('SPP_SPC_ROOT') ?: null;
        if ($root === null) {
            return;
        }
        $root = realpath($root);
        if ($root === false || !is_dir($root . '/src/StaticPHP') || !is_dir($root . '/src/Package')) {
            throw new RuntimeException('SPP_SPC_ROOT must point to a static-php-cli checkout.');
        }
        foreach (array_merge(get_declared_classes(), get_declared_interfaces(), get_declared_traits()) as $class) {
            if (self::namespaceDirectory($class) !== null) {
                $file = (new \ReflectionClass($class))->getFileName();
                if ($file !== false && !str_starts_with($file, $root . '/')) {
                    throw new RuntimeException("SPP_SPC_ROOT must be configured before loading {$class}.");
                }
            }
        }
        $map = [];
        foreach ($loader->getClassMap() as $class => $file) {
            if (($namespace = self::namespaceDirectory($class)) !== null) {
                $relative = '/src/' . $namespace . '/';
                $offset = strpos(str_replace('\\', '/', $file), $relative);
                $map[$class] = $offset === false
                    ? $root . $relative . str_replace('\\', '/', substr($class, strlen($namespace) + 1)) . '.php'
                    : $root . substr(str_replace('\\', '/', $file), $offset);
            }
        }
        $loader->addClassMap($map);
        $loader->setPsr4('StaticPHP\\', $root . '/src/StaticPHP');
        $loader->setPsr4('Package\\', $root . '/src/Package');
        $loader->setClassMapAuthoritative(false);
        $loader->setApcuPrefix(null);
    }

    private static function namespaceDirectory(string $class): ?string
    {
        foreach (['StaticPHP', 'Package'] as $namespace) {
            if (str_starts_with($class, $namespace . '\\')) {
                return $namespace;
            }
        }
        return null;
    }
}
