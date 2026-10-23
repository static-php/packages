<?php

namespace staticphp\step;

use Exception;
use RuntimeException;
use Symfony\Component\Process\Process;
use staticphp\util\TwigRenderer;

class RunSPC
{
    public static function run(bool $debug = false, string $phpVersion = '8.4', ?array $packages = null, bool $libsOnly = false, ?array $libraries = null): bool
    {
        if ($libraries !== null && !$libsOnly) {
            throw new \InvalidArgumentException('--libraries requires --libs-only.');
        }
        $craftYmlDest = BASE_PATH . '/craft.yml';

        try {
            $sourceVersion = $phpVersion;
            $metadataPath = BASE_PATH . '/downloads/.spc-meta.json';
            if (preg_match('/^\d+\.\d+$/', $phpVersion) && (is_file($metadataPath) || getenv('CI') || getenv('GITHUB_ACTIONS'))) {
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
        }

        $env = getenv('GITHUB_ACTIONS') ? ['CI' => true] : [];
        $process = new Process($args, BASE_PATH, env: $env);
        $process->setTimeout(null);
        if (Process::isTtySupported()) {
            $process->setTty(true); // Interactive mode
        }

        try {
            $exitCode = $process->run(function ($type, $buffer) {
                echo $buffer;
            });
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
            self::copyBuiltFiles($phpVersion);
        }

        return true;
    }

    private static function copyBuiltFiles(string $phpVersion): void
    {
        // Copy the built PHP binaries to our build directory
        $sourceDir = BASE_PATH . '/buildroot';
        $buildDir = BUILD_ROOT_PATH;
        $baseBuildDir = BASE_PATH . '/build';

        // Create the base build directory if it doesn't exist
        if (!is_dir($baseBuildDir) && !mkdir($baseBuildDir, 0755, true) && !is_dir($baseBuildDir)) {
            echo "Failed to create directory: {$baseBuildDir}\n";
            return;
        }

        // Check for existing PHP versions in the build directory
        $existingVersions = [];
        if (is_dir($baseBuildDir)) {
            $dirs = scandir($baseBuildDir);
            foreach ($dirs as $dir) {
                if ($dir !== '.' && $dir !== '..' && is_dir($baseBuildDir . '/' . $dir)) {
                    // Check if this directory contains a PHP binary
                    $versionBinary = $baseBuildDir . '/' . $dir . '/bin/php';
                    if (file_exists($versionBinary)) {
                        // Get the PHP version from the binary
                        $versionProcess = new Process([$versionBinary, '-r', 'echo PHP_VERSION;']);
                        $versionProcess->run();
                        $detectedVersion = trim($versionProcess->getOutput());

                        if (!empty($detectedVersion)) {
                            // Extract major.minor version
                            $parts = explode('.', $detectedVersion);
                            if (count($parts) >= 2) {
                                $majorMinor = $parts[0] . '.' . $parts[1];
                                echo "Found PHP version {$detectedVersion} (major.minor: {$majorMinor}) in directory {$dir}\n";
                                $existingVersions[$dir] = $majorMinor;
                            }
                        }
                    }
                }
            }
        }

        // Create the build directory if it doesn't exist
        if (!is_dir($buildDir) && !mkdir($buildDir, 0755, true) && !is_dir($buildDir)) {
            echo "Failed to create directory: {$buildDir}\n";
            return;
        }

        // Globs miss dotfiles, so spc's .build.json never arrives and stale ones never leave;
        // cp -a also preserves the <ext>.so -> <ext>-zts-NN.so symlinks that cp -r dereferences.
        (new Process(['rm', '-rf', $buildDir]))->mustRun();
        if (!is_dir($buildDir) && !mkdir($buildDir, 0755, true) && !is_dir($buildDir)) {
            echo "Failed to create directory: {$buildDir}\n";
            return;
        }
        (new Process(['cp', '-a', $sourceDir . '/.', $buildDir . '/']))->mustRun();

        echo "Copied PHP {$phpVersion} files to {$buildDir}\n";
    }
}
