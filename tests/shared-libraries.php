<?php

declare(strict_types=1);

use staticphp\package\library;
use staticphp\util\SharedLibraries;

require dirname(__DIR__) . '/src/package.php';
require dirname(__DIR__) . '/src/util/SharedLibraries.php';
require dirname(__DIR__) . '/src/util/BuildPayload.php';
require dirname(__DIR__) . '/src/package/library.php';

function run(array $command): string
{
    $pipes = [];
    $process = proc_open($command, [0 => ['pipe', 'r'], 1 => ['pipe', 'w'], 2 => ['pipe', 'w']], $pipes);
    if (!is_resource($process)) {
        throw new RuntimeException('Cannot run test command');
    }
    fclose($pipes[0]);
    $output = stream_get_contents($pipes[1]);
    $error = stream_get_contents($pipes[2]);
    fclose($pipes[1]);
    fclose($pipes[2]);
    if (proc_close($process) !== 0) {
        throw new RuntimeException(implode(' ', $command) . ": {$error}");
    }
    return $output;
}

function expect(bool $condition, string $message): void
{
    if (!$condition) {
        throw new RuntimeException($message);
    }
}

function rejected(callable $operation, string $reason): void
{
    try {
        $operation();
    } catch (RuntimeException $error) {
        expect(str_contains($error->getMessage(), $reason), "Expected {$reason}, got: " . $error->getMessage());
        return;
    }
    throw new RuntimeException("Expected rejection: {$reason}");
}

function removeTree(string $path): void
{
    if (is_link($path) || !is_dir($path)) {
        unlink($path);
        return;
    }
    foreach (new DirectoryIterator($path) as $entry) {
        if (!$entry->isDot()) {
            removeTree($entry->getPathname());
        }
    }
    rmdir($path);
}

$temporary = trim(run(['mktemp', '-d', '/tmp/spp-shared-libraries.XXXXXXXX']));
register_shutdown_function(static function () use ($temporary): void {
    if (is_dir($temporary)) {
        removeTree($temporary);
    }
});

