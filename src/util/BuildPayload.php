<?php

declare(strict_types=1);

namespace staticphp\util;

use RuntimeException;
use Symfony\Component\Process\Process;

final class BuildPayload
{
    private static array $loaded = [];
    private ?string $verificationStamp = null;

    private function __construct(private string $root, private array $data) {}

    public static function load(string $root, bool $required = false, bool $verify = true): ?self
    {
        $resolved = realpath($root);
        $path = ($resolved ?: $root) . '/build-payload.json';
        if (!is_file($path)) {
            if ($required) {
                throw new RuntimeException("Missing recorded build payload: {$path}");
            }
            return null;
        }
        $key = $resolved . ':' . hash_file('sha256', $path);
        if (isset(self::$loaded[$key]) && (!$verify || self::$loaded[$key]->verificationStamp === self::$loaded[$key]->stamp())) {
            return self::$loaded[$key];
        }
        $data = json_decode((string) file_get_contents($path), true, 512, JSON_THROW_ON_ERROR);
        if (($data['schema'] ?? null) !== 1 || !is_array($data['configuration'] ?? null)
            || !in_array($data['architecture'] ?? null, ['x86_64', 'aarch64'], true)
            || !in_array($data['libc'] ?? null, ['glibc', 'musl'], true)
            || !preg_match('/^\d+\.\d+(?:\.\d+(?:(?:alpha|beta|RC)\d+)?)?$/iD', $data['php_version'] ?? '')
            || !is_array($data['files'] ?? null)) {
            throw new RuntimeException("Invalid recorded build payload: {$path}");
        }
        $payload = new self($resolved ?: $root, $data);
        if ($verify) {
            $payload->verify();
        }
        self::$loaded[$key] = $payload;
        return $payload;
    }

    public static function record(string $root, array $configuration, string $phpVersion, string $architecture, ?string $sourceVersion = null, ?array $buildContext = null): self
    {
        $root = realpath($root) ?: throw new RuntimeException('Missing payload root');
        $architecture = match ($architecture) {
            'x86_64', 'amd64' => 'x86_64',
            'aarch64', 'arm64' => 'aarch64',
            default => throw new RuntimeException('Unsupported payload architecture'),
        };
        $libc = ($configuration['extra-env']['SPC_LIBC'] ?? 'glibc');
        if (!in_array($libc, ['glibc', 'musl'], true)) {
            throw new RuntimeException('Unsupported payload libc');
        }
        $data = [
            'schema' => 1,
            'state' => 'recorded',
            'kind' => ($buildContext['kind'] ?? null) === 'libraries-only' ? 'sdk' : 'consumer',
            'php_version' => $phpVersion,
            'source_version' => $sourceVersion ?? $phpVersion,
            'architecture' => $architecture,
            'libc' => $libc,
            'build_platform' => $libc === 'musl' ? 'alpine' : 'el8',
            'configuration' => $configuration,
            'files' => self::inventory($root),
            'packaging_assets' => self::assets($root),
            'skipped_shared_extensions' => is_file($root . '/skipped-shared-extensions.json')
                ? json_decode((string) file_get_contents($root . '/skipped-shared-extensions.json'), true, 512, JSON_THROW_ON_ERROR) : null,
        ];
        if ($buildContext !== null) {
            $data['build_context'] = $buildContext;
            if (isset($buildContext['consumer_build_provenance'])) {
                $data['consumer_build_provenance'] = $buildContext['consumer_build_provenance'];
            }
        }
        $previousPath = $root . '/build-payload.json';
        if (is_file($previousPath)) {
            $previous = json_decode((string) file_get_contents($previousPath), true, 512, JSON_THROW_ON_ERROR);
            $oldPaths = array_intersect_key($previous['configuration']['build-options'] ?? [], array_flip(['with-shared-libdir', 'with-config-file-path']));
            $newPaths = array_intersect_key($configuration['build-options'] ?? [], array_flip(['with-shared-libdir', 'with-config-file-path']));
            if (self::equal($oldPaths, $newPaths)) {
                foreach ($data['files'] as $relative => $identity) {
                    if (self::equal($previous['files'][$relative] ?? null, $identity) && isset($previous['prepared'][$relative])) {
                        $data['prepared'][$relative] = $previous['prepared'][$relative];
                    }
                }
            }
        }
        $sapis = $configuration['sapi'] ?? [];
        $sapis = is_array($sapis) ? $sapis : array_map('trim', explode(',', $sapis));
        if ($data['kind'] === 'consumer' && in_array('frankenphp', $sapis, true) && is_file($root . '/bin/frankenphp')) {
            $probe = new Process([$root . '/bin/frankenphp', '--version'], env: ['LD_LIBRARY_PATH' => $root . '/lib']);
            $probe->setTimeout(60);
            $probe->mustRun();
            if (!preg_match('/FrankenPHP v?(\d+\.\d+\.\d+(?:[A-Za-z0-9.~+-]*))/', $probe->getOutput(), $version)) {
                throw new RuntimeException('Cannot record the built FrankenPHP version');
            }
            $data['frankenphp_version'] = $version[1];
            $data['frankenphp_version_output'] = trim($probe->getOutput());
        }
        $moduleHeader = $root . '/include/php/Zend/zend_modules.h';
        if ($data['kind'] === 'consumer' && is_file($moduleHeader) && preg_match('/^\s*#\s*define\s+ZEND_MODULE_API_NO\s+(\d+)/m', (string) file_get_contents($moduleHeader), $api)) {
            $data['module_api'] = $api[1];
        }
        $manifestPath = $root . '/shared-libraries.json';
        if (is_file($manifestPath)) {
            $manifest = json_decode((string) file_get_contents($manifestPath), true, 512, JSON_THROW_ON_ERROR);
            foreach ($manifest['libraries'] ?? [] as $id => $entry) {
                $data['library_files'][$id] = array_is_list($entry['files']) ? $entry['files'] : array_keys($entry['files']);
                $sonames = array_values(array_unique(array_filter(array_column($entry['elf'] ?? [], 'soname'))));
                sort($sonames);
                $data['library_abi'][$id] = ['epoch' => $entry['abi_epoch'] ?? 1, 'sonames' => $sonames];
                $data['library_build_provenance'][$id] = array_intersect_key($entry, array_flip(['source_hash', 'version', 'build_identity', 'deployment_relocation']));
                if ($id === 'openssl') {
                    $files = array_is_list($entry['files']) ? $entry['files'] : array_keys($entry['files']);
                    $data['openssl_files'] = array_values(array_unique(array_merge($files, array_keys($entry['runtime_assets'] ?? []), array_values($entry['abi_dump'] ?? []))));
                }
            }
        }
        $payload = new self($root, $data);
        $payload->save();
        return $payload;
    }

