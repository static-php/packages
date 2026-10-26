<?php

declare(strict_types=1);

namespace staticphp\util;

use RuntimeException;
use Symfony\Component\Process\Process;

final class ConsumerSnapshot
{
    private array $files = [];
    private array $selected;
    private array $packages;

    private function __construct(private string $root, private string $directory, array $packages)
    {
        $this->selected = array_fill_keys(array_map(self::normalize(...), $packages), true);
        $this->packages = $packages;
    }

    public static function capture(string $root, array $metadata, array $packages, ?string $previousPayload = null): self
    {
        $root = realpath($root) ?: throw new RuntimeException('Missing consumer buildroot');
        $temporary = tempnam(dirname($root), '.consumer-snapshot-');
        if ($temporary === false || !unlink($temporary) || !mkdir($temporary, 0700)) {
            throw new RuntimeException('Cannot create consumer preservation directory');
        }
        $snapshot = new self($root, $temporary, $packages);
        try {
            $expected = [];
            foreach ($metadata['files'] ?? [] as $relative => $identity) {
                if (self::consumer($relative)) {
                    $expected[$relative] = $identity;
                    foreach ($metadata['prepared'][$relative] ?? [] as $entry) {
                        $expected[$entry['path']] = array_diff_key($entry, ['path' => true]);
                    }
                }
            }
            foreach ($metadata['packaging_assets'] ?? [] as $relative => $identity) {
                if (str_starts_with($relative, 'packaging/') || str_starts_with($relative, 'include/php/')
                    || str_starts_with($relative, 'lib/php/build/') || in_array($relative, ['bin/phpize', 'bin/php-config'], true)) {
                    $expected[$relative] = $identity;
                }
            }
            foreach ($expected as $relative => $identity) {
                $snapshot->preserve($relative, $identity, $previousPayload);
            }
            foreach (['bin', 'lib', 'modules', 'debug'] as $directory) {
                if (!is_dir($root . '/' . $directory)) {
                    continue;
                }
                foreach (new \RecursiveIteratorIterator(new \RecursiveDirectoryIterator($root . '/' . $directory, \FilesystemIterator::SKIP_DOTS)) as $file) {
                    $relative = substr($file->getPathname(), strlen($root) + 1);
                    if ($file->isLink() && self::consumer($relative)) {
                        $target = realpath($file->getPathname());
                        $targetRelative = $target === false ? '' : substr($target, strlen($root) + 1);
                        if ($target === false || !str_starts_with($target, $root . '/') || !isset($expected[$targetRelative])
                            || !self::matches($target, $expected[$targetRelative])) {
                            throw new RuntimeException('Unverified consumer symlink before partial build: ' . $relative);
                        }
                        $snapshot->preserve($relative, ['symlink' => readlink($file->getPathname())], null);
                    } elseif ($directory === 'debug' && $file->isFile() && !$file->isLink()) {
                        $basename = preg_replace('/\.debug$/D', '', $file->getFilename());
                        foreach (array_keys($metadata['files'] ?? []) as $consumer) {
                            if (self::consumer($consumer) && basename($consumer) === $basename) {
                                if (!self::matchingDebugInfo($root . '/' . $consumer, $file->getPathname())) {
                                    throw new RuntimeException('Unverified consumer debug companion before partial build: ' . $relative);
                                }
                                $snapshot->preserve($relative, self::identity($file->getPathname()), null, self::actor($consumer));
                                break;
                            }
                        }
                    }
                }
            }
            return $snapshot;
        } catch (\Throwable $error) {
            $snapshot->close();
            throw $error;
        }
    }

    private function preserve(string $relative, array $identity, ?string $fallback, ?string $actor = null): void
    {
        self::safe($relative);
        if (isset($this->files[$relative])) {
            return;
        }
        $actor ??= self::actor($relative);
        $source = $this->root . '/' . $relative;
        if (!self::matches($source, $identity) && $fallback !== null && self::matches($fallback . '/' . $relative, $identity)) {
            $source = $fallback . '/' . $relative;
        }
        if (!self::matches($source, $identity)) {
            if ($actor !== null && isset($this->selected[$actor])) {
                return;
            }
            throw new RuntimeException('Missing or changed retained consumer before partial build: ' . $relative);
        }
        $target = $this->directory . '/' . $relative;
        self::copy($source, $target, $identity);
        $this->files[$relative] = ['identity' => $identity, 'selected' => $actor !== null && isset($this->selected[$actor])];
    }

