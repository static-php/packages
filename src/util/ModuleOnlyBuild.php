<?php

declare(strict_types=1);

namespace staticphp\util;

use RuntimeException;
use Symfony\Component\Process\Process;

final class ModuleOnlyBuild
{
    private const string DIRECTORY = 'packaging/php-module-build';

    public static function capture(string $source, string $root, array $configuration): void
    {
        $source = realpath($source) ?: throw new RuntimeException('Missing configured PHP source tree');
        $root = realpath($root) ?: throw new RuntimeException('Missing PHP buildroot');
        $makefile = (string) file_get_contents($source . '/Makefile');
        preg_match_all('/^shared_objects_([a-zA-Z0-9_]+)\s*=\s*(.+)$/m', $makefile, $matches, PREG_SET_ORDER);
        $shared = $configuration['shared-extensions'] ?? [];
        $shared = is_array($shared) ? $shared : explode(',', $shared);
        $modules = [];
        foreach ($matches as $match) {
            $name = $match[1];
            if (!in_array($name, $shared, true)) {
                continue;
            }
            $directory = $source . '/ext/' . $name;
            $link = is_link($directory) ? readlink($directory) : null;
            if ($link !== null && (str_starts_with($link, '/') || str_contains($link, '..') || !is_dir($directory))) {
                throw new RuntimeException('Unsupported configured module source link: ' . $name);
            }
            $modules[$name] = ['source_link' => $link,
                'configuration_sha256' => is_file($directory . '/config.m4') ? hash_file('sha256', $directory . '/config.m4') : null];
        }
        if ($modules === []) {
            return;
        }
        $destination = $root . '/' . self::DIRECTORY;
        if (!is_dir($destination) && !mkdir($destination, 0755, true) && !is_dir($destination)) {
            throw new RuntimeException('Cannot retain configured PHP module recipes');
        }
        $files = [];
        foreach (['Makefile', 'libtool', 'main/php_config.h', 'Zend/zend_config.h'] as $relative) {
            if (!is_file($source . '/' . $relative) || is_link($source . '/' . $relative)) {
                throw new RuntimeException('Missing configured PHP module build input: ' . $relative);
            }
            $target = $destination . '/' . $relative;
            if (!is_dir(dirname($target)) && !mkdir(dirname($target), 0755, true) && !is_dir(dirname($target))) {
                throw new RuntimeException('Cannot retain PHP module build input');
            }
            if (!copy($source . '/' . $relative, $target) || !chmod($target, fileperms($source . '/' . $relative) & 0777)) {
                throw new RuntimeException('Cannot copy PHP module build input: ' . $relative);
            }
            $files[$relative] = self::identity($target);
        }
        preg_match('/^top_srcdir\s*=\s*(\S+)$/m', $makefile, $producer);
        $producerSource = $producer[1] ?? $source;
        $producerRoot = str_ends_with($producerSource, '/source/php-src')
            ? substr($producerSource, 0, -strlen('/source/php-src')) . '/buildroot' : $root;
        $manifest = ['schema' => 1, 'source' => $producerSource, 'buildroot' => $producerRoot, 'modules' => $modules,
            'php_version_header_sha256' => hash_file('sha256', $root . '/include/php/main/php_version.h'),
            'configuration' => $configuration, 'files' => $files];
        file_put_contents($destination . '/manifest.json', json_encode($manifest, JSON_THROW_ON_ERROR | JSON_PRETTY_PRINT | JSON_UNESCAPED_SLASHES) . "\n");
    }

    public static function supports(string $root, string $name): bool
    {
        return isset(self::manifest($root)['modules'][$name]);
    }

