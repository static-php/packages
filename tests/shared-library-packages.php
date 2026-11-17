<?php

declare(strict_types=1);

use staticphp\step\CreatePackages;
use Symfony\Component\Process\Process;

require dirname(__DIR__) . '/vendor/autoload.php';
require_once dirname(__DIR__) . '/src/util/SharedLibraries.php';
require_once dirname(__DIR__) . '/src/package/library.php';

function command(array $arguments, ?string $input = null): string
{
    $process = new Process($arguments);
    $process->setInput($input);
    $process->mustRun();
    return $process->getOutput();
}

function check(bool $condition, string $message): void
{
    if (!$condition) {
        throw new RuntimeException($message);
    }
}

function installApkFixture(string $root, array $artifacts, array $versions): void
{
    mkdir($root, 0755, true);
    command(['apk', '--root', $root, '--initdb', '--no-network', '--no-scripts', '--allow-untrusted', '--force-non-repository', '--repositories-file', '/dev/null', 'add', ...$artifacts]);
    $installed = command(['apk', '--root', $root, 'info', '-v']);
    foreach ($versions as $name => $version) {
        check(in_array($name . '-' . $version, explode("\n", trim($installed)), true), "APK did not install {$name} at {$version}: {$installed}");
    }
}

function packageHeader(string $artifact, string $type): string
{
    if ($type === 'rpm') {
        return command(['rpm', '-qp', '--qf', "%{NAME}\n%{VERSION}-%{RELEASE}\n[%{REQUIRENAME} %{REQUIREFLAGS:depflags} %{REQUIREVERSION}\n]\n[%{PROVIDENAME}\n]", $artifact]);
    }
    if ($type === 'deb') {
        $member = current(array_filter(explode("\n", command(['ar', 't', $artifact])), fn ($name) => str_starts_with($name, 'control.tar')));
        return command(['tar', str_ends_with($member, '.xz') ? '-xOJf' : '-xOzf', '-', './control'], command(['ar', 'p', $artifact, $member]));
    }
    return command(['tar', '-xOf', '-', '.PKGINFO'], command(['gzip', '-dc', $artifact]));
}

function removeFixture(string $path): void
{
    if (is_link($path) || !is_dir($path)) {
        unlink($path);
        return;
    }
    foreach (new DirectoryIterator($path) as $entry) {
        if (!$entry->isDot()) {
            removeFixture($entry->getPathname());
        }
    }
    rmdir($path);
}