    public static function seal(string $root): self
    {
        $payload = self::load($root, true, false);
        $inventory = self::inventory($payload->root);
        $variant = null;
        $changed = array_keys(array_filter($inventory, static fn (array $file, string $relative): bool => !self::equal($payload->data['files'][$relative] ?? null, $file), ARRAY_FILTER_USE_BOTH));
        $removed = array_diff(array_keys($payload->data['files']), array_keys($inventory));
        if ($changed !== [] || $removed !== []) {
            $variant = $payload->validateVariant(array_values(array_unique(array_merge($changed, $removed))));
        }
        if (is_file($payload->root . '/openssl-variant.json')) {
            $variant = $payload->validateVariant($changed ?? []);
        }
        $prepared = [];
        foreach ($inventory as $relative => $identity) {
            $previous = $payload->data['prepared'][$relative] ?? null;
            if ($previous !== null && self::equal($payload->data['files'][$relative] ?? null, $identity)
                && self::matches($payload->root, $previous['runtime'])
                && (!isset($previous['debug']) || self::matches($payload->root, $previous['debug']))) {
                $prepared[$relative] = $previous;
                continue;
            }
            $source = $payload->root . '/' . $relative;
            $header = self::command(['readelf', '--wide', '--file-header', $source]);
            $machine = $payload->architecture() === 'x86_64' ? 'X86-64' : 'AArch64';
            if (!preg_match('/Machine:\s*([^\n]+)/', $header, $actualMachine) || !str_contains($actualMachine[1], $machine)) {
                throw new RuntimeException("Wrong ELF architecture in recorded payload: {$relative}");
            }
            $runtime = $payload->root . '/payload/runtime/' . $relative;
            self::directory(dirname($runtime));
            if (!copy($source, $runtime) || !chmod($runtime, $identity['mode'])) {
                throw new RuntimeException("Cannot prepare payload ELF {$relative}");
            }
            $dynamic = self::command(['readelf', '--wide', '--dynamic', $runtime]);
            preg_match_all('/\((?:RUNPATH|RPATH)\).*?\[([^\]]+)\]/', $dynamic, $paths);
            $private = $payload->data['configuration']['build-options']['with-shared-libdir'] ?? '/usr/lib/php-zts/lib';
            $search = array_values(array_unique(array_filter(explode(':', implode(':', $paths[1])))));
            $allowed = array_values(array_filter($search, static fn (string $path): bool => $path === $private || preg_match('/^\$\{?ORIGIN\}?(?:\/[A-Za-z0-9_.+-]+)*$/D', $path)));
            if ($search !== [] && ($search !== $allowed || str_contains($dynamic, '(RPATH)'))) {
                if (!in_array($private, $allowed, true)) {
                    $allowed[] = $private;
                }
                self::command(['patchelf', '--set-rpath', implode(':', $allowed), $runtime]);
            }
            $sections = self::command(['readelf', '--wide', '--sections', $runtime]);
            $debug = $payload->root . '/payload/debug/' . dirname($relative) . '/' . $payload->debugName($relative);
            $hasDebug = preg_match('/\.(?:zdebug|debug)_(?:info|types)\s/', $sections) === 1;
            if ($hasDebug) {
                self::directory(dirname($debug));
                self::command(['objcopy', '--only-keep-debug', $runtime, $debug]);
            } else {
                foreach ([$payload->root . '/debug/' . $relative . '.debug', $payload->root . '/debug/' . basename($relative) . '.debug'] as $candidate) {
                    if (is_file($candidate) && self::matchingDebugInfo($source, $candidate)) {
                        self::directory(dirname($debug));
                        if (!copy($candidate, $debug)) {
                            throw new RuntimeException("Cannot prepare debuginfo {$relative}");
                        }
                        $hasDebug = true;
                        break;
                    }
                }
            }
            self::command(['objcopy', '--remove-section=.gnu_debuglink', $runtime]);
            self::command(['strip', '--strip-unneeded', $runtime]);
            if ($hasDebug) {
                self::command(['objcopy', '--add-gnu-debuglink=' . $debug, $runtime]);
            }
            $prepared[$relative] = ['runtime' => self::identity($payload->root, $runtime)];
            if ($hasDebug) {
                $prepared[$relative]['debug'] = self::identity($payload->root, $debug);
            }
        }
        $payload->data['files'] = $inventory;
        $payload->data['prepared'] = $prepared;
        $payload->data['state'] = 'sealed';
        if ($variant !== null) {
            $payload->data['openssl_variant'] = $variant;
        }
        $payload->save();
        $payload->verify();
        return $payload;
    }

