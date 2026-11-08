<?php

namespace staticphp\step;

use Exception;
use RuntimeException;
use Symfony\Component\Process\Process;
use staticphp\util\TwigRenderer;
use staticphp\util\BuildPayload;
use staticphp\util\ConsumerSnapshot;
use staticphp\util\ModuleOnlyBuild;
use Symfony\Component\Yaml\Yaml;

require_once __DIR__ . '/../util/BuildPayload.php';
require_once __DIR__ . '/../util/ConsumerSnapshot.php';
require_once __DIR__ . '/../util/ModuleOnlyBuild.php';

class RunSPC
{
    public static function run(bool $debug = false, string $phpVersion = '8.4', ?array $packages = null, bool $libsOnly = false, ?array $libraries = null): bool
    {
        if ($libraries !== null && !$libsOnly) {
            throw new \InvalidArgumentException('--libraries requires --libs-only.');
        }
        $craftYmlDest = BASE_PATH . '/craft.yml';
        $coreSeed = null;
        $requestedConfiguration = null;

        try {
            if (is_file(BASE_PATH . '/buildroot/consumer-reuse-snapshot.json')) {
                throw new RuntimeException('An interrupted partial build still has preserved consumers; restore that recorded snapshot before starting another build.');
            }
            if (defined('SPP_TYPE') && SPP_TYPE === 'apk' && !is_file('/etc/alpine-release')) {
                throw new RuntimeException('The musl payload must be compiled in the native Alpine builder.');
            }
            if (!defined('SPP_TYPE') || SPP_TYPE !== 'apk') {
                $os = @parse_ini_file('/etc/os-release') ?: [];
                if (!in_array($os['ID'] ?? '', ['rhel', 'centos', 'almalinux', 'rocky'], true)
                    || explode('.', (string) ($os['VERSION_ID'] ?? ''))[0] !== '8') {
                    throw new RuntimeException('The shared glibc payload must be compiled on EL8; use package to create RPM/DEB archives from an existing payload.');
                }
            }
            $sourceVersion = $phpVersion;
            $metadataPath = BASE_PATH . '/downloads/.spc-meta.json';
            if (preg_match('/^\d+\.\d+$/', $phpVersion)) {
                if (!is_readable($metadataPath)) {
                    throw new RuntimeException('Missing PHP release metadata; run spc-download first.');
                }
                $metadata = json_decode(file_get_contents($metadataPath), true, flags: JSON_THROW_ON_ERROR);
                $sourceVersion = $metadata['php_versions'][$phpVersion] ?? null;
                if (!is_string($sourceVersion) || !preg_match('/^' . preg_quote($phpVersion, '/') . '\.\d+(?:(?:alpha|beta|RC)\d+)?$/', $sourceVersion)) {
                    throw new RuntimeException("Invalid pinned PHP {$phpVersion} release in {$metadataPath}");
                }
            }
            echo "Selected PHP release: {$sourceVersion}\n";
            $craftYml = TwigRenderer::renderCraftTemplate($phpVersion, null, $packages, $sourceVersion, $libraries);
            $configuration = Yaml::parse($craftYml);
            $source = new Process(['python3', BASE_PATH . '/.github/actions/shared-libraries/php-source.py', '--root', BASE_PATH, '--version', $sourceVersion, '--select']);
            $source->setTimeout(null);
            $source->mustRun();
            $configuration['craft-options']['download'] = false;
            $configuration['download-options']['ignore-cache-sources'] = '';
            if (!$libsOnly) {
                $coreSeed = BuildPayload::verifiedCoreSeed(BASE_PATH . '/buildroot', $packages, $sourceVersion,
                    self::list($configuration['shared-extensions'] ?? []));
                if ($coreSeed !== null) {
                    if (self::list($coreSeed['metadata']['configuration']['extensions'] ?? []) !== self::list($configuration['extensions'] ?? [])) {
                        throw new RuntimeException('Cached PHP core has different built-in extensions');
                    }
                    $configuration['build-options']['maintainer-skip-build'] = true;
                    self::validatePartialCompileSelection($packages);
                    $repair = new Process(['python3', BASE_PATH . '/.github/actions/shared-libraries/rehydrate-php-extensions.py',
                        '--root', BASE_PATH, '--version', $sourceVersion, '--packages', implode(',', $packages)]);
                    $repair->setTimeout(null);
                    $repair->mustRun();
                    $requestedConfiguration = $configuration;
                    $configuration['shared-extensions'] = implode(',', self::partialResolutionExtensions($coreSeed['metadata'], $packages));
                }
            }
            $craftYml = Yaml::dump($configuration, 6, 4);

            // Write the rendered craft.yml to the destination
            if (!file_put_contents($craftYmlDest, $craftYml)) {
                echo "Failed to write updated craft.yml to project root.\n";
                return false;
            }
        } catch (Exception $e) {
            echo "Error rendering craft.yml template: " . $e->getMessage() . "\n";
            return false;
        }

        // Build the command arguments
        $args = [getenv('SPP_SPC_BINARY') ?: 'vendor/bin/spc', 'craft'];
        if ($debug) {
            $args[] = '--debug';
        }
        if ($libsOnly) {
            $args[] = '--libs-only';
        } else {
            $args[] = '--use-existing-libraries';
        }

        $env = getenv('GITHUB_ACTIONS') ? ['CI' => true] : [];
        $process = new Process($args, BASE_PATH, env: $env);
        $process->setTimeout(null);
        if (Process::isTtySupported()) {
            $process->setTty(true); // Interactive mode
        }

        try {
            $snapshot = $coreSeed === null ? null : ConsumerSnapshot::capture(BASE_PATH . '/buildroot', $coreSeed['metadata'], $packages, BUILD_ROOT_PATH);
            try {
                $snapshot?->activate($coreSeed['receipt']);
            } catch (\Throwable $error) {
                $snapshot?->close();
                throw $error;
            }
            $exitCode = self::executeProcess($process, $snapshot);
            if ($exitCode === 0 && !$libsOnly) {
                $versionProcess = new Process([BASE_PATH . '/buildroot/bin/php', '-n', '-r', 'echo PHP_VERSION;'], env: ['LD_LIBRARY_PATH' => BASE_PATH . '/buildroot/lib']);
                $versionProcess->mustRun();
                $actual = trim($versionProcess->getOutput());
                $matches = preg_match('/^\d+\.\d+$/', $sourceVersion)
                    ? str_starts_with($actual, $sourceVersion . '.')
                    : strcasecmp($actual, $sourceVersion) === 0;
                if (!$matches) {
                    throw new RuntimeException("Built PHP {$actual} does not match requested release {$sourceVersion}");
                }
            }
        } catch (Exception $e) {
            echo "Error building PHP: " . $e->getMessage() . "\n";
            return false;
        }

        if ($exitCode !== 0) {
            echo "Static PHP CLI build failed (exit code {$exitCode}). See the streamed output above.\n";
            return false;
        }

        echo "Static PHP CLI build completed successfully.\n";

        if (!$libsOnly) {
            $buildContext = ['kind' => 'full-consumer-build', 'source_snapshot' => self::sourceSnapshot($configuration)];
            if ($coreSeed === null && is_file(BASE_PATH . '/buildroot/consumer-cache-seed.json')) {
                unlink(BASE_PATH . '/buildroot/consumer-cache-seed.json');
            }
            if ($coreSeed !== null) {
                BuildPayload::verifiedCoreSeed(BASE_PATH . '/buildroot', $packages, $sourceVersion,
                    self::list($configuration['shared-extensions'] ?? []));
                self::mergeSkippedExtensions($coreSeed['metadata']['skipped_shared_extensions'] ?? null, $packages);
                $resolutionConfiguration = $configuration;
                $selectedConfiguration = $requestedConfiguration ?? $configuration;
                $configuration = $coreSeed['metadata']['configuration'];
                foreach (['sapi', 'shared-extensions'] as $field) {
                    $configuration[$field] = implode(',', array_values(array_unique(array_merge(self::list($configuration[$field] ?? []), self::list($selectedConfiguration[$field] ?? [])))));
                }
                foreach ($selectedConfiguration['extra-env'] ?? [] as $name => $value) {
                    if (str_starts_with($name, 'SPC_CMD_VAR_FRANKENPHP_') || in_array($name, ['GOAMD64', 'GOARM64'], true)) {
                        $configuration['extra-env'][$name] = $value;
                    }
                }
                $buildContext = ['kind' => 'partial-consumer-build', 'rebuilt_packages' => $packages,
                    'configuration' => $selectedConfiguration, 'core_seed_key' => $coreSeed['receipt']['seed_key'],
                    'source_snapshot' => self::sourceSnapshot($selectedConfiguration),
                    'compile_packages' => $packages, 'resolution_snapshot' => self::sourceSnapshot($resolutionConfiguration),
                    'reused_source_snapshot' => $coreSeed['metadata']['build_context']['source_snapshot']
                        ?? $coreSeed['receipt']['source_snapshot'] ?? null,
                    'consumer_build_provenance' => $coreSeed['metadata']['consumer_build_provenance'] ?? array_intersect_key($coreSeed['metadata'],
                        array_flip(['php_version', 'source_version', 'architecture', 'libc', 'build_platform', 'configuration', 'module_api', 'library_build_provenance']))];
            }
            if ($coreSeed === null) {
                ModuleOnlyBuild::capture(BASE_PATH . '/source/php-src', BASE_PATH . '/buildroot', $configuration);
            }
            $candidate = self::copyBuiltFiles($phpVersion);
            try {
                if (in_array('frankenphp', self::list($configuration['sapi'] ?? []), true)) {
                    $assets = BASE_PATH . '/source/frankenphp/package';
                    $destination = $candidate . '/packaging/frankenphp/package';
                    $rebuilt = $coreSeed === null || in_array('frankenphp', $packages, true);
                    if ($rebuilt) {
                        if (!is_dir($assets)) {
                            throw new RuntimeException('Built FrankenPHP is missing its pinned source package assets');
                        }
                        (new Process(['rm', '-rf', $destination]))->mustRun();
                        if (!mkdir($destination, 0755, true) && !is_dir($destination)) {
                            throw new RuntimeException('Cannot create FrankenPHP package asset directory');
                        }
                        (new Process(['cp', '-a', $assets . '/.', $destination . '/']))->mustRun();
                    } elseif (!is_dir($destination)) {
                        throw new RuntimeException('Cached FrankenPHP is missing its recorded package assets');
                    }
                }
                self::stagePieAsset($candidate);
                BuildPayload::record($candidate, $configuration, $actual, php_uname('m'), $sourceVersion, $buildContext);
                BuildPayload::seal($candidate);
                BuildPayload::load($candidate, true);
                self::publishBuiltFiles($candidate, BUILD_ROOT_PATH);
            } finally {
                if (is_dir($candidate)) {
                    (new Process(['rm', '-rf', $candidate]))->mustRun();
                }
            }
        } else {
            BuildPayload::record(BASE_PATH . '/buildroot', Yaml::parse($craftYml), $sourceVersion, php_uname('m'), $sourceVersion,
                ['kind' => 'libraries-only', 'trigger_php_version' => $sourceVersion]);
        }

        return true;
    }

