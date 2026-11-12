<?php

declare(strict_types=1);

use staticphp\package\embed;
use staticphp\package\devel;
use staticphp\util\BuildPayload;
use staticphp\package\library;
use staticphp\package\runtime_layout;
use staticphp\step\CreatePackages;
use staticphp\util\SharedLibraries;
use Symfony\Component\Process\Process;

require dirname(__DIR__) . '/vendor/autoload.php';
require_once dirname(__DIR__) . '/src/util/BuildPayload.php';
require_once dirname(__DIR__) . '/src/util/RuntimeServices.php';

function migrationCommand(array $arguments, ?string $input = null, ?string $cwd = null): string
{
    $process = new Process($arguments, $cwd);
    $process->setInput($input);
    $process->setTimeout(120);
    $process->mustRun();
    return $process->getOutput();
}

function migrationCheck(bool $condition, string $message): void
{
    if (!$condition) {
        throw new RuntimeException($message);
    }
}

function migrationRemove(string $path): void
{
    if (is_link($path) || !is_dir($path)) {
        if (file_exists($path) || is_link($path)) {
            unlink($path);
        }
        return;
    }
    foreach (new DirectoryIterator($path) as $file) {
        if (!$file->isDot()) {
            migrationRemove($file->getPathname());
        }
    }
    rmdir($path);
}

function migrationDebMember(string $artifact, string $prefix): string
{
    $members = array_values(array_filter(explode("\n", migrationCommand(['ar', 't', $artifact])), static fn (string $member): bool => str_starts_with($member, $prefix)));
    migrationCheck(count($members) === 1, 'Missing or ambiguous DEB archive member');
    return migrationCommand(['ar', 'p', $artifact, $members[0]]);
}

function migrationHeader(string $artifact, string $type): string
{
    if ($type === 'rpm') {
        return migrationCommand(['rpm', '-qp', '--qf', "%{NAME}\n%{VERSION}-%{RELEASE}\n[%{REQUIRENAME} %{REQUIREFLAGS:depflags} %{REQUIREVERSION}\n]", $artifact]);
    }
    return migrationCommand(['tar', '-xOJf', '-', './control'], migrationDebMember($artifact, 'control.tar'));
}

function migrationExtract(string $artifact, string $type, string $destination): void
{
    mkdir($destination, 0755, true);
    if ($type === 'rpm') {
        migrationCommand(['cpio', '--quiet', '--no-absolute-filenames', '-idm'], migrationCommand(['rpm2cpio', $artifact]), $destination);
        return;
    }
    migrationCommand(['tar', '-xJf', '-', '-C', $destination], migrationDebMember($artifact, 'data.tar'));
}

function migrationVersionContracts(string $root): void
{
    $directory = $root . '/version-contracts';
    mkdir($directory, 0755, true);
    $previous = getenv('SPP_BASELINE_MANIFESTS');
    $previousOwner = getenv('SPP_FORGEJO_OWNER');
    putenv('SPP_FORGEJO_OWNER=86');
    try {
        $cases = [
            ['rpm', '3.8.1_86.0~rc4', '3.8.1_86.0~rc4+ext', '0:3.8.1_86.0~rc4+ext-1.el8', 'x86_64'],
            ['deb', '3.8.1+php86.0~rc4', '3.8.1+php86.0~rc4+ext', '3.8.1+php86.0~rc4+ext-1', 'amd64'],
            ['apk', '3.8.1p86.0_rc4', '3.8.1p86.0_rc4_p0', '3.8.1p86.0_rc4_p0-r1', 'x86_64'],
        ];
        foreach ($cases as &$case) {
            if ($case[0] === 'apk') {
                foreach ([1, 2, 3] as $field) {
                    $case[$field] = CreatePackages::validApkPackageVersion($case[$field]);
                }
            }
        }
        unset($case);
        foreach ($cases as [$type, $proposed, $legacy, $published, $architecture]) {
            foreach (['freezer', 'consumers'] as $kind) {
                $name = 'php-zts-imagick';
                $baseline = $kind === 'freezer'
                    ? ['type' => $type, 'packages' => [['name' => $name, 'version' => $published, 'architecture' => $architecture]]]
                    : ['schema' => 1, 'consumer_versions' => ['8.6' => [$name => $published]]];
                $path = $directory . '/' . $type . '-' . $kind . '.json';
                file_put_contents($path, json_encode($baseline, JSON_THROW_ON_ERROR));
                putenv('SPP_BASELINE_MANIFESTS=' . json_encode([$path], JSON_THROW_ON_ERROR));
                migrationCheck(CreatePackages::migrationPackageVersion($type, $name, $proposed, 'x86_64') === $legacy, $type . ' missed the legacy spelling in a captured ' . $kind . ' baseline');
                migrationCheck(CreatePackages::migrationPackageVersion($type, $name . '-debuginfo', $proposed, 'x86_64') === $legacy, $type . ' debuginfo lost the runtime migration spelling');
                foreach (['3.8.2', '3.8.0'] as $upstream) {
                    $candidate = str_replace('3.8.1', $upstream, $proposed);
                    migrationCheck(CreatePackages::migrationPackageVersion($type, $name, $candidate, 'x86_64') === $candidate, $type . ' masks a genuine upstream update or rollback with the published spelling');
                }
                if ($kind === 'freezer') {
                    migrationCheck(CreatePackages::migrationPackageVersion($type, $name, $proposed, 'aarch64') === $proposed, $type . ' inherited a different architecture legacy spelling');
                }
            }
        }
        $path = $directory . '/deb-normalized-name.json';
        file_put_contents($path, json_encode(['type' => 'deb', 'packages' => [['name' => 'php-zts-pdo-sqlite', 'version' => '1.2.3+php86.0~rc4+ext-4', 'architecture' => 'amd64']]], JSON_THROW_ON_ERROR));
        putenv('SPP_BASELINE_MANIFESTS=' . json_encode([$path], JSON_THROW_ON_ERROR));
        migrationCheck(CreatePackages::migrationPackageVersion('deb', 'php-zts-pdo_sqlite', '1.2.3+php86.0~rc4', 'x86_64') === '1.2.3+php86.0~rc4+ext', 'DEB legacy spelling lookup missed its normalized package name');
        $runtime = new ReflectionProperty(CreatePackages::class, 'versionArch');
        $previousRuntime = $runtime->getValue();
        $runtime->setValue(null, ['8.6.0~rc4', 'x86_64']);
        $minimum = new ReflectionMethod(CreatePackages::class, 'migrationNextIteration');
        try {
            foreach ($cases as [$type, $proposed, $legacy, $published, $architecture]) {
                $path = $directory . '/' . $type . '-first-shared-revision.json';
                $revision = static fn (int $value): string => $type === 'apk' ? '-r' . $value : '-' . $value . ($type === 'rpm' ? '.el8' : '');
                $baseline = ['type' => $type, 'packages' => [['name' => 'php-zts-imagick', 'version' => $legacy . $revision(9), 'architecture' => $architecture]]];
                file_put_contents($path, json_encode($baseline, JSON_THROW_ON_ERROR));
                putenv('SPP_BASELINE_MANIFESTS=' . json_encode([$path], JSON_THROW_ON_ERROR));
                migrationCheck($minimum->invoke(null, 'php-zts-imagick', $legacy, 'x86_64', $type) === 9, $type . ' first shared build changed the captured revision without --bump');
                $baseline['migration_cutoffs']['8.6'] = [];
                file_put_contents($path, json_encode($baseline, JSON_THROW_ON_ERROR));
                migrationCheck($minimum->invoke(null, 'php-zts-imagick', $legacy, 'x86_64', $type) === 9, $type . ' changed the captured published revision after the stream is shared');
            }
        } finally {
            $runtime->setValue(null, $previousRuntime);
        }
        echo "Passed RPM/DEB/APK captured-baseline migration spellings, runtime/debug pairing, prereleases and unmasked upstream updates/rollbacks.\n";
    } finally {
        $previousOwner === false ? putenv('SPP_FORGEJO_OWNER') : putenv('SPP_FORGEJO_OWNER=' . $previousOwner);
        $previous === false ? putenv('SPP_BASELINE_MANIFESTS') : putenv('SPP_BASELINE_MANIFESTS=' . $previous);
    }
}