    public static function refreshAssets(string $root, string $downloads): self
    {
        $payload = self::load($root, true);
        if (!$payload->isSealed()) {
            throw new RuntimeException('Package asset refresh requires a sealed payload');
        }
        $source = $downloads . '/pie.phar';
        $metadataPath = $downloads . '/pie.json';
        if (!is_file($source) || !is_file($metadataPath) || is_link($source) || is_link($metadataPath)) {
            throw new RuntimeException('Frozen PIE executable and source metadata are required');
        }
        $metadata = json_decode((string) file_get_contents($metadataPath), true, 512, JSON_THROW_ON_ERROR);
        if (($metadata['schema'] ?? null) !== 1 || !preg_match('/^\d+\.\d+\.\d+[A-Za-z0-9.+~-]*$/D', $metadata['version'] ?? '')
            || !preg_match('/^[a-f0-9]{64}$/D', $metadata['sha256'] ?? '')
            || hash_file('sha256', $source) !== $metadata['sha256']) {
            throw new RuntimeException('Frozen PIE executable does not match its source metadata');
        }
        $snapshot = is_file($downloads . '/.spc-meta.json')
            ? json_decode((string) file_get_contents($downloads . '/.spc-meta.json'), true, 512, JSON_THROW_ON_ERROR) : [];
        foreach (['pie_version' => 'version', 'pie_sha256' => 'sha256'] as $field => $name) {
            if (isset($snapshot[$field]) && $snapshot[$field] !== $metadata[$name]) {
                throw new RuntimeException('Frozen PIE asset differs from the selected source snapshot');
            }
        }
        $parent = $payload->root . '/packaging';
        if (!is_dir($parent) && !mkdir($parent, 0755, true) && !is_dir($parent)) {
            throw new RuntimeException('Cannot create package asset directory');
        }
        $target = $parent . '/pie';
        if (is_link($target)) {
            throw new RuntimeException('Frozen PIE asset directory must not be a symlink');
        }
        $stage = $payload->root . '/.pie-refresh-' . bin2hex(random_bytes(8));
        $backup = $stage . '-previous';
        $previous = $payload->data;
        $installed = false;
        try {
            if (!mkdir($stage, 0755) || !copy($source, $stage . '/pie.phar') || !chmod($stage . '/pie.phar', 0755)
                || !copy($metadataPath, $stage . '/metadata.json') || !chmod($stage . '/metadata.json', 0644)) {
                throw new RuntimeException('Cannot stage frozen PIE assets');
            }
            if (is_dir($target) && !rename($target, $backup)) {
                throw new RuntimeException('Cannot preserve previous frozen PIE assets');
            }
            if (!rename($stage, $target)) {
                throw new RuntimeException('Cannot install frozen PIE assets');
            }
            $installed = true;
            $payload->data['packaging_assets'] = self::assets($payload->root);
            $payload->data['asset_updates'][] = ['kind' => 'frozen-pie', 'version' => $metadata['version'],
                'sha256' => $metadata['sha256'], 'source_metadata_sha256' => hash_file('sha256', $metadataPath),
                'previous_sha256' => $previous['packaging_assets']['packaging/pie/pie.phar']['sha256'] ?? null];
            $payload->save();
            $payload->verify();
        } catch (\Throwable $error) {
            if ($installed) {
                self::command(['rm', '-rf', '--', $target]);
            }
            if (is_dir($backup)) {
                rename($backup, $target);
            }
            $payload->data = $previous;
            $payload->save();
            throw $error;
        } finally {
            self::command(['rm', '-rf', '--', $stage, $backup]);
        }
        return $payload;
    }

