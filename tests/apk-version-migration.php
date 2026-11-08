<?php

declare(strict_types=1);

use staticphp\extension;
use staticphp\package;
use staticphp\package\cli;
use staticphp\step\CreatePackages;
use Symfony\Component\Process\Process;

require dirname(__DIR__) . '/vendor/autoload.php';

function migrationCheck(bool $condition, string $message): void
{
    if (!$condition) {
        throw new RuntimeException($message);
    }
}

function migrationCommand(array $arguments, ?string $input = null): string
{
    $process = new Process($arguments);
    $process->setInput($input);
    $process->setTimeout(null);
    $process->mustRun();
    return $process->getOutput();
}

function migrationRemove(string $path): void
{
    if (!is_dir($path) || is_link($path)) {
        unlink($path);
        return;
    }
    foreach (new DirectoryIterator($path) as $entry) {
        if (!$entry->isDot()) {
            migrationRemove($entry->getPathname());
        }
    }
    rmdir($path);
}

final class MigrationFixturePackage implements package
{
    public function __construct(private readonly string $name) {}
    public function getName(): string { return $this->name; }
    public function getFpmConfig(): array { return []; }
    public function getDebuginfoFpmConfig(): array { return []; }
    public function getFpmExtraArgs(): array { return []; }
    public function getLicense(): string { return 'MIT'; }
    public function getDescription(): string { return 'APK version migration fixture'; }
}

