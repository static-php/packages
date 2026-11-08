<?php

declare(strict_types=1);

namespace staticphp\package;

use RuntimeException;
use staticphp\package;
use staticphp\util\RuntimeServices;

final class runtime_layout implements package
{
    public function getName(): string
    {
        return 'php-zts-runtime-layout';
    }

    public function getVersion(): string
    {
        return '2';
    }

    public function sourceHash(string $type): string
    {
        $inputs = ['type' => $type];
        foreach ([__FILE__, BASE_PATH . '/src/util/RuntimeServices.php', BASE_PATH . '/src/runtime/runtime-services', BASE_PATH . '/src/runtime/apk-commit-hook'] as $path) {
            if (is_file($path)) {
                $inputs[basename($path)] = hash_file('sha256', $path);
            }
        }
        return hash('sha256', json_encode($inputs, JSON_THROW_ON_ERROR));
    }

    public function getFpmConfig(): array
    {
        $type = defined('SPP_TYPE') ? SPP_TYPE : 'rpm';
        $config = ['files' => [], 'empty_directories' => [], 'depends' => $type === 'deb' ? ['init-system-helpers'] : []];
        if ($type === 'rpm') {
            $source = TEMP_DIR . '/runtime-layout/php-zts';
            if (!is_dir(dirname($source)) && !mkdir(dirname($source), 0755, true) && !is_dir(dirname($source))) {
                throw new RuntimeException('Cannot stage the shared runtime layout');
            }
            if (!is_link($source) && !symlink('../lib64/php-zts', $source)) {
                throw new RuntimeException('Cannot stage the shared runtime alias');
            }
            $config['files'][$source] = '/usr/lib/php-zts';
            $config['empty_directories'] = ['/usr/lib64/php-zts/lib', '/usr/lib64/php-zts/modules'];
        } else {
            $config['empty_directories'] = ['/usr/lib/php-zts/lib', '/usr/lib/php-zts/modules'];
            if ($type === 'deb') {
                $source = \staticphp\step\CreatePackages::stageSymlink('../lib/php-zts', 'php-zts');
                $config['files'][$source] = '/usr/lib64/php-zts';
            }
        }
        if (class_exists(RuntimeServices::class)) {
            $service = RuntimeServices::layoutConfig($type);
            foreach ($service as $key => $value) {
                $config[$key] = isset($config[$key]) && is_array($config[$key]) ? array_merge($config[$key], $value) : $value;
            }
        }
        $metadata = TEMP_DIR . '/runtime-layout.json';
        file_put_contents($metadata, json_encode(['schema' => 1, 'type' => $type, 'version' => $this->getVersion(), 'source_hash' => $this->sourceHash($type)], JSON_THROW_ON_ERROR) . "\n");
        $config['files'][$metadata] = '/usr/share/php-zts/runtime-layout.json';
        return $config;
    }

    public function getFpmExtraArgs(): array
    {
        $type = defined('SPP_TYPE') ? SPP_TYPE : 'rpm';
        $args = class_exists(RuntimeServices::class) ? RuntimeServices::layoutArgs($type) : [];
        if ($type === 'apk') {
            return $args;
        }
        [$alias, $target, $absolute] = $type === 'rpm'
            ? ['/usr/lib/php-zts', '../lib64/php-zts', '/usr/lib64/php-zts']
            : ['/usr/lib64/php-zts', '../lib/php-zts', '/usr/lib/php-zts'];
        $script = TEMP_DIR . '/runtime-layout-preinstall-' . $type;
        $contents = "#!/bin/sh\nset -eu\n";
        $contents .= 'if [ -e ' . escapeshellarg($alias) . ' ] || [ -L ' . escapeshellarg($alias) . ' ]; then' . "\n";
        $contents .= '    if [ ! -L ' . escapeshellarg($alias) . ' ]; then echo ' . escapeshellarg("Cannot install PHP runtime alias over existing directory {$alias}") . ' >&2; exit 1; fi' . "\n";
        $contents .= '    case "$(readlink ' . escapeshellarg($alias) . ')" in ' . escapeshellarg($target) . '|' . escapeshellarg($absolute) . ') ;; *) echo ' . escapeshellarg("Conflicting PHP runtime alias {$alias}") . ' >&2; exit 1 ;; esac' . "\nfi\n";
        if (file_put_contents($script, $contents) === false || !chmod($script, 0755)) {
            throw new RuntimeException('Cannot stage the runtime layout ownership check');
        }
        // FPM rejects epoch zero; the common DEB archive must be identical on both builders.
        return $type === 'deb'
            ? ['--before-install', $script, '--source-date-epoch-default', '1', ...$args]
            : ['--before-install', $script, '--before-upgrade', $script, ...$args];
    }

    public function getDebuginfoFpmConfig(): array
    {
        return [];
    }

    public function getLicense(): string
    {
        return 'MIT';
    }

    public function getDescription(): string
    {
        return 'Private PHP ZTS runtime layout and transaction integration';
    }
}