    private static function list(array|string|null $value): array
    {
        return is_array($value) ? $value : array_values(array_filter(array_map('trim', explode(',', $value ?? ''))));
    }

    private static function partialResolutionExtensions(array $metadata, array $packages): array
    {
        $extensions = array_values(array_unique(array_merge(self::list($metadata['configuration']['shared-extensions'] ?? []),
            array_values(array_diff($packages, ['frankenphp'])))));
        $blocked = array_fill_keys(array_diff(array_column($metadata['skipped_shared_extensions']['skipped'] ?? [], 'extension'), $packages), true);
        $definitions = self::extensionDefinitions();
        do {
            $changed = false;
            foreach ($extensions as $extension) {
                if (isset($blocked[$extension]) || in_array($extension, $packages, true)) {
                    continue;
                }
                $definition = $definitions['ext-' . $extension] ?? [];
                $dependencies = [];
                foreach (['depends', 'depends@unix', 'depends@linux'] as $field) {
                    foreach ($definition[$field] ?? [] as $dependency) {
                        if (str_starts_with($dependency, 'ext-')) {
                            $dependencies[] = substr($dependency, 4);
                        }
                    }
                }
                $parent = $definition['php-extension']['display-name'] ?? null;
                if (($definition['php-extension']['arg-type'] ?? null) === 'none' && $parent !== $extension && is_string($parent)) {
                    $dependencies[] = $parent;
                }
                if (array_intersect($dependencies, array_keys($blocked)) !== []) {
                    $blocked[$extension] = true;
                    $changed = true;
                }
            }
        } while ($changed);
        return array_values(array_filter($extensions, static fn (string $extension): bool => !isset($blocked[$extension])));
    }

