<?php

declare(strict_types=1);

namespace staticphp\util;

use RuntimeException;

final class SharedLibraries
{
    private string $root;
    private array $manifest;
    private array $files = [];
    private array $sonameOwners = [];
    private array $elf = [];
    private string $manifestHash;

    public function __construct(string $root, ?string $expectedRuntimeLibdir = null)
    {
        $resolved = realpath($root);
        if ($resolved === false || !is_dir($resolved)) {
            throw new RuntimeException("Missing shared-library buildroot: {$root}");
        }
        $this->root = $resolved;
        $path = $resolved . '/shared-libraries.json';
        try {
            $this->manifest = json_decode((string) file_get_contents($path), true, 512, JSON_THROW_ON_ERROR);
        } catch (\Throwable $error) {
            throw new RuntimeException("Cannot read shared-library manifest {$path}: " . $error->getMessage(), 0, $error);
        }
        if (($this->manifest['schema'] ?? null) !== 1 || ($this->manifest['linkage'] ?? null) !== 'shared' || !is_array($this->manifest['libraries'] ?? null)) {
            throw new RuntimeException("Unsupported shared-library manifest: {$path}");
        }
        $this->manifestHash = hash_file('sha256', $path);
        $this->loadPackageVersions();
        if (array_key_exists('pending_rebuilds', $this->manifest)
            && (!is_array($this->manifest['pending_rebuilds']) || $this->manifest['pending_rebuilds'] !== [])) {
            throw new RuntimeException('Shared-library ABI transition still requires dependent rebuilds');
        }
        $libdir = $this->manifest['runtime_libdir'] ?? null;
        if (!is_string($libdir) || !in_array($libdir, ['/usr/lib/php-zts/lib', '/usr/lib64/php-zts/lib'], true)) {
            throw new RuntimeException('Invalid private runtime library directory');
        }
        if ($expectedRuntimeLibdir !== null && $libdir !== rtrim($expectedRuntimeLibdir, '/')) {
            throw new RuntimeException("Shared libraries target {$libdir}, expected {$expectedRuntimeLibdir}");
        }
        $this->architecture();
        $this->getLibc();
        $owners = [];
        $destinations = [];
        foreach ($this->manifest['libraries'] as $id => $entry) {
            if (!is_string($id) || !preg_match('/^[a-z0-9][a-z0-9-]*$/D', $id) || !is_array($entry)) {
                throw new RuntimeException('Invalid shared-library identifier');
            }
            foreach (['version', 'source_hash', 'license'] as $field) {
                if (!is_string($entry[$field] ?? null) || trim($entry[$field]) === '') {
                    throw new RuntimeException("Library {$id} has no {$field}");
                }
            }
            if (!preg_match('/^[0-9][A-Za-z0-9.+~_-]*$/D', self::normalizeVersion($entry['version']))) {
                throw new RuntimeException("Invalid version for library {$id}");
            }
            if (!is_array($entry['files'] ?? null) || $entry['files'] === [] || !is_array($entry['license_files'] ?? null) || $entry['license_files'] === []) {
                throw new RuntimeException("Library {$id} must declare runtime files and licenses");
            }
            $this->abiCapability($id);
            $mapping = [];
            foreach ($entry['files'] as $key => $value) {
                $relative = is_int($key) ? $value : $key;
                if (in_array($relative, $entry['license_files'], true) || array_key_exists($relative, $entry['runtime_assets'] ?? [])) {
                    continue;
                }
                $source = $this->source($relative);
                $destination = is_int($key) ? $libdir . '/' . basename($relative) : $value;
                $this->destination($destination);
                if (!str_starts_with($destination, $libdir . '/')) {
                    throw new RuntimeException("Library {$id} must install shared objects inside {$libdir}");
                }
                $this->expandFile($source, $destination, $mapping);
            }
            foreach ($entry['runtime_assets'] ?? [] as $relative => $destination) {
                if (is_int($relative)) {
                    if (!is_array($destination) || !isset($destination['source'], $destination['destination'])) {
                        throw new RuntimeException("Invalid runtime asset for {$id}");
                    }
                    $relative = $destination['source'];
                    $destination = $destination['destination'];
                }
                $this->destination($destination);
                if (!str_starts_with($destination, $libdir . '/') && !str_starts_with($destination, '/usr/share/php-zts/') && !str_starts_with($destination, '/etc/php-zts/')) {
                    throw new RuntimeException("Invalid private runtime asset destination {$destination}");
                }
                $this->expandFile($this->source($relative), $destination, $mapping);
            }
            foreach ($mapping as $source => $destination) {
                if (isset($owners[$source]) || isset($destinations[$destination])) {
                    throw new RuntimeException("Conflicting shared-library ownership: {$source} => {$destination}");
                }
                $owners[$source] = $id;
                $destinations[$destination] = $id;
            }
            foreach ($entry['license_files'] as $relative) {
                $source = $this->source($relative);
                $this->expandFile($source, '/usr/share/licenses/' . $this->libraryPackageName($id) . '/' . $relative, $mapping);
            }
            foreach ($entry['abi_dump'] ?? [] as $soname => $relative) {
                if (!is_string($soname) || basename($soname) !== $soname || !preg_match('/^[A-Za-z0-9_.+-]+$/D', $soname)) {
                    throw new RuntimeException("Invalid ABI dump SONAME for {$id}");
                }
                $mapping[$this->source($relative)] = '/usr/share/php-zts/libraries/abi/' . $id . '/' . $soname . '.xml';
            }
            $this->files[$id] = $mapping;
        }
        foreach ($owners as $source => $id) {
            $details = $this->readElf($source);
            if ($details === null) {
                continue;
            }
            $this->elf[$source] = $details;
            $relative = substr($source, strlen($this->root) + 1);
            $resolvedRelative = substr((string) realpath($source), strlen($this->root) + 1);
            $declared = $this->get($id)['elf'][$relative] ?? $this->get($id)['elf'][$resolvedRelative] ?? null;
            if (!is_array($declared)) {
                throw new RuntimeException("Missing ELF metadata for {$relative}");
            }
            foreach (['needed', 'runpath'] as $field) {
                $recorded = $declared[$field] ?? [];
                $actual = $details[$field];
                if (is_string($recorded) && $field === 'runpath') {
                    $recorded = explode(':', $recorded);
                }
                if (!is_array($recorded)) {
                    throw new RuntimeException("Invalid {$field} metadata for {$relative}");
                }
                sort($recorded);
                sort($actual);
                if ($recorded !== $actual) {
                    throw new RuntimeException("Stale {$field} metadata for {$relative}");
                }
            }
            if (($declared['soname'] ?? null) !== $details['soname']) {
                throw new RuntimeException("Stale SONAME metadata for {$relative}");
            }
            $soname = $details['soname'];
            if ($soname !== null) {
                if (isset($this->sonameOwners[$soname]) && $this->sonameOwners[$soname] !== $id) {
                    throw new RuntimeException("Conflicting SONAME ownership: {$soname}");
                }
                $this->sonameOwners[$soname] = $id;
            }
        }
        foreach ($this->files as $id => $mapping) {
            foreach ($mapping as $source => $destination) {
                if (is_link($source)) {
                    $link = readlink($source);
                    $resolvedTarget = realpath($source);
                    $immediateTarget = $link === false ? '' : $this->normalizeAbsolute(dirname($source) . '/' . $link);
                    if ($link === false || str_starts_with($link, '/') || $resolvedTarget === false || !isset($mapping[$immediateTarget])) {
                        throw new RuntimeException("Unowned or non-relocatable symlink: {$source}");
                    }
                    if ($this->normalizeAbsolute(dirname($destination) . '/' . $link) !== $mapping[$immediateTarget]) {
                        throw new RuntimeException("Symlink changes target after installation: {$source}");
                    }
                }
            }
            $this->dependenciesForFiles($mapping, $id);
        }
    }