    public function restore(): void
    {
        $files = $this->files;
        uasort($files, static fn (array $left, array $right): int => isset($left['identity']['symlink']) <=> isset($right['identity']['symlink']));
        foreach ($files as $relative => $entry) {
            if ($entry['selected']) {
                continue;
            }
            $source = $this->directory . '/' . $relative;
            $target = $this->root . '/' . $relative;
            if (!self::matches($source, $entry['identity'])) {
                throw new RuntimeException('Consumer preservation copy changed: ' . $relative . '; retained at ' . $this->directory);
            }
            if (self::matches($target, $entry['identity'])) {
                continue;
            }
            if (file_exists($target) || is_link($target)) {
                throw new RuntimeException('An unselected consumer changed during partial build: ' . $relative . '; retained at ' . $this->directory);
            }
            self::copy($source, $target, $entry['identity']);
        }
    }

    public function activate(array $receipt): void
    {
        $seed = $this->root . '/consumer-cache-seed.json';
        $metadata = $receipt['cached_metadata_path'] ?? '';
        self::safe($metadata);
        if (!is_file($seed) || hash_file('sha256', $this->root . '/' . $metadata) !== ($receipt['cached_metadata_sha256'] ?? null)) {
            throw new RuntimeException('Consumer preservation has no verified core seed');
        }
        $manifest = ['schema' => 1, 'root' => $this->root, 'files' => $this->files,
            'packages' => $this->packages, 'seed_sha256' => hash_file('sha256', $seed),
            'cached_metadata_path' => $metadata, 'cached_metadata_sha256' => $receipt['cached_metadata_sha256']];
        $file = $this->directory . '/manifest.json';
        file_put_contents($file, json_encode($manifest, JSON_THROW_ON_ERROR | JSON_UNESCAPED_SLASHES));
        chmod($file, 0400);
        file_put_contents($this->root . '/consumer-reuse-snapshot.json', json_encode(['schema' => 1,
            'directory' => $this->directory, 'manifest_sha256' => hash_file('sha256', $file)], JSON_THROW_ON_ERROR));
    }

    public static function active(string $root): ?self
    {
        $descriptor = $root . '/consumer-reuse-snapshot.json';
        if (!is_file($descriptor)) {
            return null;
        }
        $root = realpath($root) ?: throw new RuntimeException('Missing consumer buildroot');
        $descriptor = json_decode((string) file_get_contents($descriptor), true, 512, JSON_THROW_ON_ERROR);
        $directory = $descriptor['directory'] ?? '';
        if (($descriptor['schema'] ?? null) !== 1 || !is_string($directory) || is_link($directory)
            || dirname($directory) !== dirname($root) || !str_starts_with(basename($directory), '.consumer-snapshot-')
            || !is_file($directory . '/manifest.json') || hash_file('sha256', $directory . '/manifest.json') !== ($descriptor['manifest_sha256'] ?? null)) {
            throw new RuntimeException('Invalid active consumer preservation proof');
        }
        $manifest = json_decode((string) file_get_contents($directory . '/manifest.json'), true, 512, JSON_THROW_ON_ERROR);
        $cached = $manifest['cached_metadata_path'] ?? '';
        self::safe($cached);
        if (($manifest['schema'] ?? null) !== 1 || ($manifest['root'] ?? null) !== $root
            || hash_file('sha256', $root . '/consumer-cache-seed.json') !== ($manifest['seed_sha256'] ?? null)
            || hash_file('sha256', $root . '/' . $cached) !== ($manifest['cached_metadata_sha256'] ?? null)
            || !is_array($manifest['files'] ?? null) || !is_array($manifest['packages'] ?? null)) {
            throw new RuntimeException('Active consumer preservation no longer matches the verified core');
        }
        $metadata = json_decode((string) file_get_contents($root . '/' . $cached), true, 512, JSON_THROW_ON_ERROR);
        $snapshot = new self($root, $directory, $manifest['packages']);
        foreach ($manifest['files'] as $relative => $entry) {
            self::safe($relative);
            if (!is_array($entry) || !is_array($entry['identity'] ?? null) || !is_bool($entry['selected'] ?? null)
                || !self::matches($directory . '/' . $relative, $entry['identity'])) {
                throw new RuntimeException('Active consumer preservation bytes changed: ' . $relative);
            }
        }
        $temporaryScripts = [];
        foreach (['bin/php-config', 'lib/php/build/phpize.m4'] as $relative) {
            if (isset($manifest['files'][$relative]) && !isset($manifest['files'][$relative]['identity']['symlink'])) {
                $temporaryScripts[$relative] = $directory . '/' . $relative;
            }
        }
        BuildPayload::verifiedCoreSeed($root, $manifest['packages'], $metadata['source_version'] ?? $metadata['php_version'], [], $temporaryScripts);
        $snapshot->files = $manifest['files'];
        return $snapshot;
    }

    public function isSelected(string $extension): bool { return isset($this->selected[self::normalize($extension)]); }

    public function verifyExtension(string $extension): void
    {
        $found = false;
        foreach ($this->files as $relative => $entry) {
            if (str_starts_with($relative, 'modules/') && !isset($entry['identity']['symlink']) && self::actor($relative) === self::normalize($extension)) {
                if ($entry['selected'] || !self::matches($this->root . '/' . $relative, $entry['identity'])) {
                    throw new RuntimeException('Unverified reused shared extension: ' . $extension);
                }
                $found = true;
            }
        }
        if (!$found) {
            throw new RuntimeException('An unselected shared extension has no verified cached output: ' . $extension);
        }
    }