    private static function extensionDefinitions(): array
    {
        $definitions = [];
        foreach ([BASE_PATH . '/vendor/crazywhalecc/static-php-cli/config/pkg/ext', BASE_PATH . '/config/pkg/ext'] as $directory) {
            foreach (['yml', 'yaml', 'json'] as $suffix) {
                foreach (glob($directory . '/*.' . $suffix) ?: [] as $file) {
                    $definitions = array_replace($definitions, Yaml::parseFile($file) ?? []);
                }
            }
        }
        return $definitions;
    }

    private static function validatePartialCompileSelection(array $packages): void
    {
        $definitions = self::extensionDefinitions();
        foreach ($packages as $package) {
            if (($definitions['ext-' . $package]['php-extension']['build-with-php'] ?? false) === true
                && !ModuleOnlyBuild::supports(BASE_PATH . '/buildroot', $package)) {
                throw new RuntimeException('A verified partial build has no retained configured module recipe: ' . $package);
            }
        }
    }

    private static function executeProcess(Process $process, ?ConsumerSnapshot $snapshot): int
    {
        $restored = false;
        try {
            return $process->run(static function ($type, $buffer): void { echo $buffer; });
        } finally {
            try {
                $snapshot?->restore();
                $restored = true;
            } finally {
                if ($restored) {
                    $snapshot?->close();
                }
            }
        }
    }