    public static function graft(string $root, array $cached, array $reuseProof = []): self
    {
        if (($cached['schema'] ?? null) !== 1 || ($cached['state'] ?? null) !== 'sealed'
            || ($cached['kind'] ?? 'consumer') !== 'consumer'
            || !is_array($cached['configuration'] ?? null) || !is_array($cached['files'] ?? null)) {
            throw new RuntimeException('Consumer cache has no sealed build provenance');
        }
        $currentManifest = $root . '/shared-libraries.json';
        $manifest = is_file($currentManifest) ? json_decode((string) file_get_contents($currentManifest), true, 512, JSON_THROW_ON_ERROR) : [];
        if (isset($manifest['architecture'])) {
            $architecture = match ($manifest['architecture']) { 'amd64' => 'x86_64', 'arm64' => 'aarch64', default => $manifest['architecture'] };
            if ($architecture !== ($cached['architecture'] ?? null) || ($manifest['libc'] ?? 'glibc') !== ($cached['libc'] ?? null)) {
                throw new RuntimeException('Consumer cache does not match the current SDK architecture/libc');
            }
        }
        [$abi, $removed] = $reuseProof === [] ? [$cached['library_abi'] ?? [], []] : self::verifyAbiReuseProof($root, $cached, $manifest, $reuseProof);
        foreach ($abi as $id => $identity) {
            $entry = $manifest['libraries'][$id] ?? null;
            $sonames = $entry === null ? [] : array_values(array_unique(array_filter(array_column($entry['elf'] ?? [], 'soname'))));
            sort($sonames);
            if ($entry === null || ($entry['abi_epoch'] ?? 1) !== ($identity['epoch'] ?? null)
                || array_diff($identity['sonames'] ?? [], $sonames) !== []) {
                throw new RuntimeException("Consumer cache requires a different library ABI: {$id}");
            }
        }
        if (!empty($manifest['libraries']) && (empty($cached['library_abi']) || !is_array($cached['library_files'] ?? null))) {
            throw new RuntimeException('Consumer cache lacks recorded SDK ownership/ABI identities');
        }
        foreach ($cached['files'] as $relative => $identity) {
            $consumer = str_starts_with($relative, 'modules/') || preg_match('#^bin/(?:php(?:$|[-.]|dbg)|frankenphp)#', $relative)
                || preg_match('#^lib/libphp[^/]*\.so#', $relative);
            if ($consumer && !isset($removed[$relative]) && !self::matches($root, ['path' => $relative] + $identity)) {
                throw new RuntimeException('Cached PHP consumer bytes were not restored: ' . $relative);
            }
        }
        $currentAssets = self::assets($root);
        $consumerAsset = static fn (string $relative): bool => str_starts_with($relative, 'packaging/')
            || str_starts_with($relative, 'include/php/') || str_starts_with($relative, 'lib/php/build/')
            || in_array($relative, ['bin/php-config', 'bin/phpize'], true);
        if (!self::equal(array_filter($currentAssets, $consumerAsset, ARRAY_FILTER_USE_KEY),
            array_filter($cached['packaging_assets'] ?? [], $consumerAsset, ARRAY_FILTER_USE_KEY))) {
            throw new RuntimeException('Cached PHP package/development assets were not restored');
        }
        $skips = is_file($root . '/skipped-shared-extensions.json')
            ? json_decode((string) file_get_contents($root . '/skipped-shared-extensions.json'), true, 512, JSON_THROW_ON_ERROR) : null;
        if (!self::equal($skips, $cached['skipped_shared_extensions'] ?? null)) {
            throw new RuntimeException('Cached PHP shared-extension skip manifest was not restored');
        }
        $payload = self::record($root, $cached['configuration'], $cached['php_version'], $cached['architecture'], $cached['source_version'] ?? $cached['php_version']);
        foreach ($cached['prepared'] ?? [] as $relative => $entry) {
            if (self::equal($cached['files'][$relative] ?? null, $payload->data['files'][$relative] ?? null)
                && isset($payload->data['files'][$relative]) && self::matches($payload->root, $entry['runtime'])
                && (!isset($entry['debug']) || self::matches($payload->root, $entry['debug']))) {
                $payload->data['prepared'][$relative] = $entry;
            }
        }
        $payload->data['consumer_build_provenance'] = $cached['consumer_build_provenance'] ?? array_intersect_key($cached,
            array_flip(['php_version', 'source_version', 'architecture', 'libc', 'build_platform', 'configuration', 'module_api', 'library_build_provenance']));
        $payload->data['consumer_cache_record_sha256'] = hash('sha256', json_encode($cached, JSON_UNESCAPED_SLASHES | JSON_THROW_ON_ERROR));
        if ($reuseProof !== []) {
            $payload->data['consumer_cache_abi_reuse_proof'] = $reuseProof;
        }
        $payload->save();
        return self::seal($root);
    }