    public static function load(string $root, bool $required = true, ?string $expectedRuntimeLibdir = null): ?self
    {
        if (!$required && !is_file(rtrim($root, '/') . '/shared-libraries.json')) {
            return null;
        }
        return new self($root, $expectedRuntimeLibdir);
    }

    public function libraryIds(): array
    {
        $ids = array_keys($this->manifest['libraries']);
        sort($ids);
        return $ids;
    }

    public function get(string $id): array
    {
        if (!isset($this->manifest['libraries'][$id])) {
            throw new RuntimeException("Unknown shared library {$id}");
        }
        return $this->manifest['libraries'][$id];
    }

    public function architecture(): string
    {
        return match ($this->manifest['architecture'] ?? null) {
            'amd64', 'x86_64' => 'x86_64',
            'arm64', 'aarch64' => 'aarch64',
            default => throw new RuntimeException('Unsupported shared-library architecture'),
        };
    }

    public function getLibc(): string
    {
        $libc = $this->manifest['libc'] ?? 'glibc';
        if (!in_array($libc, ['glibc', 'musl'], true)) {
            throw new RuntimeException('Unsupported shared-library libc');
        }
        return $libc;
    }

    public function getRuntimeLibdir(): string
    {
        return $this->manifest['runtime_libdir'];
    }