    private static function sourceSnapshot(array $configuration): array
    {
        $cache = json_decode((string) file_get_contents(BASE_PATH . '/downloads/.cache.json'), true, 512, JSON_THROW_ON_ERROR);
        $artifacts = ['php-src'];
        foreach (self::list($configuration['shared-extensions'] ?? []) as $extension) {
            $artifacts[] = 'ext-' . $extension;
        }
        if (in_array('frankenphp', self::list($configuration['sapi'] ?? []), true)) {
            $artifacts = array_merge($artifacts, ['frankenphp', 'go', 'go-xcaddy']);
        }
        $records = [];
        foreach ($artifacts as $artifact) {
            if (isset($cache[$artifact]['source'])) {
                $records[$artifact] = $cache[$artifact]['source'];
                unset($records[$artifact]['time']);
            }
        }
        $recipes = [];
        foreach (glob(BASE_PATH . '/src/hook/*.php') ?: [] as $path) {
            $recipes[substr($path, strlen(BASE_PATH) + 1)] = hash_file('sha256', $path);
        }
        return ['records' => $records, 'recipe_files' => $recipes];
    }

    private static function stagePieAsset(string $root): void
    {
        $source = BASE_PATH . '/downloads/pie.phar';
        $metadataFile = BASE_PATH . '/downloads/pie.json';
        if (!is_file($source) && !is_file($metadataFile)) {
            return;
        }
        if (!is_file($source) || !is_file($metadataFile)) {
            throw new RuntimeException('Frozen PIE source requires its executable and version metadata');
        }
        $metadata = json_decode((string) file_get_contents($metadataFile), true, 512, JSON_THROW_ON_ERROR);
        if (($metadata['schema'] ?? null) !== 1 || !is_string($metadata['version'] ?? null)
            || !preg_match('/^[a-f0-9]{64}$/D', $metadata['sha256'] ?? '')
            || hash_file('sha256', $source) !== $metadata['sha256']) {
            throw new RuntimeException('Frozen PIE source does not match its recorded version and hash');
        }
        $destination = $root . '/packaging/pie';
        if (!is_dir($destination) && !mkdir($destination, 0755, true) && !is_dir($destination)) {
            throw new RuntimeException('Cannot create frozen PIE package asset directory');
        }
        if (!copy($source, $destination . '/pie.phar') || !copy($metadataFile, $destination . '/metadata.json')) {
            throw new RuntimeException('Cannot stage frozen PIE package assets');
        }
    }