try {
    mkdir($temporary . '/lib');
    mkdir($temporary . '/license');
    mkdir($temporary . '/assets');
    mkdir($temporary . '/debug');
    mkdir($temporary . '/debug/lib');
    mkdir($temporary . '/abi');
    define('TEMP_DIR', $temporary);
    file_put_contents($temporary . '/license/base.txt', 'Base test license');
    file_put_contents($temporary . '/license/top.txt', 'Top test license');
    file_put_contents($temporary . '/assets/data.txt', 'runtime data');
    file_put_contents($temporary . '/abi/base.xml', '<abi-corpus/>');
    file_put_contents($temporary . '/base.c', 'int base(void) { return 42; }');
    file_put_contents($temporary . '/top.c', 'extern int base(void); int top(void) { return base(); }');
    file_put_contents($temporary . '/main.c', 'extern int top(void); int main(void) { return top() != 42; }');
    $runtime = '/usr/lib/php-zts/lib';
    $compiler = getenv('CC') ?: 'cc';
    run([$compiler, '-shared', '-fPIC', '-g', '-Wl,-soname,libbase.so.1', '-Wl,--enable-new-dtags,-rpath,' . $runtime, $temporary . '/base.c', '-o', $temporary . '/lib/libbase.so.1.0']);
    symlink('libbase.so.1.0', $temporary . '/lib/libbase.so.1');
    symlink('libbase.so.1', $temporary . '/lib/libbase.so');
    run([$compiler, '-shared', '-fPIC', '-g', '-Wl,-soname,libtop.so.2', '-Wl,--enable-new-dtags,-rpath,' . $runtime, $temporary . '/top.c', '-L' . $temporary . '/lib', '-lbase', '-o', $temporary . '/lib/libtop.so.2']);
    symlink('libtop.so.2', $temporary . '/lib/libtop.so');
    run([$compiler, $temporary . '/main.c', '-Wl,--enable-new-dtags,-rpath,' . $runtime, '-Wl,-rpath-link,' . $temporary . '/lib', '-L' . $temporary . '/lib', '-ltop', '-o', $temporary . '/php']);
    run(['objcopy', '--only-keep-debug', $temporary . '/lib/libbase.so.1.0', $temporary . '/debug/lib/libbase.so.1.0.debug']);
    $architecture = match (php_uname('m')) {
        'x86_64', 'amd64' => 'x86_64',
        'aarch64', 'arm64' => 'aarch64',
        default => throw new RuntimeException('Unsupported test architecture'),
    };
    preg_match_all('/\(NEEDED\).*?\[([^\]]+)\]/', run(['readelf', '--dynamic', $temporary . '/lib/libbase.so.1.0']), $baseNeeded);
    preg_match_all('/\(NEEDED\).*?\[([^\]]+)\]/', run(['readelf', '--dynamic', $temporary . '/lib/libtop.so.2']), $topNeeded);
    $manifest = ['schema' => 1, 'linkage' => 'shared', 'architecture' => $architecture, 'runtime_libdir' => $runtime, 'libraries' => [
        'base' => ['version' => '1.0', 'package_versions' => ['rpm' => '1.0-2.el9', 'deb' => '1.0-2', 'apk' => '1.0-r2'], 'source_hash' => hash('sha256', 'base'), 'license' => 'MIT', 'license_files' => ['license/base.txt'], 'files' => ['lib/libbase.so.1.0', 'lib/libbase.so.1', 'lib/libbase.so', 'license/base.txt', 'assets/data.txt'], 'elf' => ['lib/libbase.so.1.0' => ['soname' => 'libbase.so.1', 'needed' => $baseNeeded[1], 'runpath' => [$runtime]]], 'runtime_assets' => ['assets/data.txt' => '/usr/share/php-zts/base/data.txt'], 'abi_dump' => ['libbase.so.1' => 'abi/base.xml']],
        'top' => ['version' => '2.1RC2', 'package_versions' => ['rpm' => '2.1~rc2-2.el9', 'deb' => '2.1~rc2-2', 'apk' => '2.1_rc2-r2'], 'source_hash' => hash('sha256', 'top'), 'license' => 'BSD-2-Clause', 'license_files' => ['license/top.txt'], 'files' => ['lib/libtop.so.2', 'lib/libtop.so'], 'elf' => ['lib/libtop.so.2' => ['soname' => 'libtop.so.2', 'needed' => $topNeeded[1], 'runpath' => [$runtime]]], 'depends' => ['base'], 'abi_epoch' => 2, 'abi_capability' => 'php-zts-lib-top-abi-2'],
    ]];
    $save = static function (array $value) use ($temporary): void {
        file_put_contents($temporary . '/shared-libraries.json', json_encode($value, JSON_THROW_ON_ERROR));
    };
    $save($manifest);
    $libraries = SharedLibraries::load($temporary, true, $runtime);
    expect($libraries->libraryIds() === ['base', 'top'], 'Unstable library identifiers');
    expect($libraries->architecture() === $architecture, 'Incorrect architecture');
    expect($libraries->getPackageVersion('base', 'deb') === '1.0-2', 'Resolved package revision ignored');
    expect($libraries->abiCapability('top') === 'php-zts-lib-top-abi-2', 'ABI epoch ignored');
    // Consumer-only repackaging restores ABI data without downloading library archives.
    // Recover native dependency revisions from the already captured package index.
    require dirname(__DIR__) . '/vendor/autoload.php';
    $withoutVersions = $manifest;
    unset($withoutVersions['libraries']['base']['package_versions']);
    $save($withoutVersions);
    $unresolved = SharedLibraries::load($temporary, true, $runtime);
    $sharedProperty = new ReflectionProperty(\staticphp\step\CreatePackages::class, 'sharedLibraries');
    $typeProperty = new ReflectionProperty(\staticphp\step\CreatePackages::class, 'packageType');
    $resolve = new ReflectionMethod(\staticphp\step\CreatePackages::class, 'resolvePublishedLibraryVersions');
    $sharedProperty->setValue(null, $unresolved);
    $previousBaselines = getenv('SPP_BASELINE_MANIFESTS');
    $previousPlan = getenv('SPP_SHARED_LIBRARY_PLAN');
    $planPath = $temporary . '/selected-libraries.json';
    file_put_contents($planPath, json_encode(['libraries' => ['top']]));
    putenv('SPP_SHARED_LIBRARY_PLAN=' . $planPath);
    foreach (['rpm' => '1.0-5.el8', 'deb' => '1.0-5', 'apk' => '1.0-r5'] as $type => $version) {
        $record = ['name' => 'php-zts-lib-base', 'version' => $version, 'architecture' => $architecture, 'provides' => ['php-zts-lib-base-abi-1']];
        $records = [array_replace($record, ['version' => '9.0-99']), array_replace($record, ['architecture' => 'wrong']), array_replace($record, ['provides' => ['php-zts-lib-base-abi-2']]), $record];
        $baselinePath = $temporary . '/published-' . $type . '.json';
        file_put_contents($baselinePath, json_encode(['type' => $type, 'published_packages' => $records]));
        putenv('SPP_BASELINE_MANIFESTS=' . json_encode([$baselinePath]));
        $typeProperty->setValue(null, $type);
        $resolve->invoke(null);
        expect($unresolved->getPackageVersion('base', $type) === $version, 'Captured native library revision was not resolved for ' . $type);
        expect($unresolved->getPackageVersion('top', $type) === $manifest['libraries']['top']['package_versions'][$type], 'Candidate library revision was overwritten');
        $unresolved->setPackageVersion('base', $type, $type === 'apk' ? '1.0-r99' : '1.0-99');
        $resolve->invoke(null);
        expect($unresolved->getPackageVersion('base', $type) === $version, 'Cached dependency floor was not rebound to the current stream');
    }
    $candidatePlan = $temporary . '/candidate-libraries.json';
    file_put_contents($candidatePlan, json_encode(['libraries' => ['base']]));
    putenv('SPP_SHARED_LIBRARY_PLAN=' . $candidatePlan);
    $unresolved->setPackageVersion('base', 'apk', '1.0-r99');
    $resolve->invoke(null);
    expect($unresolved->getPackageVersion('base', 'apk') === '1.0-r99', 'Selected candidate library floor was lowered');
    $buildOnlyPlan = $temporary . '/unpublished-libraries.json';
    file_put_contents($buildOnlyPlan, json_encode(['libraries' => [], 'build_only' => true]));
    putenv('SPP_SHARED_LIBRARY_PLAN=' . $buildOnlyPlan);
    $resolve->invoke(null);
    expect($unresolved->getPackageVersion('base', 'apk') === '1.0-r99', 'Unpublished fixture floor was replaced by a repository version');
    putenv($previousBaselines === false ? 'SPP_BASELINE_MANIFESTS' : 'SPP_BASELINE_MANIFESTS=' . $previousBaselines);
    putenv($previousPlan === false ? 'SPP_SHARED_LIBRARY_PLAN' : 'SPP_SHARED_LIBRARY_PLAN=' . $previousPlan);
    $sharedProperty->setValue(null, null);
    $typeProperty->setValue(null, 'rpm');
    $save($manifest);
    $payload = [$temporary . '/php' => '/usr/bin/php-zts-test'];
    expect($libraries->dependenciesForPackage($payload, 'deb') === ['php-zts-lib-top (>= 2.1~rc2-2)', 'php-zts-lib-top-abi-2'], 'Consumer must require its direct owner and ABI capability');
    expect($libraries->dependenciesForPackage($payload, 'apk') === ['php-zts-lib-top>=2.1_rc2-r2', 'php-zts-lib-top-abi-2'], 'APK prerelease version not normalized');
    expect($libraries->dependenciesForPackage($libraries->getPackageFiles('top'), 'rpm', 'top') === ['php-zts-lib-base >= 1.0-2.el9', 'php-zts-lib-base-abi-1'], 'Transitive library dependency missing');
    expect($libraries->dependenciesForPackage([], 'deb', null, ['libbase.so.1']) === ['php-zts-lib-base (>= 1.0-2)', 'php-zts-lib-base-abi-1'], 'Declared dlopen dependencies missing');
    expect(SharedLibraries::normalizeVersion('v3.8.0') === '3.8.0', 'Leading version marker retained');
    expect(SharedLibraries::normalizeVersion('libssh2-1.11.1') === '1.11.1', 'Artifact tag prefix retained');
    expect(SharedLibraries::normalizeVersion('grpc-v1.74.0RC1') === '1.74.0~rc1', 'Prefixed prerelease not normalized');
    expect(SharedLibraries::normalizeVersion('source-2026-10-23') === '2026-10-23', 'Numeric source version segments were lost');
    file_put_contents($temporary . '/obsolete.debug', 'old debug link');
    run(['objcopy', '--add-gnu-debuglink=' . $temporary . '/obsolete.debug', $temporary . '/lib/libbase.so.1.0']);
    $originalHash = hash_file('sha256', $temporary . '/lib/libbase.so.1.0');
    $package = new library($libraries, 'base');
    $config = $package->getFpmConfig();
    expect($package->getName() === 'php-zts-lib-base' && $package->getVersion() === '1.0', 'PHP-independent package identity changed');
    expect($config['provides_unversioned'] === ['php-zts-lib-base-abi-1'] && $config['depends'] === ['php-zts-runtime-layout'], 'Base library must require its runtime layout without a PHP dependency');
    expect($config['files'][$temporary . '/lib/libbase.so.1'] === $runtime . '/libbase.so.1', 'Runtime library path incorrect');
    expect($config['files'][$temporary . '/assets/data.txt'] === '/usr/share/php-zts/base/data.txt', 'Runtime asset path incorrect');
    expect($config['files'][$temporary . '/abi/base.xml'] === '/usr/share/php-zts/libraries/abi/base/libbase.so.1.xml', 'ABI dump missing from package');
    $metadata = json_decode(file_get_contents($temporary . '/library-base.json'), true, 512, JSON_THROW_ON_ERROR);
    expect($metadata['package_version'] === '1.0-2.el9' && $metadata['package_versions']['apk'] === '1.0-r2', 'Published metadata lost resolved library revisions');
    expect($metadata['abi_capability'] === 'php-zts-lib-base-abi-1' && $metadata['id'] === 'base', 'Installed library ABI metadata missing');
    $debugSource = $temporary . '/library-base/debug/lib/libbase.so.1.0.debug';
    expect($package->getDebuginfoFpmConfig()['files'][$debugSource] === '/usr/lib/debug' . $runtime . '/libbase.so.1.0.debug', 'Library debuginfo path incorrect');
    expect(hash_file('sha256', $temporary . '/lib/libbase.so.1.0') === $originalHash, 'Packaging mutated the ABI comparison input');
    $runtimeSource = array_search($runtime . '/libbase.so.1.0', $config['files'], true);
    expect($runtimeSource !== $temporary . '/lib/libbase.so.1.0', 'Runtime ELF was not staged before stripping');
    expect(!str_contains(run(['readelf', '--wide', '--sections', $runtimeSource]), '.debug_info'), 'Runtime package retains DWARF information');
    expect(str_contains(run(['readelf', '--string-dump=.gnu_debuglink', $runtimeSource]), 'libbase.so.1.0.debug'), 'Runtime debug link does not name installed debuginfo');
    expect(str_contains(run(['readelf', '--wide', '--sections', $debugSource]), '.debug_info'), 'Debuginfo package lost DWARF information');
    expect($package->getFpmConfig()['files'] === $config['files'], 'Preparing runtime payload twice changed staging paths');

    $libraries->setPackageVersion('base', 'deb', '1.0-3');
    $manifestBeforePersist = hash_file('sha256', $temporary . '/shared-libraries.json');
    $libraries->persist();
    expect(hash_file('sha256', $temporary . '/shared-libraries.json') === $manifestBeforePersist, 'Package revision persistence mutated the immutable SDK');
    $reloaded = SharedLibraries::load($temporary);
    expect($reloaded->getPackageVersion('base', 'deb') === '1.0-3', 'Resolved package revision did not survive manifest persistence');
    expect($reloaded->get('base')['source_hash'] === $manifest['libraries']['base']['source_hash'], 'Persisting package versions lost source identity');
    expect($reloaded->get('base')['abi_dump'] === $manifest['libraries']['base']['abi_dump'], 'Persisting package versions lost ABI metadata');
    expect(glob($temporary . '/.shared-libraries-*') === [], 'Atomic manifest temporary files were left behind');
    $format = $manifest;
    $format['libraries']['base']['package_versions']['deb'] = '1.0-10';
    $formatPath = $temporary . '/format-libraries.json';
    file_put_contents($formatPath, json_encode($format, JSON_THROW_ON_ERROR));
    $formatLibraries = SharedLibraries::load($temporary, true, $runtime, $formatPath);
    expect($formatLibraries->getPackageVersion('base', 'deb') === '1.0-10', 'Format manifest did not isolate package versions');
    $formatLibraries->setPackageVersion('base', 'deb', '1.0-11');
    $formatLibraries->persist();
    expect(SharedLibraries::load($temporary)->getPackageVersion('base', 'deb') === '1.0-3', 'Format package versions leaked into the common SDK');
    expect(hash_file('sha256', $temporary . '/shared-libraries.json') === $manifestBeforePersist, 'Format metadata mutated the common SDK');
    rejected(static fn () => $libraries->setPackageVersion('base', 'deb', '1.0-3 >= malicious'), 'Invalid resolved');
    rejected(static fn () => $libraries->setPackageVersion('base', 'deb', '1.0'), 'Invalid resolved');
    rejected(static fn () => $libraries->setPackageVersion('base', 'apk', '1.0-3'), 'Invalid resolved');
    rejected(static fn () => $libraries->getPackageVersion('base', 'invalid'), 'Unsupported package type');
    $save($manifest);
    $bad = $manifest;
    unset($bad['libraries']['top']['package_versions']);
    $save($bad);
    $unresolved = SharedLibraries::load($temporary);
    rejected(static fn () => $unresolved->dependenciesForPackage($payload, 'deb'), 'Missing resolved deb package version');
    $save($manifest);
    rejected(static fn () => SharedLibraries::load($temporary, true, '/usr/lib64/php-zts/lib'), 'expected');
    rejected(static fn () => $libraries->dependenciesForPackage([], 'rpm', null, ['missing']), 'Unknown declared');
    foreach ([['top' => ['libbase.so.1' => 'base']], null, false] as $pending) {
        $bad = $manifest;
        $bad['pending_rebuilds'] = $pending;
        $save($bad);
        rejected(static fn () => SharedLibraries::load($temporary), 'dependent rebuilds');
    }
    $bad = $manifest;
    unset($bad['libraries']['base']);
    unset($bad['libraries']['top']['depends']);
    $save($bad);
    rejected(static fn () => SharedLibraries::load($temporary), 'Unresolved private shared library');
    $bad = $manifest;
    $bad['libraries']['top']['elf']['lib/libtop.so.2']['needed'] = [];
    $save($bad);
    rejected(static fn () => SharedLibraries::load($temporary), 'Stale needed');
    $bad = $manifest;
    $bad['libraries']['base']['files'][] = '../outside.so';
    $save($bad);
    rejected(static fn () => SharedLibraries::load($temporary), 'Unsafe buildroot');
    $bad = $manifest;
    $bad['libraries']['top']['files'][] = 'lib/libbase.so.1';
    $save($bad);
    rejected(static fn () => SharedLibraries::load($temporary), 'Conflicting shared-library ownership');
    $bad = $manifest;
    $bad['libraries']['top']['abi_capabilities'] = ['php-zts-lib-top-abi-1'];
    $save($bad);
    rejected(static fn () => SharedLibraries::load($temporary), 'Historical ABI');
    $bad = $manifest;
    $bad['libraries']['base']['license_files'] = [];
    $save($bad);
    rejected(static fn () => SharedLibraries::load($temporary), 'must declare runtime files and licenses');
    mkdir($temporary . '/assets/outside');
    symlink('/etc/passwd', $temporary . '/assets/outside/escape');
    $bad = $manifest;
    $bad['libraries']['base']['runtime_assets'] = ['assets/outside' => '/usr/share/php-zts/base'];
    $save($bad);
    rejected(static fn () => SharedLibraries::load($temporary), 'Escaping buildroot');
    unlink($temporary . '/assets/outside/escape');
    $bad = $manifest;
    $bad['libraries']['base']['version'] = 'main';
    $save($bad);
    rejected(static fn () => SharedLibraries::load($temporary), 'Invalid version');
    $bad = $manifest;
    $bad['libraries']['base']['source_hash'] = '';
    $save($bad);
    rejected(static fn () => SharedLibraries::load($temporary), 'has no source_hash');
    $bad = $manifest;
    $bad['libc'] = 'unsupported';
    $save($bad);
    rejected(static fn () => SharedLibraries::load($temporary), 'Unsupported shared-library libc');
    $bad = $manifest;
    $bad['linkage'] = 'static';
    $save($bad);
    rejected(static fn () => SharedLibraries::load($temporary), 'Unsupported shared-library manifest');
    $save($manifest);
    run([$compiler, $temporary . '/main.c', '-Wl,-rpath-link,' . $temporary . '/lib', '-L' . $temporary . '/lib', '-ltop', '-o', $temporary . '/no-runpath']);
    rejected(static fn () => $libraries->dependenciesForPackage([$temporary . '/no-runpath' => '/usr/bin/no-runpath'], 'deb'), 'Missing private DT_RUNPATH');
    run([$compiler, $temporary . '/main.c', '-Wl,--enable-new-dtags,-rpath,' . $runtime . ':' . $temporary, '-Wl,-rpath-link,' . $temporary . '/lib', '-L' . $temporary . '/lib', '-ltop', '-o', $temporary . '/staging-runpath']);
    rejected(static fn () => $libraries->dependenciesForPackage([$temporary . '/staging-runpath' => '/usr/bin/staging-runpath'], 'deb'), 'Unexpected runtime search path');
    unlink($temporary . '/shared-libraries.json');
    expect(SharedLibraries::load($temporary, false) === null, 'Optional absent manifest must preserve static mode');
    echo "Passed shared-library ownership, ELF dependency, ABI, payload, and path checks.\n";
} finally {
    removeTree($temporary);
}
