<?php

namespace staticphp\step;

use RuntimeException;
use staticphp\extension;
use staticphp\package;
use staticphp\util\ExtMeta;
use staticphp\util\SkippedExtensions;
use Symfony\Component\Process\Process;
use staticphp\CraftConfig;

class CreatePackages
{
    private static array $versionArch = [];
    private static $extensions = [];
    private static $sharedExtensions = [];
    /** @var array<string,string> shared extensions dropped from this build [name => reason] */
    private static array $skippedExtensions = [];
    private static $sapis = [];
    private static $binaryDependencies = [];
    private static string $packageType = 'rpm';
    private static ?string $iterationOverride = null;
    private static bool $bump = false;
    /** @var array<string, array{0:int,1:?string}> memoised HTTP GET responses (per run) */
    private static array $httpCache = [];
    /** @var array<string,string> resolved iteration per base package (name|version|arch|type) */
    private static array $resolvedIterations = [];
    private static string $prefix = '-zts';
    private static bool $debuginfo = false;

    public static function run($packageNames = null, ?string $iteration = null, ?bool $debuginfo = null, ?bool $bump = null): true
    {
        // Skip propagation reads extension metadata, so the package configs must be registered first.
        self::bootstrapSpcGlobals();
        self::loadConfig();

        if (!defined('DOWNLOAD_PATH')) {
            define('DOWNLOAD_PATH', BUILD_ROOT_PATH . '/download');
        }
        if (!is_dir(DOWNLOAD_PATH)) {
            @mkdir(DOWNLOAD_PATH, 0755, true);
        }
        $phpBinary = BUILD_BIN_PATH . '/php';
        self::$binaryDependencies = self::getBinaryDependencies($phpBinary);

        // Use values from constants set by BaseCommand
        self::$prefix = defined('SPP_PREFIX') ? SPP_PREFIX : '-zts';
        self::$packageType = defined('SPP_TYPE') ? SPP_TYPE : 'rpm';
        self::$iterationOverride = $iteration !== null && $iteration !== '' ? $iteration : null;
        if ($bump !== null) {
            self::$bump = $bump;
        }

        // Verify that we're not trying to package a glibc binary as APK
        //if (self::$packageType === 'apk' && file_exists($phpBinary) && self::isGlibcBinary($phpBinary)) {
        //    throw new RuntimeException(
        //        "Error: Cannot create APK packages with glibc binary. APK packages require musl libc.\n" .
        //        "The binary at {$phpBinary} is linked against glibc, but APK is the Alpine Linux package format which uses musl.\n" .
        //        "Please rebuild with musl libc or use a different package type (rpm/deb)."
        //    );
        //}

        // Set debuginfo flag from parameter
        if ($debuginfo !== null) {
            self::$debuginfo = $debuginfo;
        }

        if ($packageNames !== null) {
            if (is_string($packageNames)) {
                $packageNames = [$packageNames];
            }

            foreach ($packageNames as $packageName) {
                echo "Building package: {$packageName}\n";

                if (in_array($packageName, self::$sapis, true)) {
                    self::createSapiPackage($packageName);
                }
                elseif ($packageName === 'devel') {
                    self::createSapiPackage($packageName);
                }
                elseif (in_array($packageName, self::$sharedExtensions) || isset(self::$skippedExtensions[$packageName])) {
                    self::createExtensionPackage($packageName, true);
                }
                else {
                    $genericClass = "\\staticphp\\package\\{$packageName}";
                    if (class_exists($genericClass)) {
                        self::createGenericPackage($packageName);
                    }
                    else {
                        echo "Warning: Package {$packageName} not found in configuration.\n";
                    }
                }
            }
        }
        else {
            self::createSapiPackages();
            self::createSapiPackage('devel');
            self::createGenericPackage('pie');
            // Create metapackage for APK to allow "apk add php-zts85"
            if (self::$packageType === 'apk') {
                self::createGenericPackage('meta');
            }
            self::createExtensionPackages();
        }

        echo "Package creation completed.\n";
        return true;
    }

    /**
     * Create a generic package defined in src/package/{name}.php implementing staticphp\package
     */
    private static function createGenericPackage(string $name): void
    {
        $packageClass = "\\staticphp\\package\\{$name}";
        if (!class_exists($packageClass)) {
            echo "Warning: Package class not found: {$name}\n";
            return;
        }

        [$phpVersion, $architecture] = self::getPhpVersionAndArchitecture();

        // Allow generic packages to define their own version (e.g., pie.phar version)
        $pkgVersion = $phpVersion;
        $pkg = new $packageClass();
        if (method_exists($pkg, 'getVersion')) {
            $pkgVersion = $pkg->getVersion();
        }

        $package = $pkg ?? new $packageClass();

        self::createPackageWithFpm($package, $pkgVersion, $architecture);

        // Create debuginfo packages: always for RPM, only if --debuginfo flag set for others
        $dbgConfig = $package->getDebuginfoFpmConfig();
        if (is_array($dbgConfig) && !empty($dbgConfig['files'])) {
            if (self::$debuginfo) {
                self::createPackageWithFpm($package, $pkgVersion, $architecture, true);
            }
        }
    }

    private static function loadConfig(): void
    {
        echo "Loading configuration from Twig template...\n";

        $craftConfig = CraftConfig::getInstance();

        self::$extensions = $craftConfig->getStaticExtensions();
        self::$sharedExtensions = $craftConfig->getSharedExtensions();
        self::$sapis = $craftConfig->getSapis();

        self::$skippedExtensions = SkippedExtensions::resolveFor(self::$sharedExtensions);
        if (self::$skippedExtensions !== []) {
            self::$sharedExtensions = array_values(array_diff(self::$sharedExtensions, array_keys(self::$skippedExtensions)));
        }

        echo "Loaded configuration:\n";
        echo "- SAPIs: " . implode(', ', self::$sapis) . "\n";
        echo "- Extensions: " . implode(', ', self::$extensions) . "\n";
        echo "- Shared Extensions: " . implode(', ', self::$sharedExtensions) . "\n";

        if (self::$skippedExtensions !== []) {
            echo "=== SKIPPED SHARED EXTENSIONS (allow-shared-ext-failure) ===\n";
            foreach (self::$skippedExtensions as $extension => $reason) {
                echo '  ' . str_pad($extension, 22) . $reason . "\n";
            }
            echo str_repeat('=', 60) . "\n";
        }
    }

    /** @return array<string,string> [extension => reason] */
    public static function getSkippedExtensions(): array
    {
        return self::$skippedExtensions;
    }

    public static function isSkipped(string $extension): bool
    {
        return isset(self::$skippedExtensions[$extension]);
    }

    private static function createSapiPackages(): void
    {
        echo "Creating packages for SAPIs...\n";

        foreach (self::$sapis as $sapi) {
            self::createSapiPackage($sapi);
        }
    }

    private static function createSapiPackage(string $sapi): void
    {
        $packageClass = "\\staticphp\\package\\{$sapi}";

        if (!class_exists($packageClass)) {
            echo "Warning: Package class not found for SAPI: {$sapi}\n";
            return;
        }

        // FrankenPHP has a special package creation flow
        if ($sapi === 'frankenphp') {
            $package = new $packageClass();
            $package->createPackages(self::$packageType, self::$binaryDependencies, self::$iterationOverride, self::$debuginfo, self::$bump);
            return;
        }

        [$phpVersion, $architecture] = self::getPhpVersionAndArchitecture();

        $package = new $packageClass();

        self::createPackageWithFpm($package, $phpVersion, $architecture);

        // Create debuginfo packages: always for RPM, only if --debuginfo flag set for others
        $dbgConfig = $package->getDebuginfoFpmConfig();
        if (is_array($dbgConfig) && !empty($dbgConfig['files'])) {
            if (self::$debuginfo) {
                self::createPackageWithFpm($package, $phpVersion, $architecture, true);
            }
        }
    }

