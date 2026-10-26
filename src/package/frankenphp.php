<?php

namespace staticphp\package;

use RuntimeException;
use staticphp\package;
use staticphp\step\CreatePackages;
use staticphp\util\BuildPayload;
use staticphp\util\RuntimeServices;
use Symfony\Component\Process\Process;

class frankenphp implements package
{
    public function getName(): string
    {
        // Extract version suffix from prefix for frankenphp naming
        // e.g., "php-zts8.3" -> "frankenphp8.3", "php-nts85" -> "frankenphp85", "php-zts" -> "frankenphp"
        $prefix = CreatePackages::getPrefix();

        // Remove "php" and any non-digit prefix to get just the version part
        // php-zts8.5 -> -zts8.5 -> 8.5
        // php-nts85 -> -nts85 -> 85
        $suffix = str_replace('php', '', $prefix);
        if (preg_match('/(\d+\.?\d*)/', $suffix, $matches)) {
            return 'frankenphp' . $matches[1];
        }
        return 'frankenphp';
    }

    public function getFpmConfig(): array
    {
        return ['recommends' => CreatePackages::getSapiRecommendations()];
    }

    public function getDebuginfoFpmConfig(): array
    {
        return [];
    }

    public function getFpmExtraArgs(): array
    {
        return [];
    }

    public function getLicense(): string
    {
        return 'MIT';
    }

    public function getDescription(): string
    {
        return 'FrankenPHP - The Modern PHP Webserver';
    }

    private function sharedDependencies(string $type): array
    {
        return array_values(array_unique([
            ...CreatePackages::getSharedLibraryDependencies([BUILD_BIN_PATH . '/frankenphp' => '/usr/bin/frankenphp'], $type),
            'php-zts-runtime-layout',
            CreatePackages::getSharedRuntimeCapability(),
        ]));
    }

    /**
     * Get list of versioned frankenphp packages to conflict/replace with
     * Returns empty array - versioned FrankenPHP packages can coexist
     */
    private function getVersionedConflicts(): array
    {
        return [];
    }

    /**
     * Create FrankenPHP packages (both RPM and DEB)
     */
    public function createPackages(string $packageType, array $binaryDependencies, ?string $iterationOverride = null, bool $debuginfo = false, bool $bump = false): void
    {
        echo "Creating FrankenPHP package\n";

        [, $architecture] = $this->getPhpVersionAndArchitecture();

        $this->prepareFrankenPhpRepository();

        if ($packageType === 'rpm') {
            $this->createRpmPackage($architecture, $binaryDependencies, $iterationOverride, $debuginfo, $bump);
        }
        if ($packageType === 'deb') {
            $this->createDebPackage($architecture, $binaryDependencies, $iterationOverride, $debuginfo, $bump);
        }
        if ($packageType === 'apk') {
            $this->createApkPackage($architecture, $binaryDependencies, $iterationOverride, $debuginfo, $bump);
        }
    }

    private function resolveIteration(string $name, string $version, string $architecture, string $packageType, ?string $iterationOverride, bool $bump): string
    {
        if ($iterationOverride !== null) {
            return $iterationOverride;
        }
        if ($bump) {
            return (string)\staticphp\step\CreatePackages::getRemoteNextIteration($name, $version, $architecture, $packageType);
        }
        return CreatePackages::resolveIteration($name, $version, $architecture, $packageType);
    }

