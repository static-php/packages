<?php

declare(strict_types=1);

namespace staticphp\package;

use RuntimeException;
use staticphp\package;
use staticphp\util\SharedLibraries;

final class library implements package
{
    private ?array $runtimeFiles = null;
    private array $debugFiles = [];

    public function __construct(private SharedLibraries $libraries, private string $id)
    {
        $libraries->get($id);
    }

    public function getName(): string
    {
        return $this->libraries->libraryPackageName($this->id);
    }

    public function getLibraryId(): string
    {
        return $this->id;
    }

    public function getManifestEntry(): array
    {
        return $this->libraries->get($this->id);
    }

    public function getVersion(): string
    {
        return SharedLibraries::normalizeVersion($this->getManifestEntry()['version']);
    }

    public function getSourceHash(): string
    {
        return $this->getManifestEntry()['source_hash'];
    }

    public function getFpmConfig(): array
    {
        $this->prepareFiles();
        $files = $this->runtimeFiles;
        $metadata = array_merge($this->getManifestEntry(), ['schema' => 1, 'id' => $this->id, 'runtime_libdir' => $this->libraries->getRuntimeLibdir(), 'architecture' => $this->libraries->architecture(), 'libc' => $this->libraries->getLibc()]);
        $metadata['package_version'] = $this->libraries->getPackageVersion($this->id, defined('SPP_TYPE') ? SPP_TYPE : 'rpm') ?? $this->getVersion();
        $metadata['abi_epoch'] ??= 1;
        $metadata['abi_capability'] = $this->libraries->abiCapability($this->id);
        $metadata['abi_capabilities'] = [$metadata['abi_capability']];
        if (!defined('TEMP_DIR') || !is_dir(TEMP_DIR)) {
            throw new RuntimeException('Library packaging requires an existing TEMP_DIR');
        }
        $metadataPath = TEMP_DIR . '/library-' . $this->id . '.json';
        if (file_put_contents($metadataPath, json_encode($metadata, JSON_PRETTY_PRINT | JSON_UNESCAPED_SLASHES | JSON_THROW_ON_ERROR) . "\n") === false) {
            throw new RuntimeException("Cannot write library metadata {$metadataPath}");
        }
        $files[$metadataPath] = '/usr/share/php-zts/libraries/' . $this->id . '.json';
        return [
            'files' => $files,
            'depends' => $this->libraries->dependenciesForPackage($files, defined('SPP_TYPE') ? SPP_TYPE : 'rpm', $this->id),
            'provides' => [],
            'provides_unversioned' => [$this->libraries->abiCapability($this->id)],
        ];
    }

    public function getDebuginfoFpmConfig(): array
    {
        $this->prepareFiles();
        return $this->debugFiles === [] ? [] : ['files' => $this->debugFiles, 'depends' => [$this->getName()]];
    }

    private function prepareFiles(): void
    {
        if ($this->runtimeFiles !== null) {
            return;
        }
        if (!defined('TEMP_DIR') || !is_dir(TEMP_DIR)) {
            throw new RuntimeException('Library packaging requires an existing TEMP_DIR');
        }
        $files = $this->libraries->getPackageFiles($this->id);
        foreach ($files as $source => $destination) {
            if (is_link($source) || file_get_contents($source, false, null, 0, 4) !== "\x7fELF") {
                continue;
            }
            $relative = substr($source, strlen($this->libraries->getRoot()) + 1);
            $stage = TEMP_DIR . '/library-' . $this->id;
            $runtime = $stage . '/runtime/' . $relative;
            $debug = $stage . '/debug/' . dirname($relative) . '/' . basename($destination) . '.debug';
            $this->ensureDirectory(dirname($runtime));
            if (!copy($source, $runtime) || !chmod($runtime, fileperms($source) & 0777)) {
                throw new RuntimeException("Cannot stage shared library {$source}");
            }
            $sections = $this->command(['readelf', '--wide', '--sections', $source]);
            $hasDebug = preg_match('/\.(?:zdebug|debug)_(?:info|types)\s/', $sections) === 1;
            $fallback = $this->libraries->getRoot() . '/debug/' . $relative . '.debug';
            if ($hasDebug) {
                $this->ensureDirectory(dirname($debug));
                $this->command(['objcopy', '--only-keep-debug', $source, $debug]);
            } elseif (is_file($fallback) && $this->sameBuildId($source, $fallback)) {
                $this->ensureDirectory(dirname($debug));
                if (!copy($fallback, $debug)) {
                    throw new RuntimeException("Cannot stage shared-library debuginfo {$fallback}");
                }
                $hasDebug = true;
            }
            $this->command(['objcopy', '--remove-section=.gnu_debuglink', $runtime]);
            $this->command(['strip', '--strip-unneeded', $runtime]);
            if ($hasDebug) {
                $this->command(['objcopy', '--add-gnu-debuglink=' . $debug, $runtime]);
                $this->debugFiles[$debug] = '/usr/lib/debug' . $destination . '.debug';
            }
            unset($files[$source]);
            $files[$runtime] = $destination;
        }
        $this->runtimeFiles = $files;
    }

    private function sameBuildId(string $runtime, string $debug): bool
    {
        preg_match('/Build ID:\s*(\S+)/', $this->command(['readelf', '--notes', $runtime]), $runtimeId);
        preg_match('/Build ID:\s*(\S+)/', $this->command(['readelf', '--notes', $debug]), $debugId);
        return isset($runtimeId[1], $debugId[1]) && $runtimeId[1] === $debugId[1];
    }

    private function ensureDirectory(string $path): void
    {
        if (!is_dir($path) && !mkdir($path, 0755, true) && !is_dir($path)) {
            throw new RuntimeException("Cannot create library staging directory {$path}");
        }
    }

    private function command(array $arguments): string
    {
        $pipes = [];
        $process = proc_open($arguments, [0 => ['pipe', 'r'], 1 => ['pipe', 'w'], 2 => ['pipe', 'w']], $pipes, null, ['LC_ALL' => 'C'] + getenv());
        if (!is_resource($process)) {
            throw new RuntimeException('Cannot execute shared-library packaging tool');
        }
        fclose($pipes[0]);
        $output = stream_get_contents($pipes[1]);
        $error = stream_get_contents($pipes[2]);
        fclose($pipes[1]);
        fclose($pipes[2]);
        if (proc_close($process) !== 0) {
            throw new RuntimeException('Shared-library packaging command failed: ' . implode(' ', $arguments) . ': ' . $error);
        }
        return $output;
    }

    public function getFpmExtraArgs(): array
    {
        return [];
    }

    public function getLicense(): string
    {
        return $this->getManifestEntry()['license'];
    }

    public function getDescription(): string
    {
        return 'Private shared ' . $this->id . ' runtime for PHP ZTS';
    }
}