function migrationLayoutContracts(string $root, string $type): void
{
    $layout = new runtime_layout();
    $config = $layout->getFpmConfig();
    [$alias, $target] = $type === 'rpm'
        ? ['/usr/lib/php-zts', '../lib64/php-zts']
        : ['/usr/lib64/php-zts', '../lib/php-zts'];
    $staged = array_search($alias, $config['files'], true);
    migrationCheck(is_string($staged) && is_link($staged) && readlink($staged) === $target, $type . ' runtime layout does not own its required compatibility alias');
    $args = $layout->getFpmExtraArgs();
    $install = array_search('--before-install', $args, true);
    $upgrade = array_search('--before-upgrade', $args, true);
    migrationCheck($install !== false, $type . ' layout lacks an ownership guard');
    if ($type === 'rpm') {
        migrationCheck($upgrade !== false && hash_file('sha256', $args[$install + 1]) === hash_file('sha256', $args[$upgrade + 1]), 'RPM layout install/upgrade guards differ');
    } else {
        migrationCheck($upgrade === false, 'DEB before-upgrade wrapper would discard triggered postinst dispatch');
    }
    $simulation = $root . '/layout-guards';
    $simAlias = $simulation . $alias;
    mkdir(dirname($simAlias), 0755, true);
    $script = $root . '/layout-guard.sh';
    file_put_contents($script, str_replace('/usr/', $simulation . '/usr/', file_get_contents($args[$install + 1])));
    migrationCommand(['sh', $script]);
    migrationCheck(!file_exists($simAlias) && !is_link($simAlias), $type . ' ownership guard modified an absent alias');
    symlink($target, $simAlias);
    migrationCommand(['sh', $script]);
    migrationCheck(readlink($simAlias) === $target, $type . ' ownership guard changed its existing relative alias');
    unlink($simAlias);
    symlink('/foreign/runtime', $simAlias);
    $rejected = new Process(['sh', $script]);
    $rejected->run();
    migrationCheck(!$rejected->isSuccessful() && is_link($simAlias) && readlink($simAlias) === '/foreign/runtime', $type . ' ownership guard accepted or overwrote a foreign alias');
    unlink($simAlias);
    mkdir($simAlias);
    file_put_contents($simAlias . '/keep', 'Existing user data');
    $rejected = new Process(['sh', $script]);
    $rejected->run();
    migrationCheck(!$rejected->isSuccessful() && is_dir($simAlias) && file_get_contents($simAlias . '/keep') === 'Existing user data', $type . ' ownership guard accepted or changed a pre-existing real directory');
    echo "Passed {$type} runtime-layout compatibility alias and absent/owned/foreign/directory guard checks.\n";
}