    public static function restore(string $source, string $root, string $name): array
    {
        $source = realpath($source) ?: throw new RuntimeException('Missing PHP module source tree');
        $root = realpath($root) ?: throw new RuntimeException('Missing PHP module buildroot');
        $manifest = self::manifest($root);
        if (!isset($manifest['modules'][$name])) {
            throw new RuntimeException('No retained module-only recipe for ' . $name);
        }
        if (hash_file('sha256', $root . '/include/php/main/php_version.h') !== $manifest['php_version_header_sha256']) {
            throw new RuntimeException('Configured module recipe belongs to another PHP release');
        }
        $assets = $root . '/' . self::DIRECTORY;
        foreach ($manifest['files'] as $relative => $identity) {
            self::safe($relative);
            $input = $assets . '/' . $relative;
            if (!self::matches($input, $identity)) {
                throw new RuntimeException('Retained module build input changed: ' . $relative);
            }
            if (str_ends_with($relative, '.h')) {
                $installed = $root . '/include/php/' . $relative;
                if (!self::matches($installed, $identity)) {
                    throw new RuntimeException('Configured module header differs from the verified PHP core: ' . $relative);
                }
            }
            $content = (string) file_get_contents($input);
            if ($relative === 'Makefile' || $relative === 'libtool') {
                $content = str_replace([$manifest['source'], $manifest['buildroot']], [$source, $root], $content);
            }
            $target = $source . '/' . $relative;
            if (!is_dir(dirname($target)) && !mkdir(dirname($target), 0755, true) && !is_dir(dirname($target))) {
                throw new RuntimeException('Cannot restore configured module build input');
            }
            if (file_put_contents($target, $content) === false || !chmod($target, $identity['mode'])) {
                throw new RuntimeException('Cannot restore configured module build input: ' . $relative);
            }
        }
        if (!copy($root . '/include/php/main/build-defs.h', $source . '/main/build-defs.h')) {
            throw new RuntimeException('Cannot restore PHP module build definitions');
        }
        $moduleDirectory = $source . '/ext/' . $name;
        $link = $manifest['modules'][$name]['source_link'];
        if ($link !== null && !file_exists($moduleDirectory) && !is_link($moduleDirectory)) {
            if (!is_dir(dirname($moduleDirectory) . '/' . $link) || !symlink($link, $moduleDirectory)) {
                throw new RuntimeException('Missing current selected module source: ' . $name);
            }
        }
        if (!is_dir($moduleDirectory) || !str_starts_with(realpath($moduleDirectory), $source . '/ext/')) {
            throw new RuntimeException('Selected module source escapes the PHP source tree: ' . $name);
        }
        $configuration = $manifest['modules'][$name]['configuration_sha256'];
        if ($configuration !== null && (!is_file($moduleDirectory . '/config.m4') || hash_file('sha256', $moduleDirectory . '/config.m4') !== $configuration)) {
            throw new RuntimeException('Selected module configure inputs changed; its retained core recipe cannot be reused: ' . $name);
        }
        return $manifest;
    }

    public static function command(string $source, string $name, int $concurrency, array $variables = []): array
    {
        if (!preg_match('/^[a-zA-Z0-9_]+$/D', $name) || $concurrency < 1) {
            throw new RuntimeException('Invalid module-only build request');
        }
        $source = realpath($source) ?: throw new RuntimeException('Missing PHP module source tree');
        $makefile = (string) file_get_contents($source . '/Makefile');
        if (!preg_match('/^shared_objects_' . preg_quote($name, '/') . '\s*=\s*(.+)$/m', $makefile, $objects)) {
            throw new RuntimeException('Module has no configured shared object targets: ' . $name);
        }
        $target = 'ext/' . $name . '/' . $name . '.la';
        $targetRule = '/^' . preg_quote($target, '/') . ':\s*\$\(shared_objects_' . preg_quote($name, '/') . '\)(?:\s*\$\(' . strtoupper($name) . '_SHARED_DEPENDENCIES\))?\s*$/m';
        if (!preg_match($targetRule, $makefile)) {
            throw new RuntimeException('Module-only link target has unexpected dependencies: ' . $name);
        }
        if (preg_match('/^' . strtoupper($name) . '_SHARED_DEPENDENCIES\s*=\s*(\S.*?)$/m', $makefile)) {
            throw new RuntimeException('Module-only build cannot rebuild another target: ' . $name);
        }
        $arguments = ['make', '-j' . $concurrency];
        foreach (preg_split('/\s+/', trim($objects[1])) as $object) {
            if (!preg_match('~^ext/' . preg_quote($name, '~') . '/[a-zA-Z0-9_./-]+\.lo$~D', $object) || str_contains($object, '..')) {
                throw new RuntimeException('Module object is outside its selected source directory');
            }
            if (!preg_match('/^' . preg_quote($object, '/') . ':\s*([^\r\n]+)$/m', $makefile, $rule)) {
                throw new RuntimeException('Missing configured module compilation recipe: ' . $object);
            }
            $input = trim($rule[1]);
            $resolved = realpath($input);
            if ($resolved === false || !str_starts_with($resolved, realpath($source . '/ext/' . $name) . '/')
                || !preg_match('/\.(?:c|cc|cpp|cxx|S)$/D', $resolved)) {
                throw new RuntimeException('Module compilation has a nonlocal source prerequisite: ' . $object);
            }
            $arguments[] = '-W';
            $arguments[] = $input;
        }
        foreach ($variables as $key => $value) {
            if (!preg_match('/^[A-Z_][A-Z0-9_]*$/D', $key) || !is_string($value) || str_contains($value, "\n")) {
                throw new RuntimeException('Invalid module-only make variable');
            }
            $arguments[] = $key . '=' . $value;
        }
        $arguments[] = $target;
        return $arguments;
    }