    private static function verifyAbiReuseProof(string $root, array $cached, array $manifest, array $proof): array
    {
        $metadataPath = $proof['cached_metadata_path'] ?? null;
        $metadataFile = is_string($metadataPath) ? $root . '/' . $metadataPath : '';
        if (($proof['schema'] ?? null) !== 1 || !self::matches($root, [
            'path' => $metadataPath, 'sha256' => $proof['cached_metadata_sha256'] ?? null,
            'mode' => is_file($metadataFile) ? fileperms($metadataFile) & 0777 : null,
        ]) || !self::equal(json_decode((string) file_get_contents($metadataFile), true, 512, JSON_THROW_ON_ERROR), $cached)) {
            throw new RuntimeException('ABI reuse proof does not identify the original sealed consumer metadata');
        }
        foreach (['rebuild_packages', 'incompatible_libraries', 'removed_files', 'retained_dlopen_libraries'] as $field) {
            if (!is_array($proof[$field] ?? null) || !array_is_list($proof[$field])
                || count(array_unique($proof[$field], SORT_REGULAR)) !== count($proof[$field])
                || array_filter($proof[$field], static fn ($item): bool => !is_string($item) || $item === '') !== []) {
                throw new RuntimeException('Invalid ABI reuse proof ' . $field);
            }
        }
        $list = static fn (array|string $value): array => is_array($value) ? $value : array_values(array_filter(array_map('trim', explode(',', $value))));
        $shared = $list($cached['configuration']['shared-extensions'] ?? []);
        $sapis = $list($cached['configuration']['sapi'] ?? []);
        $normalize = static fn (string $component): string => str_replace('_', '-', $component);
        $allowed = array_map($normalize, array_merge($shared, in_array('frankenphp', $sapis, true) ? ['frankenphp'] : []));
        $rebuilt = array_map($normalize, $proof['rebuild_packages']);
        if (array_diff($rebuilt, $allowed) !== []) {
            throw new RuntimeException('ABI reuse may remove only separately rebuilt shared extensions or FrankenPHP');
        }
        $expectedRemoved = [];
        foreach ($cached['files'] as $relative => $identity) {
            $component = $relative === 'bin/frankenphp' ? 'frankenphp' : null;
            if (preg_match('#^modules/([^/]+?)(?:-(?:zts|nts)-\d+)?\.so$#D', $relative, $match)) {
                $component = str_replace('_', '-', $match[1]);
            }
            if ($component !== null && in_array($component, $rebuilt, true)) {
                $expectedRemoved[] = $relative;
                foreach ($cached['prepared'][$relative] ?? [] as $entry) {
                    $expectedRemoved[] = $entry['path'];
                }
            }
        }
        $removed = $proof['removed_files'];
        sort($expectedRemoved);
        sort($removed);
        if ($removed !== $expectedRemoved) {
            throw new RuntimeException('ABI reuse proof removed files differ from the selected shared consumers');
        }
        foreach ($removed as $relative) {
            if (file_exists($root . '/' . $relative) || is_link($root . '/' . $relative)) {
                throw new RuntimeException('Incompatible cached consumer was not removed: ' . $relative);
            }
        }
        $removed = array_fill_keys($removed, true);
        $incompatible = [];
        $owners = [];
        foreach ($cached['library_abi'] ?? [] as $id => $identity) {
            foreach ($identity['sonames'] ?? [] as $soname) {
                if (isset($owners[$soname]) && $owners[$soname] !== $id) {
                    throw new RuntimeException('Cached library ABI has conflicting SONAME owners');
                }
                $owners[$soname] = $id;
            }
            $entry = $manifest['libraries'][$id] ?? null;
            $sonames = $entry === null ? [] : array_values(array_unique(array_filter(array_column($entry['elf'] ?? [], 'soname'))));
            if ($entry === null || ($entry['abi_epoch'] ?? 1) !== ($identity['epoch'] ?? null)
                || array_diff($identity['sonames'] ?? [], $sonames) !== []) {
                $incompatible[] = $id;
            }
        }
        sort($incompatible);
        $declared = $proof['incompatible_libraries'];
        sort($declared);
        if ($declared !== $incompatible) {
            throw new RuntimeException('ABI reuse proof does not cover the changed SDK library ABIs');
        }
        $graph = $proof['library_dependencies'] ?? null;
        if (!is_array($graph) || array_diff(array_keys($cached['library_abi'] ?? []), array_keys($graph)) !== []) {
            throw new RuntimeException('ABI reuse proof has no complete cached library dependency graph');
        }
        foreach ($graph as $id => $dependencies) {
            if (!isset($cached['library_abi'][$id]) || !is_array($dependencies) || !array_is_list($dependencies)
                || array_filter($dependencies, static fn ($dependency): bool => !is_string($dependency)) !== []
                || array_diff($dependencies, array_keys($cached['library_abi'])) !== []) {
                throw new RuntimeException('Invalid cached library dependency graph in ABI reuse proof');
            }
        }
        $retained = array_fill_keys($proof['retained_dlopen_libraries'], true);
        if (array_diff(array_keys($retained), array_keys($cached['library_abi'] ?? [])) !== []) {
            throw new RuntimeException('ABI reuse proof names an unknown retained dlopen library');
        }
        foreach ($cached['files'] as $relative => $identity) {
            $consumer = str_starts_with($relative, 'modules/') || preg_match('#^bin/(?:php(?:$|[-.]|dbg)|frankenphp)#', $relative)
                || preg_match('#^lib/libphp[^/]*\.so#', $relative);
            if (!$consumer || isset($removed[$relative])) {
                continue;
            }
            if (!self::matches($root, ['path' => $relative] + $identity)) {
                throw new RuntimeException('Cached PHP consumer bytes were not restored: ' . $relative);
            }
            preg_match_all('/\(NEEDED\).*\[([^\]]+)\]/', self::command(['readelf', '--wide', '--dynamic', $root . '/' . $relative]), $needed);
            foreach ($needed[1] as $soname) {
                if (isset($owners[$soname])) {
                    $retained[$owners[$soname]] = true;
                }
            }
        }
        do {
            $previous = $retained;
            foreach (array_keys($retained) as $id) {
                $retained += array_fill_keys($graph[$id], true);
            }
        } while ($previous !== $retained);
        if (array_intersect(array_keys($retained), $incompatible) !== []) {
            throw new RuntimeException('Retained PHP consumers require a changed SDK library ABI');
        }
        $abi = array_intersect_key($cached['library_abi'] ?? [], $retained);
        if (!is_array($proof['retained_library_abi'] ?? null) || !self::equal($abi, $proof['retained_library_abi'])) {
            throw new RuntimeException('ABI reuse proof retained library identities do not match the retained consumers');
        }
        return [$abi, $removed];
    }