$options = getopt('', ['type:', 'fixture-root:', 'versions-only', 'layout-only', 'custom-prefix']);
if (isset($options['custom-prefix'])) {
    $root = trim(migrationCommand(['mktemp', '-d', '/tmp/spp-custom-prefix-XXXXXXXX']));
    $previousTmp = getenv('TMPDIR');
    try {
        putenv('TMPDIR=' . $root);
        define('BASE_PATH', dirname(__DIR__));
        define('INI_PATH', BASE_PATH . '/src/ini');
        define('SPP_TYPE', 'rpm');
        define('SPP_PHP_VERSION', '8.6');
        define('SPP_TARGET', null);
        define('SPP_PREFIX', '-zts85');
        define('BUILD_ROOT_PATH', $root . '/buildroot');
        define('BUILD_BIN_PATH', BUILD_ROOT_PATH . '/bin');
        define('BUILD_LIB_PATH', BUILD_ROOT_PATH . '/lib');
        define('BUILD_MODULES_PATH', BUILD_ROOT_PATH . '/modules');
        define('DIST_PATH', $root . '/dist');
        define('DIST_RPM_PATH', DIST_PATH . '/rpm');
        define('TEMP_DIR', $root . '/tmp');
        foreach ([BUILD_BIN_PATH, BUILD_LIB_PATH, BUILD_MODULES_PATH, BUILD_ROOT_PATH . '/license', DIST_RPM_PATH, TEMP_DIR] as $directory) {
            mkdir($directory, 0755, true);
        }
        file_put_contents(BUILD_BIN_PATH . '/php', "#!/bin/sh\necho 8.6.0\n");
        chmod(BUILD_BIN_PATH . '/php', 0755);
        $helpers = strstr(file_get_contents(BASE_PATH . '/bin/spp'), 'function setupGithubToken(');
        eval('use staticphp\\step\\CreatePackages;' . $helpers);
        foreach (['prefix' => SPP_PREFIX, 'packageType' => 'rpm', 'versionArch' => ['8.6.0', 'x86_64'], 'iterationOverride' => '1'] as $property => $value) {
            (new ReflectionProperty(CreatePackages::class, $property))->setValue(null, $value);
        }
        CreatePackages::bootstrapSpcGlobals();
        $package = new staticphp\package\cli();
        ob_start();
        try {
            (new ReflectionMethod(CreatePackages::class, 'createRpmPackage'))->invoke(null, $package, '8.6.0', 'x86_64');
        } finally {
            file_put_contents($root . '/native.log', ob_get_clean());
        }
        $archives = glob(DIST_RPM_PATH . '/*.rpm');
        migrationCheck(count($archives) === 1, 'Expected one native custom-prefix CLI RPM');
        $extracted = $root . '/extracted';
        migrationExtract($archives[0], 'rpm', $extracted);
        $alias = $extracted . '/usr/lib/php-zts85';
        migrationCheck(is_link($alias) && readlink($alias) === '../lib64/php-zts85', 'Custom-prefix CLI does not own its canonical module alias');
        migrationCheck(is_dir($extracted . '/usr/lib64/php-zts85/modules') && !is_link($extracted . '/usr/lib64/php-zts85'), 'Custom-prefix CLI does not retain a real physical module directory');
        migrationCheck(is_dir($alias . '/modules'), 'Canonical custom-prefix module path cannot reach its installed modules');
        $scripts = migrationCommand(['rpm', '-qp', '--scripts', $archives[0]]);
        migrationCheck(str_contains($scripts, 'Conflicting PHP runtime alias /usr/lib/php-zts85'), 'Native custom-prefix CLI lacks its alias ownership guard');
        $package->getFpmExtraArgs();
        $simulation = $root . '/guard';
        mkdir($simulation . '/usr/lib', 0755, true);
        mkdir($simulation . '/bin');
        file_put_contents($simulation . '/bin/getent', "#!/bin/sh\nexit 0\n");
        chmod($simulation . '/bin/getent', 0755);
        $script = $simulation . '/preinstall';
        file_put_contents($script, str_replace('/usr/', $simulation . '/usr/', file_get_contents(TEMP_DIR . '/cli-before-install.sh')));
        $runGuard = static function () use ($script, $simulation): Process {
            $process = new Process(['sh', $script], null, ['PATH' => $simulation . '/bin:' . getenv('PATH')]);
            $process->run();
            return $process;
        };
        migrationCheck($runGuard()->isSuccessful(), 'Custom-prefix ownership guard rejects an absent alias');
        $guardAlias = $simulation . '/usr/lib/php-zts85';
        symlink('../lib64/php-zts85', $guardAlias);
        migrationCheck($runGuard()->isSuccessful(), 'Custom-prefix ownership guard rejects its existing relative alias');
        unlink($guardAlias);
        symlink('/foreign/runtime', $guardAlias);
        migrationCheck(!$runGuard()->isSuccessful() && readlink($guardAlias) === '/foreign/runtime', 'Custom-prefix ownership guard accepts or mutates a foreign alias');
        unlink($guardAlias);
        mkdir($guardAlias);
        file_put_contents($guardAlias . '/keep', 'Existing private modules');
        migrationCheck(!$runGuard()->isSuccessful() && file_get_contents($guardAlias . '/keep') === 'Existing private modules', 'Custom-prefix ownership guard accepts or changes an existing real directory');
        echo "Passed native RPM custom-prefix module alias, physical directory ownership and absent/owned/foreign/real-directory guards.\n";
    } finally {
        $previousTmp === false ? putenv('TMPDIR') : putenv('TMPDIR=' . $previousTmp);
        migrationRemove($root);
    }
    exit;
}
if (isset($options['versions-only'])) {
    $root = trim(migrationCommand(['mktemp', '-d', '/tmp/spp-migration-versions-XXXXXXXX']));
    try {
        migrationVersionContracts($root);
    } finally {
        migrationRemove($root);
    }
    exit;
}
if (isset($options['layout-only'])) {
    if (!isset($options['type'])) {
        $root = trim(migrationCommand(['mktemp', '-d', '/tmp/spp-layout-guards-XXXXXXXX']));
        try {
            foreach (['rpm', 'deb'] as $type) {
                echo migrationCommand([PHP_BINARY, __FILE__, '--layout-only', '--type=' . $type, '--fixture-root=' . $root]);
            }
        } finally {
            migrationRemove($root);
        }
        exit;
    }
    $type = $options['type'];
    migrationCheck(in_array($type, ['rpm', 'deb'], true), 'Unknown layout package format');
    $root = $options['fixture-root'] . '/' . $type;
    define('BASE_PATH', dirname(__DIR__));
    define('SPP_TYPE', $type);
    define('TEMP_DIR', $root . '/tmp');
    mkdir(TEMP_DIR, 0755, true);
    require_once BASE_PATH . '/src/package/runtime_layout.php';
    migrationLayoutContracts($root, $type);
    exit;
}
if (!isset($options['type'])) {
    $root = trim(migrationCommand(['mktemp', '-d', '/tmp/spp-package-migration-XXXXXXXX']));
    try {
        migrationVersionContracts($root);
        echo migrationCommand([PHP_BINARY, __FILE__, '--custom-prefix']);
        mkdir($root . '/common/lib', 0755, true);
        mkdir($root . '/common/licenses', 0755, true);
        file_put_contents($root . '/contract.c', "int migration_contract_value(void) { return 42; }\n");
        file_put_contents($root . '/common/licenses/contract', 'MIT fixture license');
        migrationCommand(['gcc', '-shared', '-nostdlib', '-fPIC', '-g', '-gdwarf-4', '-Wl,--build-id=sha1', '-Wl,-soname,libmigration-contract.so.1', '-Wl,--enable-new-dtags,-rpath,/usr/lib/php-zts/lib', '-o', $root . '/common/lib/libmigration-contract.so.1', $root . '/contract.c']);
        migrationCommand(['gcc', '-shared', '-nostdlib', '-fPIC', '-g', '-gdwarf-4', '-Wl,--build-id=sha1', '-Wl,-soname,libssl.so.3', '-Wl,--enable-new-dtags,-rpath,/usr/lib/php-zts/lib', '-o', $root . '/common/lib/libssl.so.3', $root . '/contract.c']);
        copy($root . '/common/lib/libmigration-contract.so.1', $root . '/common/lib/libphp-zts-86.so');
        foreach (['bin', 'include/php/Zend', 'lib/php/build'] as $directory) {
            mkdir($root . '/common/' . $directory, 0755, true);
        }
        file_put_contents($root . '/common/include/php/php.h', 'Fixture header');
        file_put_contents($root . '/common/include/php/Zend/zend_modules.h', "#define ZEND_MODULE_API_NO 20260800\n");
        file_put_contents($root . '/common/lib/php/build/php.m4', 'Fixture build support');
        file_put_contents($root . '/common/bin/php-config', <<<'SH'
#!/bin/sh
SED=/usr/bin/sed
prefix='/__w/producer/packages/buildroot'
ldflags='-L/__w/producer/packages/buildroot/lib'
libs='-lphp'
program_prefix=''
program_suffix=''
lib_dir='/__w/producer/packages/buildroot/lib'
extension_dir='/__w/producer/packages/buildroot/lib/php/extensions/no-debug-zts'
include_dir='/__w/producer/packages/buildroot/include/php'
includes=$(printf '%s\n' "-I$include_dir -I$include_dir/main" | "$SED" -n p)
case "$1" in --ldflags) echo "$ldflags" ;; --extension-dir) echo "$extension_dir" ;; --prefix) echo "$prefix" ;; --includes) echo "$includes" ;; --lib-dir) echo "$lib_dir" ;; esac
SH);
        file_put_contents($root . '/common/bin/phpize', "#!/bin/sh\nSED=/usr/bin/sed\nprefix='/__w/producer/packages/buildroot'\ndatarootdir='/__w/producer/packages/buildroot/share'\nincludedir='/__w/producer/packages/buildroot/include/php'\necho \"\$prefix \$includedir\" | \"\$SED\" -n p\n");
        chmod($root . '/common/bin/php-config', 0755);
        chmod($root . '/common/bin/phpize', 0755);
        BuildPayload::record($root . '/common', [
            'extensions' => 'openssl', 'shared-extensions' => [], 'sapi' => ['cli', 'embed'],
            'build-options' => ['with-shared-libs' => true, 'with-shared-libdir' => '/usr/lib/php-zts/lib', 'with-config-file-path' => '/etc/php-zts'],
            'extra-env' => ['SPC_LIBC' => 'glibc'],
        ], '8.6.0RC4', 'x86_64');
        BuildPayload::seal($root . '/common');
        foreach (['rpm', 'deb'] as $type) {
            echo migrationCommand([PHP_BINARY, __FILE__, '--type=' . $type, '--fixture-root=' . $root]);
        }
        $rpm = json_decode(file_get_contents($root . '/rpm-result.json'), true, flags: JSON_THROW_ON_ERROR);
        $deb = json_decode(file_get_contents($root . '/deb-result.json'), true, flags: JSON_THROW_ON_ERROR);
        migrationCheck($rpm['library_sha256'] === $deb['library_sha256'], 'RPM and DEB changed the common ELF payload differently');
        migrationCheck($rpm['embed_sha256'] === $deb['embed_sha256'], 'RPM and DEB changed the common embed ELF differently');
        echo "Passed RPM/DEB common ELF identity, numeric releases, runtime-layout ownership, and legacy embed alias contracts.\n";
    } finally {
        migrationRemove($root);
    }
    exit;
}