    /**
     * Create RPM package for FrankenPHP
     */
    public function createRpmPackage(string $architecture, array $binaryDependencies, ?string $iterationOverride = null, bool $debuginfo = false, bool $bump = false): void
    {
        echo "Creating RPM package for FrankenPHP...\n";

        $packageFolder = $this->packageFolder();
        $sharedLibrarySuffix = getSharedLibrarySuffix();
        $phpEmbedName = 'libphp' . $sharedLibrarySuffix . '.so';

        $version = $this->frankenVersion();

        // Append PHP version suffix to FrankenPHP version
        $rpmVersion = CreatePackages::getTaggedPackageVersion('rpm', $version);

        $name = $this->getName();
        $rpmVersion = CreatePackages::migrationPackageVersion('rpm', $name, $rpmVersion, $architecture);

        // Calculate iteration for RPM (with possible override)
        $iteration = $this->resolveIteration($name, $rpmVersion, $architecture, 'rpm', $iterationOverride, $bump);

        $versionedConflicts = $this->getVersionedConflicts();

        $rpmRelease = $iteration;
        $packageFile = DIST_RPM_PATH . "/{$name}-{$rpmVersion}-{$rpmRelease}.{$architecture}.rpm";

        $fpmArgs = [
            'fpm',
            '-s', 'dir',
            '-t', 'rpm',
            '--no-rpm-autoreqprov',
            '--rpm-compression', 'xz',
            '-p', $packageFile,  // Full path with distVersion in filename
            '-n', $name,
            '-v', $rpmVersion,
            '--description', "FrankenPHP - The Modern PHP Webserver",
            '--license', $this->getLicense(),
            '--config-files', '/etc/frankenphp/Caddyfile',
        ];

        if ($name !== 'frankenphp') {
            $fpmArgs[] = '--provides';
            $fpmArgs[] = 'frankenphp = ' . $rpmVersion . '-' . $rpmRelease;
        }

        foreach ($versionedConflicts as $conflict) {
            $fpmArgs[] = '--conflicts';
            $fpmArgs[] = $conflict;
            $fpmArgs[] = '--replaces';
            $fpmArgs[] = $conflict;
        }

        $consolidatedDeps = [];
        foreach ($binaryDependencies as $lib => $dependencyVersion) {
            if (!isset($consolidatedDeps[$lib]) || version_compare($dependencyVersion, $consolidatedDeps[$lib], '>')) {
                $consolidatedDeps[$lib] = $dependencyVersion;
            }
        }

        foreach ($consolidatedDeps as $lib => $dependencyVersion) {
            $fpmArgs[] = '--depends';
            $fpmArgs[] = str_contains($lib, '(') ? "{$lib}(64bit)" : ($dependencyVersion === '0' ? "{$lib}()(64bit)" : "{$lib}({$dependencyVersion})(64bit)");
        }
        foreach ($this->sharedDependencies('rpm') as $dependency) {
            $fpmArgs[] = '--depends';
            $fpmArgs[] = $dependency;
        }
        foreach ($this->getFpmConfig()['recommends'] as $recommendation) {
            $fpmArgs[] = '--rpm-tag';
            $fpmArgs[] = 'Recommends: ' . $recommendation;
        }

        $empty = TEMP_DIR . '/frankenphp-empty';
        if (!is_dir($empty) && !mkdir($empty, 0755, true) && !is_dir($empty)) {
            throw new RuntimeException(sprintf('Directory "%s" was not created', $empty));
        }

        // Generate autocompletion
        $completionFile = TEMP_DIR . '/frankenphp.bash';
        $ldLibraryPath = 'LD_LIBRARY_PATH=' . BUILD_LIB_PATH;
        shell()->exec($ldLibraryPath . ' ' . BUILD_BIN_PATH . '/frankenphp completion bash > ' . $completionFile);

        $fpmArgs = [...$fpmArgs, ...[
            '--depends', "$phpEmbedName",
            '--iteration', $rpmRelease,
            '--rpm-user', 'frankenphp',
            '--rpm-group', 'frankenphp',
            '--config-files', '/etc/frankenphp/Caddyfile',
            '--config-files', '/etc/frankenphp/Caddyfile.d',
            $this->runtimeBinary() . '=/usr/bin/' . $name,
            $completionFile . '=/usr/share/bash-completion/completions/' . $name,
            $this->serviceUnit($packageFolder . '/rhel/frankenphp.service', $name) . '=/usr/lib/systemd/system/' . $name . '.service',
            "{$packageFolder}/Caddyfile=/etc/frankenphp/Caddyfile",
            "{$packageFolder}/content/=/usr/share/frankenphp",
            $empty . '/=/var/lib/frankenphp',
            $empty . '/=/etc/frankenphp/Caddyfile.d',
        ]];
        array_splice($fpmArgs, 1, 0, [...RuntimeServices::serviceArgs('rpm', $name, true),
            '--after-install', RuntimeServices::frankenPostInstall('rpm', $name, '/usr/bin/' . $name),
            '--after-upgrade', RuntimeServices::frankenPostInstall('rpm', $name, '/usr/bin/' . $name, true)]);
        foreach (RuntimeServices::descriptor('frankenphp', $name, '/usr/bin/' . $name, '/etc/frankenphp/Caddyfile') as $source => $destination) {
            $fpmArgs[] = $source . '=' . $destination;
        }
        foreach (CreatePackages::consumerMetadataFiles($name, [$this->runtimeBinary() => '/usr/bin/' . $name]) as $source => $destination) {
            $fpmArgs[] = $source . '=' . $destination;
        }

        $rpmProcess = new Process($fpmArgs);
        $rpmProcess->setTimeout(null);
        $rpmProcess->run(function ($type, $buffer) {
            echo $buffer;
        });
        if (!$rpmProcess->isSuccessful()) {
            throw new RuntimeException('FrankenPHP RPM package creation failed: ' . $rpmProcess->getOutput() . $rpmProcess->getErrorOutput());
        }

        echo "RPM package created: {$packageFile}\n";

        if ($debuginfo) {
            $frankenDbg = $this->debugBinary();
            if ($frankenDbg !== null && file_exists($frankenDbg)) {
                $dbgPackageFile = DIST_RPM_PATH . "/{$name}-debuginfo-{$rpmVersion}-{$rpmRelease}.{$architecture}.rpm";
                $dbgArgs = [
                    'fpm',
                    '-s', 'dir',
                    '-t', 'rpm',
                    '--rpm-compression', 'xz',
                    '-p', $dbgPackageFile,
                    '-n', $name . '-debuginfo',
                    '-v', $rpmVersion,
                    '--iteration', $rpmRelease,
                    '--architecture', $architecture,
                    '--license', $this->getLicense(),
                    '--depends', sprintf('%s = %s-%s', $name, $rpmVersion, $rpmRelease),
                    $frankenDbg . '=/usr/lib/debug/usr/bin/' . basename($frankenDbg),
                ];
                $dbgProcess = new Process($dbgArgs);
                $dbgProcess->setTimeout(null);
                $dbgProcess->run(function ($type, $buffer) {
                    echo $buffer;
                });
                if (!$dbgProcess->isSuccessful()) {
                    throw new RuntimeException("RPM debuginfo package creation failed: " . $dbgProcess->getErrorOutput());
                }

                echo "RPM debuginfo package created: {$dbgPackageFile}\n";
            }
        }
    }

