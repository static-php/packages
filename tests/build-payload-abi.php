<?php

declare(strict_types=1);

use staticphp\util\BuildPayload;
use Symfony\Component\Process\Process;

require dirname(__DIR__) . '/vendor/autoload.php';
require_once dirname(__DIR__) . '/src/util/BuildPayload.php';

function run(array $arguments): string
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
function remove(string $path): void
{
    if (is_link($path) || !is_dir($path)) {
        unlink($path);
        return;
    }
    foreach (new DirectoryIterator($path) as $entry) {
        if (!$entry->isDot()) {
            remove($entry->getPathname());
        }
    }
    rmdir($path);
}

$root = trim(run(['mktemp', '-d', '/tmp/spp-payload-abi-XXXXXXXX']));
try {
    foreach (['bin', 'lib', 'modules', 'provenance/cached-consumer'] as $directory) {
        mkdir($root . '/' . $directory, 0755, true);
    }
    $compiler = getenv('CC') ?: 'cc';
    file_put_contents($root . '/stable.c', 'int stable(void) { return 42; }');
    file_put_contents($root . '/changing.c', 'int changing(void) { return 1; }');
    file_put_contents($root . '/main.c', 'extern int stable(void); int main(void) { return stable() != 42; }');
    file_put_contents($root . '/module.c', 'extern int changing(void); int module(void) { return changing(); }');
    foreach (['stable', 'changing'] as $name) {
        run([$compiler, '-shared', '-fPIC', '-g', '-Wl,-soname,lib' . $name . '.so.1', $root . '/' . $name . '.c', '-o', $root . '/lib/lib' . $name . '.so.1']);
    }
    run([$compiler, '-g', $root . '/main.c', '-L' . $root . '/lib', '-Wl,-l:libstable.so.1', '-o', $root . '/bin/php']);
    run([$compiler, '-shared', '-fPIC', '-g', $root . '/module.c', '-L' . $root . '/lib', '-Wl,-l:libchanging.so.1', '-o', $root . '/modules/pdo_pgsql-zts-86.so']);
    $manifest = ['schema' => 1, 'linkage' => 'shared', 'libc' => 'glibc', 'architecture' => php_uname('m'), 'libraries' => []];
    foreach (['stable', 'changing'] as $name) {
        $relative = 'lib/lib' . $name . '.so.1';
        $manifest['libraries'][$name] = ['version' => '1.0', 'source_hash' => 'fixture', 'files' => [$relative],
            'elf' => [$relative => ['soname' => basename($relative)]], 'abi_epoch' => 1];
    }
    file_put_contents($root . '/shared-libraries.json', json_encode($manifest));
    $configuration = ['php-version' => '8.6.0RC4', 'extensions' => '', 'shared-extensions' => 'pdo_pgsql', 'sapi' => 'cli',
        'build-options' => ['with-shared-libs' => true, 'with-shared-libdir' => '/usr/lib/php-zts/lib'], 'extra-env' => ['SPC_LIBC' => 'glibc']];
    BuildPayload::record($root, $configuration, '8.6.0RC4', php_uname('m'));
    $cached = BuildPayload::seal($root)->metadata();
    $metadataPath = 'provenance/cached-consumer/build-payload.json';
    file_put_contents($root . '/' . $metadataPath, json_encode($cached));
    $coreHash = hash_file('sha256', $root . '/bin/php');
    $preparedCoreHash = hash_file('sha256', $root . '/' . $cached['prepared']['bin/php']['runtime']['path']);
    $removed = ['modules/pdo_pgsql-zts-86.so'];
    foreach ($cached['prepared']['modules/pdo_pgsql-zts-86.so'] as $entry) {
        $removed[] = $entry['path'];
    }
    $proof = ['schema' => 1, 'cached_metadata_path' => $metadataPath,
        'cached_metadata_sha256' => hash_file('sha256', $root . '/' . $metadataPath),
        'rebuild_packages' => ['pdo_pgsql'], 'incompatible_libraries' => ['changing'],
        'retained_library_abi' => ['stable' => $cached['library_abi']['stable']],
        'removed_files' => $removed, 'library_dependencies' => ['changing' => [], 'stable' => []],
        'retained_dlopen_libraries' => []];
    $manifest['libraries']['changing']['abi_epoch'] = 2;
    file_put_contents($root . '/shared-libraries.json', json_encode($manifest));
    rejected(static fn () => BuildPayload::graft($root, $cached), 'requires a different library ABI');
    rejected(static fn () => BuildPayload::graft($root, $cached, $proof), 'was not removed');
    foreach ($removed as $relative) {
        unlink($root . '/' . $relative);
    }
    $tampered = $proof;
    $tampered['removed_files'][] = 'bin/php';
    rejected(static fn () => BuildPayload::graft($root, $cached, $tampered), 'removed files differ');
    $tampered = $proof;
    $tampered['rebuild_packages'] = ['cli'];
    rejected(static fn () => BuildPayload::graft($root, $cached, $tampered), 'only separately rebuilt');
    $tampered = $proof;
    $tampered['cached_metadata_sha256'] = str_repeat('0', 64);
    rejected(static fn () => BuildPayload::graft($root, $cached, $tampered), 'original sealed consumer metadata');
    $tampered = $proof;
    $tampered['incompatible_libraries'] = [];
    rejected(static fn () => BuildPayload::graft($root, $cached, $tampered), 'changed SDK library ABIs');
    $tampered = $proof;
    $tampered['retained_library_abi'] = [];
    rejected(static fn () => BuildPayload::graft($root, $cached, $tampered), 'retained library identities');
    $tampered = $proof;
    $tampered['library_dependencies']['stable'] = ['changing'];
    rejected(static fn () => BuildPayload::graft($root, $cached, $tampered), 'Retained PHP consumers require');
    $tampered = $proof;
    $tampered['retained_dlopen_libraries'] = ['changing'];
    rejected(static fn () => BuildPayload::graft($root, $cached, $tampered), 'Retained PHP consumers require');
    $manifest['libraries']['stable']['abi_epoch'] = 2;
    file_put_contents($root . '/shared-libraries.json', json_encode($manifest));
    $tampered = $proof;
    $tampered['incompatible_libraries'][] = 'stable';
    rejected(static fn () => BuildPayload::graft($root, $cached, $tampered), 'Retained PHP consumers require');
    $manifest['libraries']['stable']['abi_epoch'] = 1;
    file_put_contents($root . '/shared-libraries.json', json_encode($manifest));
    $payload = BuildPayload::graft($root, $cached, $proof);
    check($payload->isSealed(), 'Scoped ABI graft was not sealed');
    check($payload->metadata()['consumer_cache_abi_reuse_proof'] === $proof, 'Scoped ABI proof was not recorded');
    check(hash_file('sha256', $root . '/bin/php') === $coreHash, 'Scoped ABI graft changed raw core bytes');
    check(hash_file('sha256', $root . '/' . $cached['prepared']['bin/php']['runtime']['path']) === $preparedCoreHash, 'Scoped ABI graft changed prepared core bytes');
    check(!file_exists($root . '/modules/pdo_pgsql-zts-86.so'), 'Scoped ABI graft restored an incompatible module');
    check($payload->metadata()['library_abi']['changing']['epoch'] === 2, 'Graft did not retain the current SDK ABI');
    check($payload->metadata()['consumer_build_provenance']['library_build_provenance'] === $cached['library_build_provenance'], 'Original consumer provenance changed');
    check(hash_file('sha256', $root . '/' . $metadataPath) === $proof['cached_metadata_sha256'], 'Original cached metadata was modified');
    file_put_contents($root . '/proof.json', json_encode($proof));
    run(['php', dirname(__DIR__) . '/bin/build-payload', 'graft', '--root', $root, '--metadata', $root . '/' . $metadataPath, '--reuse-proof', $root . '/proof.json']);
    file_put_contents($root . '/main.c', 'int main(void) { return 0; }');
    run([$compiler, '-g', $root . '/main.c', '-o', $root . '/bin/php']);
    run([$compiler, '-shared', '-fPIC', '-g', $root . '/module.c', '-L' . $root . '/lib', '-Wl,-l:libchanging.so.1', '-o', $root . '/modules/pdo_pgsql-zts-86.so']);
    BuildPayload::record($root, $configuration, '8.6.0RC4', php_uname('m'));
    $cached = BuildPayload::seal($root)->metadata();
    file_put_contents($root . '/' . $metadataPath, json_encode($cached));
    $proof['cached_metadata_sha256'] = hash_file('sha256', $root . '/' . $metadataPath);
    $proof['retained_library_abi'] = [];
    foreach ($proof['removed_files'] as $relative) {
        unlink($root . '/' . $relative);
    }
    $manifest['libraries']['changing']['abi_epoch'] = 3;
    file_put_contents($root . '/shared-libraries.json', json_encode($manifest));
    check(BuildPayload::graft($root, $cached, $proof)->isSealed(), 'A core with no private-library dependency could not be reused');
    echo "Passed scoped ABI graft, unchanged core bytes, original provenance, direct and transitive retained dependency checks.\n";
} finally {
    remove($root);
}