    private static function mergeSkippedExtensions(?array $previous, array $packages): void
    {
        $path = BASE_PATH . '/buildroot/skipped-shared-extensions.json';
        if ($previous === null || !is_file($path)) {
            return;
        }
        $current = json_decode((string) file_get_contents($path), true, 512, JSON_THROW_ON_ERROR);
        $skipped = [];
        foreach ($previous['skipped'] ?? [] as $record) {
            if (!in_array($record['extension'], $packages, true)) {
                $skipped[$record['extension']] = $record;
            }
        }
        foreach ($current['skipped'] ?? [] as $record) {
            $skipped[$record['extension']] = $record;
        }
        $current['skipped'] = array_values($skipped);
        file_put_contents($path, json_encode($current, JSON_PRETTY_PRINT | JSON_UNESCAPED_SLASHES | JSON_THROW_ON_ERROR) . "\n");
    }

    private static function copyBuiltFiles(string $phpVersion): string
    {
        // Copy the built PHP binaries to our build directory
        $sourceDir = BASE_PATH . '/buildroot';
        $baseBuildDir = BASE_PATH . '/build';

        // Create the base build directory if it doesn't exist
        if (!is_dir($baseBuildDir) && !mkdir($baseBuildDir, 0755, true) && !is_dir($baseBuildDir)) {
            throw new RuntimeException("Failed to create directory: {$baseBuildDir}");
        }

        $buildDir = $baseBuildDir . '/.payload-' . str_replace('.', '', $phpVersion) . '-' . bin2hex(random_bytes(8));
        if (!mkdir($buildDir, 0755)) {
            throw new RuntimeException("Failed to create directory: {$buildDir}");
        }
        try {
            (new Process(['cp', '-a', $sourceDir . '/.', $buildDir . '/']))->mustRun();
        } catch (\Throwable $error) {
            (new Process(['rm', '-rf', $buildDir]))->mustRun();
            throw $error;
        }
        echo "Copied PHP {$phpVersion} files to {$buildDir}\n";
        return $buildDir;
    }

    private static function publishBuiltFiles(string $candidate, string $destination): void
    {
        if (!BuildPayload::load($candidate, true)->isSealed()) {
            throw new RuntimeException('Cannot publish an unsealed PHP payload');
        }
        if (!file_exists($destination)) {
            if (!rename($candidate, $destination)) {
                throw new RuntimeException('Cannot publish completed PHP payload');
            }
            return;
        }
        $exchange = <<<'PY'
import ctypes, os, platform, sys
candidate, destination = sys.argv[1:]
numbers = {'x86_64': 316, 'aarch64': 276}
number = numbers.get(platform.machine())
if number is None:
    raise RuntimeError('Unsupported payload exchange architecture')
libc = ctypes.CDLL(None, use_errno=True)
if libc.syscall(number, -100, os.fsencode(candidate), -100, os.fsencode(destination), 2) != 0:
    error = ctypes.get_errno()
    raise OSError(error, os.strerror(error))
PY;
        (new Process(['python3', '-c', $exchange, $candidate, $destination]))->mustRun();
    }
}