$options = getopt('', ['type:', 'native', 'php-stream:']);
if (isset($options['native']) && !isset($options['php-stream']) && ($options['type'] ?? 'apk') === 'apk') {
    if (!isset($options['type'])) {
        echo migrationCommand([PHP_BINARY, '-d', 'memory_limit=384M', __FILE__]);
    }
    foreach (['8.2', '8.3', '8.4', '8.5'] as $stream) {
        echo migrationCommand([PHP_BINARY, '-d', 'memory_limit=384M', __FILE__, '--type=apk', '--native', '--php-stream=' . $stream]);
    }
    exit;
}
$stream = $options['php-stream'] ?? '8.6';
migrationCheck(in_array($stream, ['8.2', '8.3', '8.4', '8.5', '8.6'], true), 'Unknown fixture PHP stream');
migrationCheck(!isset($options['native']) || $stream !== '8.6', 'Native fixtures cover affected PHP8.2-8.5 streams');
$fullPhpVersion = ['8.2' => '8.2.35', '8.3' => '8.3.36', '8.4' => '8.4.27', '8.5' => '8.5.12', '8.6' => '8.6.0'][$stream];
if (!isset($options['type'])) {
    foreach (['rpm', 'deb', 'apk'] as $type) {
        echo migrationCommand([PHP_BINARY, '-d', 'memory_limit=384M', __FILE__, '--type=' . $type,
            ...(isset($options['native']) && $type === 'apk' ? ['--native'] : []),
            ...(isset($options['php-stream']) ? ['--php-stream=' . $stream] : [])]);
    }
    exit;
}
$type = $options['type'];
migrationCheck(in_array($type, ['rpm', 'deb', 'apk'], true), 'Unknown package type');
$root = trim(migrationCommand(['mktemp', '-d', '/tmp/spp-apk-version-migration-XXXXXXXX']));
$oldTmp = getenv('TMPDIR');
$oldPath = getenv('PATH');
try {
    putenv('TMPDIR=' . $root);
    if (($nfpm = getenv('SPP_NFPM_BINARY')) !== false) {
        putenv('PATH=' . dirname($nfpm) . PATH_SEPARATOR . $oldPath);
    }
    define('BASE_PATH', dirname(__DIR__));
    define('INI_PATH', BASE_PATH . '/src/ini');
    define('SPP_TYPE', $type);
    define('SPP_PHP_VERSION', $stream);
    define('SPP_PREFIX', isset($options['native']) ? '-zts' : '-zts86');
    define('SPP_TARGET', null);
    define('BUILD_ROOT_PATH', $root . '/buildroot');
    define('BUILD_BIN_PATH', BUILD_ROOT_PATH . '/bin');
    define('BUILD_LIB_PATH', BUILD_ROOT_PATH . '/lib');
    define('BUILD_MODULES_PATH', BUILD_ROOT_PATH . '/modules');
    define('DIST_PATH', $root . '/dist');
    define('DIST_APK_PATH', DIST_PATH . '/apk');
    define('TEMP_DIR', $root . '/tmp');
    foreach ([BUILD_BIN_PATH, BUILD_LIB_PATH, BUILD_MODULES_PATH, BUILD_ROOT_PATH . '/debug',
        BUILD_ROOT_PATH . '/license', DIST_APK_PATH, TEMP_DIR, $root . '/repo/x86_64'] as $directory) {
        mkdir($directory, 0755, true);
    }
    file_put_contents(BUILD_BIN_PATH . '/php', 'fixture PHP executable');
    $helpers = strstr(file_get_contents(BASE_PATH . '/bin/spp'), 'function setupGithubToken(');
    migrationCheck(is_string($helpers), 'Cannot load package path helpers');
    eval('use staticphp\\step\\CreatePackages;' . $helpers);
    (new ReflectionProperty(CreatePackages::class, 'prefix'))->setValue(null, SPP_PREFIX);
    (new ReflectionProperty(CreatePackages::class, 'versionArch'))->setValue(null, [$fullPhpVersion, 'x86_64']);
    (new ReflectionProperty(CreatePackages::class, 'packageType'))->setValue(null, $type);
    CreatePackages::bootstrapSpcGlobals();
    $prefix = CreatePackages::getPrefix();
    $configs = [];
    foreach (['amqp', 'igbinary'] as $name) {
        $package = new extension($name);
        migrationCheck($package->getName() === $prefix . '-' . $name, 'Extension package was renamed');
        file_put_contents(BUILD_MODULES_PATH . '/' . $name . getSharedLibrarySuffix() . '.so', 'fixture ' . $name);
        file_put_contents(BUILD_ROOT_PATH . '/debug/' . $name . getSharedLibrarySuffix() . '.so.debug', 'fixture debug ' . $name);
        $configs[$name] = $package->getFpmConfig();
        $configs[$name . '-debuginfo'] = $package->getDebuginfoFpmConfig();
        foreach ([$configs[$name], $configs[$name . '-debuginfo']] as $config) {
            migrationCheck(!isset($config['apk_provider_priority']), 'Package still uses replacement provider priority');
            migrationCheck(!in_array($prefix . '-' . $name . '=100', $config['provides'] ?? [], true), 'Package still provides a renamed alias');
        }
    }
    echo "Passed {$type} unchanged extension/runtime/debug package identities.\n";
    if (isset($options['native'])) {
        $create = new ReflectionMethod(CreatePackages::class, 'createApkWithNfpm');
        $archive = static function (string $name, string $version, int $revision, array $config, bool $debug = false) use ($create, $root): string {
            ob_start();
            try {
                $create->invoke(null, new MigrationFixturePackage(preg_replace('/-debuginfo$/', '', $name)), $name,
                    $version, 'x86_64', (string) $revision, $config, $debug);
            } finally {
                file_put_contents($root . '/package.log', ob_get_clean(), FILE_APPEND);
            }
            $path = DIST_APK_PATH . '/' . $name . '-' . $version . '-r' . $revision . '.x86_64.apk';
            return $path;
        };
        $publish = static function (string $path) use ($root): void {
            copy($path, $root . '/repo/x86_64/' . preg_replace('/\.x86_64(?=\.apk$)/', '', basename($path)));
        };
        $header = static fn (string $path): string => migrationCommand(['tar', '-xOf', '-', '.PKGINFO'], migrationCommand(['gzip', '-dc', $path]));
        $tag = str_replace('.', '', $stream);
        $patch = explode('.', $fullPhpVersion)[2];
        $history = $stream !== '8.5' ? [
            'amqp' => ["2.2.0p{$tag}-r2", "2.2.0p{$tag}.{$patch}_p0-r0"],
            'igbinary' => ["3.2.17~rc1_{$tag}-r1", "3.2.17~rc1p{$tag}-r0", "3.2.17_rc1_p{$tag}-r0"],
        ] : [
            'amqp' => ['2.2.0~dev_85-r1', '2.2.0p85-r2'],
            'igbinary' => ['3.2.17~rc1_85-r1', '3.2.17~rc1p85-r2'],
        ];
        $records = [];
        foreach ($history as $name => $versions) {
            foreach ($versions as $version) {
                $records[] = ['name' => $prefix . '-' . $name, 'version' => $version, 'architecture' => 'x86_64'];
            }
        }
        $baseline = $root . '/baseline.json';
        file_put_contents($baseline, json_encode(['schema' => 1, 'type' => 'apk', 'php_stream' => $stream, 'packages' => $records], JSON_THROW_ON_ERROR));
        putenv('SPP_BASELINE_MANIFESTS=' . json_encode([$baseline], JSON_THROW_ON_ERROR));
        $versions = $revisions = $old = $oldDebug = $future = $cutoffs = [];
        $capability = CreatePackages::getSharedRuntimeCapability();
        $iteration = new ReflectionMethod(CreatePackages::class, 'migrationNextIteration');
        foreach (['amqp', 'igbinary'] as $name) {
            $logical = $prefix . '-' . $name;
            // Hypothetical higher upstream releases verify recovery without asserting they exist yet.
            $proposed = CreatePackages::getTaggedPackageVersion('apk', $name === 'amqp' ? '2.2.1' : '3.2.18');
            $version = CreatePackages::migrationPackageVersion('apk', $logical, $proposed, 'x86_64');
            $revision = $iteration->invoke(null, $logical, $version, 'x86_64', 'apk');
            $versions[$name] = $version;
            $revisions[$name] = $revision;
            $cutoffs[$logical] = $version . '-r' . $revision;
            $config = $configs[$name];
            $config['depends'] = [$prefix . '-cli', $capability];
            $new = $archive($logical, $version, $revision, $config);
            $publish($new);
            $info = $header($new);
            migrationCheck(str_contains($info, 'pkgname = ' . $logical . "\n"), 'Runtime package was renamed');
            migrationCheck(!str_contains($info, 'provides = ' . $logical . '='), 'Runtime uses a versioned self-alias');
            $debug = $archive($logical . '-debuginfo', $version, $revision, $configs[$name . '-debuginfo'], true);
            $publish($debug);
            migrationCheck(str_contains($header($debug), 'depend = ' . $logical . '=' . $version . '-r' . $revision . "\n"), 'Debug dependency lost its exact original runtime identity');
            $future[$name] = $archive($logical, $version, $revision + 1, $config);
            $future[$name . '-debuginfo'] = $archive($logical . '-debuginfo', $version, $revision + 1, $configs[$name . '-debuginfo'], true);
            foreach ($history[$name] as $historical) {
                preg_match('/^(.*)-r(\d+)$/D', $historical, $parts);
                $path = $archive($logical, $parts[1], (int) $parts[2], [
                    'depends' => [$prefix . '-cli'], 'files' => $config['files'], 'config-files' => $config['config-files'],
                ]);
                $old[$name][] = $path;
                $publish($path);
                // APK rejects exact dependencies containing a malformed historical tilde.
                if (!str_contains($historical, '~')) {
                    $debug = $archive($logical . '-debuginfo', $parts[1], (int) $parts[2], [
                        'depends' => [$logical], 'files' => $configs[$name . '-debuginfo']['files'],
                    ], true);
                    $oldDebug[$path] = $debug;
                    $publish($debug);
                }
            }
        }
        $oldCliRevision = $stream !== '8.5' ? 1 : 2;
        $oldCli = $archive($prefix . '-cli', $fullPhpVersion, $oldCliRevision, ['files' => [BUILD_BIN_PATH . '/php' => '/usr/bin/migration-fixture-php']]);
        $publish($oldCli);
        $cutoffPath = $root . '/cutoffs.json';
        file_put_contents($cutoffPath, json_encode(['schema' => 1, 'formats' => ['apk' => $cutoffs]], JSON_THROW_ON_ERROR));
        putenv('SPP_MIGRATION_CUTOFFS=' . $cutoffPath);
        $conflicts = (new ReflectionMethod(CreatePackages::class, 'migrationConflicts'))->invoke(null);
        $link = $root . '/libphp-fixture.so';
        symlink('php-zts/lib/libphp-fixture.so', $link);
        $newCli = $archive($prefix . '-cli', $fullPhpVersion, 100, [
            'provides_unversioned' => [$capability], 'conflicts' => $conflicts,
            'files' => [BUILD_BIN_PATH . '/php' => '/usr/bin/migration-fixture-php', $link => '/usr/lib/libphp-fixture.so'],
        ]);
        $publish($newCli);
        $run = static function (string $script) use ($root): void {
            $container = 'spp-apk-migration-' . bin2hex(random_bytes(6));
            try {
                migrationCommand(['docker', 'run', '--rm', '--memory', '384m', '--cpus', '1', '--network', 'none', '--name', $container,
                    '--volume', $root . ':/task:Z', 'alpine:3.21', 'sh', '-ec', $script]);
            } finally {
                (new Process(['docker', 'rm', '-f', $container]))->run();
            }
        };
        $setup = <<<'SH'
set -eu
apk index --allow-untrusted -o /task/repo/x86_64/APKINDEX.tar.gz /task/repo/x86_64/*.apk >/dev/null
printf '%s\n' /task/repo > /etc/apk/repositories
! tar -xOf /task/repo/x86_64/APKINDEX.tar.gz APKINDEX | grep -E '^P:php-zts-(amqp|igbinary)-shared'
SH;
        $cliCheck = 'apk info -v -e php-zts-cli | grep -Fx ' . escapeshellarg('php-zts-cli-' . $fullPhpVersion . '-r100') . "\n"
            . "test \"\$(readlink /usr/lib/libphp-fixture.so)\" = php-zts/lib/libphp-fixture.so\n";
        $moduleCheck = '';
        foreach ($versions as $name => $version) {
            $moduleCheck .= 'apk info -v -e php-zts-' . $name . ' | grep -Fx ' . escapeshellarg('php-zts-' . $name . '-' . $version . '-r' . $revisions[$name]) . "\n";
        }
        $cases = max(array_map('count', $old));
        for ($index = 0; $index < $cases; $index++) {
            $initial = [$oldCli];
            $debugCheck = '';
            foreach ($old as $name => $paths) {
                $runtime = $paths[$index % count($paths)];
                $initial[] = $runtime;
                if (isset($oldDebug[$runtime])) {
                    $initial[] = $oldDebug[$runtime];
                    $debugCheck .= "apk info -e php-zts-{$name}-debuginfo\n";
                } else {
                    $debugCheck .= "! apk info -e php-zts-{$name}-debuginfo\n";
                }
            }
            $files = implode(' ', array_map(static fn (string $path): string => escapeshellarg('/task/dist/apk/' . basename($path)), $initial));
            $before = implode("\n", array_map(static fn (string $path): string => 'apk info -v | grep -Fx ' . escapeshellarg(basename($path, '.x86_64.apk')), $initial));
            $run($setup . "\napk add --repositories-file /dev/null --allow-untrusted {$files}\n{$before}\n"
                . "sed -i 's/^\\(php-zts-[a-z-]*\\).*/\\1/' /etc/apk/world\ncp /etc/apk/world /tmp/world.before\n"
                . "apk info | sort >/tmp/names.before\napk upgrade --allow-untrusted\n"
                . $cliCheck . $moduleCheck . $debugCheck
                . "apk info | sort >/tmp/names.after\ncmp /tmp/names.before /tmp/names.after\ncmp /tmp/world.before /etc/apk/world\n"
                . "cp /lib/apk/db/installed /tmp/installed.before\napk upgrade --allow-untrusted\ncmp /tmp/installed.before /lib/apk/db/installed\n");
        }
        $run($setup . "\napk add --allow-untrusted php-zts-amqp php-zts-igbinary php-zts-amqp-debuginfo php-zts-igbinary-debuginfo\n"
            . $cliCheck . $moduleCheck . "apk info -e php-zts-amqp-debuginfo\napk info -e php-zts-igbinary-debuginfo\n");
        $run($setup . "\napk add --repositories-file /dev/null --allow-untrusted " . escapeshellarg('/task/dist/apk/' . basename($oldCli))
            . "\nsed -i 's/^php-zts-cli.*/php-zts-cli/' /etc/apk/world\napk upgrade --allow-untrusted\n"
            . $cliCheck . "! apk info -e php-zts-amqp\n! apk info -e php-zts-igbinary\n");
        $currentIndex = 'apk index --allow-untrusted -o /task/repo/x86_64/APKINDEX.tar.gz ' . implode(' ', array_map(
            static fn (string $path): string => escapeshellarg('/task/repo/x86_64/' . basename($path)), glob($root . '/repo/x86_64/*.apk')));
        foreach ($future as $path) { $publish($path); }
        $allIndex = 'apk index --allow-untrusted -o /task/repo/x86_64/APKINDEX.tar.gz /task/repo/x86_64/*.apk';
        $futureCheck = '';
        foreach ($versions as $name => $version) {
            foreach (['', '-debuginfo'] as $debug) {
                $futureCheck .= 'apk info -v -e php-zts-' . $name . $debug . ' | grep -Fx ' . escapeshellarg('php-zts-' . $name . $debug . '-' . $version . '-r' . ($revisions[$name] + 1)) . "\n";
            }
        }
        $run(str_replace($allIndex, $currentIndex, $setup)
            . "\napk add --allow-untrusted php-zts-amqp php-zts-igbinary php-zts-amqp-debuginfo php-zts-igbinary-debuginfo\n"
            . $moduleCheck . "{$allIndex} >/dev/null\napk update --allow-untrusted\napk upgrade --allow-untrusted\n"
            . $futureCheck . "cp /lib/apk/db/installed /tmp/installed.before\napk upgrade --allow-untrusted\ncmp /tmp/installed.before /lib/apk/db/installed\n");
        $held = $old['amqp'][$stream !== '8.5' ? 0 : 1];
        $heldVersion = $history['amqp'][$stream !== '8.5' ? 0 : 1];
        $run($setup . "\napk add --repositories-file /dev/null --allow-untrusted " . escapeshellarg('/task/dist/apk/' . basename($oldCli)) . ' '
            . escapeshellarg('/task/dist/apk/' . basename($held)) . "\n"
            . "sed -i -e 's/^php-zts-cli.*/php-zts-cli/' -e 's/^php-zts-amqp.*/php-zts-amqp={$heldVersion}/' /etc/apk/world\n"
            . "grep -Fx 'php-zts-amqp={$heldVersion}' /etc/apk/world\napk upgrade --allow-untrusted >/tmp/held.log 2>&1 || :\n"
            . 'apk info -v -e php-zts-cli | grep -Fx ' . escapeshellarg('php-zts-cli-' . $fullPhpVersion . '-r' . $oldCliRevision) . "\n"
            . 'apk info -v -e php-zts-amqp | grep -Fx ' . escapeshellarg('php-zts-amqp-' . $heldVersion) . "\n");
        echo "Passed native Alpine {$stream} hypothetical higher-upstream upgrades with unchanged names, debug/fresh/absent/pinned packages, later revisions and repeat no-op upgrades. Metadata fixture only.\n";
    }
} finally {
    $oldTmp === false ? putenv('TMPDIR') : putenv('TMPDIR=' . $oldTmp);
    $oldPath === false ? putenv('PATH') : putenv('PATH=' . $oldPath);
    migrationRemove($root);
}