    /**
     * Create DEB package for FrankenPHP
     */
    public function createDebPackage(string $architecture, array $binaryDependencies, ?string $iterationOverride = null, bool $debuginfo = false, bool $bump = false): void
    {
        echo "Creating DEB package for FrankenPHP...\n";

        $packageFolder = $this->packageFolder();
        $sharedLibrarySuffix = getSharedLibrarySuffix();
        // libphp filename with shared library suffix: libphp-zts-85.so, libphp-nts-84.so
        $phpEmbedName = 'libphp' . $sharedLibrarySuffix . '.so';

        $version = $this->frankenVersion();

        $name = $this->getName();

        // Convert system architecture to Debian architecture naming
        $debArch = match($architecture) {
            'x86_64' => 'amd64',
            'aarch64' => 'arm64',
            default => $architecture,
        };

        // For DEB packages, append PHP version to package version for proper sorting
        // e.g., 1.11.0+php85 is higher than 1.11.0+php83
        $debVersion = CreatePackages::migrationPackageVersion('deb', $name, CreatePackages::getTaggedPackageVersion('deb', $version), $debArch);

        // Calculate iteration for DEB (with possible override)
        $iteration = $this->resolveIteration($name, $debVersion, $debArch, 'deb', $iterationOverride, $bump);
        $debIteration = $iteration;

        $versionedConflicts = $this->getVersionedConflicts();

        // Debian filename format: {name}_{version}-{revision}_{arch}.deb
        $packageFile = DIST_DEB_PATH . "/{$name}_{$debVersion}-{$debIteration}_{$debArch}.deb";

        $fpmArgs = [
            'fpm',
            '-s', 'dir',
            '-t', 'deb',
            '--deb-compression', 'xz',
            '-p', $packageFile,
            '-n', $name,
            '-v', $debVersion,
            '--architecture', $debArch,
            '--license', $this->getLicense(),
            '--config-files', '/etc/frankenphp/Caddyfile',
            '--provides', 'frankenphp',
        ];

        foreach ($versionedConflicts as $conflict) {
            $fpmArgs[] = '--conflicts';
            $fpmArgs[] = $conflict;
            $fpmArgs[] = '--replaces';
            $fpmArgs[] = $conflict;
        }

        $systemLibraryMap = [
            'ld-linux-x86-64.so.2' => 'libc6',
            'ld-linux-aarch64.so.1' => 'libc6',
            'libm.so.6' => 'libc6',
            'libc.so.6' => 'libc6',
            'libpthread.so.0' => 'libc6',
            'libutil.so.1' => 'libc6',
            'libdl.so.2' => 'libc6',
            'librt.so.1' => 'libc6',
            'libresolv.so.2' => 'libc6',
            'libgcc_s.so.1' => 'libgcc-s1',
            'libstdc++.so.6' => 'libstdc++6',
        ];

        $consolidatedDeps = [];
        foreach ($binaryDependencies as $lib => $ver) {
            $lib = preg_replace('/\(.*/', '', $lib);
            if (isset($systemLibraryMap[$lib])) {
                // Use mapped name for system libraries
                $packageName = $systemLibraryMap[$lib];
            }
            else {
                // For other libraries, remove .so suffix
                $packageName = preg_replace('/\.so(\.\d+)?$/', '', $lib);
            }

            $numericVersion = preg_replace('/[^0-9.]/', '', $ver);
            if (!isset($consolidatedDeps[$packageName]) || version_compare($numericVersion, $consolidatedDeps[$packageName], '>')) {
                $consolidatedDeps[$packageName] = $numericVersion;
            }
        }

        foreach ($consolidatedDeps as $packageName => $numericVersion) {
            $fpmArgs[] = '--depends';
            $fpmArgs[] = "{$packageName} (>= {$numericVersion})";
        }
        foreach ($this->sharedDependencies('deb') as $dependency) {
            $fpmArgs[] = '--depends';
            $fpmArgs[] = $dependency;
        }
        foreach ($this->getFpmConfig()['recommends'] as $recommendation) {
            $fpmArgs[] = '--deb-recommends';
            $fpmArgs[] = $recommendation;
        }

        $empty = TEMP_DIR . '/frankenphp-empty';
        if (!is_dir($empty) && !mkdir($empty, 0755, true) && !is_dir($empty)) {
            throw new RuntimeException(sprintf('Directory "%s" was not created', $empty));
        }

        // Determine the FrankenPHP suffix (just version, not prefix)
        // Extract version from package name: frankenphp8.5 or frankenphp85
        $prefix = CreatePackages::getPrefix();
        $frankenphpSuffix = '';
        // Extract version numbers from prefix (e.g., "php-zts8.5" -> "8.5", "php-nts85" -> "85")
        if (preg_match('/(\d+\.?\d*)/', $prefix, $matches)) {
            $frankenphpSuffix = $matches[1];
        }

        $completionFile = TEMP_DIR . '/frankenphp' . $frankenphpSuffix . '.bash';
        $ldLibraryPath = 'LD_LIBRARY_PATH=' . BUILD_LIB_PATH;
        shell()->exec($ldLibraryPath . ' ' . BUILD_BIN_PATH . '/frankenphp completion bash > ' . $completionFile);

        $fpmArgs = [...$fpmArgs, ...[
            '--depends', $phpEmbedName,
            '--after-install', RuntimeServices::frankenPostInstall('deb', $name, '/usr/bin/' . $name),
            '--iteration', $debIteration,
            $this->runtimeBinary() . '=/usr/bin/frankenphp' . $frankenphpSuffix,
            $completionFile . '=/usr/share/bash-completion/completions/frankenphp' . $frankenphpSuffix,
            $this->serviceUnit($packageFolder . '/debian/frankenphp.service', $name) . '=/usr/lib/systemd/system/' . $name . '.service',
            "{$packageFolder}/Caddyfile=/etc/frankenphp/Caddyfile",
            "{$packageFolder}/content/=/usr/share/frankenphp",
            $empty . '/=/var/lib/frankenphp'
        ]];
        array_splice($fpmArgs, 1, 0, RuntimeServices::serviceArgs('deb', $name, true));
        foreach (RuntimeServices::descriptor('frankenphp', $name, '/usr/bin/' . $name, '/etc/frankenphp/Caddyfile') as $source => $destination) {
            $fpmArgs[] = $source . '=' . $destination;
        }
        foreach (CreatePackages::consumerMetadataFiles($name, [$this->runtimeBinary() => '/usr/bin/' . $name]) as $source => $destination) {
            $fpmArgs[] = $source . '=' . $destination;
        }

        $rpmProcess = new Process($fpmArgs);
        $rpmProcess->setTimeout(null);
        $rpmProcess->run(function ($type, $buffer) {
            echo $buffer;
        });

        if (!$rpmProcess->isSuccessful()) {
            throw new RuntimeException('FrankenPHP DEB package creation failed: ' . $rpmProcess->getErrorOutput());
        }
        echo "DEB package created: {$packageFile}\n";

        // Create FrankenPHP debuginfo package if debug file exists (only if --debuginfo flag set for DEB)
        if ($debuginfo) {
            $frankenDbg = $this->debugBinary();
            if ($frankenDbg !== null && file_exists($frankenDbg)) {
                $dbgDebName = "{$name}-debuginfo";
                $dbgPackageFile = DIST_DEB_PATH . "/{$dbgDebName}_{$debVersion}-{$debIteration}_{$debArch}.deb";
                $dbgArgs = [
                    'fpm',
                    '-s', 'dir',
                    '-t', 'deb',
                    '--deb-compression', 'xz',
                    '-p', $dbgPackageFile,
                    '-n', $dbgDebName,
                    '-v', $debVersion,
                    '--iteration', $debIteration,
                    '--architecture', $debArch,
                    '--license', $this->getLicense(),
                    '--depends', sprintf('%s (= %s-%s)', $name, $debVersion, $debIteration),
                    $frankenDbg . '=/usr/lib/debug/usr/bin/' . basename($frankenDbg),
                ];
                $dbgProcess = new Process($dbgArgs);
                $dbgProcess->setTimeout(null);
                $dbgProcess->run(function ($type, $buffer) {
                    echo $buffer;
                });
                if (!$dbgProcess->isSuccessful()) {
                    throw new RuntimeException("DEB debuginfo package creation failed: " . $dbgProcess->getErrorOutput());
                }

                echo "DEB debuginfo package created: {$dbgPackageFile}\n";
            }
        }
    }