    public function getRoot(): string
    {
        return $this->root;
    }

    public function getPackageFiles(string $id): array
    {
        $this->get($id);
        $files = $this->files[$id];
        if (defined('SPP_TYPE') && SPP_TYPE === 'rpm' && $this->getLibc() === 'glibc') {
            foreach ($files as &$destination) {
                if (str_starts_with($destination, '/usr/lib/php-zts/')) {
                    $destination = '/usr/lib64/php-zts/' . substr($destination, strlen('/usr/lib/php-zts/'));
                }
            }
            unset($destination);
        }
        return $files;
    }

    public function getInstallLibdir(string $type): string
    {
        if (!in_array($type, ['rpm', 'deb', 'apk'], true)) {
            throw new RuntimeException("Unsupported package type {$type}");
        }
        return $type === 'rpm' && $this->getLibc() === 'glibc' ? '/usr/lib64/php-zts/lib' : $this->getRuntimeLibdir();
    }

    public function getSonameOwners(): array
    {
        return $this->sonameOwners;
    }

    public function libraryPackageName(string $id): string
    {
        $this->get($id);
        return 'php-zts-lib-' . $id;
    }

    public function setPackageVersion(string $id, string $type, string $fullVersion): void
    {
        $this->get($id);
        $this->validatePackageVersion($type, $fullVersion);
        $this->manifest['libraries'][$id]['package_versions'][$type] = $fullVersion;
    }

    public function getPackageVersion(string $id, string $type): ?string
    {
        if (!in_array($type, ['rpm', 'deb', 'apk'], true)) {
            throw new RuntimeException("Unsupported package type {$type}");
        }
        $entry = $this->get($id);
        $version = $entry['package_versions'][$type] ?? null;
        if ($version !== null) {
            $this->validatePackageVersion($type, $version);
        }
        return $version;
    }

    public function persist(): void
    {
        $path = $this->packageVersionsPath();
        if (!is_dir(dirname($path)) && !mkdir(dirname($path), 0755, true) && !is_dir(dirname($path))) {
            throw new RuntimeException('Cannot create package version sidecar directory');
        }
        $temporary = tempnam(dirname($path), '.shared-libraries-');
        if ($temporary === false) {
            throw new RuntimeException('Cannot stage shared-library manifest');
        }
        try {
            $versions = [];
            foreach ($this->manifest['libraries'] as $id => $entry) {
                $versions[$id] = (object) ($entry['package_versions'] ?? []);
            }
            $data = ['schema' => 1, 'manifest_sha256' => $this->manifestHash, 'package_versions' => (object) $versions];
            $contents = json_encode($data, JSON_PRETTY_PRINT | JSON_UNESCAPED_SLASHES | JSON_THROW_ON_ERROR) . "\n";
            if (file_put_contents($temporary, $contents) !== strlen($contents) || !chmod($temporary, file_exists($path) ? (fileperms($path) & 0777) : 0644) || !rename($temporary, $path)) {
                throw new RuntimeException("Cannot persist shared-library package versions {$path}");
            }
        } finally {
            if (file_exists($temporary)) {
                unlink($temporary);
            }
        }
    }

    public function exportManifest(string $path): void
    {
        $data = $this->manifest;
        foreach ($data['libraries'] as &$entry) {
            foreach (['elf', 'runtime_assets', 'abi_dump', 'abi_dump_hashes', 'package_versions'] as $field) {
                if (isset($entry[$field]) && is_array($entry[$field])) {
                    $entry[$field] = (object) $entry[$field];
                }
            }
        }
        unset($entry);
        $data['libraries'] = (object) $data['libraries'];
        if (!is_dir(dirname($path)) && !mkdir(dirname($path), 0755, true) && !is_dir(dirname($path))) {
            throw new RuntimeException('Cannot create exported manifest directory');
        }
        if (file_put_contents($path, json_encode($data, JSON_PRETTY_PRINT | JSON_UNESCAPED_SLASHES | JSON_THROW_ON_ERROR) . "\n") === false) {
            throw new RuntimeException("Cannot export packaging manifest {$path}");
        }
    }

