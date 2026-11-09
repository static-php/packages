<?php

declare(strict_types=1);

use staticphp\util\BuildPayload;
use Symfony\Component\Process\Process;

require dirname(__DIR__) . '/vendor/autoload.php';
require_once dirname(__DIR__) . '/src/util/BuildPayload.php';

function command(array $arguments): string
{
    $process = new Process($arguments);
    $process->setTimeout(null);
    $process->mustRun();
    return $process->getOutput();
}
function check(bool $condition, string $message): void
{
    if (!$condition) {
        throw new RuntimeException($message);
    }
}
function rejected(callable $operation, string $message): void
{
    try {
        $operation();
    } catch (RuntimeException $error) {
        check(str_contains($error->getMessage(), $message), 'Unexpected rejection: ' . $error->getMessage());
        return;
    }
    throw new RuntimeException('Expected rejection: ' . $message);
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

$root = trim(command(['mktemp', '-d', '/tmp/spp-build-payload-XXXXXXXX']));
try {
    mkdir($root . '/bin');
    mkdir($root . '/lib');
    mkdir($root . '/packaging');
    file_put_contents($root . '/answer.c', 'int answer(void) { return 42; }');
    file_put_contents($root . '/main.c', 'extern int answer(void); int main(void) { return answer() != 42; }');
    file_put_contents($root . '/packaging/Caddyfile', 'localhost { respond "Hello" }');
    mkdir($root . '/include/php/Zend', 0755, true);
    file_put_contents($root . '/include/php/Zend/zend_modules.h', "#define ZEND_MODULE_API_NO 20260922\n");
    $compiler = getenv('CC') ?: 'cc';
    command([$compiler, '-shared', '-fPIC', '-g', '-Wl,-soname,libanswer.so.1', '-Wl,--enable-new-dtags,-rpath,/usr/lib/php-zts/lib', $root . '/answer.c', '-o', $root . '/lib/libanswer.so.1']);
    mkdir($root . '/modules');
    copy($root . '/lib/libanswer.so.1', $root . '/modules/curl-zts-86.so');
    symlink('libanswer.so.1', $root . '/lib/libanswer.so');
    command([$compiler, '-g', $root . '/main.c', '-L' . $root . '/lib', '-lanswer', '-Wl,--enable-new-dtags,-rpath,/usr/lib/php-zts/lib', '-o', $root . '/bin/php']);
    $manifest = ['schema' => 1, 'linkage' => 'shared', 'libc' => 'glibc', 'architecture' => php_uname('m'), 'libraries' => [
        'openssl' => ['version' => '1.0', 'source_hash' => 'fixture', 'files' => ['lib/libanswer.so.1'], 'elf' => ['lib/libanswer.so.1' => ['soname' => 'libanswer.so.1']], 'abi_epoch' => 1],
    ]];
    file_put_contents($root . '/shared-libraries.json', json_encode($manifest));
    $configuration = ['php-version' => '8.6.0RC4', 'extensions' => 'openssl', 'shared-extensions' => 'curl,fileinfo', 'sapi' => 'cli', 'build-options' => ['with-shared-libs' => true, 'with-shared-libdir' => '/usr/lib/php-zts/lib', 'with-config-file-path' => '/etc/php-zts'], 'extra-env' => ['SPC_LIBC' => 'glibc']];
    $architecture = php_uname('m');
    $sourceHash = hash_file('sha256', $root . '/bin/php');
    BuildPayload::record($root, $configuration, '8.6.0RC4', $architecture);
    $payload = BuildPayload::seal($root);
    check($payload->isSealed() && $payload->phpVersion() === '8.6.0RC4', 'Sealed build identity lost');
    check($payload->configuration() === $configuration, 'Recorded build configuration changed');
    check($payload->metadata()['module_api'] === '20260922', 'Recorded PHP module ABI is missing');
    $prepared = $payload->prepareFile($root . '/bin/php');
    check($sourceHash === hash_file('sha256', $root . '/bin/php'), 'Sealing changed SDK ABI input');
    check($prepared['runtime'] === $root . '/payload/runtime/bin/php', 'Prepared runtime path is unstable');
    check(basename($prepared['debug']) === 'php-zts.debug', 'Prepared debug name differs from installed CLI');
    check(!str_contains(command(['readelf', '--sections', $prepared['runtime']]), '.debug_info'), 'Prepared runtime retains DWARF');
    check(str_contains(command(['readelf', '--string-dump=.gnu_debuglink', $prepared['runtime']]), 'php-zts.debug'), 'Prepared debuglink does not match installed debugger name');
    check(str_contains(command(['readelf', '--sections', $prepared['debug']]), '.debug_info'), 'Detached debug information is missing');
    define('BUILD_ROOT_PATH', $root);
    define('BUILD_BIN_PATH', $root . '/bin');
    define('BUILD_MODULES_PATH', $root . '/modules');
    function getBinarySuffix(): string { return '-zts'; }
    function getSharedLibrarySuffix(): string { return '-zts-86'; }
    function getModuledir(): string { return '/usr/lib/php-zts/modules'; }
    $debugConfig = (new \staticphp\package\cli())->getDebuginfoFpmConfig();
    check(!is_file($root . '/debug/php.debug'), 'Fixture must exercise sealed debug files without legacy paths');
    check(($debugConfig['files'][$prepared['debug']] ?? null) === '/usr/lib/debug/usr/bin/php-zts.debug', 'Sealed CLI debuginfo was omitted from packaging');
    $extension = new class extends \staticphp\extension {
        public function __construct() {}
        public function isSharedExtension(): bool { return true; }
    };
    (new ReflectionProperty(\staticphp\extension::class, 'name'))->setValue($extension, 'curl');
    $module = $payload->prepareFile($root . '/modules/curl-zts-86.so');
    check(!is_file($root . '/debug/curl-zts-86.so.debug'), 'Fixture must omit legacy extension debug paths');
    check(($extension->getDebuginfoFpmConfig()['files'][$module['debug']] ?? null) === '/usr/lib/debug/usr/lib/php-zts/modules/curl-zts-86.so.debug', 'Sealed extension debuginfo was omitted from packaging');
    $sealedHash = hash_file('sha256', $prepared['runtime']);
    $before = filemtime($prepared['runtime']);
    BuildPayload::seal($root);
    check(hash_file('sha256', $prepared['runtime']) === $sealedHash && filemtime($prepared['runtime']) === $before, 'Sealing twice repeated runtime mutation');
    BuildPayload::record($root, $configuration, '8.6.0RC4', $architecture);
    BuildPayload::seal($root);
    check(hash_file('sha256', $prepared['runtime']) === $sealedHash && filemtime($prepared['runtime']) === $before, 'Unchanged library re-recording discarded prepared bytes');
    mkdir($root . '/downloads');
    file_put_contents($root . '/downloads/pie.phar', 'frozen PIE fixture');
    $pie = ['schema' => 1, 'version' => '1.5.3', 'sha256' => hash_file('sha256', $root . '/downloads/pie.phar')];
    file_put_contents($root . '/downloads/pie.json', json_encode($pie));
    file_put_contents($root . '/downloads/.spc-meta.json', json_encode(['pie_version' => $pie['version'], 'pie_sha256' => $pie['sha256']]));
    $elfLedger = BuildPayload::load($root)->metadata()['files'];
    $assetPayload = BuildPayload::refreshAssets($root, $root . '/downloads');
    check($assetPayload->metadata()['files'] === $elfLedger && hash_file('sha256', $prepared['runtime']) === $sealedHash, 'PIE-only refresh changed PHP/library ELF bytes');
    check($assetPayload->metadata()['packaging_assets']['packaging/pie/pie.phar']['sha256'] === $pie['sha256'], 'Frozen PIE hash was not recorded');
    check($assetPayload->metadata()['asset_updates'][0]['version'] === '1.5.3', 'PIE source update provenance is missing');
    BuildPayload::refreshAssets($root, $root . '/downloads');
    check(count(BuildPayload::load($root)->metadata()['asset_updates']) === 1, 'Unchanged package assets should keep the sealed payload unchanged');
    file_put_contents($root . '/downloads/pie.phar', 'wrong asset', FILE_APPEND);
    rejected(static fn () => BuildPayload::refreshAssets($root, $root . '/downloads'), 'does not match');
    check(hash_file('sha256', $root . '/packaging/pie/pie.phar') === $pie['sha256'], 'Rejected PIE source changed the sealed package asset');
    $cached = BuildPayload::load($root)->metadata();
    $originalBinary = file_get_contents($root . '/bin/php');
    file_put_contents($root . '/bin/php', 'wrong cached binary', FILE_APPEND);
    rejected(static fn () => BuildPayload::graft($root, $cached), 'Cached PHP consumer bytes were not restored');
    file_put_contents($root . '/bin/php', $originalBinary);
    file_put_contents($root . '/answer.c', 'int answer(void) { return 43; }');
    command([$compiler, '-shared', '-fPIC', '-g', '-Wl,-soname,libanswer.so.1', '-Wl,--enable-new-dtags,-rpath,/usr/lib/php-zts/lib', $root . '/answer.c', '-o', $root . '/lib/libanswer.so.1']);
    $payload = BuildPayload::graft($root, $cached);
    check(hash_file('sha256', $prepared['runtime']) === $sealedHash, 'Compatible library graft recompiled/changed the PHP consumer');
    check(isset($payload->metadata()['consumer_build_provenance']), 'Consumer graft lost original build provenance');
    $variant = ['schema' => 1, 'profile' => 'rpm', 'input_fingerprint' => str_repeat('a', 64), 'overlay_receipt_sha256' => str_repeat('b', 64), 'modified_files' => [], 'files' => [
        'lib/libanswer.so.1' => ['sha256' => hash_file('sha256', $root . '/lib/libanswer.so.1'), 'mode' => fileperms($root . '/lib/libanswer.so.1') & 0777],
    ]];
    file_put_contents($root . '/openssl-variant.json', json_encode($variant));
    $payload = BuildPayload::seal($root);
    check($payload->metadata()['openssl_variant']['profile'] === 'rpm', 'An unchanged RPM overlay was not recorded');
    file_put_contents($root . '/bin/phpize', "#!/bin/sh\nexit 0\n");
    file_put_contents($root . '/bin/php-config', "#!/bin/sh\nexit 0\n");
    copy($root . '/lib/libanswer.so.1', $root . '/lib/libphp-zts-86.so');
    symlink('libphp-zts-86.so', $root . '/lib/libphp.so');
    mkdir($root . '/provenance/cached-consumer', 0755, true);
    $metadataPath = $root . '/provenance/cached-consumer/build-payload.json';
    file_put_contents($metadataPath, json_encode($payload->metadata()));
    $coreFiles = [];
    foreach (['bin/php', 'bin/phpize', 'bin/php-config', 'include/php/Zend/zend_modules.h', 'lib/libphp-zts-86.so'] as $relative) {
        $coreFiles[$relative] = ['sha256' => hash_file('sha256', $root . '/' . $relative), 'mode' => fileperms($root . '/' . $relative) & 0777];
    }
    $coreFiles['lib/libphp.so'] = ['symlink' => 'libphp-zts-86.so'];
    $receipt = ['schema' => 1, 'state' => 'verified', 'key' => str_repeat('a', 64), 'seed_key' => str_repeat('b', 64),
        'php_stream' => '8.6', 'rebuild_packages' => ['frankenphp'], 'changed_components' => ['frankenphp'],
        'cached_metadata_path' => 'provenance/cached-consumer/build-payload.json', 'cached_metadata_sha256' => hash_file('sha256', $metadataPath), 'core_files' => $coreFiles];
    file_put_contents($root . '/consumer-cache-seed.json', json_encode($receipt));
    check(BuildPayload::verifiedCoreSeed($root, ['frankenphp'], '8.6.0RC4') !== null, 'Verified partial-build core seed was refused');
    rejected(static fn () => BuildPayload::verifiedCoreSeed($root, ['imagick'], '8.6.0RC4'), 'matching verified');
    rejected(static fn () => BuildPayload::verifiedCoreSeed($root, ['frankenphp'], '8.6.0RC5'), 'different PHP source');
    file_put_contents($root . '/bin/php-config', 'changed', FILE_APPEND);
    rejected(static fn () => BuildPayload::verifiedCoreSeed($root, ['frankenphp'], '8.6.0RC4'), 'Cached core input changed');
    unlink($root . '/bin/php-config');
    unlink($root . '/bin/phpize');
    unlink($root . '/lib/libphp.so');
    unlink($root . '/lib/libphp-zts-86.so');
    $originalAssets = file_get_contents($root . '/packaging/Caddyfile');
    file_put_contents($root . '/packaging/Caddyfile', 'changed');
    rejected(static fn () => BuildPayload::load($root), 'package assets changed');
    file_put_contents($root . '/packaging/Caddyfile', $originalAssets);
    $payload = BuildPayload::load($root);
    file_put_contents($prepared['runtime'], 'changed', FILE_APPEND);
    rejected(static fn () => $payload->verify(), 'bytes changed');
    rejected(static fn () => $payload->prepareFile($root . '/bin/php'), 'changed before packaging');
    BuildPayload::seal($root);
    file_put_contents($root . '/bin/php', 'changed', FILE_APPEND);
    $variant['modified_files'][] = 'bin/php';
    $variant['files']['bin/php'] = ['sha256' => hash_file('sha256', $root . '/bin/php'), 'mode' => fileperms($root . '/bin/php') & 0777];
    file_put_contents($root . '/openssl-variant.json', json_encode($variant));
    rejected(static fn () => BuildPayload::seal($root), 'cannot change common ELF');
    echo "Passed build identity, ELF/debug sealing, consumer graft, variant selection, verified partial-core seeds and immutability.\n";
} finally {
    removeFixture($root);
}
