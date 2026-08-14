<?php

namespace staticphp\package;

use RuntimeException;
use staticphp\package;
use staticphp\step\CreatePackages;
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
        return [];
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

    /**
     * Resolve iteration for a FrankenPHP package: --iteration override wins, then --bump
     * (remote query, shared with CreatePackages), then FrankenPHP's own local globber
     * (which uses a distinct filename layout).
     */
    private function resolveIteration(string $name, string $version, string $architecture, string $packageType, ?string $iterationOverride, bool $bump): string
    {
        if ($iterationOverride !== null) {
            return $iterationOverride;
        }
        if ($bump) {
            return (string)\staticphp\step\CreatePackages::getRemoteNextIteration($name, $version, $architecture, $packageType);
        }
        return (string)$this->getNextIteration($name, $version, $architecture, $packageType);
    }

    /**
     * Create RPM package for FrankenPHP
     */
    public function createRpmPackage(string $architecture, array $binaryDependencies, ?string $iterationOverride = null, bool $debuginfo = false, bool $bump = false): void
    {
        echo "Creating RPM package for FrankenPHP...\n";

        $packageFolder = DIST_PATH . '/frankenphp/package';
        $sharedLibrarySuffix = getSharedLibrarySuffix();
        $phpEmbedName = 'libphp' . $sharedLibrarySuffix . '.so';

        $ldLibraryPath = 'LD_LIBRARY_PATH=' . BUILD_LIB_PATH;
        [, $output] = shell()->execWithResult($ldLibraryPath . ' ' . BUILD_BIN_PATH . '/frankenphp --version');
        $output = implode("\n", $output);
        if (!preg_match('/FrankenPHP v(\d+\.\d+\.\d+)/', $output, $matches)) {
            throw new RuntimeException("Unable to detect FrankenPHP version from output: " . $output);
        }
        $version = $matches[1];

        // Append PHP version suffix to FrankenPHP version
        $rpmVersion = $version . CreatePackages::getPhpVersionTag('rpm', $version);

        $name = $this->getName();

        // Calculate iteration for RPM (with possible override)
        $iteration = $this->resolveIteration($name, $rpmVersion, $architecture, 'rpm', $iterationOverride, $bump);

        $versionedConflicts = $this->getVersionedConflicts();

        // Add distribution version to filename only
        $distVersion = $this->getDistVersion();
        $rpmRelease = $distVersion !== '' ? "{$iteration}.{$distVersion}" : $iteration;
        $packageFile = DIST_RPM_PATH . "/{$name}-{$rpmVersion}-{$rpmRelease}.{$architecture}.rpm";

        $fpmArgs = [
            'fpm',
            '-s', 'dir',
            '-t', 'rpm',
            '--rpm-compression', 'xz',
            '-p', $packageFile,  // Full path with distVersion in filename
            '-n', $name,
            '-v', $rpmVersion,
            '--description', "FrankenPHP - The Modern PHP Webserver",
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

        $consolidatedDeps = [];
        foreach ($binaryDependencies as $lib => $dependencyVersion) {
            if (!isset($consolidatedDeps[$lib]) || version_compare($dependencyVersion, $consolidatedDeps[$lib], '>')) {
                $consolidatedDeps[$lib] = $dependencyVersion;
            }
        }

        foreach ($consolidatedDeps as $lib => $dependencyVersion) {
            $fpmArgs[] = '--depends';
            $fpmArgs[] = "$lib({$dependencyVersion})(64bit)";
        }

        if (!is_dir("{$packageFolder}/empty/") && !mkdir("{$packageFolder}/empty/", 0755, true) && !is_dir("{$packageFolder}/empty/")) {
            throw new RuntimeException(sprintf('Directory "%s" was not created', "{$packageFolder}/empty/"));
        }

        // Generate autocompletion
        $completionFile = TEMP_DIR . '/frankenphp.bash';
        $ldLibraryPath = 'LD_LIBRARY_PATH=' . BUILD_LIB_PATH;
        shell()->exec($ldLibraryPath . ' ' . BUILD_BIN_PATH . '/frankenphp completion bash > ' . $completionFile);

        $fpmArgs = [...$fpmArgs, ...[
            '--depends', "$phpEmbedName",
            '--before-install', "{$packageFolder}/rhel/preinstall.sh",
            '--after-install', "{$packageFolder}/rhel/postinstall.sh",
            '--before-remove', "{$packageFolder}/rhel/preuninstall.sh",
            '--after-remove', "{$packageFolder}/rhel/postuninstall.sh",
            '--iteration', $rpmRelease,
            '--rpm-user', 'frankenphp',
            '--rpm-group', 'frankenphp',
            '--config-files', '/etc/frankenphp/Caddyfile',
            '--config-files', '/etc/frankenphp/Caddyfile.d',
            BUILD_BIN_PATH . '/frankenphp=/usr/bin/frankenphp',
            $completionFile . '=/usr/share/bash-completion/completions/frankenphp',
            "{$packageFolder}/rhel/frankenphp.service=/usr/lib/systemd/system/frankenphp.service",
            "{$packageFolder}/Caddyfile=/etc/frankenphp/Caddyfile",
            "{$packageFolder}/content/=/usr/share/frankenphp",
            "{$packageFolder}/empty/=/var/lib/frankenphp",
            "{$packageFolder}/empty/=/etc/frankenphp/Caddyfile.d",
        ]];

        $rpmProcess = new Process($fpmArgs);
        $rpmProcess->setTimeout(null);
        $rpmProcess->run(function ($type, $buffer) {
            echo $buffer;
        });

        echo "RPM package created: {$packageFile}\n";

        if ($debuginfo) {
            $frankenDbg = BUILD_ROOT_PATH . '/debug/frankenphp.debug';
            if (file_exists($frankenDbg)) {
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
                    $frankenDbg . '=/usr/lib/debug/usr/bin/frankenphp.debug',
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

        $packageFolder = DIST_PATH . '/frankenphp/package';
        $sharedLibrarySuffix = getSharedLibrarySuffix();
        // libphp filename with shared library suffix: libphp-zts-85.so, libphp-nts-84.so
        $phpEmbedName = 'libphp' . $sharedLibrarySuffix . '.so';

        $ldLibraryPath = 'LD_LIBRARY_PATH=' . BUILD_LIB_PATH;
        [, $output] = shell()->execWithResult($ldLibraryPath . ' ' . BUILD_BIN_PATH . '/frankenphp --version');
        $output = implode("\n", $output);
        if (!preg_match('/FrankenPHP v(\d+\.\d+\.\d+)/', $output, $matches)) {
            throw new RuntimeException("Unable to detect FrankenPHP version from output: " . $output);
        }
        $version = $matches[1];

        $name = $this->getName();

        // Convert system architecture to Debian architecture naming
        $debArch = match($architecture) {
            'x86_64' => 'amd64',
            'aarch64' => 'arm64',
            default => $architecture,
        };

        // For DEB packages, append PHP version to package version for proper sorting
        // e.g., 1.11.0+php85 is higher than 1.11.0+php83
        $debVersion = $version . CreatePackages::getPhpVersionTag('deb', $version);

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

        if (!is_dir("{$packageFolder}/empty/") && !mkdir("{$packageFolder}/empty/", 0755, true) && !is_dir("{$packageFolder}/empty/")) {
            throw new RuntimeException(sprintf('Directory "%s" was not created', "{$packageFolder}/empty/"));
        }

        // Determine the FrankenPHP suffix (just version, not prefix)
        // Extract version from package name: frankenphp8.5 or frankenphp85
        $prefix = CreatePackages::getPrefix();
        $frankenphpSuffix = '';
        // Extract version numbers from prefix (e.g., "php-zts8.5" -> "8.5", "php-nts85" -> "85")
        if (preg_match('/(\d+\.?\d*)/', $prefix, $matches)) {
            $frankenphpSuffix = $matches[1];
        }

        $patchPackageFolder = BASE_PATH . '/src/package/frankenphp';
        $completionFile = TEMP_DIR . '/frankenphp' . $frankenphpSuffix . '.bash';
        $ldLibraryPath = 'LD_LIBRARY_PATH=' . BUILD_LIB_PATH;
        shell()->exec($ldLibraryPath . ' ' . BUILD_BIN_PATH . '/frankenphp completion bash > ' . $completionFile);

        $fpmArgs = [...$fpmArgs, ...[
            '--depends', $phpEmbedName,
            '--after-install', "{$packageFolder}/debian/postinst.sh",
            '--before-remove', "{$packageFolder}/debian/prerm.sh",
            '--after-remove', "{$packageFolder}/debian/postrm.sh",
            '--iteration', $debIteration,
            BUILD_BIN_PATH . '/frankenphp=/usr/bin/frankenphp' . $frankenphpSuffix,
            $completionFile . '=/usr/share/bash-completion/completions/frankenphp' . $frankenphpSuffix,
            "{$packageFolder}/debian/frankenphp.service=/usr/lib/systemd/system/frankenphp{$frankenphpSuffix}.service",
            "{$packageFolder}/Caddyfile=/etc/frankenphp/Caddyfile",
            "{$packageFolder}/content/=/usr/share/frankenphp",
            "{$packageFolder}/empty/=/var/lib/frankenphp"
        ]];

        $rpmProcess = new Process($fpmArgs);
        $rpmProcess->setTimeout(null);
        $rpmProcess->run(function ($type, $buffer) {
            echo $buffer;
        });

        echo "DEB package created: {$packageFile}\n";

        // Create FrankenPHP debuginfo package if debug file exists (only if --debuginfo flag set for DEB)
        if ($debuginfo) {
            $frankenDbg = BUILD_ROOT_PATH . '/debug/frankenphp.debug';
            if (file_exists($frankenDbg)) {
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
                    $frankenDbg . '=/usr/lib/debug/usr/bin/frankenphp.debug',
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

        $packageFolder = DIST_PATH . '/frankenphp/package';
        $sharedLibrarySuffix = getSharedLibrarySuffix();
        // libphp filename with shared library suffix: libphp-zts-85.so, libphp-nts-84.so
        $phpEmbedName = 'libphp' . $sharedLibrarySuffix . '.so';

        $ldLibraryPath = 'LD_LIBRARY_PATH=' . BUILD_LIB_PATH;
        [, $output] = shell()->execWithResult($ldLibraryPath . ' ' . BUILD_BIN_PATH . '/frankenphp --version');
        $output = implode("\n", $output);
        if (!preg_match('/FrankenPHP v(\d+\.\d+\.\d+)/', $output, $matches)) {
            throw new RuntimeException("Unable to detect FrankenPHP version from output: " . $output);
        }
        $version = $matches[1];

        $name = $this->getName();

        // For APK packages, append PHP version to package version for proper sorting
        // e.g., 1.11.0p85 is higher than 1.11.0p83
        $apkVersion = $version . CreatePackages::getPhpVersionTag('apk', $version);
        // apk spells pre-releases _alpha/_beta/_rc and has no _dev; a tilde reaches apk add as-is
        // and is rejected there. Mirrors the translation CreatePackages does for extensions.
        $apkVersion = str_replace(['~dev', '~'], ['_pre', '_'], $apkVersion);

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

        $nfpmConfig['depends'] = $depends;
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
                'src' => BUILD_BIN_PATH . '/frankenphp',
                'dst' => '/usr/bin/frankenphp' . $frankenphpSuffix,
            ],
            [
                'src' => $completionFile,
                'dst' => '/usr/share/bash-completion/completions/frankenphp' . $frankenphpSuffix,
            ],
            [
                'src' => "{$packageFolder}/alpine/frankenphp.openrc",
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

        $nfpmConfig['scripts'] = [
            'postinstall' => "{$packageFolder}/alpine/post-install.sh",
            'preremove' => "{$packageFolder}/alpine/pre-deinstall.sh",
            'postremove' => "{$packageFolder}/alpine/post-deinstall.sh",
        ];

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
            $frankenDbg = BUILD_ROOT_PATH . '/debug/frankenphp.debug';
            if (file_exists($frankenDbg)) {
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
                    'dst' => '/usr/lib/debug/usr/bin/frankenphp' . $frankenphpSuffix . '.debug',
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

    /**
     * Prepare FrankenPHP repository by cloning or updating
     */
    private function prepareFrankenPhpRepository(): string
    {
        $repoUrl = 'https://github.com/php/frankenphp.git';
        $targetPath = DIST_PATH . '/frankenphp';

        $tagProcess = new Process([
            'bash', '-c',
            "git ls-remote --tags $repoUrl | grep -o 'refs/tags/[^{}]*$' | sed 's#refs/tags/##' | sort -V | tail -n1"
        ]);
        $tagProcess->run();
        if (!$tagProcess->isSuccessful()) {
            throw new RuntimeException("Failed to fetch tags: " . $tagProcess->getErrorOutput());
        }
        $latestTag = trim($tagProcess->getOutput());

        if (!is_dir($targetPath . '/.git')) {
            echo "Cloning FrankenPHP into DIST_PATH...\n";
            $clone = new Process(['git', 'clone', $repoUrl, $targetPath]);
            $clone->run();
            if (!$clone->isSuccessful()) {
                throw new RuntimeException("Git clone failed: " . $clone->getErrorOutput());
            }
        }
        else {
            echo "FrankenPHP already exists, fetching tags...\n";
            $fetch = new Process(['git', 'fetch', '--tags'], cwd: $targetPath);
            $fetch->run();
            if (!$fetch->isSuccessful()) {
                throw new RuntimeException("Git fetch failed: " . $fetch->getErrorOutput());
            }
        }

        $checkout = new Process(['git', 'checkout', $latestTag], cwd: $targetPath);
        $checkout->run();
        if (!$checkout->isSuccessful()) {
            throw new RuntimeException("Git checkout failed: " . $checkout->getErrorOutput());
        }

        return $latestTag;
    }

    /**
     * Get PHP version and architecture
     */
    private function getPhpVersionAndArchitecture(): array
    {
        $basePhpVersion = SPP_PHP_VERSION;
        $phpBinary = BUILD_BIN_PATH . '/php';

        if (!file_exists($phpBinary)) {
            throw new RuntimeException("Warning: PHP binary not found at {$phpBinary}, using base PHP version: {$basePhpVersion}");
        }
        $versionProcess = new Process([$phpBinary, '-r', 'echo PHP_VERSION;']);
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
     * Get next iteration number for package
     */
    private function getNextIteration(string $name, string $version, string $architecture, string $packageType): int
    {
        $maxIteration = ($packageType === 'apk') ? -1 : 0;

        if ($packageType === 'rpm') {
            // RPM: {name}-{version}-{iteration}.{distVersion}.{arch}.rpm
            // Also match old formats:
            // - {name}-{version}-{iteration}.{phpSuffix}.{distVersion}.{arch}.rpm (with phpSuffix)
            // - {name}-{version}-{iteration}.{arch}.rpm (no distVersion)
            $rpmPattern = DIST_RPM_PATH . "/{$name}-{$version}-*.rpm";
            $rpmFiles = glob($rpmPattern);

            foreach ($rpmFiles as $file) {
                // Match all formats: iteration followed by 0-2 parts, then arch.rpm
                if (preg_match("/{$name}-" . preg_quote($version, '/') . "-(\d+)(?:\.[^.]+){0,2}\.{$architecture}\.rpm$/", $file, $matches)) {
                    $iteration = (int)$matches[1];
                    $maxIteration = max($maxIteration, $iteration);
                }
            }
        }

        if ($packageType === 'deb') {
            // DEB: {name}-{phpSuffix}_{version}-{iteration}_{arch}.deb
            // Also match old formats for backwards compatibility
            $debPattern = DIST_DEB_PATH . "/{$name}*.deb";
            $debFiles = glob($debPattern);

            foreach ($debFiles as $file) {
                // Match new format: {name}-{phpSuffix}_{version}-{iteration}_{arch}.deb
                // The name might have the phpSuffix included or not
                if (preg_match("/" . preg_quote($name, '/') . "(?:-[^_]+)?_" . preg_quote($version, '/') . "-(\d+)_{$architecture}\.deb$/", $file, $matches)) {
                    $iteration = (int)$matches[1];
                    $maxIteration = max($maxIteration, $iteration);
                }
            }
        }

        if ($packageType === 'apk') {
            // APK: {name}-{version}-r{iteration}.{phpSuffix}.{arch}.apk
            // Also match old format: {name}-{version}-r{iteration}.{arch}.apk (no phpSuffix)
            $apkPattern = DIST_APK_PATH . "/{$name}-{$version}-r*.apk";
            $apkFiles = glob($apkPattern);

            foreach ($apkFiles as $file) {
                // Match both formats: r{iteration} followed by 0-1 parts, then arch.apk
                if (preg_match("/{$name}-" . preg_quote($version, '/') . "-r(\d+)(?:\.[^.]+)?\.{$architecture}\.apk$/", $file, $matches)) {
                    $iteration = (int)$matches[1];
                    $maxIteration = max($maxIteration, $iteration);
                }
            }
        }

        return $maxIteration + 1;
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

    /**
     * Get distribution version for RPM filenames (e.g., "el9", "el8", "fc39")
     */
    private function getDistVersion(): string
    {
        if (!file_exists('/etc/os-release')) {
            return '';
        }

        $osRelease = parse_ini_file('/etc/os-release');
        if (!$osRelease || !isset($osRelease['ID'], $osRelease['VERSION_ID'])) {
            return '';
        }

        $id = $osRelease['ID'];
        $versionId = $osRelease['VERSION_ID'];

        // Extract major version number
        if (preg_match('/^(\d+)/', $versionId, $matches)) {
            $majorVersion = $matches[1];
        } else {
            return '';
        }

        // Map distribution ID to prefix
        $distMap = [
            'rhel' => 'el',
            'centos' => 'el',
            'rocky' => 'el',
            'almalinux' => 'el',
            'fedora' => 'fc',
        ];

        $prefix = $distMap[$id] ?? '';
        return $prefix !== '' ? $prefix . $majorVersion : '';
    }
}