    private function packageVersionsPath(): string
    {
        $directory = defined('DIST_PATH') ? DIST_PATH . '/.package-metadata' : (defined('TEMP_DIR') ? TEMP_DIR : $this->root) . '/.package-metadata';
        return $directory . '/' . $this->manifestHash . '.json';
    }

    private function loadPackageVersions(): void
    {
        $path = $this->packageVersionsPath();
        if (!is_file($path)) {
            return;
        }
        $data = json_decode((string) file_get_contents($path), true, 512, JSON_THROW_ON_ERROR);
        if (($data['schema'] ?? null) !== 1 || ($data['manifest_sha256'] ?? null) !== $this->manifestHash || !is_array($data['package_versions'] ?? null)) {
            throw new RuntimeException('Invalid shared-library package version sidecar');
        }
        foreach ($data['package_versions'] as $id => $versions) {
            if (!isset($this->manifest['libraries'][$id]) || !is_array($versions)) {
                throw new RuntimeException('Invalid library in package version sidecar');
            }
            foreach ($versions as $type => $version) {
                $this->validatePackageVersion($type, $version);
                $this->manifest['libraries'][$id]['package_versions'][$type] = $version;
            }
        }
    }

    private function validatePackageVersion(string $type, mixed $version): void
    {
        if (!in_array($type, ['rpm', 'deb', 'apk'], true)) {
            throw new RuntimeException("Unsupported package type {$type}");
        }
        if (!is_string($version) || !preg_match('/^[0-9][A-Za-z0-9.+:~_^\-]*$/D', $version) || !preg_match($type === 'apk' ? '/-r[0-9]+$/D' : '/-[A-Za-z0-9.+~_]+$/D', $version)) {
            throw new RuntimeException("Invalid resolved {$type} package version");
        }
    }

    public function abiCapability(string $id): string
    {
        $entry = $this->get($id);
        $epoch = $entry['abi_epoch'] ?? 1;
        if (!is_int($epoch) || $epoch < 1) {
            throw new RuntimeException("Invalid ABI epoch for {$id}");
        }
        $capability = $this->libraryPackageName($id) . '-abi-' . $epoch;
        if (isset($entry['abi_capability']) && $entry['abi_capability'] !== $capability) {
            throw new RuntimeException("Invalid ABI capability for {$id}");
        }
        foreach ($entry['abi_capabilities'] ?? [] as $provided) {
            if ($provided !== $capability) {
                throw new RuntimeException("Historical ABI capability for {$id} must not satisfy current consumers");
            }
        }
        return $capability;
    }

    /** @return list<array{id:string,package:string,version:string,capability:string}> */
    public function dependenciesForFiles(array $files, ?string $ownId = null, array $extraDlopen = []): array
    {
        $ids = [];
        if ($ownId !== null) {
            $entry = $this->get($ownId);
            $extraDlopen = array_merge($extraDlopen, $entry['depends'] ?? [], $entry['dlopen_dependencies'] ?? []);
        }
        foreach ($extraDlopen as $dependency) {
            $id = is_string($dependency) ? (isset($this->manifest['libraries'][$dependency]) ? $dependency : ($this->sonameOwners[$dependency] ?? null)) : null;
            if ($id === null) {
                throw new RuntimeException('Unknown declared shared-library dependency: ' . json_encode($dependency));
            }
            $ids[$id] = true;
        }
        $expanded = [];
        foreach ($files as $source => $destination) {
            if (!is_string($source) || !is_string($destination)) {
                throw new RuntimeException('Package payload must map source paths to destination paths');
            }
            $this->expandFile($source, $destination, $expanded);
        }
        foreach ($expanded as $source => $destination) {
            if (is_link($source)) {
                continue;
            }
            $details = $this->elf[$source] ?? $this->readElf($source);
            if ($details === null) {
                continue;
            }
            $private = false;
            foreach ($details['needed'] as $soname) {
                if (isset($this->sonameOwners[$soname])) {
                    $ids[$this->sonameOwners[$soname]] = true;
                    $private = true;
                } elseif (!$this->isSystemSoname($soname)) {
                    throw new RuntimeException("Unresolved private shared library {$soname} needed by {$source}");
                }
            }
            if ($private || $extraDlopen !== []) {
                if (!in_array($this->getRuntimeLibdir(), $details['runpath'], true)) {
                    throw new RuntimeException("Missing private DT_RUNPATH on {$source}");
                }
            }
            foreach (array_merge($details['runpath'], $details['rpath']) as $searchPath) {
                if ($searchPath !== $this->getRuntimeLibdir() && !preg_match('/^\$\{?ORIGIN\}?(?:\/[A-Za-z0-9_.+-]+)*$/D', $searchPath)) {
                    throw new RuntimeException("Unexpected runtime search path {$searchPath} on {$source}");
                }
            }
        }
        unset($ids[$ownId ?? '']);
        ksort($ids);
        $dependencies = [];
        foreach (array_keys($ids) as $id) {
            $dependencies[] = ['id' => $id, 'package' => $this->libraryPackageName($id), 'version' => self::normalizeVersion($this->get($id)['version']), 'capability' => $this->abiCapability($id)];
        }
        return $dependencies;
    }