$options = getopt('', ['type:']);
if (!isset($options['type'])) {
    foreach (['rpm', 'deb', 'apk'] as $type) {
        echo command([PHP_BINARY, __FILE__, '--type=' . $type]);
    }
    exit;
}
$type = $options['type'];
check(in_array($type, ['rpm', 'deb', 'apk'], true), 'Unknown package type');
$root = trim(command(['mktemp', '-d', '/tmp/php-zts-library-packages-XXXXXX']));
$oldTmp = getenv('TMPDIR');
try {
    putenv('TMPDIR=' . $root);
    define('BASE_PATH', dirname(__DIR__));
    define('SPP_TYPE', $type);
    define('SPP_PHP_VERSION', '8.4');
    define('SPP_TARGET', null);
    define('SPP_PREFIX', '-zts');
    define('BUILD_ROOT_PATH', $root . '/buildroot');
    define('BUILD_BIN_PATH', BUILD_ROOT_PATH . '/bin');
    define('BUILD_LIB_PATH', BUILD_ROOT_PATH . '/lib');
    define('BUILD_MODULES_PATH', BUILD_ROOT_PATH . '/modules');
    define('DIST_PATH', $root . '/dist');
    define('DIST_RPM_PATH', DIST_PATH . '/rpm');
    define('DIST_DEB_PATH', DIST_PATH . '/deb');
    define('DIST_APK_PATH', DIST_PATH . '/apk');
    define('TEMP_DIR', $root . '/tmp');
    $architecture = match (php_uname('m')) {
        'aarch64', 'arm64' => 'aarch64',
        'x86_64', 'amd64' => 'x86_64',
        default => throw new RuntimeException('Unsupported fixture architecture'),
    };
    foreach ([BUILD_LIB_PATH, BUILD_ROOT_PATH . '/licenses', BUILD_ROOT_PATH . '/debug/lib', DIST_RPM_PATH, DIST_DEB_PATH, DIST_APK_PATH, TEMP_DIR] as $directory) {
        mkdir($directory, 0755, true);
    }
    file_put_contents($root . '/base.c', "int fixture_base(void) { return 42; }\n");
    file_put_contents($root . '/consumer.c', "extern int fixture_base(void);\nint fixture_consumer(void) { return fixture_base(); }\n");
    file_put_contents(BUILD_ROOT_PATH . '/licenses/base', 'Fixture MIT license');
    file_put_contents(BUILD_ROOT_PATH . '/licenses/consumer', 'Fixture MIT license');
    $libdir = '/usr/lib/php-zts/lib';
    command(['gcc', '-shared', '-nostdlib', '-fPIC', '-g', '-Wl,-soname,libfixture-base++.so.1', '-Wl,--enable-new-dtags,-rpath,' . $libdir, '-o', BUILD_LIB_PATH . '/libfixture-base++.so.1', $root . '/base.c']);
    symlink('libfixture-base++.so.1', BUILD_LIB_PATH . '/libfixture-base++.so');
    command(['gcc', '-shared', '-nostdlib', '-fPIC', '-g', '-Wl,-soname,libfixture-consumer.so.1', '-Wl,--enable-new-dtags,-rpath,' . $libdir, '-o', BUILD_LIB_PATH . '/libfixture-consumer.so.1', $root . '/consumer.c', '-L' . BUILD_LIB_PATH, '-lfixture-base++']);
    $manifest = ['schema' => 1, 'linkage' => 'shared', 'architecture' => $architecture, 'libc' => $type === 'apk' ? 'musl' : 'glibc', 'runtime_libdir' => $libdir, 'libraries' => []];
    foreach (['base', 'consumer'] as $id) {
        $relative = 'lib/libfixture-' . $id . ($id === 'base' ? '++' : '') . '.so.1';
        $output = command(['readelf', '--wide', '--dynamic', BUILD_ROOT_PATH . '/' . $relative]);
        preg_match_all('/\(NEEDED\).*?\[([^\]]+)\]/', $output, $needed);
        command(['objcopy', '--only-keep-debug', BUILD_ROOT_PATH . '/' . $relative, BUILD_ROOT_PATH . '/debug/' . $relative . '.debug']);
        $manifest['libraries']['fixture-' . $id] = [
            'version' => $id === 'base' ? '1.19' : '1.2.3', 'source_hash' => str_repeat('a', 64), 'license' => 'MIT',
            'license_files' => ['licenses/' . $id], 'files' => [$relative],
            'runtime_assets' => (object)[], 'dlopen_dependencies' => [], 'depends' => [],
            'elf' => [$relative => ['soname' => 'libfixture-' . $id . ($id === 'base' ? '++' : '') . '.so.1', 'needed' => $needed[1], 'runpath' => [$libdir]]],
        ];
    }
    file_put_contents(BUILD_ROOT_PATH . '/shared-libraries.json', json_encode($manifest, JSON_THROW_ON_ERROR));
    ob_start();
    try {
        CreatePackages::run(null, '1', true, false, true, null, BUILD_ROOT_PATH);
        $revisionOne = glob(DIST_PATH . '/' . $type . '/*.' . $type);
        $oldConsumer = current(array_filter($revisionOne, static fn ($file) => str_contains(basename($file), 'fixture-consumer') && !str_contains(basename($file), '-debuginfo')));
        $oldHeader = packageHeader($oldConsumer, $type);
        $manifest = json_decode(file_get_contents(BUILD_ROOT_PATH . '/shared-libraries.json'), true, flags: JSON_THROW_ON_ERROR);
        $oldFloor = staticphp\util\SharedLibraries::load(BUILD_ROOT_PATH)->getPackageVersion('fixture-base', $type);
        file_put_contents($root . '/base.c', "int fixture_base(void) { return 42; }\nint fixture_added(void) { return 42; }\n");
        file_put_contents($root . '/consumer.c', "extern int fixture_added(void);\nint fixture_consumer(void) { return fixture_added(); }\n");
        command(['gcc', '-shared', '-nostdlib', '-fPIC', '-g', '-Wl,-soname,libfixture-base++.so.1', '-Wl,--enable-new-dtags,-rpath,' . $libdir, '-o', BUILD_LIB_PATH . '/libfixture-base++.so.1', $root . '/base.c']);
        command(['gcc', '-shared', '-nostdlib', '-fPIC', '-g', '-Wl,-soname,libfixture-consumer.so.1', '-Wl,--enable-new-dtags,-rpath,' . $libdir, '-o', BUILD_LIB_PATH . '/libfixture-consumer.so.1', $root . '/consumer.c', '-L' . BUILD_LIB_PATH, '-lfixture-base++']);
        foreach (['base', 'consumer'] as $id) {
            $manifest['libraries']['fixture-' . $id]['source_hash'] = hash_file('sha256', $root . '/' . $id . '.c');
        }
        file_put_contents(BUILD_ROOT_PATH . '/shared-libraries.json', json_encode($manifest, JSON_THROW_ON_ERROR));
        CreatePackages::run(null, '2', true, false, true, null, BUILD_ROOT_PATH);
    } finally {
        $log = ob_get_clean();
    }
    check(!is_file(BUILD_BIN_PATH . '/php'), 'Fixture unexpectedly contains PHP');
    $artifacts = array_values(array_diff(glob(DIST_PATH . '/' . $type . '/*.' . $type), $revisionOne));
    check(count($artifacts) === 4, "Expected runtime/debuginfo pairs for {$type}: {$log}");
    $headers = [];
    foreach ($artifacts as $artifact) {
        $debug = str_contains(basename($artifact), '-debuginfo');
        if ($type === 'rpm') {
            check(command(['rpm', '-qp', '--qf', '%{PAYLOADCOMPRESSOR}', $artifact]) === ($debug ? 'gzip' : 'xz'), 'Incorrect RPM payload compression');
            check(command(['rpm2cpio', $artifact]) !== '', 'RPM payload could not be decompressed');
        } elseif ($type === 'deb') {
            $members = explode("\n", command(['ar', 't', $artifact]));
            check(in_array($debug ? 'data.tar.gz' : 'data.tar.xz', $members, true), 'Incorrect DEB payload compression');
        }
        if ($debug) {
            continue;
        }
        $header = packageHeader($artifact, $type);
        $id = str_contains(basename($artifact), 'fixture-consumer') ? 'consumer' : 'base';
        $headers[$id] = $header;
        check(!str_contains($header, 'php-zts-cli'), "Library depends on PHP CLI: {$header}");
        check(!str_contains($header, 'php84'), "Library version is PHP-tagged: {$header}");
        check(str_contains($header, $id === 'base' ? '1.19' : '1.2.3'), "Missing upstream library version: {$header}");
        check(!preg_match('/php-zts-lib-[a-z0-9-]+-abi-[0-9]+/', $header), "Synthetic library ABI capability remains: {$header}");
        if ($type === 'rpm') {
            check(!preg_match('/^libfixture-.*\(\)\(64bit\)$/m', $header), 'Private library advertises a system SONAME capability');
        }
    }
    check(str_contains($headers['consumer'], 'php-zts-lib-fixture-base'), 'Direct library dependency missing');
    $manifest = json_decode(file_get_contents(BUILD_ROOT_PATH . '/shared-libraries.json'), true, flags: JSON_THROW_ON_ERROR);
    $newFloor = staticphp\util\SharedLibraries::load(BUILD_ROOT_PATH)->getPackageVersion('fixture-base', $type);
    check($newFloor !== $oldFloor && str_contains($newFloor, $type === 'apk' ? '-r2' : '-2'), 'Resolved library revision was not persisted');
    $requirement = static fn (string $version): string => match ($type) {
        'deb' => 'php-zts-lib-fixture-base (>= ' . $version . ')',
        'apk' => 'php-zts-lib-fixture-base>=' . $version,
        default => 'php-zts-lib-fixture-base >= ' . $version,
    };
    check(str_contains($headers['consumer'], $requirement($newFloor)), 'New-symbol consumer permits the older library revision');
    $soname = staticphp\util\SharedLibraries::load(BUILD_ROOT_PATH)->sonameCapability('libfixture-base++.so.1');
    check($soname === ($type === 'deb' ? 'php-zts-soname-libfixture-2dbase-2b-2b.so.1' : 'php-zts-soname-libfixture-2dbase++.so.1'), 'New SONAME capability contains architecture');
    $legacySoname = str_replace('php-zts-soname-', 'php-zts-soname-' . str_replace('_', '-5f', $architecture) . '-', $soname);
    check(str_contains($headers['base'], $legacySoname), 'Published consumers lost their compatible provider alias');
    check(!str_contains($headers['consumer'], $legacySoname), 'New consumer requires a legacy architecture-qualified capability');
    check(str_contains($headers['base'], $soname), 'Library package omits its actual private SONAME provide');
    check(str_contains($headers['consumer'], $soname), 'Consumer package omits its required private SONAME');
    if ($type === 'rpm') {
        $runtimeArtifacts = array_values(array_filter($artifacts, static fn ($file) => !str_contains(basename($file), '-debuginfo')));
        $provides = [];
        $requires = [];
        foreach ($runtimeArtifacts as $artifact) {
            $provides = [...$provides, ...explode("\n", trim(command(['rpm', '-qp', '--qf', "[%{PROVIDENAME}\n]", $artifact])))];
            $requires = [...$requires, ...explode("\n", trim(command(['rpm', '-qp', '--qf', "[%{REQUIRENAME}\n]", $artifact])))];
        }
        check(in_array($soname, $requires, true), 'RPM rewrote the private SONAME requirement');
        foreach ($requires as $dependency) {
            if (str_starts_with($dependency, 'php-zts-soname-')) {
                check(in_array($dependency, $provides, true), "No exact RPM provider for {$dependency}");
            }
        }
        $rpmRoot = $root . '/installed-libraries';
        mkdir($rpmRoot);
        command(['rpm', '--root', $rpmRoot, '--initdb']);
        // Supply only the shell prerequisite for the disabled layout script.
        $shellFixture = $root . '/fixture-shell.rpm';
        command(['fpm', '-s', 'empty', '-t', 'rpm', '-n', 'fixture-shell', '-v', '1', '--provides', '/bin/sh', '-p', $shellFixture]);
        command(['rpm', '--root', $rpmRoot, '-i', $shellFixture]);
        // Enforce native dependency resolution for the production-generated archives.
        command(['rpm', '--root', $rpmRoot, '-U', '--noscripts', ...$artifacts, ...array_filter($revisionOne, static fn ($file) => str_contains(basename($file), 'php-zts-runtime-layout'))]);
    }
    check(str_contains($oldHeader, $requirement($oldFloor)), 'Old compatible consumer lost its original library floor');
    check(!str_contains($headers['base'], 'php-zts-lib-fixture-consumer'), 'Dependency incorrectly inherited from another package');
    $consumers = json_decode(file_get_contents(DIST_PATH . '/' . $type . '/shared-library-consumers.json'), true, flags: JSON_THROW_ON_ERROR);
    check($consumers['consumers']['php-zts-lib-fixture-consumer']['libraries'] === ['fixture-base'], 'Incorrect consumer metadata');
    if ($type === 'apk') {
        foreach ($artifacts as $artifact) {
            $debug = str_contains(basename($artifact), '-debuginfo');
            $name = 'php-zts-lib-fixture-' . (str_contains(basename($artifact), 'fixture-consumer') ? 'consumer' : 'base');
            $version = str_contains($name, 'consumer') ? '1.2.3-r2' : '1.19-r2';
            $header = packageHeader($artifact, $type);
            check(str_contains($header, "pkgver = {$version}\n"), "APK normalized the upstream version: {$header}");
            if ($debug) {
                check(str_contains($header, "depend = {$name}={$version}\n"), "Incorrect exact debuginfo dependency: {$header}");
            }
        }
        installApkFixture($root . '/installed-libraries', [...$artifacts, ...array_filter($revisionOne, static fn ($file) => str_contains(basename($file), 'php-zts-runtime-layout'))], [
            'php-zts-lib-fixture-base' => '1.19-r2',
            'php-zts-lib-fixture-base-debuginfo' => '1.19-r2',
            'php-zts-lib-fixture-consumer' => '1.2.3-r2',
            'php-zts-lib-fixture-consumer-debuginfo' => '1.2.3-r2',
        ]);
        $createApk = new ReflectionMethod(CreatePackages::class, 'createApkWithNfpm');
        $library = new staticphp\package\library(staticphp\util\SharedLibraries::load(BUILD_ROOT_PATH, true, $libdir), 'fixture-base');
        $versions = [
            '1' => '1', '1.19' => '1.19', '1.19.0' => '1.19.0',
            '1.19~alpha1' => '1.19_alpha1', '1.19~~dev' => '1.19_alpha0_alpha0',
            '8.6.0~beta1' => '8.6.0_beta1', '1.13.1.0_p82_p35' => '1.13.1.0_p82_p35',
            '3.6.0~alpha1_p86_p0~rc3' => '3.6.0_alpha1_p86_p0_rc3',
        ];
        $index = 0;
        foreach ($versions as $input => $expected) {
            $version = CreatePackages::toApkVersion((string) $input);
            check($version === $expected, 'Unexpected APK version conversion');
            $name = 'php-zts-lib-fixture-version-' . $index;
            $pair = [];
            foreach (['' => false, '-debuginfo' => true] as $suffix => $debug) {
                $packageName = $name . $suffix;
                ob_start();
                try {
                    $createApk->invoke(null, $library, $packageName, $version, $architecture, '1', [
                        'files' => [$root . '/base.c' => '/usr/share/php-zts/' . $packageName],
                    ], $debug);
                } finally {
                    ob_end_clean();
                }
                $artifact = DIST_APK_PATH . "/{$packageName}-{$version}-r1.{$architecture}.apk";
                $header = packageHeader($artifact, 'apk');
                check(str_contains($header, "pkgver = {$expected}-r1\n"), "APK altered {$input}: {$header}");
                if ($debug) {
                    check(str_contains($header, "depend = {$name}={$expected}-r1\n"), "APK debug dependency differs from runtime header: {$header}");
                }
                $pair[] = $artifact;
            }
            installApkFixture($root . '/installed-version-' . $index++, $pair, [$name => $expected . '-r1', $name . '-debuginfo' => $expected . '-r1']);
        }
    }
    $libraries = staticphp\util\SharedLibraries::load(BUILD_ROOT_PATH);
    $base = new staticphp\package\library($libraries, 'fixture-base');
    $legacyArchive = $root . '/legacy-provider.' . $type;
    if ($type === 'apk') {
        $baseArchive = current(array_filter($artifacts, static fn ($file) => str_contains(basename($file), 'fixture-base-') && !str_contains(basename($file), '-debuginfo')));
        $original = file_get_contents($baseArchive);
        $config = $base->getFpmConfig();
        $config['provides_unversioned'] = [$legacySoname];
        ob_start();
        try {
            $createApk->invoke(null, $base, $base->getName(), $base->getVersion(), $architecture, '2', $config, false, false);
        } finally {
            ob_end_clean();
        }
        copy($baseArchive, $legacyArchive);
        file_put_contents($baseArchive, $original);
        preg_match_all('/^provides = (.+)$/m', packageHeader($legacyArchive, $type), $matches);
        $publishedProvides = $matches[1];
    } else {
        command(['fpm', '-s', 'dir', '-t', $type, '-n', $base->getName(), '-v', $base->getVersion(),
            '--iteration', '2', '--architecture', $architecture, '--provides', $legacySoname,
            '--depends', 'php-zts-runtime-layout', '-p', $legacyArchive,
            BUILD_LIB_PATH . '/libfixture-base++.so.1=' . $libraries->getInstallLibdir($type) . '/libfixture-base++.so.1']);
        $publishedProvides = $type === 'rpm'
            ? explode("\n", trim(command(['rpm', '-qp', '--qf', "[%{PROVIDENAME}\n]", $legacyArchive])))
            : array_map('trim', explode(',', command(['dpkg-deb', '-f', $legacyArchive, 'Provides'])));
    }
    check(in_array($legacySoname, $publishedProvides, true) && !in_array($soname, $publishedProvides, true), 'Incremental fixture does not reproduce the published provider');
    $legacyHash = hash_file('sha256', $legacyArchive);
    $inventoryPath = $root . '/published-inventory.json';
    $planPath = $root . '/selected-library.json';
    file_put_contents($inventoryPath, json_encode(['schema' => 1, 'type' => $type, 'php_stream' => '8.4', 'architecture' => $architecture,
        'inventory' => [['name' => $base->getName(), 'version' => $newFloor, 'architecture' => $architecture, 'provides' => $publishedProvides]]], JSON_THROW_ON_ERROR));
    file_put_contents($planPath, json_encode(['libraries' => ['fixture-consumer']], JSON_THROW_ON_ERROR));
    putenv('SPP_NATIVE_INVENTORY=' . $inventoryPath);
    putenv('SPP_SHARED_LIBRARY_PLAN=' . $planPath);
    $beforePartial = glob(DIST_PATH . '/' . $type . '/*.' . $type);
    ob_start();
    try {
        CreatePackages::run(null, '3', true, false, true, ['fixture-consumer'], BUILD_ROOT_PATH);
    } finally {
        ob_end_clean();
        putenv('SPP_NATIVE_INVENTORY');
        putenv('SPP_SHARED_LIBRARY_PLAN');
    }
    $partial = array_values(array_diff(glob(DIST_PATH . '/' . $type . '/*.' . $type), $beforePartial));
    $newConsumer = current(array_filter($partial, static fn ($file) => !str_contains(basename($file), '-debuginfo')
        && (str_contains(basename($file), 'fixture-consumer-') || str_contains(basename($file), 'fixture-consumer_'))));
    $partialHeader = packageHeader($newConsumer, $type);
    check(str_contains($partialHeader, $legacySoname) && !str_contains($partialHeader, $soname), 'Partial update requires an unpublished SONAME spelling');
    check(hash_file('sha256', $legacyArchive) === $legacyHash, 'Partial update rewrote the retained provider');
    $layout = current(array_filter($revisionOne, static fn ($file) => str_contains(basename($file), 'php-zts-runtime-layout')));
    $installRoot = $root . '/installed-incremental';
    if ($type === 'rpm') {
        mkdir($installRoot);
        command(['rpm', '--root', $installRoot, '--initdb']);
        command(['rpm', '--root', $installRoot, '-i', $shellFixture]);
        command(['rpm', '--root', $installRoot, '-U', '--noscripts', $layout, $legacyArchive]);
        command(['rpm', '--root', $installRoot, '-U', '--noscripts', $newConsumer]);
    } elseif ($type === 'apk') {
        installApkFixture($installRoot, [$layout, $legacyArchive], [$base->getName() => $newFloor]);
        command(['apk', '--root', $installRoot, '--no-network', '--no-scripts', '--allow-untrusted', '--force-non-repository', '--repositories-file', '/dev/null', 'add', $newConsumer]);
    } elseif (is_executable('/usr/bin/apt-get')) {
        $status = $root . '/installed-status';
        file_put_contents($status, file_get_contents('/var/lib/dpkg/status') . "\n"
            . command(['dpkg-deb', '-f', $legacyArchive]) . "Status: install ok installed\n\n"
            . command(['dpkg-deb', '-f', $layout]) . "Status: install ok installed\n\n");
        command(['apt-get', '-s', '-o', 'Dir::State::status=' . $status, '-o', 'Debug::NoLocking=true', 'install', $newConsumer]);
    }
    echo "Passed {$type} shared library packages, incremental published providers, revision floors and debuginfo without PHP.\n";
} finally {
    $oldTmp === false ? putenv('TMPDIR') : putenv('TMPDIR=' . $oldTmp);
    removeFixture($root);
}

function getLibdir(): string
{
    return SPP_TYPE === 'rpm' ? '/usr/lib64' : '/usr/lib';
}

function getSharedLibrarySuffix(): string
{
    return '-zts-84';
}