    private static function createExtensionPackages(): void
    {
        echo "Creating packages for extensions...\n";

        foreach (self::$sharedExtensions as $extension) {
            if (ExtMeta::isAddon($extension)) {
                continue;
            }
            self::createExtensionPackage($extension);
        }
    }

    /**
     * @param bool $explicit the caller named this extension on --packages, so a recorded
     *                       skip is an error rather than something to pass over quietly
     */
    private static function createExtensionPackage(string $extension, bool $explicit = false): void
    {
        if (isset(self::$skippedExtensions[$extension])) {
            if ($explicit) {
                throw new RuntimeException("Extension {$extension} was requested explicitly but the build skipped it: " . self::$skippedExtensions[$extension]);
            }
            echo "SKIPPED: not packaging {$extension} — " . self::$skippedExtensions[$extension] . "\n";
            return;
        }

        // Addons are configure flags of their parent and ship inside its .so; only --packages names one.
        if (ExtMeta::isAddon($extension)) {
            echo "Not packaging {$extension}: it is an addon of another extension and ships with that extension's package.\n";
            return;
        }

        // Unreachable in allow-failure mode: spc deletes the .so and records the skip. Reaching
        // it means the two mechanisms drifted, so it stays fatal regardless of the manifest.
        $sharedObject = BUILD_MODULES_PATH . '/' . $extension . getSharedLibrarySuffix() . '.so';
        if (!file_exists($sharedObject)) {
            throw new RuntimeException("Shared object missing for extension {$extension}: {$sharedObject} — refusing to create a content-less package");
        }

        [$phpVersion, $architecture] = self::getPhpVersionAndArchitecture();
        $extensionVersion = self::getExtensionVersion($extension, $phpVersion);

        $package = new extension($extension);
        $packageClass = "\\staticphp\\package\\{$extension}";
        if (class_exists($packageClass)) {
            $package = new $packageClass($extension);
        }

        if (!file_exists(INI_PATH . '/extension/' . $extension . '.ini')) {
            echo "Warning: INI file for extension {$extension} not found, skipping package creation.\n";
            return;
        }

        self::createPackageWithFpm($package, $extensionVersion, $architecture);

        // Create debuginfo packages: always for RPM, only if --debuginfo flag set for others
        $dbgConfig = $package->getDebuginfoFpmConfig();
        if (is_array($dbgConfig) && !empty($dbgConfig['files'])) {
            if (self::$debuginfo) {
                self::createPackageWithFpm($package, $extensionVersion, $architecture, true);
            }
        }
    }

    private static function getExtensionVersion(string $extension, string $phpVersion): string
    {
        $phpBinary = BUILD_BIN_PATH . '/php';

        if (!file_exists($phpBinary)) {
            throw new RuntimeException("Warning: PHP binary not found at {$phpBinary}, using PHP version for extension {$extension}: {$phpVersion}");
        }

        $extensionClass = "\\staticphp\\package\\extension\\$extension";
        if (!class_exists($extensionClass)) {
            $extensionClass = extension::class;
        }
        $extensionC = new $extensionClass($extension);
        $dependencies = $extensionC->getExtensionDependencies($extension);
        $args = [
            '-n', '-d', 'error_reporting=0', '-d', 'extension_dir=' . BUILD_MODULES_PATH,
        ];
        foreach ($dependencies as $dependency) {
            $depExt = new extension($dependency);
            if ($depExt->isSharedExtension() && !ExtMeta::isAddon($dependency)) {
                $args[] = '-d';
                $args[] = "extension={$dependency}";
            }
        }
        $args[] = '-d';
        $args[] = "extension={$extension}";
        // No try/catch: an extension that dies on a signal here is a broken package, and
        // Process::wait()'s ProcessSignaledException is the only thing that surfaces it.
        $probe = static function (?array $env) use ($phpBinary, $args, $extension): string {
            $p = new Process([$phpBinary, ...$args, '-r', "echo phpversion('{$extension}');"], env: $env);
            $p->run();
            return trim(preg_replace('/^Warning:.*$/m', '', trim($p->getOutput())));
        };

        $rawExtensionVersion = $probe(null);

        // Some shared extensions need libphp-zts-NN.so via LD_LIBRARY_PATH to dlopen.
        if ($rawExtensionVersion === '' && is_dir(BUILD_LIB_PATH)) {
            $rawExtensionVersion = $probe(['LD_LIBRARY_PATH' => BUILD_LIB_PATH]);
        }
        if ($rawExtensionVersion === '') {
            $rawExtensionVersion = self::detectExtensionVersionFromSource($extension);
        }

        $extensionVersion = null;
        if (preg_match('/\d+\.\d+(?:\.\d+)?(?:[.-]?(?:alpha|beta|rc|dev)\d*)?/i', $rawExtensionVersion, $m)) {
            $extensionVersion = self::normalizeVersion($m[0]);
        }

        if (empty($extensionVersion)) {
            throw new RuntimeException("Warning: Could not detect version for extension {$extension}");
        }

        echo "Detected version for extension {$extension}: {$extensionVersion}\n";

        return $extensionVersion;
    }

    /**
     * Turn an upstream PHP/extension version string into a package-orderable one:
     *   8.6.0beta1 / 8.6.0-beta1 / 8.6.0RC1 / 8.6.0-dev  ->  8.6.0~beta1 / 8.6.0~rc1 / 8.6.0~dev
     * Plain releases pass through untouched. Without the tilde a pre-release outranks its
     * own GA release (rpmvercmp reads 8.6.0beta1 > 8.6.0); with it the chain is
     *   8.5.4 < 8.6.0~alpha3 < 8.6.0~beta1 < 8.6.0~rc1 < 8.6.0
     */
    public static function normalizeVersion(string $raw): string
    {
        if (preg_match('/^(\d+\.\d+(?:\.\d+)?)(?:[.-]?((?:alpha|beta|rc|dev)\d*))?$/i', trim($raw), $m)) {
            return $m[1] . (empty($m[2]) ? '' : '~' . strtolower($m[2]));
        }
        return trim($raw);
    }

    /**
     * The tag binding a package to the PHP it was built against, appended to that package's own
     * version: '_86' (rpm), '+php86' (deb), 'p86' (apk).
     *
     * While that PHP is a pre-release the tag carries its marker as well, so an extension built
     * against 8.6.0~alpha3 sorts below the same one built against ~beta1 and both below the GA
     * rebuild — otherwise the version never moves and a .so built against an older libphp stays
     * installed after PHP is bumped.
     *
     * rpm and deb put the marker behind the version digits ('_86~beta1'), where it keeps the
     * ordering across minors that the digits carry: 8.6's pre-release builds still outrank 8.5's,
     * exactly as php-zts-cli 8.6.0~alpha3 outranks 8.5.4. apk has no tilde and only accepts the
     * post-suffix behind its own underscore once a pre-release suffix is present ('6.2.0_rc2p86'
     * is rejected), so there the marker goes in front and forces the '_p' form.
     */
    public static function getPhpVersionTag(string $packageType, string $packageVersion): string
    {
        [$fullPhpVersion] = self::getPhpVersionAndArchitecture();
        if (preg_match('/^(\d+)\.(\d+)/', $fullPhpVersion, $m)) {
            $phpVersionSuffix = $m[1] . $m[2];
        } else {
            $phpVersionSuffix = str_replace('.', '', $fullPhpVersion);
        }
        $preRelease = preg_match('/~(.+)$/', $fullPhpVersion, $m) ? '~' . $m[1] : '';

        return match ($packageType) {
            'deb' => '+php' . $phpVersionSuffix . $preRelease,
            'apk' => $preRelease . (str_contains($packageVersion . $preRelease, '~') ? '_p' : 'p') . $phpVersionSuffix,
            default => '_' . $phpVersionSuffix . $preRelease,
        };
    }