    public static function normalizeVersion(string $raw): string
    {
        $raw = trim($raw);
        if (preg_match('/^(?:v|[A-Za-z][A-Za-z0-9_.-]*?[-_]v?)(?=\d)/i', $raw, $prefix)) {
            $raw = substr($raw, strlen($prefix[0]));
        }
        if (preg_match('/^(\d+\.\d+(?:\.\d+)?)(?:(?:[.-]|~{1,2})?((?:alpha|beta|rc|dev)\d*))?$/i', trim($raw), $match)) {
            $marker = strtolower($match[2] ?? '');
            return $match[1] . ($marker === '' ? '' : (str_starts_with($marker, 'dev') ? '~~' : '~') . $marker);
        }
        return trim($raw);
    }

    public function dependenciesForPackage(array $files, string $type, ?string $ownId = null, array $extraDlopen = []): array
    {
        if (!in_array($type, ['rpm', 'deb', 'apk'], true)) {
            throw new RuntimeException("Unsupported package type {$type}");
        }
        $result = [];
        foreach ($this->dependenciesForFiles($files, $ownId, $extraDlopen) as $dependency) {
            $version = $this->getPackageVersion($dependency['id'], $type);
            if ($version === null) {
                throw new RuntimeException("Missing resolved {$type} package version for {$dependency['id']}");
            }
            $result[] = match ($type) {
                'deb' => $dependency['package'] . ' (>= ' . $version . ')',
                'apk' => $dependency['package'] . '>=' . $version,
                default => $dependency['package'] . ' >= ' . $version,
            };
            $result[] = $dependency['capability'];
        }
        return $result;
    }

    private function source(mixed $relative): string
    {
        if (!is_string($relative) || $relative === '' || str_contains($relative, "\0") || str_contains($relative, '\\') || str_starts_with($relative, '/') || preg_match('#(?:^|/)\.{1,2}(?:/|$)#', $relative)) {
            throw new RuntimeException('Unsafe buildroot path: ' . json_encode($relative));
        }
        $path = $this->root . '/' . $relative;
        $resolved = realpath($path);
        if ($resolved === false || !str_starts_with($resolved, $this->root . '/')) {
            throw new RuntimeException("Missing or escaping shared-library file: {$relative}");
        }
        return $path;
    }

    private function destination(mixed $path): void
    {
        if (!is_string($path) || (!str_starts_with($path, '/usr/') && !str_starts_with($path, '/etc/php-zts/')) || str_contains($path, "\0") || str_contains($path, '\\') || preg_match('#(?:^|/)\.{1,2}(?:/|$)#', $path)) {
            throw new RuntimeException('Unsafe runtime destination: ' . json_encode($path));
        }
    }