    public static function verifiedCoreSeed(string $root, ?array $packages, string $sourceVersion, array $availableShared = [], array $temporaryBuildScripts = []): ?array
    {
        $path = $root . '/consumer-cache-seed.json';
        if ($packages === null || !is_file($path) || array_intersect($packages, ['php', 'cli', 'cgi', 'fpm', 'embed', 'devel'])) {
            return null;
        }
        $receipt = json_decode((string) file_get_contents($path), true, 512, JSON_THROW_ON_ERROR);
        $requested = $packages;
        sort($requested);
        $recorded = $receipt['rebuild_packages'] ?? [];
        sort($recorded);
        if (($receipt['schema'] ?? null) !== 1 || ($receipt['state'] ?? null) !== 'verified'
            || $requested !== $recorded || !is_array($receipt['core_files'] ?? null)
            || !preg_match('/^[a-f0-9]{64}$/D', $receipt['key'] ?? '')
            || !preg_match('/^[a-f0-9]{64}$/D', $receipt['seed_key'] ?? '')) {
            throw new RuntimeException('Partial build requires a matching verified consumer core seed');
        }
        $metadataPath = $receipt['cached_metadata_path'] ?? '';
        $metadataFile = $root . '/' . $metadataPath;
        if (!self::matches($root, ['path' => $metadataPath, 'sha256' => $receipt['cached_metadata_sha256'] ?? null,
            'mode' => is_file($metadataFile) ? fileperms($metadataFile) & 0777 : null])) {
            throw new RuntimeException('Cached core build provenance changed');
        }
        $cached = json_decode((string) file_get_contents($metadataFile), true, 512, JSON_THROW_ON_ERROR);
        if (($cached['state'] ?? null) !== 'sealed' || strcasecmp($cached['source_version'] ?? $cached['php_version'] ?? '', $sourceVersion) !== 0
            || !str_starts_with($sourceVersion, ($receipt['php_stream'] ?? '?') . '.')) {
            throw new RuntimeException('Cached core belongs to a different PHP source release');
        }
        $shared = $cached['configuration']['shared-extensions'] ?? '';
        $shared = is_array($shared) ? $shared : array_filter(array_map('trim', explode(',', $shared)));
        if (array_diff($requested, array_merge(['frankenphp'], $shared, $availableShared)) !== []) {
            throw new RuntimeException('Partial core reuse is limited to separately built extensions and FrankenPHP');
        }
        foreach ($receipt['core_files'] as $relative => $identity) {
            if (isset($identity['symlink'])) {
                $file = $root . '/' . $relative;
                $resolved = realpath($file);
                if (str_starts_with($relative, '/') || preg_match('#(?:^|/)\.{1,2}(?:/|$)#', $relative)
                    || !is_link($file) || readlink($file) !== $identity['symlink'] || $resolved === false
                    || !str_starts_with($resolved, (realpath($root) ?: $root) . '/')) {
                    throw new RuntimeException('Cached core symlink changed: ' . $relative);
                }
            } elseif (!self::matches($root, ['path' => $relative] + $identity)) {
                $original = $temporaryBuildScripts[$relative] ?? null;
                if (!in_array($relative, ['bin/php-config', 'lib/php/build/phpize.m4'], true) || !is_string($original)
                    || !is_file($original) || is_link($original) || hash_file('sha256', $original) !== ($identity['sha256'] ?? null)
                    || (fileperms($original) & 0777) !== ($identity['mode'] ?? null)) {
                    throw new RuntimeException('Cached core input changed: ' . $relative);
                }
                $contents = file_get_contents($original);
                $temporary = $relative === 'bin/php-config'
                    ? preg_replace('/^extension_dir=.*$/m', 'extension_dir="' . $root . '/modules"', $contents)
                    : str_replace('test "[$]$1" = "no" && $1=yes', '# test "[$]$1" = "no" && $1=yes', $contents);
                if ($temporary === $contents || !is_file($root . '/' . $relative) || is_link($root . '/' . $relative)
                    || file_get_contents($root . '/' . $relative) !== $temporary
                    || (fileperms($root . '/' . $relative) & 0777) !== $identity['mode']) {
                    throw new RuntimeException('Unverified temporary build-script change: ' . $relative);
                }
            }
        }
        foreach (['bin/php', 'bin/phpize', 'bin/php-config', 'include/php/Zend/zend_modules.h', 'lib/libphp.so'] as $required) {
            if (!isset($receipt['core_files'][$required]) || !is_file($root . '/' . $required)) {
                throw new RuntimeException('Cached core is incomplete: ' . $required);
            }
        }
        return ['metadata' => $cached, 'receipt' => $receipt];
    }

    public function configuration(): array { return $this->data['configuration']; }
    public function phpVersion(): string { return $this->data['php_version']; }
    public function architecture(): string { return $this->data['architecture']; }
    public function isSealed(): bool { return ($this->data['state'] ?? '') === 'sealed'; }
    public function metadata(): array { return $this->data; }

    public function prepareFile(string $source): array
    {
        $relative = substr($source, strlen($this->root) + 1);
        if (str_starts_with($source, $this->root . '/') && !isset($this->data['files'][$relative])) {
            foreach ($this->data['prepared'] ?? [] as $original => $entry) {
                if ($entry['runtime']['path'] === $relative) {
                    $relative = $original;
                    break;
                }
            }
        }
        if (!str_starts_with($source, $this->root . '/') || !isset($this->data['files'][$relative])) {
            if (str_starts_with($source, $this->root . '/') && is_file($source) && !is_link($source)
                && file_get_contents($source, false, null, 0, 4) === "\x7fELF") {
                throw new RuntimeException('Unrecorded ELF cannot enter a sealed package: ' . $relative);
            }
            return ['runtime' => $source, 'debug' => null];
        }
        if (!$this->isSealed() || !isset($this->data['prepared'][$relative])) {
            throw new RuntimeException("Payload must be sealed before packaging {$relative}");
        }
        $entry = $this->data['prepared'][$relative];
        if (!self::matches($this->root, ['path' => $relative] + $this->data['files'][$relative])
            || !self::matches($this->root, $entry['runtime'])
            || (isset($entry['debug']) && !self::matches($this->root, $entry['debug']))) {
            throw new RuntimeException("Sealed payload changed before packaging {$relative}");
        }
        return ['runtime' => $this->root . '/' . $entry['runtime']['path'], 'debug' => isset($entry['debug']) ? $this->root . '/' . $entry['debug']['path'] : null];
    }