$type = $options['type'];
migrationCheck(in_array($type, ['rpm', 'deb'], true), 'Unknown package format');
$root = $options['fixture-root'] ?? '';
migrationCheck(is_dir($root . '/common/lib'), 'Missing fixture root');
$stage = $root . '/' . $type;
$previousTmp = getenv('TMPDIR');
$previousCutoffs = getenv('SPP_MIGRATION_CUTOFFS');
try {
    mkdir($stage, 0755, true);
    putenv('TMPDIR=' . $stage);
    define('BASE_PATH', dirname(__DIR__));
    define('SPP_TYPE', $type);
    define('SPP_PHP_VERSION', '8.6');
    define('SPP_TARGET', null);
    define('SPP_PREFIX', '-zts');
    define('BUILD_ROOT_PATH', $root . '/common');
    define('BUILD_BIN_PATH', BUILD_ROOT_PATH . '/bin');
    define('BUILD_LIB_PATH', BUILD_ROOT_PATH . '/lib');
    define('BUILD_MODULES_PATH', BUILD_ROOT_PATH . '/modules');
    define('BUILD_INCLUDE_PATH', BUILD_ROOT_PATH . '/include');
    define('DIST_PATH', $stage . '/dist');
    define('DIST_RPM_PATH', DIST_PATH . '/rpm');
    define('DIST_DEB_PATH', DIST_PATH . '/deb');
    define('DIST_APK_PATH', DIST_PATH . '/apk');
    define('TEMP_DIR', $stage . '/tmp');
    foreach ([TEMP_DIR, DIST_RPM_PATH, DIST_DEB_PATH, DIST_APK_PATH] as $directory) {
        mkdir($directory, 0755, true);
    }
    require_once BASE_PATH . '/src/package/runtime_layout.php';
    $helpers = strstr(file_get_contents(BASE_PATH . '/bin/spp'), 'function setupGithubToken(');
    migrationCheck(is_string($helpers), 'Missing package path helpers');
    eval('use staticphp\\step\\CreatePackages;' . $helpers);
    foreach (['prefix' => '-zts', 'packageType' => $type, 'versionArch' => ['8.6.0~rc4', 'x86_64'], 'iterationOverride' => '8'] as $property => $value) {
        (new ReflectionProperty(CreatePackages::class, $property))->setValue(null, $value);
    }

    if ($type === 'rpm') {
        putenv('SPP_RPM_REPO_URL=https://rpm-migration.invalid');
        $indexes = ['https://rpm-migration.invalid/x86_64/el8/' => [200, 'php-zts-contract-8.6.0~rc4-9.el8.x86_64.rpm']];
        (new ReflectionProperty(CreatePackages::class, 'httpCache'))->setValue(null, $indexes);
        migrationCheck(CreatePackages::getRemoteNextIteration('php-zts-contract', '8.6.0~rc4', 'x86_64', 'rpm') === 10, 'RPM release discovery lost the published EL8 revision');
        $comparison = new Process(['rpmdev-vercmp', '8.6.0~rc4-9.el8', '8.6.0~rc4-10']);
        $comparison->run();
        migrationCheck($comparison->getExitCode() === 12 && str_contains($comparison->getOutput(), ' < '), 'Native RPM ordering rejects the suffix-free migration revision');
    }

    if ($type === 'deb') {
        putenv('SPP_FORGEJO_OWNER=86');
        $inventory = $stage . '/revision-inventory.json';
        file_put_contents($inventory, json_encode(['schema' => 1, 'type' => 'deb', 'php_stream' => '8.6',
            'architecture' => 'x86_64', 'inventory' => [
                ['name' => 'php-zts-lib-migration-contract', 'version' => '1.2.3-10', 'architecture' => 'amd64'],
                ['name' => 'php-zts-lib-migration-contract', 'version' => '1.2.3-12', 'architecture' => 'arm64'],
            ]], JSON_THROW_ON_ERROR));
        putenv('SPP_NATIVE_INVENTORY=' . $inventory);
        migrationCheck(CreatePackages::getRemoteNextIteration('php-zts-lib-migration-contract', '1.2.3', 'amd64', 'deb') === 11, 'Library revision discovery must use the selected native repository and architecture');
        putenv('SPP_NATIVE_INVENTORY');
        putenv('SPP_FORGEJO_OWNER');
    }

    migrationLayoutContracts($stage, $type);

    $source = BUILD_LIB_PATH . '/libmigration-contract.so.1';
    $sourceHash = hash_file('sha256', $source);
    $manifest = [
        'schema' => 1, 'linkage' => 'shared', 'architecture' => 'x86_64', 'libc' => 'glibc',
        'runtime_libdir' => '/usr/lib/php-zts/lib',
        'libraries' => ['migration-contract' => [
            'version' => '1.2.3', 'source_hash' => str_repeat('a', 64), 'license' => 'MIT',
            'license_files' => ['licenses/contract'], 'files' => ['lib/libmigration-contract.so.1'],
            'runtime_assets' => (object)[], 'dlopen_dependencies' => [], 'depends' => [],
            'elf' => ['lib/libmigration-contract.so.1' => ['soname' => 'libmigration-contract.so.1', 'needed' => [], 'runpath' => ['/usr/lib/php-zts/lib']]],
        ], 'openssl' => [
            'version' => '3.6.0', 'source_hash' => str_repeat('b', 64), 'license' => 'Apache-2.0',
            'license_files' => ['licenses/contract'], 'files' => ['lib/libssl.so.3'],
            'runtime_assets' => (object)[], 'dlopen_dependencies' => [], 'depends' => [],
            'elf' => ['lib/libssl.so.3' => ['soname' => 'libssl.so.3', 'needed' => [], 'runpath' => ['/usr/lib/php-zts/lib']]],
        ]],
    ];
    file_put_contents(BUILD_ROOT_PATH . '/shared-libraries.json', json_encode($manifest, JSON_THROW_ON_ERROR));
    if ($type === 'deb') {
        $sdk = $stage . '/shared-sdk';
        mkdir($sdk . '/lib', 0755, true);
        mkdir($sdk . '/licenses');
        copy($source, $sdk . '/lib/libmigration-contract.so.1');
        copy(BUILD_ROOT_PATH . '/licenses/contract', $sdk . '/licenses/contract');
        $sdkManifest = $manifest;
        unset($sdkManifest['libraries']['openssl']);
        file_put_contents($sdk . '/shared-libraries.json', json_encode($sdkManifest, JSON_THROW_ON_ERROR));
        BuildPayload::record($sdk, ['extensions' => [], 'shared-extensions' => [], 'sapi' => [],
            'build-options' => ['with-shared-libs' => true, 'with-shared-libdir' => '/usr/lib/php-zts/lib'],
            'extra-env' => ['SPC_LIBC' => 'glibc']], '8.4.0', 'x86_64', null, ['kind' => 'libraries-only']);
        BuildPayload::seal($sdk);
        $sdkSeal = hash_file('sha256', $sdk . '/build-payload.json');
        $sdkMetadata = hash_file('sha256', $sdk . '/shared-libraries.json');
        foreach (['8.3' => 1, '8.6' => 8] as $stream => $publishedRevision) {
            $ownerRoot = $stage . '/owner-' . $stream;
            mkdir($ownerRoot);
            $ownerManifest = $ownerRoot . '/shared-libraries.json';
            file_put_contents($ownerManifest, json_encode($sdkManifest, JSON_THROW_ON_ERROR));
            $inventory = $ownerRoot . '/inventory.json';
            file_put_contents($inventory, json_encode(['schema' => 1, 'type' => 'deb', 'php_stream' => $stream,
                'architecture' => 'x86_64', 'inventory' => [
                    ['name' => 'php-zts-lib-migration-contract', 'version' => '1.2.3-' . $publishedRevision, 'architecture' => 'amd64'],
                    ['name' => 'php-zts-lib-migration-contract', 'version' => '1.2.3-50', 'architecture' => 'arm64'],
                ]], JSON_THROW_ON_ERROR));
            $environment = ['SPP_DIST_PATH' => $ownerRoot, 'SPP_NATIVE_INVENTORY' => $inventory,
                'SPP_FORGEJO_OWNER' => false, 'SPP_BASELINE_MANIFESTS' => false, 'SPP_SHARED_LIBRARY_PLAN' => false];
            $command = new Process([PHP_BINARY, BASE_PATH . '/bin/spp', 'package', '--type=deb', '--phpv=' . $stream,
                '--libs-only', '--libraries=migration-contract', '--buildroot=' . $sdk,
                '--library-manifest=' . $ownerManifest, '--bump'], env: $environment);
            $command->setTimeout(120);
            $command->mustRun();
            $archives = glob($ownerRoot . '/deb/php-zts-lib-migration-contract_*.deb');
            migrationCheck(count($archives) === 1, 'Owner library archive missing or duplicated');
            $release = '1.2.3-' . ($publishedRevision + 1);
            migrationCheck(str_contains(migrationHeader($archives[0], 'deb'), 'Version: ' . $release . "\n"), 'CLI library revision followed the canonical SDK or another architecture');
            (new Process([PHP_BINARY, BASE_PATH . '/bin/build-payload', 'manifest', '--root', $sdk, '--type', 'deb',
                '--library-manifest=' . $ownerManifest, '--output', $ownerManifest], env: $environment))->mustRun();
            $resolved = json_decode(file_get_contents($ownerManifest), true, flags: JSON_THROW_ON_ERROR);
            migrationCheck($resolved['libraries']['migration-contract']['package_versions']['deb'] === $release, 'Owner manifest lost its archive revision');
        }
        migrationCheck(hash_file('sha256', $sdk . '/build-payload.json') === $sdkSeal
            && hash_file('sha256', $sdk . '/shared-libraries.json') === $sdkMetadata, 'Owner packaging mutated the shared SDK');
        BuildPayload::load($sdk, true)->verify();
        echo "Passed owner-scoped CLI library archives from one sealed canonical PHP 8.4 SDK.\n";
    }
    $libraries = new SharedLibraries(BUILD_ROOT_PATH, '/usr/lib/php-zts/lib');
    $libraries->setPackageVersion('migration-contract', $type, '1.2.3-8');
    (new ReflectionProperty(CreatePackages::class, 'sharedLibraries'))->setValue(null, $libraries);
    $method = new ReflectionMethod(CreatePackages::class, $type === 'rpm' ? 'createRpmPackage' : 'createDebPackage');
    $packages = [new library($libraries, 'migration-contract'), new library($libraries, 'openssl'), new runtime_layout()];
    $libphp = 'libphp-zts-86.so';
    copy($source, BUILD_LIB_PATH . '/' . $libphp);
    $packages[] = new embed();
    $packages[] = new devel();
    $sapiGuard = new ReflectionMethod(CreatePackages::class, 'assertRecordedSapiAvailable');
    $sapiGuard->invoke(null, 'embed');
    try {
        $sapiGuard->invoke(null, 'fpm');
        throw new RuntimeException('Package-only selection accepted a SAPI absent from the recorded payload');
    } catch (RuntimeException $exception) {
        migrationCheck(str_contains($exception->getMessage(), 'SAPI fpm was not built'), 'Unexpected recorded SAPI availability failure');
    }
    $cutoffs = $stage . '/migration-cutoffs.json';
    file_put_contents($cutoffs, json_encode(['schema' => 1, 'formats' => [
        'rpm' => ['php-zts-intl' => '1.2-3'],
        'deb' => ['php-zts-intl' => '1.2-3'],
    ]], JSON_THROW_ON_ERROR));
    putenv('SPP_MIGRATION_CUTOFFS=' . $cutoffs);
    file_put_contents($stage . '/generation-fixture', 'Shared-core generation fixture');
    foreach (['cli', 'intl'] as $kind) {
        $packages[] = new class($kind, $stage . '/generation-fixture') implements staticphp\package {
            public function __construct(private string $kind, private string $source) {}
            public function getName(): string { return 'php-zts-' . $this->kind; }
            public function getVersion(): string { return $this->kind === 'cli' ? '8.6.0~rc4' : '1.2.4'; }
            public function getFpmConfig(): array {
                return [
                    'files' => [$this->source => '/usr/share/' . $this->getName() . '/generation-fixture'],
                    'depends' => $this->kind === 'cli' ? [] : ['php-zts-cli'],
                    'recommends' => $this->kind === 'cli' ? CreatePackages::getSapiRecommendations() : [],
                ];
            }
            public function getFpmExtraArgs(): array { return []; }
            public function getDebuginfoFpmConfig(): array { return []; }
            public function getDescription(): string { return 'Shared-core generation contract fixture'; }
            public function getLicense(): string { return 'MIT'; }
        };
    }
    $extractions = [];
    $headers = [];
    $relationships = [];
    $archivePaths = [];
    foreach ($packages as $package) {
        $name = $package->getName();
        $version = method_exists($package, 'getVersion') ? $package->getVersion() : '8.6.0~rc4';
        ob_start();
        try {
            $method->invoke(null, $package, $version, 'x86_64');
        } finally {
            file_put_contents($stage . '/' . $name . '.log', ob_get_clean());
        }
        $archives = glob(DIST_PATH . '/' . $type . '/' . $name . '*.' . $type);
        migrationCheck(count($archives) === 1, 'Expected one native archive for ' . $name);
        $archivePaths[$name] = $archives[0];
        $headers[$name] = migrationHeader($archives[0], $type);
        foreach (['requires' => 'Depends', 'provides' => 'Provides', 'conflicts' => 'Conflicts', 'recommends' => 'Recommends'] as $rpmField => $debField) {
            if ($type === 'rpm') {
                $relationships[$name][$rpmField] = trim(migrationCommand(['rpm', '-qp', '--' . $rpmField, $archives[0]]));
            } else {
                preg_match('/^' . $debField . ': (.*)$/m', $headers[$name], $field);
                $relationships[$name][$rpmField] = $field[1] ?? '';
            }
        }
        $extractions[$name] = $stage . '/extracted/' . $name;
        migrationExtract($archives[0], $type, $extractions[$name]);
        if ($type === 'rpm') {
            migrationCheck(migrationCommand(['rpm', '-qp', '--qf', '%{RELEASE}', $archives[0]]) === '8', 'RPM header Release retains a distro suffix');
            migrationCheck(str_ends_with(basename($archives[0]), '-8.x86_64.rpm'), 'RPM filename differs from neutral header Release');
        }
    }
    $capability = 'php-zts-shared-runtime-86-abi20260800-rc4-1';
    migrationCheck(CreatePackages::getSharedRuntimeCapability() === $capability, 'Shared runtime capability has the wrong PHP stream/module ABI/generation');
    migrationCheck(str_contains($relationships['php-zts-cli']['provides'], $capability), 'CLI native package does not provide its shared-core generation');
    migrationCheck(str_contains($relationships['php-zts-intl']['requires'], $capability), 'Shared module native package does not require its shared-core generation');
    foreach (['php-zts-cli', 'php-zts-intl'] as $name) {
        migrationCheck(str_contains($relationships[$name]['requires'], 'php-zts-runtime-layout'), 'Runtime consumer lacks a hard layout dependency');
    }
    $expectedConflict = $type === 'rpm' ? 'php-zts-intl < 1.2-3' : 'php-zts-intl (<< 1.2-3)';
    migrationCheck(str_contains($relationships['php-zts-cli']['conflicts'], $expectedConflict), 'CLI native package does not exclude the captured legacy module cutoff');
    migrationCheck(!str_contains($relationships['php-zts-intl']['conflicts'], 'php-zts-intl'), 'Module inherited the core migration cutoff against itself');
    $recommendations = preg_split($type === 'rpm' ? '/\\n/' : '/,\\s*/', $relationships['php-zts-cli']['recommends']);
    sort($recommendations);
    migrationCheck($recommendations === ['php-zts-curl', 'php-zts-fileinfo'], 'CLI native shared-core migration changed cURL/Fileinfo recommendations');
    foreach (['php-zts-curl', 'php-zts-fileinfo'] as $optional) {
        migrationCheck(!str_contains($relationships['php-zts-cli']['requires'], $optional), 'CLI migration changed an optional shared module into a hard dependency');
    }

    $triggerPaths = ['/usr/lib/php-zts', '/usr/lib64/php-zts', '/usr/share/php-zts/services', '/usr/share/php-zts/consumers', '/usr/bin', '/usr/sbin', '/usr/lib/systemd/system'];
    if ($type === 'rpm') {
        $scripts = migrationCommand(['rpm', '-qp', '--qf', '[%{TRANSFILETRIGGERSCRIPTS}\n]', $archivePaths['php-zts-runtime-layout']]);
        $names = explode("\n", trim(migrationCommand(['rpm', '-qp', '--qf', '[%{TRANSFILETRIGGERNAME}\n]', $archivePaths['php-zts-runtime-layout']])));
        migrationCheck(str_contains($scripts, '/usr/libexec/php-zts/runtime-services finish'), 'RPM runtime layout lacks a registered native transaction file-trigger script');
        foreach ($triggerPaths as $path) {
            migrationCheck(in_array($path, $names, true), 'RPM transaction file trigger does not watch ' . $path);
        }
    } else {
        $triggers = explode("\n", trim(migrationCommand(['tar', '-xOJf', '-', './triggers'], migrationDebMember($archivePaths['php-zts-runtime-layout'], 'control.tar'))));
        foreach ($triggerPaths as $path) {
            migrationCheck(in_array('interest-noawait ' . $path, $triggers, true), 'DEB native trigger metadata lacks interest-noawait for ' . $path);
        }
        $helper = $stage . '/native-trigger-helper';
        $marker = $stage . '/native-trigger-marker';
        file_put_contents($helper, "#!/bin/sh\nprintf '%s\\n' \"\$*\" >> " . escapeshellarg($marker) . "\n");
        chmod($helper, 0755);
        $postinst = migrationCommand(['tar', '-xOJf', '-', './postinst'], migrationDebMember($archivePaths['php-zts-runtime-layout'], 'control.tar'));
        $script = $stage . '/native-layout-postinst';
        file_put_contents($script, str_replace('/usr/libexec/php-zts/runtime-services', $helper, $postinst));
        migrationCommand(['sh', $script, 'configure']);
        migrationCheck(!file_exists($marker), 'DEB layout refreshes before the complete runtime has configured');
        migrationCommand(['sh', $script, 'triggered', 'php-zts-runtime']);
        migrationCheck(file_get_contents($marker) === "finish\n", 'Extracted native DEB postinst does not dispatch its runtime trigger exactly once');
    }

    $libraryName = 'php-zts-lib-migration-contract';
    migrationCheck(str_contains($relationships['php-zts-lib-openssl']['requires'], 'ca-certificates'), 'Private OpenSSL native package does not require the distro CA trust store');
    migrationCheck(str_contains($headers[$libraryName], 'php-zts-runtime-layout'), 'Independently installed library lacks a hard layout dependency');
    migrationCheck(!str_contains($headers[$libraryName], 'php-zts-cli'), 'Independent library depends on a PHP stream');
    migrationCheck(!str_contains($headers['php-zts-runtime-layout'], 'php-zts-cli'), 'Runtime layout depends on a PHP stream');
    migrationCheck(str_contains($headers['php-zts-runtime-layout'], $type === 'rpm' ? "\n2-8\n" : "Version: 2-8\n"), 'Runtime layout version is PHP-tagged');
    migrationCheck(str_contains($headers['php-zts-embed'], 'php-zts-runtime-layout'), 'Embed lacks a hard layout dependency');
    $physical = $type === 'rpm' ? '/usr/lib64' : '/usr/lib';
    $layoutRoot = $extractions['php-zts-runtime-layout'];
    if ($type === 'rpm') {
        migrationCheck(is_link($layoutRoot . '/usr/lib/php-zts'), 'RPM layout does not own the canonical alias');
        migrationCheck(readlink($layoutRoot . '/usr/lib/php-zts') === '../lib64/php-zts', 'RPM canonical alias has an incorrect target');
        migrationCheck(!is_link($layoutRoot . '/usr/lib64/php-zts'), 'RPM layout replaces the old real private directory with a link');
    } else {
        migrationCheck(is_dir($layoutRoot . '/usr/lib/php-zts'), 'DEB layout lacks its real canonical tree');
        migrationCheck(!is_link($layoutRoot . '/usr/lib/php-zts'), 'DEB canonical tree is unexpectedly an alias');
    }
    $libraryPayload = $extractions[$libraryName] . $physical . '/php-zts/lib/libmigration-contract.so.1';
    $embedPayload = $extractions['php-zts-embed'] . $physical . '/php-zts/lib/' . $libphp;
    migrationCheck(is_file($libraryPayload) && is_file($embedPayload), 'Private ELF payload destination is missing');
    $legacyEmbed = $extractions['php-zts-embed'] . $physical . '/' . $libphp;
    migrationCheck(is_link($legacyEmbed), 'Embed package does not own its legacy public file alias');
    migrationCheck(readlink($legacyEmbed) === 'php-zts/lib/' . $libphp, 'Legacy embed alias points outside the private tree');
    migrationCheck(str_contains($headers['php-zts-devel'], 'php-zts-runtime-layout'), 'Devel package lacks a hard runtime-layout dependency');
    $phpConfig = $extractions['php-zts-devel'] . '/usr/bin/php-config-zts';
    migrationCheck(is_executable($phpConfig), 'Installed php-config is missing or not executable');
    migrationCheck(!str_contains(file_get_contents($phpConfig), '/usr/bin/sed'), 'Installed php-config requires the EL8 producer sed path');
    $linkFlags = trim(migrationCommand([$phpConfig, '--ldflags']));
    migrationCheck(str_contains($linkFlags, '-L' . $physical . '/php-zts/lib') && str_contains($linkFlags, '-Wl,-rpath,/usr/lib/php-zts/lib'), 'Installed php-config cannot link against the private embed/runtime tree');
    migrationCheck(!str_contains(file_get_contents($phpConfig), '/__w/producer/') && !str_contains($linkFlags, $root), 'Installed php-config leaks the relocated producer buildroot');
    migrationCheck(trim(migrationCommand([$phpConfig, '--includes'])) === '-I/usr/include/php-zts -I/usr/include/php-zts/main', 'Installed php-config includes point outside the installed development tree');
    migrationCheck(trim(migrationCommand([$phpConfig, '--extension-dir'])) === $physical . '/php-zts/modules', 'Installed php-config installs extensions outside the packaged module tree');
    migrationCheck(trim(migrationCommand([$phpConfig, '--lib-dir'])) === $physical, 'Installed php-config reports the wrong embed alias directory');
    $phpize = $extractions['php-zts-devel'] . '/usr/bin/phpize-zts';
    migrationCheck(!str_contains(file_get_contents($phpize), '/usr/bin/sed'), 'Installed phpize requires the EL8 producer sed path');
    migrationCheck(!str_contains(file_get_contents($phpize), '/__w/producer/'), 'Installed phpize leaks the relocated producer buildroot');
    migrationCheck(trim(migrationCommand([$phpize])) === '/usr /usr/include/php-zts', 'Installed phpize retains its producer include directory');

    $simulation = $stage . '/scriptlet-root';
    $simLib = $simulation . $physical;
    mkdir($simLib . '/php-zts/lib', 0755, true);
    file_put_contents($simLib . '/php-zts/lib/' . $libphp, 'Installed embed fixture');
    symlink('php-zts/lib/' . $libphp, $simLib . '/' . $libphp);
    foreach (['libphp.so', 'libphp-zts.so'] as $alias) {
        symlink($simLib . '/' . $libphp, $simLib . '/' . $alias);
    }
    (new devel())->getFpmExtraArgs();
    foreach (['install', 'remove'] as $operation) {
        $script = str_replace($physical . '/', $simLib . '/', file_get_contents(TEMP_DIR . '/devel-after-' . $operation . '.sh'));
        file_put_contents($stage . '/devel-' . $operation . '.sh', $script);
    }
    migrationCommand(['sh', $stage . '/devel-install.sh', '2']);
    $oldRemoval = <<<'SH'
#!/bin/sh
for name in libphp.so libphp-zts.so; do
    if [ -L "$FIXTURE_LIB/$name" ] && [ "$(readlink "$FIXTURE_LIB/$name")" = "$FIXTURE_LIB/libphp-zts-86.so" ]; then
        rm -f "$FIXTURE_LIB/$name"
    fi
done
SH;
    file_put_contents($stage . '/legacy-devel-remove.sh', str_replace('$FIXTURE_LIB', $simLib, $oldRemoval));
    migrationCommand(['sh', $stage . '/legacy-devel-remove.sh', '1']);
    foreach (['libphp.so', 'libphp-zts.so'] as $alias) {
        migrationCheck(is_link($simLib . '/' . $alias) && readlink($simLib . '/' . $alias) === $libphp, 'Old devel removal script deleted a new stream alias');
    }
    foreach (['upgrade', '1', '2'] as $reason) {
        migrationCommand(['sh', $stage . '/devel-remove.sh', $reason]);
        migrationCheck(is_link($simLib . '/libphp.so') && is_link($simLib . '/libphp-zts.so'), 'New devel upgrade removal deleted active linker aliases');
    }
    unlink($simLib . '/libphp.so');
    symlink('libphp-zts-85.so', $simLib . '/libphp.so');
    migrationCommand(['sh', $stage . '/devel-remove.sh', '0']);
    migrationCheck(is_link($simLib . '/libphp.so') && readlink($simLib . '/libphp.so') === 'libphp-zts-85.so', 'Devel uninstall deleted another stream alias');
    migrationCheck(!is_link($simLib . '/libphp-zts.so'), 'Devel uninstall left its own obsolete alias');

    $simBin = $simulation . '/usr/bin';
    mkdir($simBin, 0755, true);
    file_put_contents($simBin . '/php-zts', 'CLI fixture');
    symlink($simBin . '/php-zts', $simBin . '/php');
    (new \staticphp\package\cli())->getFpmExtraArgs();
    foreach (['install', 'remove'] as $operation) {
        $script = str_replace('/usr/bin/', $simBin . '/', file_get_contents(TEMP_DIR . '/cli-after-' . $operation . '.sh'));
        file_put_contents($stage . '/cli-' . $operation . '.sh', $script);
    }
    migrationCommand(['sh', $stage . '/cli-install.sh', '2']);
    $oldCliRemoval = 'if [ -L "$1/php" ] && [ "$(readlink "$1/php")" = "$1/php-zts" ]; then rm -f "$1/php"; fi';
    migrationCommand(['sh', '-c', $oldCliRemoval, 'legacy-cli-removal', $simBin]);
    migrationCheck(is_link($simBin . '/php') && readlink($simBin . '/php') === 'php-zts', 'Old CLI removal deleted the php command during upgrade');
    migrationCommand(['sh', $stage . '/cli-remove.sh', '1']);
    migrationCheck(is_link($simBin . '/php'), 'CLI upgrade removal deleted the php command');
    migrationCommand(['sh', $stage . '/cli-remove.sh', '0']);
    migrationCheck(!is_link($simBin . '/php'), 'CLI uninstall retained its php alias');
    migrationCommand(['sh', $stage . '/cli-install.sh', '1']);
    migrationCheck(readlink($simBin . '/php') === 'php-zts', 'Fresh CLI install did not create php');
    unlink($simBin . '/php');
    file_put_contents($simBin . '/php-other', 'Other PHP fixture');
    symlink('php-other', $simBin . '/php');
    migrationCommand(['sh', $stage . '/cli-install.sh', '2']);
    migrationCommand(['sh', $stage . '/cli-remove.sh', '0']);
    migrationCheck(readlink($simBin . '/php') === 'php-other', 'CLI replaced or removed another PHP installation');

    migrationCheck(hash_file('sha256', $source) === $sourceHash, 'Native packaging mutated the common source ELF');
    file_put_contents($root . '/' . $type . '-result.json', json_encode([
        'library_sha256' => hash_file('sha256', $libraryPayload),
        'embed_sha256' => hash_file('sha256', $embedPayload),
    ], JSON_THROW_ON_ERROR));
    echo "Passed {$type} native neutral release, independent layout dependencies, canonical/legacy path ownership, shared-core generation/cutoffs, native transaction triggers, recommendations and source immutability.\n";
} finally {
    $previousTmp === false ? putenv('TMPDIR') : putenv('TMPDIR=' . $previousTmp);
    $previousCutoffs === false ? putenv('SPP_MIGRATION_CUTOFFS') : putenv('SPP_MIGRATION_CUTOFFS=' . $previousCutoffs);
}