    /** Pull SPC's internal-env constants and package configs in after BaseCommand has set BUILD_ROOT_PATH. */
    public static function bootstrapSpcGlobals(): void
    {
        static $done = false;
        if ($done) {
            return;
        }
        $done = true;
        $spcRoot = BASE_PATH . '/vendor/crazywhalecc/static-php-cli';
        require_once $spcRoot . '/src/globals/internal-env.php';
        foreach (['lib', 'target', 'ext'] as $kind) {
            \StaticPHP\Config\PackageConfig::loadFromDir($spcRoot . "/config/pkg/{$kind}", 'core');
        }
    }

    private static function detectExtensionVersionFromSource(string $extension): string
    {
        $sourceDir = SOURCE_PATH . '/php-src/ext/' . $extension;
        if (!is_dir($sourceDir)) {
            return '';
        }
        // PECL extensions ship a package.xml at the source root.
        $packageXml = $sourceDir . '/package.xml';
        if (is_file($packageXml)) {
            $xml = @simplexml_load_file($packageXml);
            if ($xml !== false && isset($xml->version->release)) {
                return trim((string) $xml->version->release);
            }
        }
        // Otherwise scan the C headers for PHP_<EXT>_VERSION (the macro PHP_MINFO_FUNCTION
        // emits as the phpversion() result). Look in php_<ext>.h first, then any header
        // matching <ext>*.h.
        $candidates = array_merge(
            [$sourceDir . '/php_' . $extension . '.h'],
            (array) glob($sourceDir . '/*.h'),
        );
        foreach ($candidates as $hdr) {
            if (!is_file($hdr)) {
                continue;
            }
            $contents = (string) file_get_contents($hdr);
            if (preg_match('/define\s+PHP_' . strtoupper($extension) . '_VERSION\s+"([^"]+)"/i', $contents, $m)) {
                return trim($m[1]);
            }
        }
        return '';
    }

    private static function createPackageWithFpm(package $package, string $phpVersion, string $architecture, bool $isDebuginfo = false): void
    {
        if (self::$packageType === 'rpm') {
            self::createRpmPackage($package, $phpVersion, $architecture, $isDebuginfo);
        }

        if (self::$packageType === 'deb') {
            self::createDebPackage($package, $phpVersion, $architecture, $isDebuginfo);
        }

        if (self::$packageType === 'apk') {
            self::createApkPackage($package, $phpVersion, $architecture, $isDebuginfo);
        }
    }

    private static function createRpmPackage(package $package, string $phpVersion, string $architecture, bool $isDebuginfo = false): void
    {
        $name = $isDebuginfo ? $package->getName() . '-debuginfo' : $package->getName();
        $config = $isDebuginfo ? $package->getDebuginfoFpmConfig() : $package->getFpmConfig();
        $extraArgs = $isDebuginfo ? [] : $package->getFpmExtraArgs();

        echo "Creating RPM package for {$name}...\n";

        // For RPM packages, append PHP version to package version for extensions
        // This ensures proper version ordering when the same extension version is built for different PHP versions
        [$fullPhpVersion] = self::getPhpVersionAndArchitecture();
        $rpmVersion = $phpVersion;

        // If package version differs from PHP version, it's an extension - append PHP version
        if ($phpVersion !== $fullPhpVersion) {
            $rpmVersion = $phpVersion . self::getPhpVersionTag('rpm', $phpVersion);
        }

        // Calculate iteration for RPM (--iteration override > --bump remote query > local)
        $baseIteration = self::resolveIteration($name, $rpmVersion, $architecture, 'rpm');

        // Add distribution version to iteration for RPM metadata
        $distVersion = self::getDistVersion();
        $iteration = $distVersion !== '' ? "{$baseIteration}.{$distVersion}" : $baseIteration;
        $distSuffix = $distVersion !== '' ? ".{$distVersion}" : '';
        $packageFile = DIST_RPM_PATH . "/{$name}-{$rpmVersion}-{$iteration}.{$architecture}.rpm";

        $fpmArgs = [...[
            'fpm',
            '-s', 'dir',
            '-t', 'rpm',
            '--rpm-compression', 'xz',
            '-p', $packageFile,  // Full path with phpSuffix and distVersion in filename
            '--name', $name,
            '--version', $rpmVersion,
            '--iteration', $iteration,
            '--architecture', $architecture,
            '--description', $package->getDescription(),
            '--license', $package->getLicense(),
            '--maintainer', 'Marc Henderkes <pkg@henderkes.com>',
            '--vendor', 'Marc Henderkes <pkg@henderkes.com>',
            '--url', 'pkgs.henderkes.com',
        ], ...$extraArgs];

        // Ensure non-CLI packages depend on the same PHP major.minor as php-zts-cli (ignore iteration/patch)
        if ($name !== self::getPrefix() . '-cli') {
            [$fullPhpVersion] = self::getPhpVersionAndArchitecture();
            if (preg_match('/^(\d+)\.(\d+)/', $fullPhpVersion, $m)) {
                $maj = (int)$m[1];
                $min = (int)$m[2];
                $nextMin = $min + 1;
                $lowerBound = sprintf('%d.%d', $maj, $min);
                $upperBound = sprintf('%d.%d', $maj, $nextMin);
                // RPM range: >= X.Y and < X.(Y+1)
                $fpmArgs[] = '--depends';
                $fpmArgs[] = self::getPrefix() . "-cli >= {$lowerBound}";
                $fpmArgs[] = '--depends';
                $fpmArgs[] = self::getPrefix() . "-cli < {$upperBound}";
            }
        }

        if (str_ends_with($name, '-debuginfo')) {
            $base = preg_replace('/-debuginfo$/', '', $name);
            $fpmArgs[] = '--depends';
            $fpmArgs[] = sprintf('%s = %s-%s', $base, $rpmVersion, $iteration);
        }

        if (isset($config['provides']) && is_array($config['provides'])) {
            foreach ($config['provides'] as $provide) {
                $fpmArgs[] = '--provides';
                $fpmArgs[] = "$provide = $rpmVersion-$iteration";
                if (str_ends_with($provide, '.so')) {
                    $provide = str_replace('.so', '.so()(64bit)', $provide);
                    $fpmArgs[] = '--provides';
                    $fpmArgs[] = "$provide = $rpmVersion-$iteration";
                }
            }
        }

        if (isset($config['replaces']) && is_array($config['replaces'])) {
            foreach ($config['replaces'] as $replace) {
                $fpmArgs[] = '--replaces';
                $fpmArgs[] = "$replace < {$rpmVersion}-{$iteration}";
            }
        }

        if (isset($config['conflicts']) && is_array($config['conflicts'])) {
            foreach ($config['conflicts'] as $conflict) {
                $fpmArgs[] = '--conflicts';
                $fpmArgs[] = $conflict;
            }
        }

        $consolidatedDeps = [];
        foreach (self::$binaryDependencies as $lib => $version) {
            if (!isset($consolidatedDeps[$lib]) || version_compare($version, $consolidatedDeps[$lib], '>')) {
                $consolidatedDeps[$lib] = $version;
            }
        }

        foreach ($consolidatedDeps as $lib => $version) {
            $fpmArgs[] = '--depends';
            $fpmArgs[] = "{$lib}({$version})(64bit)";
        }
        if (isset($config['depends']) && is_array($config['depends'])) {
            foreach ($config['depends'] as $depend) {
                $fpmArgs[] = '--depends';
                if (preg_match('/\.so(\.\d+)*$/', $depend)) {
                    $depend .= '()(64bit)';
                }
                $fpmArgs[] = $depend;
            }
        }

        if (isset($config['directories']) && is_array($config['directories'])) {
            foreach ($config['directories'] as $dir) {
                $fpmArgs[] = '--directories';
                $fpmArgs[] = $dir;
            }
        }

        if (isset($config['config-files']) && is_array($config['config-files'])) {
            foreach ($config['config-files'] as $configFile) {
                $fpmArgs[] = '--config-files';
                $fpmArgs[] = $configFile;
            }
        }

        if (isset($config['rpm_attrs']) && is_array($config['rpm_attrs'])) {
            foreach ($config['rpm_attrs'] as $attr) {
                $fpmArgs[] = '--rpm-attr';
                $fpmArgs[] = $attr;
            }
        }

        if (isset($config['files']) && is_array($config['files'])) {
            foreach ($config['files'] as $source => $dest) {
                if (file_exists($source)) {
                    if (str_starts_with($source, BUILD_BIN_PATH . '/') &&
                        is_executable($source) &&
                        basename($source) !== basename($dest)) {
                        $source = self::fixBinaryDebugLink($source, $dest);
                    }
                    $fpmArgs[] = $source . '=' . $dest;
                }
                else {
                    self::requireSourceFile($source, $dest);
                }
            }
        }

        if (isset($config['empty_directories']) && is_array($config['empty_directories'])) {
            $emptyDir = TEMP_DIR . '/spp_empty/';
            if (!file_exists($emptyDir) && !mkdir($emptyDir, 0755, true) && !is_dir($emptyDir)) {
                throw new RuntimeException(sprintf('Directory "%s" was not created', $emptyDir));
            }
            if (is_dir($emptyDir)) {
                $files = array_diff(scandir($emptyDir), ['.', '..']);
                if (!empty($files)) {
                    exec('rm -rf ' . escapeshellarg($emptyDir . '/*'));
                }
            }
            foreach ($config['empty_directories'] as $dir) {
                $fpmArgs[] = $emptyDir . '=' . $dir;
            }
        }

        $rpmProcess = new Process($fpmArgs);
        $rpmProcess->setTimeout(null);
        $rpmProcess->run(function ($type, $buffer) {
            echo $buffer;
        });
        if (!$rpmProcess->isSuccessful()) {
            throw new RuntimeException("RPM package creation failed: " . $rpmProcess->getErrorOutput());
        }

        echo "RPM package created: {$packageFile}\n";
    }