    public static function build(string $source, string $root, string $name, int $concurrency, array $variables, callable $deploy): string
    {
        self::restore($source, $root, $name);
        $arguments = self::command($source, $name, $concurrency, $variables);
        $protected = self::protectedFiles($root, $name);
        $process = new Process($arguments, $source);
        $process->setTimeout(null);
        try {
            $process->mustRun(static function ($type, $buffer): void { echo $buffer; });
        } finally {
            foreach ($protected as $file => $identity) {
                if (!self::matches($file, $identity)) {
                    throw new RuntimeException('Module-only build changed a retained core/library: ' . $file);
                }
            }
        }
        $archive = (string) file_get_contents($source . '/ext/' . $name . '/' . $name . '.la');
        if (!preg_match('/^dlname=\x27([^\x27]+)\x27$/m', $archive, $matches)
            || !preg_match('/^' . preg_quote($name, '/') . '(?:-[a-zA-Z0-9_.-]+)?\.so$/D', $matches[1])) {
            throw new RuntimeException('Module-only link did not produce its expected shared library');
        }
        $input = $source . '/ext/' . $name . '/.libs/' . $matches[1];
        $destination = $root . '/modules/' . $matches[1];
        if (!is_file($input) || is_link($input)) {
            throw new RuntimeException('Module-only linked shared library is missing');
        }
        $deploy($input, $destination);
        $unversioned = $root . '/modules/' . $name . '.so';
        if ($unversioned !== $destination) {
            if (file_exists($unversioned) || is_link($unversioned)) {
                unlink($unversioned);
            }
            if (!symlink(basename($destination), $unversioned)) {
                throw new RuntimeException('Cannot create selected module compatibility link');
            }
        }
        return $destination;
    }

    private static function manifest(string $root): array
    {
        $path = $root . '/' . self::DIRECTORY . '/manifest.json';
        if (!is_file($path)) {
            throw new RuntimeException('Verified partial module build requires retained configured PHP module recipes');
        }
        $manifest = json_decode((string) file_get_contents($path), true, 512, JSON_THROW_ON_ERROR);
        if (($manifest['schema'] ?? null) !== 1 || !is_array($manifest['modules'] ?? null) || !is_array($manifest['files'] ?? null)) {
            throw new RuntimeException('Invalid retained configured PHP module recipes');
        }
        return $manifest;
    }

    private static function protectedFiles(string $root, string $selected): array
    {
        $files = [];
        $manifest = json_decode((string) file_get_contents($root . '/shared-libraries.json'), true, 512, JSON_THROW_ON_ERROR);
        foreach ($manifest['libraries'] ?? [] as $library) {
            foreach ($library['files'] ?? [] as $key => $value) {
                $relative = is_int($key) ? $value : $key;
                if (!is_string($relative)) {
                    throw new RuntimeException('Invalid protected shared-library path');
                }
                self::safe($relative);
                if (is_file($root . '/' . $relative) || is_link($root . '/' . $relative)) {
                    $files[$root . '/' . $relative] = self::identity($root . '/' . $relative);
                }
            }
        }
        foreach (array_merge(glob($root . '/bin/php*') ?: [], glob($root . '/bin/frankenphp') ?: [], glob($root . '/lib/libphp*') ?: [], glob($root . '/modules/*') ?: []) as $file) {
            if (dirname($file) === $root . '/modules'
                && preg_match('/^' . preg_quote($selected, '/') . '(?:-[a-zA-Z0-9_.-]+)?\.so$/D', basename($file))) {
                continue;
            }
            if (is_file($file) || is_link($file)) {
                $files[$file] = self::identity($file);
            }
        }
        return $files;
    }

    private static function safe(string $relative): void
    {
        if ($relative === '' || str_starts_with($relative, '/') || str_contains($relative, '..') || str_contains($relative, '\\')) {
            throw new RuntimeException('Invalid configured module asset path');
        }
    }

    private static function identity(string $file): array
    {
        return is_link($file) ? ['symlink' => readlink($file)] : ['sha256' => hash_file('sha256', $file), 'mode' => fileperms($file) & 0777];
    }
    private static function matches(string $file, array $identity): bool
    {
        clearstatcache(true, $file);
        if (isset($identity['symlink'])) {
            return is_link($file) && readlink($file) === $identity['symlink'];
        }
        return is_file($file) && !is_link($file) && hash_file('sha256', $file) === $identity['sha256'] && (fileperms($file) & 0777) === $identity['mode'];
    }
}