    /**
     * Create APK package for FrankenPHP
     */
    public function createApkPackage(string $architecture, array $binaryDependencies, ?string $iterationOverride = null, bool $debuginfo = false, bool $bump = false): void
    {
        echo "Creating APK package for FrankenPHP using nfpm...\n";

        $packageFolder = $this->packageFolder();
        $sharedLibrarySuffix = getSharedLibrarySuffix();
        // libphp filename with shared library suffix: libphp-zts-85.so, libphp-nts-84.so
        $phpEmbedName = 'libphp' . $sharedLibrarySuffix . '.so';

        $version = $this->frankenVersion();

        $name = $this->getName();

        // For APK packages, append PHP version to package version for proper sorting
        // e.g., 1.11.0p85 is higher than 1.11.0p83
        $apkVersion = CreatePackages::migrationPackageVersion('apk', $name, CreatePackages::getTaggedPackageVersion('apk', $version), $architecture);

        // Calculate iteration for APK (with possible override)
        $iteration = $this->resolveIteration($name, $apkVersion, $architecture, 'apk', $iterationOverride, $bump);

        $versionedConflicts = $this->getVersionedConflicts();

        // Build nfpm config
        $nfpmConfig = [
            'name' => $name,
            'arch' => $architecture,
            'platform' => 'linux',
            'version' => $apkVersion,
            'release' => $iteration,
            'section' => 'default',
            'priority' => 'optional',
            'maintainer' => 'Marc Henderkes <pkg@henderkes.com>',
            'description' => "FrankenPHP - Modern PHP application server",
            'vendor' => 'Marc Henderkes',
            'homepage' => 'https://apks.henderkes.com',
            'license' => $this->getLicense(),
        ];

        // Build dependencies
        // For APK, depend on the embed package, not the .so file
        $embedPackageName = CreatePackages::getPrefix() . '-embed';
        $depends = [$embedPackageName];

        // Alpine library dependencies
        $alpineLibMap = [
            'ld-linux-x86-64.so.2' => 'musl',
            'ld-linux-aarch64.so.1' => 'musl',
            'libc.so.6' => 'musl',
            'libm.so.6' => 'musl',
            'libpthread.so.0' => 'musl',
            'libutil.so.1' => 'musl',
            'libdl.so.2' => 'musl',
            'librt.so.1' => 'musl',
            'libresolv.so.2' => 'musl',
            'libgcc_s.so.1' => 'libgcc',
            'libstdc++.so.6' => 'libstdc++',
        ];

        $consolidatedDeps = [];
        foreach ($binaryDependencies as $lib => $ver) {
            $lib = preg_replace('/\(.*/', '', $lib);
            if (str_starts_with($lib, 'libc.musl-') || str_starts_with($lib, 'ld-musl-')) {
                $lib = 'libc.so.6';
            }
            if (isset($alpineLibMap[$lib])) {
                $packageName = $alpineLibMap[$lib];
            } else {
                $packageName = preg_replace('/\.so(\.\d+)*$/', '', $lib);
            }
            $numericVersion = preg_replace('/[^0-9.]/', '', $ver);
            if (!isset($consolidatedDeps[$packageName]) || version_compare($numericVersion, $consolidatedDeps[$packageName], '>')) {
                $consolidatedDeps[$packageName] = $numericVersion;
            }
        }

        foreach ($consolidatedDeps as $packageName => $numericVersion) {
            $depends[] = "{$packageName}>={$numericVersion}";
        }

        $nfpmConfig['depends'] = array_values(array_unique([...$depends, ...$this->sharedDependencies('apk')]));
        $nfpmConfig['provides'] = [$this->getName() !== 'frankenphp' ? 'frankenphp' : ''];
        $nfpmConfig['replaces'] = $versionedConflicts;
        $nfpmConfig['conflicts'] = $versionedConflicts;

        // Determine the FrankenPHP suffix (just version numbers)
        $prefix = CreatePackages::getPrefix();
        $frankenphpSuffix = '';
        if (preg_match('/(\d+\.?\d*)/', $prefix, $matches)) {
            $frankenphpSuffix = $matches[1];
        }

        $completionFile = TEMP_DIR . '/frankenphp' . $frankenphpSuffix . '.bash';
        $ldLibraryPath = 'LD_LIBRARY_PATH=' . BUILD_LIB_PATH;
        shell()->exec($ldLibraryPath . ' ' . BUILD_BIN_PATH . '/frankenphp completion bash > ' . $completionFile);

        // Build contents
        $contents = [
            [
                'src' => $this->runtimeBinary(),
                'dst' => '/usr/bin/frankenphp' . $frankenphpSuffix,
            ],
            [
                'src' => $completionFile,
                'dst' => '/usr/share/bash-completion/completions/frankenphp' . $frankenphpSuffix,
            ],
            [
                'src' => $this->serviceUnit($packageFolder . '/alpine/frankenphp.openrc', $name),
                'dst' => "/etc/init.d/frankenphp{$frankenphpSuffix}",
            ],
            [
                'src' => "{$packageFolder}/Caddyfile",
                'dst' => '/etc/frankenphp/Caddyfile',
                'type' => 'config',
            ],
            [
                'src' => "{$packageFolder}/content/",
                'dst' => '/usr/share/frankenphp/',
            ],
            [
                'dst' => '/var/lib/frankenphp',
                'type' => 'dir',
            ],
            [
                'dst' => '/etc/frankenphp/Caddyfile.d',
                'type' => 'dir',
            ],
        ];

        $nfpmConfig['contents'] = $contents;
        foreach (RuntimeServices::descriptor('frankenphp', $name, '/usr/bin/' . $name, '/etc/frankenphp/Caddyfile') as $source => $destination) {
            $nfpmConfig['contents'][] = ['src' => $source, 'dst' => $destination];
        }
        foreach (CreatePackages::consumerMetadataFiles($name, [$this->runtimeBinary() => '/usr/bin/' . $name]) as $source => $destination) {
            $nfpmConfig['contents'][] = ['src' => $source, 'dst' => $destination];
        }

        $nfpmConfig['scripts'] = [
            'postinstall' => RuntimeServices::frankenPostInstall('apk', $name, '/usr/bin/' . $name),
        ];
        $serviceArgs = RuntimeServices::serviceArgs('apk', $name, true);
        $nfpmConfig['scripts']['preremove'] = $serviceArgs[array_search('--before-remove', $serviceArgs, true) + 1];
        $nfpmConfig['apk']['scripts'] = RuntimeServices::apkScripts($name, true);

        // Write nfpm config
        $nfpmConfigFile = TEMP_DIR . "/nfpm-{$name}.yaml";
        if (!yaml_emit_file($nfpmConfigFile, $nfpmConfig, YAML_UTF8_ENCODING)) {
            throw new RuntimeException("Failed to write YAML file: {$nfpmConfigFile}");
        }

        echo "nfpm config written to: {$nfpmConfigFile}\n";

        // Run nfpm with full filename including PHP version suffix
        $outputFile = DIST_APK_PATH . "/{$name}-{$apkVersion}-r{$iteration}.{$architecture}.apk";
        $nfpmProcess = new Process([
            'nfpm', 'package',
            '--config', $nfpmConfigFile,
            '--packager', 'apk',
            '--target', $outputFile
        ]);
        $nfpmProcess->setTimeout(null);
        $nfpmProcess->run(function ($type, $buffer) {
            echo $buffer;
        });

        if (!$nfpmProcess->isSuccessful()) {
            echo "nfpm config file contents:\n";
            echo file_get_contents($nfpmConfigFile);
            throw new RuntimeException("nfpm package creation failed: " . $nfpmProcess->getErrorOutput());
        }

        @unlink($nfpmConfigFile);

        // Create FrankenPHP debuginfo package if debug file exists (only if --debuginfo flag set for APK)
        if ($debuginfo) {
            $frankenDbg = $this->debugBinary();
            if ($frankenDbg !== null && file_exists($frankenDbg)) {
                $this->createApkDebuginfo($name, $apkVersion, $iteration, $architecture, $frankenDbg, $frankenphpSuffix);
            }
        }
    }

