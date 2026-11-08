<?php

namespace staticphp\step;

use RuntimeException;
use staticphp\extension;
use staticphp\package;
use staticphp\util\ExtMeta;
use staticphp\util\SkippedExtensions;
use staticphp\util\SharedLibraries;
use staticphp\util\BuildPayload;
use staticphp\package\library;
use staticphp\package\pie;
use staticphp\package\runtime_layout;
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
    private static array $migrationBaselines = [];
    private static string $prefix = '-zts';
    private static bool $debuginfo = false;
    private static ?SharedLibraries $sharedLibraries = null;
    private static array $consumerManifest = [];

    public static function run($packageNames = null, ?string $iteration = null, ?bool $debuginfo = null, ?bool $bump = null, bool $libsOnly = false, ?array $libraries = null): true
    {
        self::$prefix = defined('SPP_PREFIX') ? SPP_PREFIX : '-zts';
        self::$packageType = defined('SPP_TYPE') ? SPP_TYPE : 'rpm';
        self::$iterationOverride = $iteration !== null && $iteration !== '' ? $iteration : null;
        self::$bump = $bump ?? self::$bump;
        self::$debuginfo = $debuginfo ?? self::$debuginfo;
        self::$resolvedIterations = [];
        self::$versionArch = [];
        self::$consumerManifest = [];
        $manifestRoot = $libsOnly && is_file(BASE_PATH . '/buildroot/shared-libraries.json') ? BASE_PATH . '/buildroot' : BUILD_ROOT_PATH;
        $shared = CraftConfig::getInstance()->getRawConfig()['build-options']['with-shared-libs'] ?? false;
        self::$sharedLibraries = SharedLibraries::load($manifestRoot, $libsOnly || $shared, self::runtimeLibdir());
        if (($payload = BuildPayload::load($manifestRoot)) !== null && !$payload->isSealed()) {
            throw new RuntimeException('Seal the recorded payload before packaging it.');
        }
        if ($payload !== null && self::$packageType !== 'apk' && self::$sharedLibraries !== null
            && in_array('openssl', self::$sharedLibraries->libraryIds(), true)
            && ($payload->metadata()['openssl_variant']['profile'] ?? null) !== self::$packageType) {
            throw new RuntimeException('Select and seal the matching OpenSSL package profile before packaging.');
        }
        if (self::$sharedLibraries !== null) {
            self::createRuntimeLayoutPackage(self::$sharedLibraries->architecture());
        }
        if ($libsOnly) {
            if (($packageNames === ['runtime_layout'] || $packageNames === 'runtime_layout') && $libraries === null) {
                return true;
            }
            self::createLibraryPackages($libraries);
            echo "Library package creation completed.\n";
            return true;
        }
        if ($libraries !== null) {
            throw new \InvalidArgumentException('--libraries requires --libs-only.');
        }
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
        self::$binaryDependencies = self::$sharedLibraries === null ? self::getBinaryDependencies($phpBinary) : [];

        // Verify that we're not trying to package a glibc binary as APK
        //if (self::$packageType === 'apk' && file_exists($phpBinary) && self::isGlibcBinary($phpBinary)) {
        //    throw new RuntimeException(
        //        "Error: Cannot create APK packages with glibc binary. APK packages require musl libc.\n" .
        //        "The binary at {$phpBinary} is linked against glibc, but APK is the Alpine Linux package format which uses musl.\n" .
        //        "Please rebuild with musl libc or use a different package type (rpm/deb)."
        //    );
        //}

        if ($packageNames !== null) {
            if (is_string($packageNames)) {
                $packageNames = [$packageNames];
            }

            $includeCli = in_array('cli', $packageNames, true);
            $packageNames = array_values(array_filter($packageNames, static fn (string $name): bool => $name !== 'cli'));
            foreach ($packageNames as $packageName) {
                echo "Building package: {$packageName}\n";

                if (in_array($packageName, self::$sapis, true) || in_array($packageName, ['cli', 'cgi', 'fpm', 'embed', 'frankenphp'], true)) {
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
            if ($includeCli) {
                self::prepareMigrationCutoffs();
                self::createSapiPackage('cli');
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
            if (in_array('cli', self::$sapis, true)) {
                self::prepareMigrationCutoffs();
                self::createSapiPackage('cli');
            }
        }

        echo "Package creation completed.\n";
        return true;
    }

    public static function stageSymlink(string $target, string $name): string
    {
        if (str_starts_with($target, '/') || basename($name) !== $name) {
            throw new RuntimeException('Compatibility symlinks must have relative targets and simple names');
        }
        $directory = TEMP_DIR . '/aliases/' . hash('sha256', $target . "\0" . $name);
        if (!is_dir($directory) && !mkdir($directory, 0755, true) && !is_dir($directory)) {
            throw new RuntimeException('Cannot stage compatibility symlink');
        }
        $path = $directory . '/' . $name;
        if (is_link($path) && readlink($path) !== $target) {
            throw new RuntimeException('Conflicting compatibility symlink');
        }
        if (!is_link($path) && !symlink($target, $path)) {
            throw new RuntimeException('Cannot stage compatibility symlink');
        }
        return $path;
    }

    public static function runtimeLibdir(): string
    {
        return function_exists('getRuntimeLibdir') ? getRuntimeLibdir() : (defined('SPP_TYPE') && SPP_TYPE === 'apk' ? getLibdir() . '/php-zts/lib' : '/usr/lib/php-zts/lib');
    }

    private static function isPhpIndependent(package $package): bool
    {
        return $package instanceof library || $package instanceof runtime_layout;
    }

    private static function createRuntimeLayoutPackage(string $architecture): void
    {
        $package = new runtime_layout();
        $baselines = json_decode(getenv('SPP_BASELINE_MANIFESTS') ?: '[]', true, 512, JSON_THROW_ON_ERROR);
        $unchanged = $baselines !== [];
        $reusable = [];
        foreach ($baselines as $path) {
            if (!is_string($path) || !is_file($path)) {
                throw new RuntimeException('Missing captured runtime layout baseline');
            }
            $baseline = json_decode((string) file_get_contents($path), true, 512, JSON_THROW_ON_ERROR);
            $layout = $baseline['runtime_layout'] ?? [];
            $matching = ($layout['version'] ?? null) === $package->getVersion()
                && ($layout['source_hash'] ?? null) === $package->sourceHash(self::$packageType);
            $unchanged = $unchanged && $matching && ($layout['complete'] ?? true);
            if ($matching && isset($layout['archive'], $layout['archive_sha256'], $layout['package_version'])) {
                $reusable[] = [$path, $layout];
            }
        }
        if ($unchanged) {
            return;
        }
        if ($reusable !== []) {
            usort($reusable, static function (array $left, array $right): int {
                $release = static function (array $record): int {
                    if (!preg_match('/^(?:0:)?[^:-]+-(?:r)?(\d+)$/D', $record['package_version'], $match)) {
                        throw new RuntimeException('Unexpected runtime layout package release');
                    }
                    return (int) $match[1];
                };
                return $release($right[1]) <=> $release($left[1]);
            });
            [$path, $layout] = $reusable[0];
            self::reuseLayoutArchive($path, $layout, $architecture);
            return;
        }
        $pattern = match (self::$packageType) {
            'rpm' => DIST_RPM_PATH . '/php-zts-runtime-layout-' . $package->getVersion() . '-*.noarch.rpm',
            'deb' => DIST_DEB_PATH . '/php-zts-runtime-layout_' . $package->getVersion() . '-*_all.deb',
            'apk' => DIST_APK_PATH . '/php-zts-runtime-layout-' . $package->getVersion() . '-r*.' . $architecture . '.apk',
        };
        if (glob($pattern) !== []) {
            return;
        }
        self::createPackageWithFpm($package, $package->getVersion(), self::$packageType === 'rpm' ? 'noarch' : (self::$packageType === 'deb' ? 'all' : $architecture));
    }

    private static function reuseLayoutArchive(string $baselinePath, array $layout, string $architecture): void
    {
        $directory = realpath(dirname($baselinePath));
        $archive = realpath(dirname($baselinePath) . '/' . $layout['archive']);
        if ($directory === false || $archive === false || !str_starts_with($archive, $directory . '/')
            || !is_file($archive) || hash_file('sha256', $archive) !== $layout['archive_sha256']) {
            throw new RuntimeException('Invalid retained runtime layout archive');
        }
        if (self::$packageType === 'rpm') {
            $query = new Process(['rpm', '-qp', '--qf', '%{NAME}|%{VERSION}|%{RELEASE}|%{ARCH}', $archive]);
            $query->mustRun();
            [$name, $version, $release, $arch] = explode('|', trim($query->getOutput()));
            $fullVersion = $version . '-' . $release;
            $filename = $name . '-' . $fullVersion . '.' . $arch . '.rpm';
            $validArch = $arch === 'noarch';
        } else {
            $kind = self::$packageType === 'deb' ? 'debian' : 'alpine';
            $code = 'import json,runpy,sys; h=runpy.run_path(sys.argv[1]); print(json.dumps(h["package_metadata"](sys.argv[2],sys.argv[3])))';
            $query = new Process(['python3', '-c', $code, BASE_PATH . '/bin/forgejo-helper', $archive, $kind]);
            $query->mustRun();
            [$name, $fullVersion, $arch] = json_decode(trim($query->getOutput()), true, 512, JSON_THROW_ON_ERROR);
            $filename = self::$packageType === 'deb' ? $name . '_' . $fullVersion . '_' . $arch . '.deb' : $name . '-' . $fullVersion . '.' . $arch . '.apk';
            $validArch = self::$packageType === 'deb' ? $arch === 'all' : $arch === $architecture;
        }
        if ($name !== 'php-zts-runtime-layout' || $fullVersion !== preg_replace('/^0:/', '', $layout['package_version']) || !$validArch) {
            throw new RuntimeException('Retained runtime layout archive has unexpected native headers');
        }
        $destination = DIST_PATH . '/' . self::$packageType . '/' . $filename;
        if (is_file($destination) && hash_file('sha256', $destination) !== $layout['archive_sha256']) {
            throw new RuntimeException('Conflicting retained runtime layout archive bytes');
        }
        if (!is_file($destination) && !copy($archive, $destination)) {
            throw new RuntimeException('Cannot reuse retained runtime layout archive');
        }
    }

    private static function migrationNextIteration(string $name, string $version, string $architecture, string $type): int
    {
        if ($name === 'php-zts-runtime-layout' || str_starts_with($name, 'php-zts-lib-')) {
            return $type === 'apk' ? 0 : 1;
        }
        $paths = json_decode(getenv('SPP_BASELINE_MANIFESTS') ?: '[]', true, 512, JSON_THROW_ON_ERROR);
        $minimum = $type === 'apk' ? 0 : 1;
        $matchName = $type === 'deb' ? str_replace('_', '-', $name) : $name;
        $normalizeArch = static fn (string $arch): string => match ($arch) {'amd64' => 'x86_64', 'arm64' => 'aarch64', default => $arch};
        [$phpVersion] = self::getPhpVersionAndArchitecture();
        preg_match('/^(\d+\.\d+)/', $phpVersion, $targetStream);
        $advanceAbi = false;
        if (($planPath = getenv('SPP_SHARED_LIBRARY_PLAN')) !== false && $planPath !== '') {
            $plan = self::migrationBaseline($planPath);
            if (($plan['schema'] ?? null) !== 1 || ($plan['type'] ?? null) !== $type) {
                throw new RuntimeException('Invalid shared-library package plan');
            }
            $advanceAbi = ($plan['abi_break'] ?? false) === true && isset($plan['published_consumers'][$targetStream[1]][$matchName]);
        }
        foreach ($paths as $path) {
            $data = self::migrationBaseline($path);
            if (isset($data['type']) && $data['type'] !== $type) {
                continue;
            }
            if (array_key_exists($targetStream[1], $data['migration_cutoffs'] ?? []) && !$advanceAbi) {
                continue;
            }
            $packages = array_merge($data['packages'] ?? [], $data['published_packages'] ?? []);
            foreach ($data['consumer_versions'] ?? [] as $stream => $consumers) {
                if (array_key_exists($stream, $data['migration_cutoffs'] ?? []) && !$advanceAbi) {
                    continue;
                }
                if (isset($consumers[$matchName])) {
                    $packages[] = ['name' => $matchName, 'version' => $consumers[$matchName]];
                }
            }
            foreach ($packages as $package) {
                if (($package['name'] ?? '') !== $matchName
                    || (isset($package['architecture']) && $normalizeArch($package['architecture']) !== $normalizeArch($architecture))) {
                    continue;
                }
                $published = preg_replace('/^\d+:/', '', $package['version']);
                $pattern = '/^' . preg_quote($version, '/') . ($type === 'apk' ? '-r' : '-') . '(\d+)(?:[.][A-Za-z0-9.+_~]+)?$/D';
                if (preg_match($pattern, $published, $release)) {
                    $minimum = max($minimum, (int) $release[1] + 1);
                }
            }
        }
        return $minimum;
    }

    public static function migrationPackageVersion(string $type, string $name, string $proposed, string $architecture): string
    {
        $paths = json_decode(getenv('SPP_BASELINE_MANIFESTS') ?: '[]', true, 512, JSON_THROW_ON_ERROR);
        if (!is_array($paths) || !array_is_list($paths)) {
            throw new RuntimeException('SPP_BASELINE_MANIFESTS must be a JSON array');
        }
        $matchName = $type === 'deb' ? str_replace('_', '-', $name) : $name;
        $baseName = preg_replace('/-debuginfo$/', '', $matchName);
        $normalizeArch = static fn (string $arch): string => match ($arch) {'amd64' => 'x86_64', 'arm64' => 'aarch64', default => $arch};
        $versions = [];
        foreach ($paths as $path) {
            if (!is_string($path) || !is_file($path)) {
                throw new RuntimeException('Missing captured package baseline');
            }
            $data = self::migrationBaseline($path);
            if (isset($data['type']) && $data['type'] !== $type) {
                continue;
            }
            foreach (array_merge($data['packages'] ?? [], $data['published_packages'] ?? []) as $package) {
                if (is_array($package) && in_array($package['name'] ?? '', [$matchName, $baseName], true)
                    && (!isset($package['architecture']) || $normalizeArch($package['architecture']) === $normalizeArch($architecture))) {
                    $versions[] = $package['version'];
                }
            }
            foreach ($data['consumer_versions'] ?? [] as $consumers) {
                foreach ([$matchName, $baseName] as $candidateName) {
                    if (isset($consumers[$candidateName])) {
                        $versions[] = $consumers[$candidateName];
                    }
                }
            }
        }
        $identity = self::migrationVersionIdentity($type, $proposed);
        if ($identity !== null) {
            preg_match('/^([0-9]+(?:\.[0-9]+){1,2})/', $identity[0], $numeric);
            $knownVersions = [];
            $differentPhase = false;
            foreach (array_unique($versions) as $version) {
                $base = preg_replace('/^\d+:/', '', $version);
                $base = preg_replace($type === 'apk' ? '/-r\d+$/' : '/-[^-]+$/', '', $base);
                $known = self::migrationVersionIdentity($type, $base);
                if ($known === null || explode('.', $known[1])[0] !== explode('.', $identity[1])[0]
                    || !preg_match('/^' . preg_quote($numeric[1], '/') . '(?:~|$)/D', $known[0])) {
                    continue;
                }
                $differentPhase = $differentPhase || $known[0] !== $identity[0];
                $knownVersions[$base] = $known[0];
            }
            if ($differentPhase) {
                $latest = null;
                foreach ($knownVersions as $base => $upstream) {
                    if ($latest === null || self::compareMigrationVersions($type, $base, $latest[0]) > 0) {
                        $latest = [$base, $upstream];
                    }
                }
                $currentUpstream = $type === 'apk' ? self::toApkVersion($identity[0]) : $identity[0];
                $publishedUpstream = $type === 'apk' ? self::toApkVersion($latest[1]) : $latest[1];
                if (self::compareMigrationVersions($type, $currentUpstream, $publishedUpstream) < 0) {
                    return $proposed;
                }
            }
        }
        $retained = null;
        foreach (array_unique($versions) as $published) {
            $published = preg_replace('/^\d+:/', '', $published);
            $published = preg_replace($type === 'apk' ? '/-r\d+$/' : '/-[^-]+$/', '', $published);
            if ($published === $proposed) {
                continue;
            }
            if ($published === $proposed . ($type === 'apk' ? '_p0' : '+ext')) {
                if ($retained === null || self::compareMigrationVersions($type, $published, $retained) > 0) {
                    $retained = $published;
                }
                continue;
            }
            if ($identity === null) {
                continue;
            }
            $oldIdentity = self::migrationVersionIdentity($type, $published);
            $floor = null;
            preg_match('/^([0-9]+(?:\.[0-9]+){1,2})/', $identity[0], $numeric);
            if ($oldIdentity !== null && explode('.', $oldIdentity[1])[0] === explode('.', $identity[1])[0]
                && preg_match('/^' . preg_quote($numeric[1], '/') . '(?:~|$)/D', $oldIdentity[0])) {
                $candidatePhase = substr($identity[0], strlen($numeric[1]));
                if ($oldIdentity[0] === $identity[0]) {
                    $floor = $published;
                } elseif ($type !== 'apk' && preg_match('/^([0-9]+(?:\.[0-9]+){1,2}(?:_|\+php)\d.*)\+ext(.*)$/D', $published, $parts)) {
                    $floor = $parts[1] . '+ext' . $candidatePhase;
                } elseif ($type === 'apk' && preg_match('/^([0-9]+(?:\.[0-9]+){1,2}p\d+\.\d+.*)_p0(.*)$/D', $published, $parts)) {
                    $floor = $parts[1] . '_p0' . self::toApkVersion($candidatePhase);
                }
            }
            if ($floor !== null) {
                $floor = self::replaceMigrationPhpPhase($type, $floor, preg_replace('/^[0-9.]+/', '', $identity[1]));
            }
            if ($floor !== null && ($retained === null || self::compareMigrationVersions($type, $floor, $retained) > 0)) {
                $retained = $floor;
            }
        }
        return $retained ?? $proposed;
    }

    private static function migrationPhase(string $phase, bool $apk = false): ?string
    {
        if ($apk) {
            $phase = preg_replace_callback('/_alpha0_alpha(\d*)/', static fn (array $match): string => '~~dev' . (int) ($match[1] ?: 0), $phase);
            $phase = str_replace('_pre', '~~dev0', $phase);
            $phase = preg_replace('/_(alpha|beta|rc)/', '~$1', $phase);
        }
        if (!preg_match('/^(?:~{1,2}(?:alpha|beta|rc|dev)\d*)*$/D', $phase)) {
            return null;
        }
        return preg_replace_callback('/~{1,2}(alpha|beta|rc|dev)(\d*)/', static fn (array $match): string =>
            ($match[1] === 'dev' ? '~~' : '~') . $match[1] . (int) ($match[2] ?: 0), $phase);
    }

    private static function migrationBaseline(string $path): array
    {
        $content = file_get_contents($path);
        if ($content === false) {
            throw new RuntimeException('Cannot read captured package baseline');
        }
        $hash = hash('sha256', $content);
        if (!isset(self::$migrationBaselines[$path]) || self::$migrationBaselines[$path][0] !== $hash) {
            $value = json_decode($content, true, 512, JSON_THROW_ON_ERROR);
            if (!is_array($value)) {
                throw new RuntimeException('Invalid captured package baseline');
            }
            self::$migrationBaselines[$path] = [$hash, $value];
        }
        return self::$migrationBaselines[$path][1];
    }

    private static function migrationVersionIdentity(string $type, string $version): ?array
    {
        $number = '([0-9]+(?:\.[0-9]+){1,2})';
        $phase = '((?:~{1,2}(?:alpha|beta|rc|dev)\d*)*)';
        if ($type !== 'apk') {
            $tag = $type === 'rpm' ? '_' : '\+php';
            if (!preg_match('/^' . $number . $phase . $tag . '(\d{2,})(?:\.(\d+))?' . $phase . '(?:\+ext' . $phase . ')?$/D', $version, $parts)) {
                return null;
            }
            $upstream = self::migrationPhase(($parts[2] ?? '') . ($parts[6] ?? ''));
            $php = self::migrationPhase($parts[5] ?? '');
            return $upstream === null || $php === null ? null : [$parts[1] . $upstream, $parts[3] . '.' . (int) ($parts[4] ?: 0) . $php];
        }
        $phase = '((?:_(?:alpha|beta|rc|pre)\d*)*)';
        if (preg_match('/^' . $number . $phase . '_p(\d{2,})(?:_p(\d+))?' . $phase . '$/D', $version, $parts)) {
            $upstream = self::migrationPhase($parts[2], true);
            $php = self::migrationPhase($parts[5], true);
            return $upstream === null || $php === null ? null : [$parts[1] . $upstream, $parts[3] . '.' . (int) ($parts[4] ?: 0) . $php];
        }
        if (preg_match('/^' . $number . 'p(\d{2,})(?:\.(\d+))?' . $phase . '(?:_p0' . $phase . ')?$/D', $version, $parts)) {
            $upstream = self::migrationPhase($parts[5] ?? '', true);
            $php = self::migrationPhase($parts[4], true);
            return $upstream === null || $php === null ? null : [$parts[1] . $upstream, $parts[2] . '.' . (int) ($parts[3] ?: 0) . $php];
        }
        return null;
    }

    public static function migrationUpstreamVersion(string $type, string $version): ?string
    {
        $version = preg_replace('/^\d+:/', '', $version);
        $version = preg_replace($type === 'apk' ? '/-r\d+$/' : '/-[^-]+$/', '', $version);
        $identity = self::migrationVersionIdentity($type, $version);
        if ($identity !== null) {
            return $identity[0];
        }
        if ($type === 'apk' && preg_match('/^([0-9]+(?:\.[0-9]+){1,2})~((?:alpha|beta|rc|dev)\d*)(?:_|p)(8[2-6])$/D', $version, $parts)) {
            return $parts[1] . self::migrationPhase('~' . $parts[2]);
        }
        return null;
    }

    private static function replaceMigrationPhpPhase(string $type, string $version, string $phpPhase): string
    {
        $number = '[0-9]+(?:\.[0-9]+){1,2}';
        if ($type !== 'apk') {
            $phase = '(?:~{1,2}(?:alpha|beta|rc|dev)\d*)*';
            $tag = $type === 'rpm' ? '_' : '\+php';
            $pattern = '/^(' . $number . $phase . $tag . '\d{2,}(?:\.\d+)?)' . $phase . '(\+ext' . $phase . ')?$/D';
        } else {
            $phase = '(?:_(?:alpha|beta|rc|pre)\d*)*';
            $phpPhase = self::toApkVersion($phpPhase);
            $pattern = '/^(' . $number . $phase . '_p\d{2,}(?:_p\d+)?)' . $phase . '$/D';
            if (preg_match($pattern, $version, $parts)) {
                return $parts[1] . $phpPhase;
            }
            $pattern = '/^(' . $number . 'p\d{2,}(?:\.\d+)?)' . $phase . '(_p0' . $phase . ')?$/D';
        }
        if (!preg_match($pattern, $version, $parts)) {
            throw new RuntimeException('Cannot update the PHP phase in a retained package version: ' . $version);
        }
        return $parts[1] . $phpPhase . ($parts[2] ?? '');
    }

    private static function compareMigrationVersions(string $type, string $left, string $right): int
    {
        if ($left === $right) {
            return 0;
        }
        if ($type === 'rpm') {
            $process = new Process(['rpmdev-vercmp', $left, $right]);
            $process->run();
            return match ($process->getExitCode()) {0 => 0, 11 => 1, 12 => -1, default => throw new RuntimeException('Cannot compare captured migration RPM versions')};
        }
        if ($type === 'apk') {
            $process = new Process(['apk', 'version', '-t', $left, $right]);
            $process->mustRun();
            return match (trim($process->getOutput())) {'=' => 0, '>' => 1, '<' => -1, default => throw new RuntimeException('Cannot compare captured migration APK versions')};
        }
        foreach (['gt' => 1, 'lt' => -1] as $operation => $order) {
            $process = new Process(['dpkg', '--compare-versions', $left, $operation, $right]);
            $process->run();
            if ($process->getExitCode() === 0) {
                return $order;
            }
            if ($process->getExitCode() !== 1) {
                throw new RuntimeException('Cannot compare captured migration DEB versions');
            }
        }
        return 0;
    }

    private static function prepareMigrationCutoffs(): void
    {
        if (getenv('SPP_MIGRATION_CUTOFFS') || !getenv('SPP_BASELINE_MANIFESTS')) {
            return;
        }
        $baselines = json_decode((string) getenv('SPP_BASELINE_MANIFESTS'), true, 512, JSON_THROW_ON_ERROR);
        if (!is_array($baselines) || !array_is_list($baselines)) {
            throw new RuntimeException('SPP_BASELINE_MANIFESTS must be a JSON array of captured baseline manifests');
        }
        if ($baselines === []) {
            return;
        }
        $output = TEMP_DIR . '/migration-cutoffs-' . self::$packageType . '.json';
        [$version] = self::getPhpVersionAndArchitecture();
        preg_match('/^(\d+\.\d+)/', $version, $stream);
        $arguments = ['python3', BASE_PATH . '/bin/shared-library-plan', 'migration-cutoffs', '--type', self::$packageType,
            '--artifacts', DIST_PATH . '/' . self::$packageType, '--php-version', $stream[1], '--output', $output];
        foreach ($baselines as $baseline) {
            if (!is_string($baseline) || !is_file($baseline)) {
                throw new RuntimeException('Missing captured migration baseline');
            }
            $arguments[] = '--baseline-manifest';
            $arguments[] = $baseline;
        }
        $process = new Process($arguments);
        $process->setTimeout(null);
        $process->mustRun();
        putenv('SPP_MIGRATION_CUTOFFS=' . $output);
    }

    private static function migrationConflicts(): array
    {
        $path = getenv('SPP_MIGRATION_CUTOFFS') ?: BUILD_ROOT_PATH . '/migration-cutoffs.json';
        if (!is_file($path)) {
            return [];
        }
        $data = json_decode((string) file_get_contents($path), true, 512, JSON_THROW_ON_ERROR);
        if (($data['schema'] ?? null) !== 1 || !is_array($data['formats'][self::$packageType] ?? null)) {
            throw new RuntimeException('Invalid migration cutoff manifest');
        }
        $conflicts = [];
        foreach ($data['formats'][self::$packageType] as $name => $version) {
            if (!is_string($name) || !preg_match('/^[a-z0-9][a-z0-9+_.-]*$/D', $name)
                || !is_string($version) || !preg_match('/^[0-9][A-Za-z0-9.+:~_^\-]*$/D', $version)) {
                throw new RuntimeException('Invalid package migration cutoff');
            }
            if ($name === self::getPrefix() . '-cli' || $name === 'php-zts-runtime-layout' || str_starts_with($name, 'php-zts-lib-')) {
                throw new RuntimeException('Migration cutoffs may only constrain PHP runtime consumers');
            }
            $conflicts[] = match (self::$packageType) {
                'deb' => $name . ' (<< ' . $version . ')',
                'apk' => $name . '<' . $version,
                default => $name . ' < ' . $version,
            };
        }
        return array_values(array_unique($conflicts));
    }

    public static function getSharedRuntimeCapability(): string
    {
        [$version] = self::getPhpVersionAndArchitecture();
        if (!preg_match('/^(\d+)\.(\d+)/', $version, $parts)) {
            throw new RuntimeException('Cannot determine PHP stream for shared runtime capability');
        }
        $payload = BuildPayload::load(BUILD_ROOT_PATH);
        $api = $payload?->metadata()['module_api'] ?? null;
        foreach ([BUILD_ROOT_PATH . '/include/php/Zend/zend_modules.h', BUILD_ROOT_PATH . '/include/Zend/zend_modules.h'] as $header) {
            if ($api === null && is_file($header) && preg_match('/^#define\s+ZEND_MODULE_API_NO\s+(\d+)/m', (string) file_get_contents($header), $match)) {
                $api = $match[1];
                break;
            }
        }
        if ($api === null && $payload !== null) {
            throw new RuntimeException('Recorded PHP payload lacks the Zend module ABI');
        }
        $api ??= sprintf('%d%02d00', (int) $parts[1], (int) $parts[2]);
        $phase = self::getPhpAbiPrerelease();
        $qualifier = $phase === '' ? '' : '-' . preg_replace('/~+/', '-', ltrim($phase, '~'));
        return self::getPrefix() . '-shared-runtime-' . $parts[1] . $parts[2] . '-abi' . $api . $qualifier . '-1';
    }

    private static function createLibraryPackages(?array $ids): void
    {
        $manifest = self::$sharedLibraries;
        if ($manifest === null) {
            throw new RuntimeException('Library packaging requires shared-libraries.json.');
        }
        $ids ??= $manifest->libraryIds();
        $packages = [];
        $architecture = $manifest->architecture();
        $packageArchitecture = self::$packageType === 'deb' ? match ($architecture) {
            'x86_64' => 'amd64',
            'aarch64' => 'arm64',
            default => $architecture,
        } : $architecture;
        foreach ($ids as $id) {
            $package = new library($manifest, $id);
            $version = self::$packageType === 'apk' ? self::toApkVersion($package->getVersion()) : $package->getVersion();
            $iteration = self::resolveIteration($package->getName(), $version, $packageArchitecture, self::$packageType);
            $release = $iteration;
            $manifest->setPackageVersion($id, self::$packageType, $version . (self::$packageType === 'apk' ? '-r' : '-') . $release);
            $packages[] = $package;
        }
        foreach ($packages as $package) {
            self::createPackageWithFpm($package, $package->getVersion(), $architecture);
            if (self::$debuginfo && !empty($package->getDebuginfoFpmConfig()['files'])) {
                self::createPackageWithFpm($package, $package->getVersion(), $architecture, true);
            }
        }
        $manifest->persist();
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
            if ($sapi !== 'cli') {
                self::createSapiPackage($sapi);
            }
        }
    }

    private static function createSapiPackage(string $sapi): void
    {
        self::assertRecordedSapiAvailable($sapi);
        $packageClass = "\\staticphp\\package\\{$sapi}";

        if (!class_exists($packageClass)) {
            echo "Warning: Package class not found for SAPI: {$sapi}\n";
            return;
        }

        // FrankenPHP has a special package creation flow
        if ($sapi === 'frankenphp') {
            $package = new $packageClass();
            $files = [BUILD_BIN_PATH . '/frankenphp' => '/usr/bin/' . $package->getName()];
            $dependencies = self::$sharedLibraries === null ? self::$binaryDependencies : self::getPayloadBinaryDependencies($files);
            $package->createPackages(self::$packageType, $dependencies, self::$iterationOverride, self::$debuginfo, self::$bump);
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

    private static function assertRecordedSapiAvailable(string $sapi): void
    {
        if (!in_array($sapi, ['cli', 'cgi', 'fpm', 'embed', 'frankenphp'], true)
            || ($payload = BuildPayload::load(BUILD_ROOT_PATH)) === null) {
            return;
        }
        $builtSapis = $payload->configuration()['sapi'] ?? [];
        $builtSapis = is_string($builtSapis) ? array_map('trim', explode(',', $builtSapis)) : $builtSapis;
        if (!in_array($sapi, $builtSapis, true)) {
            throw new RuntimeException("SAPI {$sapi} was not built in the recorded payload");
        }
        $source = match ($sapi) {
            'embed' => BUILD_LIB_PATH . '/libphp' . getSharedLibrarySuffix() . '.so',
            'frankenphp' => BUILD_BIN_PATH . '/frankenphp',
            default => BUILD_BIN_PATH . '/php' . ($sapi === 'cli' ? '' : '-' . $sapi),
        };
        if (!is_file($source)) {
            throw new RuntimeException("Recorded SAPI {$sapi} has no package source: {$source}");
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

        $extensionVersion = self::parseExtensionVersion($rawExtensionVersion);

        if (empty($extensionVersion)) {
            throw new RuntimeException("Warning: Could not detect version for extension {$extension}");
        }

        echo "Detected version for extension {$extension}: {$extensionVersion}\n";

        return $extensionVersion;
    }

    private static function parseExtensionVersion(string $raw): ?string
    {
        if (!preg_match('/\d+\.\d+(?:\.\d+)?(?:(?:[.-]|~{1,2})?(?:alpha|beta|rc)\d*)?(?:(?:[.-]|~{1,2})?dev\d*)?/i', $raw, $m)) {
            return null;
        }
        return self::normalizeVersion($m[0]);
    }

    /**
     * Turn an upstream PHP/extension version string into a package-orderable one:
     *   8.6.0beta1 / 8.6.0-beta1 / 8.6.0RC1 / 8.6.0-dev  ->  8.6.0~beta1 / 8.6.0~rc1 / 8.6.0~~dev
     * Plain releases pass through untouched. Without the tilde a pre-release outranks its
     * own GA release (rpmvercmp reads 8.6.0beta1 > 8.6.0); with it the chain is
     *   8.5.4 < 8.6.0~alpha3 < 8.6.0~beta1 < 8.6.0~rc1 < 8.6.0
     */
    public static function normalizeVersion(string $raw): string
    {
        if (preg_match('/^(\d+\.\d+(?:\.\d+)?)(?:(?:[.-]|~{1,2})?((?:alpha|beta|rc)\d*))?(?:(?:[.-]|~{1,2})?(dev\d*))?$/i', trim($raw), $m)) {
            $phase = strtolower($m[2] ?? '');
            $development = strtolower($m[3] ?? '');
            // dev must precede alpha; a single tilde leaves these labels in lexical order.
            return $m[1] . ($phase === '' ? '' : '~' . $phase) . ($development === '' ? '' : '~~' . $development);
        }
        return trim($raw);
    }

    /**
     * APK requires a suffix after an upstream prerelease instead of the stable version's p tag.
     */
    public static function getPhpVersionTag(string $packageType, string $packageVersion, bool $includePhpPrerelease = true): string
    {
        [$fullPhpVersion] = self::getPhpVersionAndArchitecture();
        if (preg_match('/^(\d+)\.(\d+)/', $fullPhpVersion, $m)) {
            $phpVersionSuffix = sprintf('%d%d', (int)$m[1], (int)$m[2]);
        } else {
            $phpVersionSuffix = str_replace('.', '', $fullPhpVersion);
        }
        if ($includePhpPrerelease) {
            $phpVersionSuffix .= self::getPhpAbiPrerelease();
        }
        return match ($packageType) {
            'deb' => '+php' . $phpVersionSuffix,
            'apk' => (str_contains($packageVersion, '~') ? '_p' : 'p') . $phpVersionSuffix,
            default => '_' . $phpVersionSuffix,
        };
    }

    public static function getTaggedPackageVersion(string $packageType, string $packageVersion, bool $includePhpPrerelease = true): string
    {
        $packageVersion = self::normalizeVersion($packageVersion);
        // Otherwise RPM compares the PHP tag's digits as the prerelease number.
        $packageVersion = preg_replace('/(~{1,2}(?:alpha|beta|rc|dev))$/', '${1}0', $packageVersion);
        $version = $packageVersion . self::getPhpVersionTag($packageType, $packageVersion, $includePhpPrerelease);

        return $packageType === 'apk' ? self::toApkVersion($version) : $version;
    }

    public static function getPhpAbiPrerelease(): string
    {
        [$version] = self::getPhpVersionAndArchitecture();
        if (!preg_match('/^\d+\.\d+\.0(~.*)$/D', self::normalizeVersion($version), $parts)) {
            return '';
        }
        return self::migrationPhase($parts[1]) ?? '';
    }

    public static function toApkVersion(string $version): string
    {
        // APK orders _pre after _alpha; a second _alpha keeps dev below even alpha0.
        return str_replace(['~~dev', '~dev', '~'], ['_alpha0_alpha0', '_alpha0_alpha0', '_'], $version);
    }

    /** Pull SPC's internal-env constants and package configs in after BaseCommand has set BUILD_ROOT_PATH. */
    public static function bootstrapSpcGlobals(): void
    {
        static $done = false;
        if ($done) {
            return;
        }
        $done = true;
        $spcRoot = getenv('SPP_SPC_ROOT') ?: BASE_PATH . '/vendor/crazywhalecc/static-php-cli';
        require_once $spcRoot . '/src/globals/internal-env.php';
        if (!is_dir(SPC_LOGS_DIR) && !mkdir(SPC_LOGS_DIR, 0755, true) && !is_dir(SPC_LOGS_DIR)) {
            throw new RuntimeException('Cannot create SPC log directory: ' . SPC_LOGS_DIR);
        }
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

    private static function packageConfig(package $package, bool $isDebuginfo): array
    {
        $config = $isDebuginfo ? $package->getDebuginfoFpmConfig() : $package->getFpmConfig();
        if (($payload = BuildPayload::load(BUILD_ROOT_PATH)) !== null && $payload->isSealed()) {
            $files = [];
            foreach ($config['files'] ?? [] as $source => $destination) {
                $prepared = $source;
                if ($isDebuginfo) {
                    foreach ($payload->metadata()['prepared'] ?? [] as $relative => $entry) {
                        if (isset($entry['debug']) && in_array($source, [BUILD_ROOT_PATH . '/debug/' . $relative . '.debug', BUILD_ROOT_PATH . '/debug/' . basename($relative) . '.debug'], true)) {
                            $prepared = BUILD_ROOT_PATH . '/' . $entry['debug']['path'];
                            break;
                        }
                    }
                } elseif (!is_link($source) && is_file($source) && file_get_contents($source, false, null, 0, 4) === "\x7fELF") {
                    $prepared = $payload->prepareFile($source)['runtime'];
                }
                $files[$prepared] = $destination;
            }
            $config['files'] = $files;
        }
        if (self::$sharedLibraries === null) {
            return $config;
        }
        if ($package instanceof runtime_layout) {
            self::$binaryDependencies = [];
            return $config;
        }
        self::$binaryDependencies = $isDebuginfo ? [] : self::getPayloadBinaryDependencies($config['files'] ?? []);
        if (!$isDebuginfo) {
            if (!$package instanceof runtime_layout) {
                $config['depends'][] = 'php-zts-runtime-layout';
            }
            if (!self::isPhpIndependent($package) && !$package instanceof pie) {
                $capability = self::getSharedRuntimeCapability();
                if ($package->getName() === self::getPrefix() . '-cli') {
                    $config['provides_unversioned'][] = $capability;
                    $config['conflicts'] = array_values(array_unique([...($config['conflicts'] ?? []), ...self::migrationConflicts()]));
                    $cutoffs = getenv('SPP_MIGRATION_CUTOFFS') ?: BUILD_ROOT_PATH . '/migration-cutoffs.json';
                    if (is_file($cutoffs)) {
                        $config['files'][$cutoffs] = '/usr/share/php-zts/migration/cutoffs.json';
                    }
                    if (self::$packageType === 'apk') {
                        $config['provides_unversioned'][] = self::getPrefix() . '-shared-runtime=1';
                    }
                } else {
                    $config['depends'][] = $capability;
                }
                if (self::$packageType === 'apk' && in_array($package->getName(), [self::getPrefix() . '-curl', self::getPrefix() . '-fileinfo'], true)) {
                    $config['apk_install_if'] = [self::getPrefix() . '-cli', self::getPrefix() . '-shared-runtime=1'];
                }
            }
            $id = $package instanceof library ? $package->getLibraryId() : null;
            $config['depends'] = array_values(array_unique([
                ...($config['depends'] ?? []),
                ...self::$sharedLibraries->dependenciesForPackage($config['files'] ?? [], self::$packageType, $id, $config['dlopen_dependencies'] ?? []),
            ]));
            if (self::isPhpIndependent($package)) {
                self::recordConsumer($package->getName(), $config['files'] ?? [], $id, $config['dlopen_dependencies'] ?? []);
            } else {
                $config['files'] = array_merge($config['files'] ?? [], self::consumerMetadataFiles($package->getName(), $config['files'] ?? [], $config['dlopen_dependencies'] ?? []));
            }
        }
        return $config;
    }

    public static function getSharedLibraryDependencies(array $files, string $type, array $dlopen = []): array
    {
        return self::$sharedLibraries?->dependenciesForPackage($files, $type, null, $dlopen) ?? [];
    }

    public static function getPayloadBinaryDependencies(array $files): array
    {
        $owners = self::$sharedLibraries?->getSonameOwners() ?? [];
        $result = [];
        foreach (self::payloadFiles($files) as $file) {
            if (!is_file($file) || file_get_contents($file, false, null, 0, 4) !== "\x7fELF") {
                continue;
            }
            $dynamic = new Process(['readelf', '--wide', '--dynamic', $file]);
            $dynamic->mustRun();
            preg_match_all('/\(NEEDED\).*\[([^\]]+)\]/', $dynamic->getOutput(), $needed);
            foreach ($needed[1] as $soname) {
                if (!isset($owners[$soname]) && !preg_match('/^libphp(?:-[a-zA-Z0-9.-]+)?\.so$/', $soname)) {
                    $result[$soname] ??= '0';
                }
            }
            $process = new Process(['readelf', '--wide', '--version-info', $file]);
            $process->mustRun();
            $library = null;
            foreach (explode("\n", $process->getOutput()) as $line) {
                if (preg_match('/File:\s+(\S+)/', $line, $m)) {
                    $library = $m[1];
                }
                if ($library === null || isset($owners[$library]) || !preg_match('/Name:\s+((?:GLIBC|GLIBCXX|CXXABI|GCC)_[0-9.]+)/', $line, $m)) {
                    continue;
                }
                $key = $library . '(' . $m[1] . ')';
                $result[$key] = $m[1];
                unset($result[$library]);
            }
        }
        return $result;
    }

    private static function payloadFiles(array $files): array
    {
        $result = [];
        foreach (array_keys($files) as $source) {
            if (is_dir($source)) {
                foreach (new \RecursiveIteratorIterator(new \RecursiveDirectoryIterator($source, \FilesystemIterator::SKIP_DOTS)) as $file) {
                    if ($file->isFile()) {
                        $result[] = $file->getPathname();
                    }
                }
            } else {
                $result[] = $source;
            }
        }
        return $result;
    }

    public static function recordConsumer(string $name, array $files, ?string $id = null, array $dlopen = []): void
    {
        if (self::$sharedLibraries === null) {
            return;
        }
        $name = self::$packageType === 'deb' ? str_replace('_', '-', $name) : $name;
        $libraries = $id === null ? self::consumerLibraryIds($files, $dlopen)
            : array_column(self::$sharedLibraries->dependenciesForFiles($files, $id, $dlopen), 'id');
        self::$consumerManifest[$name] = [
            'php_version' => $id === null ? (defined('SPP_PHP_VERSION') ? SPP_PHP_VERSION : null) : null,
            'architecture' => self::$sharedLibraries->architecture(),
            'libraries' => $libraries,
            'files' => $files,
        ];
        $path = DIST_PATH . '/' . self::$packageType . '/shared-library-consumers.json';
        file_put_contents($path, json_encode(['schema' => 1, 'consumers' => self::$consumerManifest], JSON_PRETTY_PRINT | JSON_UNESCAPED_SLASHES | JSON_THROW_ON_ERROR) . "\n");
    }

    public static function consumerMetadataFiles(string $name, array $files, array $dlopen = []): array
    {
        if (self::$sharedLibraries === null) {
            return [];
        }
        $name = self::$packageType === 'deb' ? str_replace('_', '-', $name) : $name;
        if (!preg_match('/^[a-z0-9][a-z0-9+_.-]*$/D', $name)) {
            throw new RuntimeException('Invalid PHP consumer package name');
        }
        self::recordConsumer($name, $files, null, $dlopen);
        $version = defined('SPP_PHP_VERSION') ? SPP_PHP_VERSION : self::getPhpVersionAndArchitecture()[0];
        if (!preg_match('/^(\d+\.\d+)(?:\D|$|\.\d)/', $version, $match)) {
            throw new RuntimeException('Invalid PHP consumer stream');
        }
        $metadata = ['schema' => 1, 'name' => $name, 'php_stream' => $match[1],
            'architecture' => self::$sharedLibraries->architecture(), 'libraries' => self::$consumerManifest[$name]['libraries']];
        $directory = TEMP_DIR . '/consumer-metadata/' . self::$packageType;
        if (!is_dir($directory) && !mkdir($directory, 0755, true) && !is_dir($directory)) {
            throw new RuntimeException('Cannot create PHP consumer metadata directory');
        }
        $path = $directory . '/' . $name . '.json';
        $contents = json_encode($metadata, JSON_PRETTY_PRINT | JSON_UNESCAPED_SLASHES | JSON_THROW_ON_ERROR) . "\n";
        if (file_put_contents($path, $contents) !== strlen($contents)) {
            throw new RuntimeException('Cannot persist PHP consumer metadata');
        }
        return [$path => '/usr/share/php-zts/consumers/' . $name . '.json'];
    }

    private static function consumerLibraryIds(array $files, array $dlopen): array
    {
        $scan = $files;
        foreach (self::payloadFiles($files) as $file) {
            if (!is_file($file) || file_get_contents($file, false, null, 0, 4) !== "\x7fELF") {
                continue;
            }
            $dynamic = new Process(['readelf', '--wide', '--dynamic', $file]);
            $dynamic->mustRun();
            preg_match_all('/\(NEEDED\).*\[(libphp(?:-[a-zA-Z0-9.-]+)?\.so)\]/', $dynamic->getOutput(), $needed);
            foreach ($needed[1] as $soname) {
                $runtime = realpath(BUILD_LIB_PATH . '/' . $soname);
                if ($runtime === false) {
                    throw new RuntimeException('Missing linked PHP runtime: ' . $soname);
                }
                $scan[$runtime] = self::runtimeLibdir() . '/' . $soname;
            }
        }
        $pending = array_column(self::$sharedLibraries->dependenciesForFiles($scan, null, $dlopen), 'id');
        $ids = [];
        while ($pending !== []) {
            $id = array_pop($pending);
            if (isset($ids[$id])) {
                continue;
            }
            $ids[$id] = true;
            foreach (self::$sharedLibraries->dependenciesForFiles(self::$sharedLibraries->getPackageFiles($id), $id) as $dependency) {
                $pending[] = $dependency['id'];
            }
        }
        $result = array_keys($ids);
        sort($result);
        return $result;
    }

    private static function createRpmPackage(package $package, string $phpVersion, string $architecture, bool $isDebuginfo = false): void
    {
        $name = $isDebuginfo ? $package->getName() . '-debuginfo' : $package->getName();
        $config = self::packageConfig($package, $isDebuginfo);
        $extraArgs = $isDebuginfo ? [] : $package->getFpmExtraArgs();

        echo "Creating RPM package for {$name}...\n";

        // For RPM packages, append PHP version to package version for extensions
        // This ensures proper version ordering when the same extension version is built for different PHP versions
        $fullPhpVersion = self::isPhpIndependent($package) ? $phpVersion : self::getPhpVersionAndArchitecture()[0];
        $rpmVersion = $phpVersion;

        // If package version differs from PHP version, it's an extension - append PHP version
        if ($phpVersion !== $fullPhpVersion) {
            $rpmVersion = self::getTaggedPackageVersion('rpm', $phpVersion, !$package instanceof pie);
        }

        $rpmVersion = self::migrationPackageVersion('rpm', $name, $rpmVersion, $architecture);

        // Calculate iteration for RPM (--iteration override > --bump remote query > local)
        $baseIteration = self::resolveIteration($name, $rpmVersion, $architecture, 'rpm');

        $iteration = $baseIteration;
        $packageFile = DIST_RPM_PATH . "/{$name}-{$rpmVersion}-{$iteration}.{$architecture}.rpm";

        $fpmArgs = [...[
            'fpm',
            '-s', 'dir',
            '-t', 'rpm',
            '--no-rpm-autoreqprov',
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
        if (!self::isPhpIndependent($package) && $name !== self::getPrefix() . '-cli') {
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

        foreach ($config['provides_unversioned'] ?? [] as $provide) {
            $fpmArgs[] = '--provides';
            $fpmArgs[] = $provide;
        }

        foreach ($config['recommends'] ?? [] as $recommend) {
            $fpmArgs[] = '--rpm-tag';
            $fpmArgs[] = 'Recommends: ' . $recommend;
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
            $fpmArgs[] = str_contains($lib, '(') ? "{$lib}(64bit)" : ($version === '0' ? "{$lib}()(64bit)" : "{$lib}({$version})(64bit)");
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
                if (file_exists($source) || is_link($source)) {
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
        $config = self::packageConfig($package, $isDebuginfo);
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
        $fullPhpVersion = self::isPhpIndependent($package) ? $phpVersion : self::getPhpVersionAndArchitecture()[0];
        $debVersion = $phpVersion;

        // If package version differs from PHP version, it's an extension - append PHP version
        if ($phpVersion !== $fullPhpVersion) {
            $debVersion = self::getTaggedPackageVersion('deb', $phpVersion, !$package instanceof pie);
        }

        $debVersion = self::migrationPackageVersion('deb', $name, $debVersion, $debArch);

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
        if (!self::isPhpIndependent($package) && $name !== self::getPrefix() . '-cli') {
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

        foreach ($config['provides_unversioned'] ?? [] as $provide) {
            $fpmArgs[] = '--provides';
            $fpmArgs[] = $provide;
        }

        foreach ($config['recommends'] ?? [] as $recommend) {
            $fpmArgs[] = '--deb-recommends';
            $fpmArgs[] = $recommend;
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
            $lib = preg_replace('/\(.*/', '', $lib);
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
                if (file_exists($source) || is_link($source)) {
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
        $config = self::packageConfig($package, $isDebuginfo);
        $extraArgs = $isDebuginfo ? [] : $package->getFpmExtraArgs();

        echo "Creating APK package for {$name} using nfpm...\n";

        // For APK packages, append PHP version to package version for extensions
        // This ensures proper version ordering when the same extension version is built for different PHP versions
        $fullPhpVersion = self::isPhpIndependent($package) ? $phpVersion : self::getPhpVersionAndArchitecture()[0];
        $apkVersion = $phpVersion;

        // If package version differs from PHP version, it's an extension - append PHP version
        if ($phpVersion !== $fullPhpVersion) {
            $apkVersion = self::getTaggedPackageVersion('apk', $phpVersion, !$package instanceof pie);
        }

        $apkVersion = self::migrationPackageVersion('apk', $name, self::toApkVersion($apkVersion), $architecture);

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
            'version_schema' => 'none',
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

        if (!$isDebuginfo && !empty($config['apk_scripts'])) {
            $nfpmConfig['apk']['scripts'] = $config['apk_scripts'];
        }

        // Build dependencies
        $depends = [];

        // Ensure non-CLI packages depend on the same PHP major.minor
        if (!self::isPhpIndependent($package) && $name !== self::getPrefix() . '-cli') {
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
            $lib = preg_replace('/\(.*/', '', $lib);
            $packageName = preg_replace('/\.so(\.\d+)*$/', '', $lib);
            if (str_starts_with($packageName, 'libc.musl-') || str_starts_with($packageName, 'ld-musl-')) {
                $packageName = 'musl';
            }
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
            $nfpmConfig['provides'] = array_values(array_unique([...$provides, ...($config['provides_unversioned'] ?? [])]));
        }
        if (!empty($config['provides_unversioned'])) {
            $nfpmConfig['provides'] = array_values(array_unique([...($nfpmConfig['provides'] ?? []), ...$config['provides_unversioned']]));
        }
        if (isset($config['replaces']) && is_array($config['replaces'])) {
            $nfpmConfig['replaces'] = $config['replaces'];
        }
        if (isset($config['conflicts']) && is_array($config['conflicts'])) {
            $nfpmConfig['conflicts'] = $config['conflicts'];
            foreach ($config['conflicts'] as $conflict) {
                if (is_string($conflict) && preg_match('/^[a-z0-9][a-z0-9+_.-]*(?:[<>]=?|=)[0-9][A-Za-z0-9.+_:-]*$/D', $conflict)) {
                    $nfpmConfig['depends'][] = '!' . $conflict;
                }
            }
            $nfpmConfig['depends'] = array_values(array_unique($nfpmConfig['depends'] ?? []));
        }

        // Build contents (files)
        $contents = [];
        if (isset($config['files']) && is_array($config['files'])) {
            foreach ($config['files'] as $source => $dest) {
                if (file_exists($source) || is_link($source)) {
                    // Fix debug link for renamed binaries
                    if (str_starts_with($source, BUILD_BIN_PATH . '/') &&
                        is_executable($source) &&
                        basename($source) !== basename($dest)) {
                        $source = self::fixBinaryDebugLink($source, $dest);
                    }
                    $contentItem = is_link($source)
                        ? ['src' => readlink($source), 'dst' => $dest, 'type' => 'symlink']
                        : ['src' => $source, 'dst' => $dest];
                    // Mark config files
                    if (!is_link($source) && isset($config['config-files']) && in_array($dest, $config['config-files'])) {
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

        $metadataArgs = [];
        if (!$isDebuginfo && !empty($config['apk_install_if'])) {
            $metadataArgs = ['--install-if', implode(' ', $config['apk_install_if'])];
        }
        if (isset($config['apk_provider_priority'])) {
            if (!is_int($config['apk_provider_priority']) || $config['apk_provider_priority'] < 0 || $config['apk_provider_priority'] > 65535) {
                throw new RuntimeException('Invalid APK provider priority');
            }
            $metadataArgs = [...$metadataArgs, '--provider-priority', (string) $config['apk_provider_priority']];
        }
        if ($metadataArgs !== []) {
            $metadata = new Process(['python3', BASE_PATH . '/bin/apk-control-metadata', $outputFile, ...$metadataArgs]);
            $metadata->setTimeout(null);
            $metadata->mustRun();
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
        if (($payload = BuildPayload::load(BUILD_ROOT_PATH)) !== null) {
            self::$versionArch = [self::normalizeVersion($payload->phpVersion()), $payload->architecture()];
            return self::$versionArch;
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
        if (($payload = BuildPayload::load(BUILD_ROOT_PATH)) !== null && $payload->isSealed()) {
            return $payload->prepareFile($sourceBinary)['runtime'];
        }
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
                $next = max(self::getNextIteration($base, $version, $architecture, $packageType), self::migrationNextIteration($base, $version, $architecture, $packageType));
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
            $baseUrl = getenv('SPP_RPM_REPO_URL') ?: 'https://rpm.henderkes.com';
            $pattern = '#' . preg_quote($name, '#') . '-' . preg_quote($version, '#')
                . '-(\d+)(?:\.[^."/]+){0,2}\.' . preg_quote($architecture, '#') . '\.rpm#';
            foreach (['el8', 'el9', 'el10'] as $repository) {
                $url = rtrim($baseUrl, '/') . "/{$architecture}/{$repository}/";
                [$code, $body] = self::httpGet($url);
                if ($code === 404) {
                    continue;
                }
                if ($code !== 200 || $body === null) {
                    throw new RuntimeException("--bump: failed to fetch RPM index {$url} (HTTP {$code})");
                }
                if (preg_match_all($pattern, $body, $matches)) {
                    foreach ($matches[1] as $it) {
                        $maxIteration = max($maxIteration, (int)$it);
                    }
                }
            }
            return $maxIteration + 1;
        }

        $host = getenv('SPP_FORGEJO_HOST') ?: 'https://git.henderkes.com';
        $hosts = (str_starts_with($name, 'php-zts-lib-') || $name === 'php-zts-runtime-layout')
            ? array_values(array_filter(array_map('trim', explode(',', getenv('SPP_FORGEJO_HOSTS') ?: $host . ',https://git.static-php.dev'))))
            : [$host];
        foreach (array_unique($hosts) as $forgejoHost) {
            $maxIteration = max($maxIteration, self::getForgejoNextIteration($name, $version, $packageType, $forgejoHost) - 1);
        }
        return $maxIteration + 1;
    }

    private static function getForgejoNextIteration(string $name, string $version, string $packageType, string $host, ?string $owner = null): int
    {
        $maxIteration = $packageType === 'apk' ? -1 : 0;
        if ($owner === null && (str_starts_with($name, 'php-zts-lib-') || $name === 'php-zts-runtime-layout')) {
            foreach (['82', '83', '84', '85', '86'] as $streamOwner) {
                $maxIteration = max($maxIteration, self::getForgejoNextIteration($name, $version, $packageType, $host, $streamOwner) - 1);
            }
            return $maxIteration + 1;
        }
        $forgeType = $packageType === 'deb' ? 'debian' : 'alpine';
        $owner ??= self::getForgejoOwner();
        $packages = [];
        $previous = null;
        for ($page = 1; ; $page++) {
            $url = rtrim($host, '/') . "/api/v1/packages/{$owner}?type={$forgeType}&limit=1000&page={$page}";
            [$code, $body] = self::httpGet($url);
            if ($code !== 200 || $body === null) {
                throw new RuntimeException("--bump: failed to query Forgejo {$url} (HTTP {$code})");
            }
            $batch = json_decode($body, true);
            if (!is_array($batch) || !array_is_list($batch) || $batch === $previous) {
                throw new RuntimeException("--bump: invalid Forgejo pagination for {$url}");
            }
            if ($batch === []) {
                break;
            }
            $packages = array_merge($packages, $batch);
            $previous = $batch;
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
        $arguments = ['curl', '-sSL', '--user-agent', 'APT-HTTP/1.3 (2.6.1)', '--max-time', '90', '-H', 'Accept: application/json', '-w', "\n%{http_code}"];
        if (preg_match('#^/api/v1/packages/([^/?]+)#', parse_url($url, PHP_URL_PATH) ?: '', $m)) {
            $owner = rawurldecode($m[1]);
            $password = getenv('FORGEJO_PASSWORD') ?: '';
            if ($password !== '') {
                $arguments = [...$arguments, '--user', $owner . ':' . $password];
            }
        }
        $process = new Process([...$arguments, $url]);
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

    public static function getSapiRecommendations(): array
    {
        return [self::getPrefix() . '-fileinfo', self::getPrefix() . '-curl'];
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