    private static function createDebPackage(
        package $package,
        string $phpVersion,
        string $architecture,
        bool $isDebuginfo = false,
    ): void
    {
        $name = $isDebuginfo ? $package->getName() . '-debuginfo' : $package->getName();
        $config = $isDebuginfo ? $package->getDebuginfoFpmConfig() : $package->getFpmConfig();
        $extraArgs = $isDebuginfo ? [] : (method_exists($package, 'getDebExtraArgs') ? $package->getDebExtraArgs() : $package->getFpmExtraArgs());

        echo "Creating DEB package for {$name}...\n";

        // Convert system architecture to Debian architecture naming
        $debArch = match($architecture) {
            'x86_64' => 'amd64',
            'aarch64' => 'arm64',
            default => $architecture,
        };

        // For DEB packages, append PHP version to package version for extensions
        // This ensures proper version ordering when the same extension version is built for different PHP versions
        // e.g., redis 6.0.2+php85 is higher than redis 6.0.2+php83
        [$fullPhpVersion] = self::getPhpVersionAndArchitecture();
        $debVersion = $phpVersion;

        // If package version differs from PHP version, it's an extension - append PHP version
        if ($phpVersion !== $fullPhpVersion) {
            $debVersion = $phpVersion . self::getPhpVersionTag('deb', $phpVersion);
        }

        // Calculate iteration for DEB (--iteration override > --bump remote query > local)
        $iteration = self::resolveIteration($name, $debVersion, $debArch, 'deb');

        //$osRelease = parse_ini_file('/etc/os-release');
        //$distroCodename = $osRelease['VERSION_CODENAME'] ?? null;
        //$debIteration = $distroCodename !== '' ? "{$iteration}~{$distroCodename}" : $iteration;
        $debIteration = $iteration;
        $fullVersion = "{$debVersion}-{$debIteration}";

        // Debian filename format: {name}_{version}-{revision}_{arch}.deb
        $packageFile = DIST_DEB_PATH . "/{$name}_{$debVersion}-{$debIteration}_{$debArch}.deb";

        $fpmArgs = [...[
            'fpm',
            '-s', 'dir',
            '-t', 'deb',
            '--deb-compression', 'xz',
            '-p', $packageFile,
            '--name', $name,
            '--version', $debVersion,
            '--architecture', $debArch,
            '--iteration', $debIteration,       // Debian revision (includes distro)
            '--description', $package->getDescription(),
            '--license', $package->getLicense(),
            '--maintainer', 'Marc Henderkes <pkg@henderkes.com>',
            '--vendor', 'Marc Henderkes <pkg@henderkes.com>',
            '--url', 'pkgs.henderkes.com',
        ], ...$extraArgs];

        // Ensure non-CLI packages depend on the same PHP major.minor as php-zts-cli (ignore iteration/patch)
        // IMPORTANT: Use the actual PHP runtime version, not the package's own version (extensions have their own versioning)
        if ($name !== self::getPrefix() . '-cli') {
            [$fullPhpVersion] = self::getPhpVersionAndArchitecture();
            if (preg_match('/^(\d+)\.(\d+)/', $fullPhpVersion, $m)) {
                $maj = (int)$m[1];
                $min = (int)$m[2];
                $nextMin = $min + 1;
                $lowerBound = sprintf('%d.%d', $maj, $min);
                // For Debian, use an upper bound with tilde to exclude the next minor and its pre-releases
                $upperBound = sprintf('%d.%d~', $maj, $nextMin);
                $fpmArgs[] = '--depends';
                $fpmArgs[] = self::getPrefix() . "-cli (>= {$lowerBound})";
                $fpmArgs[] = '--depends';
                $fpmArgs[] = self::getPrefix() . "-cli (<< {$upperBound})";
            }
        }

        // If this is a debuginfo package, make it depend exactly on its base package version-iteration
        if (str_ends_with($name, '-debuginfo')) {
            $base = preg_replace('/-debuginfo$/', '', $name);
            $fpmArgs[] = '--depends';
            $fpmArgs[] = sprintf('%s (= %s)', $base, $fullVersion);
        }

        if (isset($config['provides']) && is_array($config['provides'])) {
            foreach ($config['provides'] as $provide) {
                $fpmArgs[] = '--provides';
                $fpmArgs[] = "{$provide} (= {$fullVersion})";
            }
        }

        if (isset($config['replaces']) && is_array($config['replaces'])) {
            foreach ($config['replaces'] as $replace) {
                $fpmArgs[] = '--replaces';
                $fpmArgs[] = "{$replace} (<= {$fullVersion})";
            }
        }

        if (isset($config['conflicts']) && is_array($config['conflicts'])) {
            foreach ($config['conflicts'] as $conflict) {
                $fpmArgs[] = '--conflicts';
                $fpmArgs[] = $conflict;
            }
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
        foreach (self::$binaryDependencies as $lib => $version) {
            if (isset($systemLibraryMap[$lib])) {
                // Use mapped name for system libraries
                $packageName = $systemLibraryMap[$lib];
            }
            else {
                // For other libraries, remove .so suffix
                $packageName = preg_replace('/\.so(\.\d+)?$/', '', $lib);
            }

            $numericVersion = preg_replace('/[^0-9.]/', '', $version);
            if (!isset($consolidatedDeps[$packageName]) || version_compare($numericVersion, $consolidatedDeps[$packageName], '>')) {
                $consolidatedDeps[$packageName] = $numericVersion;
            }
        }

        foreach ($consolidatedDeps as $packageName => $numericVersion) {
            $fpmArgs[] = '--depends';
            $fpmArgs[] = "{$packageName} (>= {$numericVersion})";
        }
        if (isset($config['depends']) && is_array($config['depends'])) {
            foreach ($config['depends'] as $depend) {
                $fpmArgs[] = '--depends';
                $fpmArgs[] = $depend;
            }
        }

        if (isset($config['directories']) && is_array($config['directories'])) {
            foreach ($config['directories'] as $dir) {
                $fpmArgs[] = '--directories';
                $fpmArgs[] = $dir;
            }
        }

        if (isset($config['config-files']) && is_array($config['config-files'])) {
            foreach ($config['config-files'] as $configFile) {
                $fpmArgs[] = '--config-files';
                $fpmArgs[] = $configFile;
            }
        }
        $fpmArgs[] = '--deb-no-default-config-files';

        if (isset($config['files']) && is_array($config['files'])) {
            foreach ($config['files'] as $source => $dest) {
                if (file_exists($source)) {
                    // Check if this is a binary that needs its debug link fixed
                    // Only fix binaries in BUILD_BIN_PATH that are being renamed
                    if (str_starts_with($source, BUILD_BIN_PATH . '/') &&
                        is_executable($source) &&
                        basename($source) !== basename($dest)) {
                        // Fix the debug link and use the temporary binary instead
                        $source = self::fixBinaryDebugLink($source, $dest);
                    }
                    $fpmArgs[] = $source . '=' . $dest;
                }
                else {
                    self::requireSourceFile($source, $dest);
                }
            }
        }

        if (isset($config['empty_directories']) && is_array($config['empty_directories'])) {
            $emptyDir = TEMP_DIR . '/spp_empty';
            if (!file_exists($emptyDir) && !mkdir($emptyDir, 0755, true) && !is_dir($emptyDir)) {
                throw new RuntimeException(sprintf('Directory "%s" was not created', $emptyDir));
            }
            if (is_dir($emptyDir)) {
                $files = array_diff((array)scandir($emptyDir), ['.', '..']);
                if (!empty($files)) {
                    exec('rm -rf ' . escapeshellarg($emptyDir . '/*'));
                }
            }
            foreach ($config['empty_directories'] as $dir) {
                $fpmArgs[] = $emptyDir . '=' . $dir;
            }
        }

        $debProcess = new Process($fpmArgs);
        $debProcess->setTimeout(null);
        $debProcess->run(function ($type, $buffer) {
            echo $buffer;
        });
        if (!$debProcess->isSuccessful()) {
            throw new RuntimeException("DEB package creation failed: " . $debProcess->getErrorOutput());
        }

        echo "DEB package created: {$packageFile}\n";
    }

    private static function createApkPackage(package $package, string $phpVersion, string $architecture, bool $isDebuginfo = false): void
    {
        $name = $isDebuginfo ? $package->getName() . '-debuginfo' : $package->getName();
        $config = $isDebuginfo ? $package->getDebuginfoFpmConfig() : $package->getFpmConfig();
        $extraArgs = $isDebuginfo ? [] : $package->getFpmExtraArgs();

        echo "Creating APK package for {$name} using nfpm...\n";

        // For APK packages, append PHP version to package version for extensions
        // This ensures proper version ordering when the same extension version is built for different PHP versions
        [$fullPhpVersion] = self::getPhpVersionAndArchitecture();
        $apkVersion = $phpVersion;

        // If package version differs from PHP version, it's an extension - append PHP version
        if ($phpVersion !== $fullPhpVersion) {
            $apkVersion = $phpVersion . self::getPhpVersionTag('apk', $phpVersion);
        }

        // apk spells pre-releases _alpha/_beta/_pre/_rc; nfpm passes a tilde straight through and
        // apk add then rejects it. apk has no _dev, so a dev snapshot maps to _pre — it sorts
        // below the release it precedes, same as ~dev does. RPM and DEB keep ~.
        $apkVersion = str_replace(['~dev', '~'], ['_pre', '_'], $apkVersion);

        // Calculate iteration for APK (--iteration override > --bump remote query > local)
        $iteration = self::resolveIteration($name, $apkVersion, $architecture, 'apk');

        // APK uses r{iteration} format for revision number
        $apkIteration = $iteration;

        // Use nfpm instead of fpm for APK packages
        self::createApkWithNfpm($package, $name, $apkVersion, $architecture, $apkIteration, $config, $isDebuginfo);
    }
    private static function createApkWithNfpm(package $package, string $name, string $phpVersion, string $architecture, string $iteration, array $config, bool $isDebuginfo): void
    {
        $fullVersion = "{$phpVersion}-r{$iteration}";

        // Create nfpm YAML config
        $nfpmConfig = [
            'name' => $name,
            'arch' => $architecture,
            'platform' => 'linux',
            'version' => $phpVersion,
            'release' => $iteration,
            'section' => 'default',
            'priority' => 'optional',
            'maintainer' => 'Marc Henderkes <pkg@henderkes.com>',
            'description' => $package->getDescription(),
            'vendor' => 'Marc Henderkes',
            'homepage' => 'https://apks.henderkes.com',
            'license' => $package->getLicense(),
            'apk' => [
                'signature' => [
                    'key_name' => self::getPrefix(),
                ],
            ],
        ];

        // Add scripts from getFpmExtraArgs (only for non-debuginfo packages)
        if (!$isDebuginfo) {
            $extraArgs = $package->getFpmExtraArgs();
            $scripts = [];

            // Parse fpm extra args to extract script paths
            for ($i = 0; $i < count($extraArgs); $i++) {
                if ($extraArgs[$i] === '--after-install' && isset($extraArgs[$i + 1])) {
                    $scripts['postinstall'] = $extraArgs[$i + 1];
                    $i++;
                } elseif ($extraArgs[$i] === '--after-remove' && isset($extraArgs[$i + 1])) {
                    $scripts['postremove'] = $extraArgs[$i + 1];
                    $i++;
                } elseif ($extraArgs[$i] === '--before-install' && isset($extraArgs[$i + 1])) {
                    $scripts['preinstall'] = $extraArgs[$i + 1];
                    $i++;
                } elseif ($extraArgs[$i] === '--before-remove' && isset($extraArgs[$i + 1])) {
                    $scripts['preremove'] = $extraArgs[$i + 1];
                    $i++;
                }
            }

            if (!empty($scripts)) {
                $nfpmConfig['scripts'] = $scripts;
            }
        }

        // Build dependencies
        $depends = [];

        // Ensure non-CLI packages depend on the same PHP major.minor
        if ($name !== self::getPrefix() . '-cli') {
            [$fullPhpVersion] = self::getPhpVersionAndArchitecture();
            if (preg_match('/^(\d+)\.(\d+)/', $fullPhpVersion, $m)) {
                $maj = (int)$m[1];
                $min = (int)$m[2];
                $nextMin = $min + 1;
                $lowerBound = sprintf('%d.%d', $maj, $min);
                $upperBound = sprintf('%d.%d', $maj, $nextMin);
                $depends[] = self::getPrefix() . "-cli>={$lowerBound}";
                $depends[] = self::getPrefix() . "-cli<{$upperBound}";
            }
        }

        // Debuginfo packages depend on their base package
        if (str_ends_with($name, '-debuginfo')) {
            $base = preg_replace('/-debuginfo$/', '', $name);
            $depends[] = sprintf('%s=%s', $base, $fullVersion);
        }

        // Alpine library dependencies
        $alpineLibMap = [
            'ld-linux-x86-64' => 'musl',
            'ld-linux-aarch64' => 'musl',
            'libc' => 'musl',
            'libm' => 'musl',
            'libpthread' => 'musl',
            'libutil' => 'musl',
            'libdl' => 'musl',
            'librt' => 'musl',
            'libresolv' => 'musl',
            'libgcc_s' => 'libgcc',
        ];

        $consolidatedDeps = [];
        foreach (self::$binaryDependencies as $lib => $version) {
            $packageName = preg_replace('/\.so(\.\d+)*$/', '', $lib);
            if (isset($alpineLibMap[$packageName])) {
                $packageName = $alpineLibMap[$packageName];
            }
            $numericVersion = preg_replace('/[^0-9.]/', '', $version);
            if (!isset($consolidatedDeps[$packageName]) || version_compare($numericVersion, $consolidatedDeps[$packageName], '>')) {
                $consolidatedDeps[$packageName] = $numericVersion;
            }
        }

        foreach ($consolidatedDeps as $packageName => $numericVersion) {
            $depends[] = "{$packageName}>={$numericVersion}";
        }

        if (isset($config['depends']) && is_array($config['depends'])) {
            $depends = array_merge($depends, $config['depends']);
        }

        if (!empty($depends)) {
            $nfpmConfig['depends'] = $depends;
        }

        // Add provides, replaces, conflicts
        if (isset($config['provides']) && is_array($config['provides'])) {
            // For APK cli packages: filter out the base prefix from provides since we have a separate meta package
            // This prevents conflicts between php-zts-cli (which provides php-zts) and the php-zts meta package
            $provides = $config['provides'];
            if ($name === self::getPrefix() . '-cli') {
                $provides = array_values(array_filter($provides, fn($p) => $p !== self::getPrefix()));
                $provides = array_values($provides);
            }
            $nfpmConfig['provides'] = $provides;
        }
        if (isset($config['replaces']) && is_array($config['replaces'])) {
            $nfpmConfig['replaces'] = $config['replaces'];
        }
        if (isset($config['conflicts']) && is_array($config['conflicts'])) {
            $nfpmConfig['conflicts'] = $config['conflicts'];
        }

        // Build contents (files)
        $contents = [];
        if (isset($config['files']) && is_array($config['files'])) {
            foreach ($config['files'] as $source => $dest) {
                if (file_exists($source)) {
                    // Fix debug link for renamed binaries
                    if (str_starts_with($source, BUILD_BIN_PATH . '/') &&
                        is_executable($source) &&
                        basename($source) !== basename($dest)) {
                        $source = self::fixBinaryDebugLink($source, $dest);
                    }
                    $contentItem = ['src' => $source, 'dst' => $dest];
                    // Mark config files
                    if (isset($config['config-files']) && in_array($dest, $config['config-files'])) {
                        $contentItem['type'] = 'config';
                    }
                    $contents[] = $contentItem;
                } else {
                    self::requireSourceFile($source, $dest);
                }
            }
        }

        // Handle empty directories
        if (isset($config['empty_directories']) && is_array($config['empty_directories'])) {
            foreach ($config['empty_directories'] as $dir) {
                $contentItem = ['dst' => $dir, 'type' => 'dir'];

                // Add file_info if specified for this directory
                if (isset($config['apk_file_info'][$dir])) {
                    $fileInfo = $config['apk_file_info'][$dir];
                    $contentItem['file_info'] = [
                        'mode' => octdec($fileInfo['mode']),
                        'owner' => $fileInfo['owner'],
                        'group' => $fileInfo['group'],
                    ];
                }

                $contents[] = $contentItem;
            }
        }

        if (!empty($contents)) {
            $nfpmConfig['contents'] = $contents;
        }

        // Write nfpm config to YAML file
        $nfpmConfigFile = TEMP_DIR . "/nfpm-{$name}.yaml";
        if (!yaml_emit_file($nfpmConfigFile, $nfpmConfig, YAML_UTF8_ENCODING)) {
            throw new RuntimeException("Failed to write YAML file: {$nfpmConfigFile}");
        }

        echo "nfpm config written to: {$nfpmConfigFile}\n";

        // Run nfpm to create the package with full filename including PHP version suffix
        $outputFile = DIST_APK_PATH . "/{$name}-{$phpVersion}-r{$iteration}.{$architecture}.apk";
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

        // Clean up config file
        @unlink($nfpmConfigFile);
    }

    /**
     * A file missing from BUILD_MODULES_PATH is the .so the package exists for: shipping
     * the package anyway leaves an "extension=" drop-in pointing at nothing, which is how
     * a build failure used to turn into a published, broken package. Everything else
     * (optional asset trees, e.g. spx's web-ui) stays a warning.
     */
    private static function requireSourceFile(string $source, string $dest): void
    {
        if (str_starts_with($source, BUILD_MODULES_PATH . '/')) {
            throw new RuntimeException("Source file not found: {$source} (required for {$dest})");
        }
        echo "Warning: Source file not found: {$source}\n";
    }

    private static function getPhpVersionAndArchitecture(): array
    {
        if (!empty(self::$versionArch)) {
            return self::$versionArch;
        }
        $basePhpVersion = SPP_PHP_VERSION;
        $phpBinary = BUILD_BIN_PATH . '/php';

        if (!file_exists($phpBinary)) {
            throw new RuntimeException("Warning: PHP binary not found at {$phpBinary}, using base PHP version: {$basePhpVersion}");
        }
        $versionProcess = new Process([$phpBinary, '-r', 'echo PHP_VERSION;']);
        $versionProcess->run();
        $detectedVersion = trim($versionProcess->getOutput());

        if (!empty($detectedVersion)) {
            $fullPhpVersion = self::normalizeVersion($detectedVersion);
            if ($fullPhpVersion !== $detectedVersion) {
                echo "Normalized pre-release PHP version {$detectedVersion} -> {$fullPhpVersion}\n";
            }
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

        self::$versionArch = [$fullPhpVersion, $architecture];
        return [$fullPhpVersion, $architecture];
    }

    /**
     * Check if a binary is linked against glibc
     */
    public static function isGlibcBinary(string $binaryPath): bool
    {
        $fileProcess = new Process(['file', $binaryPath]);
        $fileProcess->run();
        $fileOutput = $fileProcess->getOutput();

        // If it's not musl and not statically linked, it's glibc
        $isMusl = str_contains($fileOutput, 'musl');
        $isStatic = str_contains($fileOutput, 'statically linked');

        return !$isMusl && !$isStatic;
    }

    private static function getBinaryDependencies(string $binaryPath): array
    {
        // Detect if this is a musl binary
        $fileProcess = new Process(['file', $binaryPath]);
        $fileProcess->run();
        $fileOutput = $fileProcess->getOutput();
        $isMusl = str_contains($fileOutput, 'musl') || str_contains($fileOutput, 'statically linked');

        // For musl binaries, we need to use the musl dynamic linker instead of ldd
        if ($isMusl) {
            $output = self::getMuslBinaryDependencies($binaryPath);
        } else {
            $process = new Process(['ldd', '-v', $binaryPath]);
            $process->run();

            if (!$process->isSuccessful()) {
                throw new RuntimeException("ldd failed: " . $process->getErrorOutput());
            }

            $output = $process->getOutput();
        }

        $output = preg_replace('/.*?' . preg_quote($binaryPath, '/') . ':\s*\n/s', '', $output, 1);

        $output = preg_replace('/\n\s*\/.*?:.*/s', '', $output, 1);

        $lines = explode("\n", $output);
        $dependencies = [];

        foreach ($lines as $line) {
            $trimmed = trim($line);

            if ($trimmed === '') {
                continue;
            }

            if (preg_match('#^([\w.\-+]+)\s+\(([^)]+)\)\s+=>\s+(/\S+)$#', $trimmed, $m)) {
                $lib = $m[1];
                $version = $m[2];

                if (!preg_match('/\d+(\.\d+)+/', $version)) {
                    continue;
                }

                if (!isset($dependencies[$lib]) || version_compare($version, $dependencies[$lib], '>')) {
                    $dependencies[$lib] = $version;
                }
            }
        }

        return $dependencies;
    }

    /**
     * Get dependencies for musl-linked binaries using the musl dynamic linker
     */
    private static function getMuslBinaryDependencies(string $binaryPath): string
    {
        // Detect architecture from the binary
        $archProcess = new Process(['uname', '-m']);
        $archProcess->run();
        $arch = trim($archProcess->getOutput());

        // Map architecture to musl loader name
        $archMap = [
            'x86_64' => 'x86_64',
            'aarch64' => 'aarch64',
            'arm64' => 'aarch64',
            'armv7l' => 'armv7',
            'armhf' => 'armhf',
        ];

        $muslArch = $archMap[$arch] ?? 'x86_64';

        // Try to find the musl dynamic linker in common locations
        $basePaths = ['/lib', '/usr/lib', '/usr/lib64'];
        $muslLoaders = [];

        foreach ($basePaths as $basePath) {
            $muslLoaders[] = "{$basePath}/ld-musl-{$muslArch}.so.1";
            // Also try without .1 suffix (some systems)
            $muslLoaders[] = "{$basePath}/ld-musl-{$muslArch}.so";
        }

        $muslLoader = null;
        foreach ($muslLoaders as $loader) {
            if (file_exists($loader)) {
                $muslLoader = $loader;
                break;
            }
        }

        if ($muslLoader === null) {
            throw new RuntimeException("Could not find musl dynamic linker for architecture {$arch} (tried: " . implode(', ', $muslLoaders) . ")");
        }

        echo "Using musl dynamic linker: {$muslLoader}\n";

        // Use the musl loader to list dependencies
        $process = new Process([$muslLoader, '--list', $binaryPath]);
        $process->run();

        if (!$process->isSuccessful()) {
            // If the binary is statically linked, --list might fail
            // Check if it's actually static
            $readelfProcess = new Process(['readelf', '-d', $binaryPath]);
            $readelfProcess->run();
            if (!str_contains($readelfProcess->getOutput(), 'NEEDED')) {
                echo "Binary {$binaryPath} appears to be statically linked (no dynamic dependencies)\n";
                return '';
            }
            throw new RuntimeException("Musl ldd failed: " . $process->getErrorOutput());
        }

        return $process->getOutput();
    }

    /**
     * Fix GNU debuglink in a binary to match its new filename
     * This is needed when binaries are renamed during packaging (e.g., php -> php-zts8.3)
     */
    private static function fixBinaryDebugLink(string $sourceBinary, string $targetBinaryName): string
    {
        // Extract just the filename from the target path
        $targetFilename = basename($targetBinaryName);
        $newDebugFileName = $targetFilename . '.debug';

        // Create a temporary copy of the binary to modify
        $tempBinary = TEMP_DIR . '/' . $targetFilename;

        // Copy the source binary to temp location
        if (!copy($sourceBinary, $tempBinary)) {
            echo "Warning: Failed to copy {$sourceBinary} to {$tempBinary}, debug link won't be fixed\n";
            return $sourceBinary;
        }

        // Ensure the temporary binary is executable
        chmod($tempBinary, 0755);

        // Find the original debug file
        // Map binary names to their debug files using the prefix
        $binaryName = basename($sourceBinary);
        $binarySuffix = getBinarySuffix();
        $debugMap = [
            'php' => BUILD_ROOT_PATH . '/debug/php.debug',
            'php-cgi' => BUILD_ROOT_PATH . '/debug/php-cgi.debug',
            'php-fpm' => BUILD_ROOT_PATH . '/debug/php-fpm.debug',
            'frankenphp' => BUILD_ROOT_PATH . '/debug/frankenphp.debug',
        ];

        $originalDebugFile = $debugMap[$binaryName] ?? null;

        // If no debug file exists, we can't fix the debug link
        if ($originalDebugFile === null || !file_exists($originalDebugFile)) {
            echo "No debug file found for {$binaryName}, skipping debug link fix\n";
            return $tempBinary;
        }

        // Create a temporary copy of the debug file with the new name
        // objcopy needs the actual file to exist to compute the checksum
        $tempDebugFile = TEMP_DIR . '/' . $newDebugFileName;
        if (!copy($originalDebugFile, $tempDebugFile)) {
            echo "Warning: Failed to copy debug file, debug link won't be fixed\n";
            return $tempBinary;
        }

        // Remove existing debug link
        $removeProcess = new Process(['objcopy', '--remove-section=.gnu_debuglink', $tempBinary]);
        $removeProcess->run();
        if (!$removeProcess->isSuccessful()) {
            echo "Warning: Failed to remove debug link from {$tempBinary}: " . $removeProcess->getErrorOutput() . "\n";
            @unlink($tempDebugFile);
            return $sourceBinary;
        }

        // Add new debug link pointing to the renamed debug file
        $addProcess = new Process(['objcopy', '--add-gnu-debuglink=' . $tempDebugFile, $tempBinary]);
        $addProcess->run();
        if (!$addProcess->isSuccessful()) {
            echo "Warning: Failed to add debug link to {$tempBinary}: " . $addProcess->getErrorOutput() . "\n";
            @unlink($tempDebugFile);
            return $sourceBinary;
        }

        echo "Fixed debug link in {$targetFilename}: {$newDebugFileName}\n";

        // Clean up the temporary debug file (we don't need it anymore, just needed it for objcopy)
        @unlink($tempDebugFile);

        return $tempBinary;
    }

    private static function getNextIteration(string $name, string $phpVersion, string $architecture, string $packageType): int
    {
        $maxIteration = ($packageType === 'apk') ? -1 : 0;

        if ($packageType === 'rpm') {
            // RPM: {name}-{version}-{iteration}.{distVersion}.{arch}.rpm
            // Also match old formats:
            // - {name}-{version}-{iteration}.{phpSuffix}.{distVersion}.{arch}.rpm (with phpSuffix)
            // - {name}-{version}-{iteration}.{arch}.rpm (no distVersion)
            $rpmPattern = DIST_RPM_PATH . "/{$name}-{$phpVersion}-*.rpm";
            $rpmFiles = glob($rpmPattern);

            foreach ($rpmFiles as $file) {
                // Match all formats: iteration followed by 0-2 parts, then arch.rpm
                if (preg_match("/{$name}-" . preg_quote($phpVersion, '/') . "-(\d+)(?:\.[^.]+){0,2}\.{$architecture}\.rpm$/", $file, $matches)) {
                    $iteration = (int)$matches[1];
                    $maxIteration = max($maxIteration, $iteration);
                }
            }
        }

        if ($packageType === 'deb') {
            // DEB: {name}_{version}-{iteration}_{arch}.deb
            $debPattern = DIST_DEB_PATH . "/{$name}_{$phpVersion}-*.deb";
            $debFiles = glob($debPattern);

            foreach ($debFiles as $file) {
                // Match: {name}_{version}-{iteration}_{arch}.deb
                if (preg_match("/" . preg_quote($name, '/') . "_" . preg_quote($phpVersion, '/') . "-(\d+)_{$architecture}\.deb$/", $file, $matches)) {
                    $iteration = (int)$matches[1];
                    $maxIteration = max($maxIteration, $iteration);
                }
            }
        }

        if ($packageType === 'apk') {
            // APK: {name}-{version}-r{iteration}.{phpSuffix}.{arch}.apk
            // Also match old format: {name}-{version}-r{iteration}.{arch}.apk (no phpSuffix)
            $apkPattern = DIST_APK_PATH . "/{$name}-{$phpVersion}-r*.apk";
            $apkFiles = glob($apkPattern);

            foreach ($apkFiles as $file) {
                // Match both formats: r{iteration} followed by 0-1 parts, then arch.apk
                if (preg_match("/{$name}-" . preg_quote($phpVersion, '/') . "-r(\d+)(?:\.[^.]+)?\.{$architecture}\.apk$/", $file, $matches)) {
                    $iteration = (int)$matches[1];
                    $maxIteration = max($maxIteration, $iteration);
                }
            }
        }

        return $maxIteration + 1;
    }

    /**
     * Resolve the iteration to use for a package, honouring the precedence:
     *   1. explicit --iteration override (same value for every package)
     *   2. --bump: (max iteration currently published on the remote for this exact
     *      name+version+arch) + 1, computed per package
     *   3. default: next iteration derived from locally-present dist files
     */
    public static function resolveIteration(string $name, string $version, string $architecture, string $packageType): string
    {
        if (self::$iterationOverride !== null) {
            return self::$iterationOverride;
        }
        // A -debuginfo package pins its base with a strict (= version-iteration) dependency,
        // so the pair must resolve to ONE iteration. Resolve per base package and memoise:
        // the debuginfo call hits the cache instead of re-querying (remote registries can
        // have drifted base/debuginfo iterations from past partial publishes, and a local
        // dist scan after the base file exists would be off by one).
        $base = preg_replace('/-debuginfo$/', '', $name);
        $key = "{$base}|{$version}|{$architecture}|{$packageType}";
        if (!isset(self::$resolvedIterations[$key])) {
            if (self::$bump) {
                $next = self::getRemoteNextIteration($base, $version, $architecture, $packageType);
                if (self::$debuginfo) {
                    // Heal existing drift: jump past the highest published iteration of either
                    // half, so neither upload collides with an orphaned counterpart.
                    $next = max($next, self::getRemoteNextIteration($base . '-debuginfo', $version, $architecture, $packageType));
                }
            } else {
                $next = self::getNextIteration($base, $version, $architecture, $packageType);
            }
            self::$resolvedIterations[$key] = (string)$next;
        }
        return self::$resolvedIterations[$key];
    }

    /**
     * Query the hosted repositories for the highest iteration currently published for
     * {name}-{version} (for RPM: on the given arch/dist) and return it + 1.
     *
     * Sources:
     *   - rpm  -> autoindex at {SPP_RPM_REPO_URL}/{arch}/el{N}/  (createrepo dir listing)
     *   - deb  -> Forgejo   {SPP_FORGEJO_HOST}/api/v1/packages/{owner}?type=debian
     *   - apk  -> Forgejo   {SPP_FORGEJO_HOST}/api/v1/packages/{owner}?type=alpine
     *
     * A version that is not published yet yields the first release (1 for rpm/deb, 0 for
     * apk). A transport failure throws, so a --bump build fails loudly instead of silently
     * emitting a colliding low iteration.
     */
    public static function getRemoteNextIteration(string $name, string $version, string $architecture, string $packageType): int
    {
        $maxIteration = ($packageType === 'apk') ? -1 : 0;

        if ($packageType === 'rpm') {
            $dist = self::getDistVersion();
            if ($dist === '') {
                throw new RuntimeException("--bump: unable to determine RPM dist version (el8/el9/...) for {$name}");
            }
            $baseUrl = getenv('SPP_RPM_REPO_URL') ?: 'https://rpm.henderkes.com';
            $url = rtrim($baseUrl, '/') . "/{$architecture}/{$dist}/";
            [$code, $body] = self::httpGet($url);
            if ($code === 404) {
                return $maxIteration + 1;
            }
            if ($code !== 200 || $body === null) {
                throw new RuntimeException("--bump: failed to fetch RPM index {$url} (HTTP {$code})");
            }
            // {name}-{version}-{iteration}[.{phpSuffix}][.{dist}].{arch}.rpm
            $pattern = '#' . preg_quote($name, '#') . '-' . preg_quote($version, '#')
                . '-(\d+)(?:\.[^."/]+){0,2}\.' . preg_quote($architecture, '#') . '\.rpm#';
            if (preg_match_all($pattern, $body, $matches)) {
                foreach ($matches[1] as $it) {
                    $maxIteration = max($maxIteration, (int)$it);
                }
            }
            return $maxIteration + 1;
        }

        // deb / apk live in the Forgejo package registry
        $forgeType = $packageType === 'deb' ? 'debian' : 'alpine';
        $host = getenv('SPP_FORGEJO_HOST') ?: 'https://git.henderkes.com';
        $owner = self::getForgejoOwner();
        $url = rtrim($host, '/') . "/api/v1/packages/{$owner}?type={$forgeType}&limit=1000";
        [$code, $body] = self::httpGet($url);
        if ($code !== 200 || $body === null) {
            throw new RuntimeException("--bump: failed to query Forgejo {$url} (HTTP {$code})");
        }
        $packages = json_decode($body, true);
        if (!is_array($packages)) {
            throw new RuntimeException("--bump: invalid Forgejo response for {$url}");
        }

        // Debian package names cannot contain underscores, so the registry stores them
        // dash-normalised (php-zts-pdo_mysql -> php-zts-pdo-mysql), matching the convention
        // used by bin/forgejo-helper. Alpine keeps underscores as-is.
        $matchName = $packageType === 'deb' ? str_replace('_', '-', $name) : $name;

        foreach ($packages as $pkg) {
            if (!is_array($pkg) || ($pkg['name'] ?? null) !== $matchName) {
                continue;
            }
            $remoteVersion = (string)($pkg['version'] ?? '');
            if ($packageType === 'deb') {
                // registry version: {version}-{revision}
                if (str_starts_with($remoteVersion, $version . '-')) {
                    $revision = substr($remoteVersion, strlen($version) + 1);
                    if ($revision !== '' && ctype_digit($revision)) {
                        $maxIteration = max($maxIteration, (int)$revision);
                    }
                }
            }
            else {
                // apk registry version: {version}-r{iteration}
                if (preg_match('/^' . preg_quote($version, '/') . '-r(\d+)$/', $remoteVersion, $m)) {
                    $maxIteration = max($maxIteration, (int)$m[1]);
                }
            }
        }

        return $maxIteration + 1;
    }

    /**
     * Forgejo owner is the PHP major.minor with the dot stripped (e.g. 8.4 -> "84").
     * Overridable via SPP_FORGEJO_OWNER.
     */
    private static function getForgejoOwner(): string
    {
        $override = getenv('SPP_FORGEJO_OWNER');
        if ($override !== false && $override !== '') {
            return $override;
        }
        [$fullPhpVersion] = self::getPhpVersionAndArchitecture();
        if (preg_match('/^(\d+)\.(\d+)/', $fullPhpVersion, $m)) {
            return $m[1] . $m[2];
        }
        return str_replace('.', '', $fullPhpVersion);
    }

    /**
     * Minimal HTTP GET via curl. Returns [httpCode, body]; [0, null] on transport failure.
     */
    private static function httpGet(string $url): array
    {
        // The RPM index and the Forgejo listing are the same URL for every package in a
        // job, so memoise to avoid refetching a multi-MB directory index per package.
        if (isset(self::$httpCache[$url])) {
            return self::$httpCache[$url];
        }

        // Request JSON: the RPM repo is served by Caddy's file_server, whose JSON directory
        // listing is far smaller/faster than the HTML autoindex (e.g. 0.8MB/2s vs 4MB/120s);
        // the Forgejo API returns JSON regardless. The .rpm filenames appear verbatim in
        // both payloads, so the same regex extracts iterations either way.
        $process = new Process(['curl', '-sSL', '--max-time', '90', '-H', 'Accept: application/json', '-w', "\n%{http_code}", $url]);
        $process->run();
        if (!$process->isSuccessful()) {
            return [0, null]; // transport failure: do not cache, allow a retry
        }
        $output = $process->getOutput();
        $nl = strrpos($output, "\n");
        if ($nl === false) {
            return [0, null];
        }
        $result = [(int)substr($output, $nl + 1), substr($output, 0, $nl)];
        self::$httpCache[$url] = $result;
        return $result;
    }

    public static function getPrefix(): string
    {
        // Return the prefix set by the user, prepended with "php"
        // For example: "-zts" becomes "php-zts", "-zts8.5" becomes "php-zts8.5"
        return 'php' . self::$prefix;
    }

    /**
     * Get PHP version suffix for package filenames (e.g., "static-83" for PHP 8.3)
     */
    private static function getPhpVersionSuffix(): string
    {
        [$phpVersion,] = self::getPhpVersionAndArchitecture();

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
    private static function getDistVersion(): string
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

    /**
     * Get list of versioned package names to conflict/replace with
     * For example, for php-zts8.5-cli, returns [php-zts8.0-cli, php-zts8.1-cli, ..., php-zts8.9-cli] excluding 8.5
     * For RPM packages (using unversioned prefix like -zts), returns empty array (RPM uses module system instead)
     */
    public static function getVersionedConflicts(string $suffix): array
    {
        // Versioned packages can coexist - no conflicts
        return [];
    }
}