    private function createApkDebuginfo(string $name, string $version, string $iteration, string $architecture, string $frankenDbg, string $frankenphpSuffix): void
    {
        $dbgName = $name . '-debuginfo';

        $nfpmConfig = [
            'name' => $dbgName,
            'arch' => $architecture,
            'platform' => 'linux',
            'version' => $version,
            'release' => $iteration,
            'section' => 'default',
            'priority' => 'optional',
            'maintainer' => 'Marc Henderkes <pkg@henderkes.com>',
            'description' => "Debug symbols for FrankenPHP",
            'vendor' => 'Marc Henderkes',
            'homepage' => 'https://apks.henderkes.com',
            'license' => $this->getLicense(),
            'depends' => [sprintf('%s=%s-r%s', $name, $version, $iteration)],
            'contents' => [
                [
                    'src' => $frankenDbg,
                    'dst' => '/usr/lib/debug/usr/bin/' . basename($frankenDbg),
                ],
            ],
        ];

        $nfpmConfigFile = TEMP_DIR . "/nfpm-{$dbgName}.yaml";
        if (!yaml_emit_file($nfpmConfigFile, $nfpmConfig, YAML_UTF8_ENCODING)) {
            throw new RuntimeException("Failed to write YAML file: {$nfpmConfigFile}");
        }

        $phpSuffix = $this->getPhpVersionSuffix();
        $outputFile = DIST_APK_PATH . "/{$dbgName}-{$version}-r{$iteration}.{$phpSuffix}.{$architecture}.apk";
        $dbgProcess = new Process([
            'nfpm', 'package',
            '--config', $nfpmConfigFile,
            '--packager', 'apk',
            '--target', $outputFile
        ]);
        $dbgProcess->setTimeout(null);
        $dbgProcess->run(function ($type, $buffer) {
            echo $buffer;
        });

        if (!$dbgProcess->isSuccessful()) {
            throw new RuntimeException("nfpm debuginfo package creation failed: " . $dbgProcess->getErrorOutput());
        }

        @unlink($nfpmConfigFile);

        echo "APK debuginfo package created: {$outputFile}\n";
    }