    private function expandFile(string $source, string $destination, array &$mapping): void
    {
        if (!file_exists($source) && !is_link($source)) {
            throw new RuntimeException("Missing package payload: {$source}");
        }
        if (str_starts_with($source, $this->root . '/')) {
            $resolved = realpath($source);
            if ($resolved === false || !str_starts_with($resolved, $this->root . '/')) {
                throw new RuntimeException("Escaping buildroot payload: {$source}");
            }
        }
        if (is_link($source) || !is_dir($source)) {
            if (is_link($source) && !str_starts_with($source, $this->root . '/')) {
                $link = readlink($source);
                $target = $link === false ? '' : $this->normalizeAbsolute(dirname($destination) . '/' . $link);
                if ($link === false || str_starts_with($link, '/') || str_contains($link, "\0") || str_contains($link, '\\')
                    || (!str_starts_with($target, '/usr/lib/') && !str_starts_with($target, '/usr/lib64/'))) {
                    throw new RuntimeException("Invalid package compatibility symlink: {$source}");
                }
                $mapping[$source] = $destination;
                return;
            }
            if (!is_file($source)) {
                throw new RuntimeException("Invalid package payload file: {$source}");
            }
            if (isset($mapping[$source]) && $mapping[$source] !== $destination) {
                throw new RuntimeException("Conflicting destinations for {$source}");
            }
            $mapping[$source] = $destination;
            return;
        }
        foreach (new \DirectoryIterator($source) as $file) {
            if (!$file->isDot()) {
                $this->expandFile($file->getPathname(), rtrim($destination, '/') . '/' . $file->getFilename(), $mapping);
            }
        }
    }

    private function normalizeAbsolute(string $path): string
    {
        $parts = [];
        foreach (explode('/', $path) as $part) {
            if ($part === '..') {
                array_pop($parts);
            } elseif ($part !== '' && $part !== '.') {
                $parts[] = $part;
            }
        }
        return '/' . implode('/', $parts);
    }

    private function readElf(string $source): ?array
    {
        if (is_link($source)) {
            return null;
        }
        $stream = fopen($source, 'rb');
        if ($stream === false) {
            throw new RuntimeException("Cannot inspect package payload {$source}");
        }
        $magic = fread($stream, 4);
        fclose($stream);
        if ($magic !== "\x7fELF") {
            return null;
        }
        $pipes = [];
        $process = proc_open(['readelf', '--wide', '--file-header', '--dynamic', $source], [0 => ['pipe', 'r'], 1 => ['pipe', 'w'], 2 => ['pipe', 'w']], $pipes, null, ['LC_ALL' => 'C'] + getenv());
        if (!is_resource($process)) {
            throw new RuntimeException('Cannot execute readelf');
        }
        fclose($pipes[0]);
        $output = stream_get_contents($pipes[1]);
        $error = stream_get_contents($pipes[2]);
        fclose($pipes[1]);
        fclose($pipes[2]);
        if (proc_close($process) !== 0) {
            throw new RuntimeException("Cannot inspect ELF {$source}: {$error}");
        }
        $architecture = $this->architecture();
        if (!preg_match('/Machine:\s*([^\n]+)/', $output, $machine) || ($architecture === 'x86_64' && !str_contains($machine[1], 'X86-64')) || ($architecture === 'aarch64' && !str_contains($machine[1], 'AArch64'))) {
            throw new RuntimeException("Wrong ELF architecture for {$source}");
        }
        preg_match_all('/\(NEEDED\).*?\[([^\]]+)\]/', $output, $needed);
        preg_match('/\(SONAME\).*?\[([^\]]+)\]/', $output, $soname);
        preg_match('/\(RUNPATH\).*?\[([^\]]*)\]/', $output, $runpath);
        preg_match('/\(RPATH\).*?\[([^\]]*)\]/', $output, $rpath);
        return ['soname' => $soname[1] ?? null, 'needed' => array_values(array_unique($needed[1])), 'runpath' => isset($runpath[1]) && $runpath[1] !== '' ? explode(':', $runpath[1]) : [], 'rpath' => isset($rpath[1]) && $rpath[1] !== '' ? explode(':', $rpath[1]) : []];
    }

    private function isSystemSoname(string $soname): bool
    {
        return preg_match('/^(?:lib(?:c|m|dl|pthread|rt|resolv|anl|util)\.so\.\d+|libc\.musl-[a-z0-9_]+\.so\.\d+|ld-linux[^\/]*\.so\.\d+|ld-musl-[a-z0-9_]+\.so\.\d+|libphp(?:-[a-z0-9.-]+)?\.so)$/D', $soname) === 1;
    }
}