    public function close(): void
    {
        $descriptor = $this->root . '/consumer-reuse-snapshot.json';
        if (is_file($descriptor)) {
            $data = json_decode((string) file_get_contents($descriptor), true);
            if (($data['directory'] ?? null) === $this->directory) {
                unlink($descriptor);
            }
        }
        if (is_dir($this->directory)) {
            (new Process(['rm', '-rf', $this->directory]))->mustRun();
        }
    }

    private static function copy(string $source, string $target, array $identity): void
    {
        if (!is_dir(dirname($target)) && !mkdir(dirname($target), 0755, true) && !is_dir(dirname($target))) {
            throw new RuntimeException('Cannot create consumer preservation parent');
        }
        if (isset($identity['symlink'])) {
            if (!symlink($identity['symlink'], $target)) {
                throw new RuntimeException('Cannot preserve consumer symlink: ' . $target);
            }
        } elseif (!copy($source, $target) || !chmod($target, $identity['mode'])) {
            throw new RuntimeException('Cannot preserve consumer file');
        }
        if (!self::matches($target, $identity)) {
            throw new RuntimeException('Consumer preservation bytes changed: ' . $target);
        }
    }

    private static function matches(string $path, array $identity): bool
    {
        clearstatcache(true, $path);
        return isset($identity['symlink']) ? is_link($path) && readlink($path) === $identity['symlink']
            : is_file($path) && !is_link($path) && hash_file('sha256', $path) === ($identity['sha256'] ?? null)
                && (fileperms($path) & 0777) === ($identity['mode'] ?? null);
    }

    private static function identity(string $path): array
    {
        return ['sha256' => hash_file('sha256', $path), 'mode' => fileperms($path) & 0777];
    }

    private static function normalize(string $name): string { return str_replace('_', '-', $name); }

    private static function matchingDebugInfo(string $runtime, string $debug): bool
    {
        if (!is_file($runtime)) {
            return false;
        }
        $read = static function (array $arguments): string {
            $process = new Process($arguments);
            $process->mustRun();
            return $process->getOutput();
        };
        preg_match('/Build ID:\s*(\S+)/', $read(['readelf', '--notes', $runtime]), $first);
        preg_match('/Build ID:\s*(\S+)/', $read(['readelf', '--notes', $debug]), $second);
        if (isset($first[1], $second[1])) {
            return $first[1] === $second[1];
        }
        $section = $read(['readelf', '--wide', '--hex-dump=.gnu_debuglink', $runtime]);
        preg_match_all('/^\s*0x[0-9a-f]+\s+((?:[0-9a-f]{8}\s*){1,4})/mi', $section, $words);
        $bytes = hex2bin(preg_replace('/\s+/', '', implode('', $words[1])));
        if ($bytes === false || ($end = strpos($bytes, "\0")) === false || strlen($bytes) < 8
            || substr($bytes, 0, $end) !== basename($debug)) {
            return false;
        }
        $offset = ($end + 4) & ~3;
        return strlen($bytes) === $offset + 4
            && sprintf('%08x', unpack('Vcrc', substr($bytes, $offset, 4))['crc']) === hash_file('crc32b', $debug);
    }

    private static function consumer(string $relative): bool
    {
        return str_starts_with($relative, 'modules/') || preg_match('#^bin/(?:php(?:$|[-.]|dbg)|frankenphp$)#D', $relative)
            || preg_match('#^lib/libphp[^/]*\.(?:so(?:\.[^/]*)?|a|la)$#D', $relative);
    }

    private static function actor(string $relative): ?string
    {
        $relative = preg_replace('#^payload/(?:runtime|debug)/#', '', $relative);
        if (preg_match('#^(?:modules|debug)/([^/]+?)(?:-(?:zts|nts)-\d+)?\.so(?:\.debug)?$#D', $relative, $match)) {
            return self::normalize($match[1]);
        }
        if (in_array($relative, ['bin/frankenphp', 'bin/frankenphp.debug'], true) || str_starts_with($relative, 'packaging/frankenphp/') || $relative === 'debug/frankenphp.debug') {
            return 'frankenphp';
        }
        if (preg_match('#^include/php/ext/([^/]+)/#', $relative, $match)) {
            return self::normalize($match[1]);
        }
        return null;
    }

    private static function safe(string $relative): void
    {
        if ($relative === '' || str_starts_with($relative, '/') || str_contains($relative, '\\') || preg_match('#(?:^|/)\.{1,2}(?:/|$)#', $relative)) {
            throw new RuntimeException('Invalid consumer preservation path');
        }
    }
}