    private function prepareFrankenPhpRepository(): string
    {
        return $this->packageFolder();
    }

    private function packageFolder(): string
    {
        $recorded = BUILD_ROOT_PATH . '/packaging/frankenphp/package';
        $paths = BuildPayload::load(BUILD_ROOT_PATH) !== null
            ? [$recorded]
            : [$recorded, BASE_PATH . '/source/frankenphp/package', BASE_PATH . '/spc/source/frankenphp/package', DIST_PATH . '/frankenphp/package'];
        foreach ($paths as $path) {
            if (is_file($path . '/Caddyfile') && is_dir($path . '/content')) {
                return $path;
            }
        }
        throw new RuntimeException('Missing pinned FrankenPHP package assets; rebuild/export the recorded payload rather than fetching a different source tag.');
    }

    private function frankenVersion(): string
    {
        $recorded = BuildPayload::load(BUILD_ROOT_PATH)?->metadata()['frankenphp_version'] ?? null;
        if (is_string($recorded) && preg_match('/^\d+\.\d+\.\d+$/D', $recorded)) {
            return $recorded;
        }
        $process = new Process([BUILD_BIN_PATH . '/frankenphp', '--version'], env: ['LD_LIBRARY_PATH' => BUILD_LIB_PATH]);
        $process->mustRun();
        if (!preg_match('/FrankenPHP v(\d+\.\d+\.\d+)/', $process->getOutput(), $matches)) {
            throw new RuntimeException('Cannot resolve recorded FrankenPHP version');
        }
        return $matches[1];
    }