    public function verify(): void
    {
        if (!self::equal(self::inventory($this->root), $this->data['files'])) {
            throw new RuntimeException('Recorded payload ELF inputs changed; rebuild or apply a verified OpenSSL variant before sealing.');
        }
        if (!self::equal(self::assets($this->root), $this->data['packaging_assets'] ?? [])) {
            throw new RuntimeException('Recorded package assets changed');
        }
        $skips = is_file($this->root . '/skipped-shared-extensions.json')
            ? json_decode((string) file_get_contents($this->root . '/skipped-shared-extensions.json'), true, 512, JSON_THROW_ON_ERROR) : null;
        if (!self::equal($skips, $this->data['skipped_shared_extensions'] ?? null)) {
            throw new RuntimeException('Recorded shared-extension skip manifest changed');
        }
        if (!$this->isSealed()) {
            $this->verificationStamp = $this->stamp();
            return;
        }
        foreach ($this->data['prepared'] ?? [] as $files) {
            foreach ($files as $file) {
                if (!self::matches($this->root, $file)) {
                    throw new RuntimeException('Sealed payload ELF/debug bytes changed: ' . ($file['path'] ?? 'unknown'));
                }
            }
        }
        if (array_keys($this->data['prepared'] ?? []) !== array_keys($this->data['files'])) {
            throw new RuntimeException('Incomplete sealed payload ledger');
        }
        $this->verificationStamp = $this->stamp();
    }

    private function stamp(): string
    {
        $paths = array_keys($this->data['files']);
        foreach ($this->data['prepared'] ?? [] as $files) {
            foreach ($files as $file) {
                $paths[] = $file['path'];
            }
        }
        $paths = array_merge($paths, array_keys($this->data['packaging_assets'] ?? []), ['skipped-shared-extensions.json']);
        $state = [];
        foreach (['bin', 'lib', 'modules', 'usr', 'include', 'packaging', 'payload'] as $directory) {
            if (is_dir($this->root . '/' . $directory)) {
                foreach (new \RecursiveIteratorIterator(new \RecursiveDirectoryIterator($this->root . '/' . $directory, \FilesystemIterator::SKIP_DOTS), \RecursiveIteratorIterator::SELF_FIRST) as $file) {
                    $state['member:' . substr($file->getPathname(), strlen($this->root) + 1)] = $file->isLink() ? 'link' : ($file->isDir() ? 'directory' : 'file');
                }
            }
        }
        foreach ($paths as $relative) {
            $path = $this->root . '/' . $relative;
            clearstatcache(true, $path);
            $stat = @lstat($path);
            $state[$relative] = $stat === false ? null : [$stat['size'], $stat['mode'], $stat['mtime'], $stat['ctime'], $stat['ino']];
        }
        return hash('sha256', json_encode($state, JSON_THROW_ON_ERROR));
    }

    private function validateVariant(array $changed): array
    {
        $path = $this->root . '/openssl-variant.json';
        $variant = is_file($path) ? json_decode((string) file_get_contents($path), true, 512, JSON_THROW_ON_ERROR) : [];
        if (($variant['schema'] ?? null) !== 1 || !in_array($variant['profile'] ?? null, ['rpm', 'deb'], true)
            || !is_array($variant['files'] ?? null) || !is_array($variant['modified_files'] ?? null)
            || !preg_match('/^[a-f0-9]{64}$/D', $variant['input_fingerprint'] ?? '')
            || !preg_match('/^[a-f0-9]{64}$/D', $variant['overlay_receipt_sha256'] ?? '')) {
            throw new RuntimeException('Sealed payload inputs changed without a declared OpenSSL variant');
        }
        foreach ($changed as $relative) {
            $record = $variant['files'][$relative] ?? null;
            if (!in_array($relative, $variant['modified_files'], true) || !is_array($record)
                || !in_array($relative, $this->data['openssl_files'] ?? [], true)
                || !self::matches($this->root, ['path' => $relative] + $record)) {
                throw new RuntimeException("OpenSSL variant cannot change common ELF {$relative}");
            }
        }
        foreach ($variant['files'] as $relative => $record) {
            $owned = in_array($relative, $this->data['openssl_files'] ?? [], true)
                || (str_starts_with($relative, 'debug/') && str_ends_with($relative, '.debug') && in_array(substr($relative, 6, -6), $this->data['openssl_files'] ?? [], true));
            if (!$owned || !is_array($record)) {
                throw new RuntimeException('OpenSSL variant includes an unowned file: ' . $relative);
            }
            if (isset($record['symlink'])) {
                $path = $this->root . '/' . $relative;
                $actual = is_link($path) ? readlink($path) : false;
                if ($actual !== $record['symlink'] || hash('sha256', 'symlink:' . $actual) !== ($record['sha256'] ?? null)) {
                    throw new RuntimeException('OpenSSL variant symlink changed: ' . $relative);
                }
            } elseif (!self::matches($this->root, ['path' => $relative] + $record)) {
                throw new RuntimeException('OpenSSL variant file changed: ' . $relative);
            }
        }
        return ['profile' => $variant['profile'], 'input_fingerprint' => $variant['input_fingerprint'], 'receipt_sha256' => $variant['overlay_receipt_sha256']];
    }

    private function debugName(string $relative): string
    {
        $name = basename($relative);
        if (str_starts_with($relative, 'bin/') && in_array($name, ['php', 'php-cgi', 'php-fpm', 'phpdbg'], true)) {
            $prefix = basename($this->data['configuration']['build-options']['with-config-file-path'] ?? '/etc/php-zts');
            $name .= str_starts_with($prefix, 'php') ? substr($prefix, 3) : '-zts';
        }
        if ($relative === 'bin/frankenphp') {
            $prefix = basename($this->data['configuration']['build-options']['with-config-file-path'] ?? '/etc/php-zts');
            if (preg_match('/(\d+\.?\d*)/', $prefix, $version)) {
                $name .= $version[1];
            }
        }
        return $name . '.debug';
    }