    private function runtimeBinary(): string
    {
        return BuildPayload::load(BUILD_ROOT_PATH)?->prepareFile(BUILD_BIN_PATH . '/frankenphp')['runtime'] ?? BUILD_BIN_PATH . '/frankenphp';
    }

    private function debugBinary(): ?string
    {
        return BuildPayload::load(BUILD_ROOT_PATH)?->prepareFile(BUILD_BIN_PATH . '/frankenphp')['debug'] ?? BUILD_ROOT_PATH . '/debug/frankenphp.debug';
    }

    private function serviceUnit(string $source, string $service): string
    {
        $contents = file_get_contents($source);
        $contents = str_replace('/usr/bin/frankenphp', '/usr/bin/' . $service, $contents);
        $contents = str_replace(['/run/frankenphp', 'RuntimeDirectory=frankenphp'], ['/run/' . $service, 'RuntimeDirectory=' . $service], $contents);
        $path = TEMP_DIR . '/' . $service . (str_ends_with($source, '.openrc') ? '.openrc' : '.service');
        file_put_contents($path, $contents);
        chmod($path, 0755);
        return $path;
    }

    /**
     * Get PHP version and architecture
     */
    private function getPhpVersionAndArchitecture(): array
    {
        if (($payload = BuildPayload::load(BUILD_ROOT_PATH)) !== null) {
            return [$payload->phpVersion(), $payload->architecture()];
        }
        $basePhpVersion = SPP_PHP_VERSION;
        $phpBinary = BUILD_BIN_PATH . '/php';

        if (!file_exists($phpBinary)) {
            throw new RuntimeException("Warning: PHP binary not found at {$phpBinary}, using base PHP version: {$basePhpVersion}");
        }
        $versionProcess = new Process([$phpBinary, '-n', '-r', 'echo PHP_VERSION;'], env: ['LD_LIBRARY_PATH' => BUILD_LIB_PATH]);
        $versionProcess->run();
        $detectedVersion = trim($versionProcess->getOutput());

        if (!empty($detectedVersion)) {
            $fullPhpVersion = $detectedVersion;
            echo "Detected full PHP version from binary: {$fullPhpVersion}\n";
        }
        else {
            throw new RuntimeException("Warning: Could not detect PHP version from binary using base version: {$basePhpVersion}");
        }

        $archProcess = new Process(['uname', '-m']);
        $archProcess->run();
        $architecture = trim($archProcess->getOutput());

        if (empty($architecture)) {
            $archProcess = new Process(['arch']);
            $archProcess->run();
            $architecture = trim($archProcess->getOutput());

            if (empty($architecture)) {
                echo "Warning: Could not determine architecture, using x86_64 as fallback\n";
                $architecture = 'x86_64';
            }
        }

        return [$fullPhpVersion, $architecture];
    }

    /**
     * Get PHP version suffix for package filenames (e.g., "static-83" for PHP 8.3)
     */
    private function getPhpVersionSuffix(): string
    {
        [$phpVersion,] = $this->getPhpVersionAndArchitecture();

        // Extract major.minor version (e.g., "8.3.29" -> "8.3")
        if (preg_match('/^(\d+)\.(\d+)/', $phpVersion, $matches)) {
            $majorMinorNoDot = $matches[1] . $matches[2]; // e.g., "83"
        } else {
            $majorMinorNoDot = str_replace('.', '', $phpVersion);
        }

        // Construct suffix: static-{version} (e.g., "static-83")
        return 'static-' . $majorMinorNoDot;
    }

}