    private static function inventory(string $root): array
    {
        $files = [];
        foreach (['bin', 'lib', 'modules', 'usr'] as $directory) {
            if (!is_dir($root . '/' . $directory)) {
                continue;
            }
            $iterator = new \RecursiveIteratorIterator(new \RecursiveDirectoryIterator($root . '/' . $directory, \FilesystemIterator::SKIP_DOTS));
            foreach ($iterator as $file) {
                if (!$file->isFile() || $file->isLink() || str_ends_with($file->getFilename(), '.debug') || $file->getSize() < 4) {
                    continue;
                }
                $path = $file->getPathname();
                if (file_get_contents($path, false, null, 0, 4) !== "\x7fELF") {
                    continue;
                }
                $relative = substr($path, strlen($root) + 1);
                if (str_starts_with($relative, 'usr/lib/debug/')) {
                    continue;
                }
                $files[$relative] = ['sha256' => hash_file('sha256', $path), 'mode' => fileperms($path) & 0777];
            }
        }
        ksort($files);
        return $files;
    }

    private static function identity(string $root, string $file): array
    {
        return ['path' => substr($file, strlen($root) + 1), 'sha256' => hash_file('sha256', $file), 'mode' => fileperms($file) & 0777];
    }

    private static function assets(string $root): array
    {
        $files = [];
        foreach (['packaging', 'include', 'lib/php/build'] as $directory) {
            if (!is_dir($root . '/' . $directory)) {
                continue;
            }
            foreach (new \RecursiveIteratorIterator(new \RecursiveDirectoryIterator($root . '/' . $directory, \FilesystemIterator::SKIP_DOTS)) as $file) {
                if (!$file->isFile() && !$file->isLink()) {
                    continue;
                }
                if ($directory === 'lib/php/build' && str_ends_with($file->getFilename(), '.bak')) {
                    continue;
                }
                $path = $file->getPathname();
                $files[substr($path, strlen($root) + 1)] = $file->isLink()
                    ? ['symlink' => readlink($path)]
                    : ['sha256' => hash_file('sha256', $path), 'mode' => fileperms($path) & 0777];
            }
        }
        foreach (['bin/php-config', 'bin/phpize'] as $relative) {
            $path = $root . '/' . $relative;
            if (is_file($path)) {
                $files[$relative] = ['sha256' => hash_file('sha256', $path), 'mode' => fileperms($path) & 0777];
            }
        }
        ksort($files);
        return $files;
    }

    private static function matches(string $root, array $file): bool
    {
        $relative = $file['path'] ?? null;
        if (!is_string($relative) || str_starts_with($relative, '/') || str_contains($relative, '\\') || preg_match('#(?:^|/)\.{1,2}(?:/|$)#', $relative)) {
            return false;
        }
        $path = $root . '/' . $relative;
        return is_file($path) && !is_link($path) && hash_file('sha256', $path) === ($file['sha256'] ?? null)
            && (fileperms($path) & 0777) === ($file['mode'] ?? null);
    }

    private static function matchingDebugInfo(string $first, string $second): bool
    {
        preg_match('/Build ID:\s*(\S+)/', self::command(['readelf', '--notes', $first]), $a);
        preg_match('/Build ID:\s*(\S+)/', self::command(['readelf', '--notes', $second]), $b);
        if (isset($a[1], $b[1])) {
            return $a[1] === $b[1];
        }
        $section = self::command(['readelf', '--wide', '--hex-dump=.gnu_debuglink', $first]);
        preg_match_all('/^\s*0x[0-9a-f]+\s+((?:[0-9a-f]{8}\s*){1,4})/mi', $section, $words);
        $bytes = hex2bin(preg_replace('/\s+/', '', implode('', $words[1])));
        if ($bytes === false || ($end = strpos($bytes, "\0")) === false || strlen($bytes) < 8
            || substr($bytes, 0, $end) !== basename($second)) {
            return false;
        }
        $offset = ($end + 4) & ~3;
        if (strlen($bytes) !== $offset + 4) {
            return false;
        }
        $crc = unpack('Vcrc', substr($bytes, $offset, 4))['crc'];
        return sprintf('%08x', $crc) === hash_file('crc32b', $second);
    }

    private static function command(array $arguments): string
    {
        $process = new Process($arguments, env: ['LC_ALL' => 'C']);
        $process->setTimeout(null);
        $process->mustRun();
        return $process->getOutput();
    }

    private static function equal(mixed $first, mixed $second): bool
    {
        if (!is_array($first) || !is_array($second)) {
            return $first === $second;
        }
        if (array_is_list($first) !== array_is_list($second)) {
            return false;
        }
        if (!array_is_list($first)) {
            ksort($first);
            ksort($second);
        }
        if (array_keys($first) !== array_keys($second)) {
            return false;
        }
        foreach ($first as $key => $value) {
            if (!self::equal($value, $second[$key])) {
                return false;
            }
        }
        return true;
    }

    private static function directory(string $path): void
    {
        if (!is_dir($path) && !mkdir($path, 0755, true) && !is_dir($path)) {
            throw new RuntimeException("Cannot create payload directory {$path}");
        }
    }

    private function save(): void
    {
        $contents = json_encode($this->data, JSON_PRETTY_PRINT | JSON_UNESCAPED_SLASHES | JSON_THROW_ON_ERROR) . "\n";
        $path = $this->root . '/build-payload.json';
        $temporary = tempnam($this->root, '.build-payload-');
        try {
            if ($temporary === false || file_put_contents($temporary, $contents) !== strlen($contents) || !chmod($temporary, 0644) || !rename($temporary, $path)) {
                throw new RuntimeException('Cannot save build payload metadata');
            }
        } finally {
            if ($temporary !== false && file_exists($temporary)) {
                unlink($temporary);
            }
        }
    }
}
