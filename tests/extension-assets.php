<?php

declare(strict_types=1);

use staticphp\util\BuildPayload;

require dirname(__DIR__) . '/vendor/autoload.php';

function check(bool $condition, string $message): void
{
    if (!$condition) {
        throw new RuntimeException($message);
    }
}

$root = sys_get_temp_dir() . '/spp-extension-assets-' . bin2hex(random_bytes(8));
try {
    foreach (['modules', 'downloads'] as $directory) {
        mkdir($root . '/' . $directory, 0755, true);
    }
    file_put_contents($root . '/modules/spx-zts-85.so', 'module filename fixture');
    $archive = new PharData($root . '/downloads/spx.zip');
    $archive['php-spx/assets/web-ui/index.html'] = 'pinned SPX UI';
    $archive['php-spx/assets/web-ui/js/main.js'] = 'pinned script';
    $archive['php-spx/src/spx.c'] = 'not a package asset';
    unset($archive);
    $source = ['cache_type' => 'archive', 'filename' => 'spx.zip', 'hash' => hash_file('sha1', $root . '/downloads/spx.zip')];
    file_put_contents($root . '/downloads/.cache.json', json_encode(['ext-spx' => ['source' => $source]]));
    file_put_contents($root . '/downloads/pie.phar', 'frozen PIE fixture');
    $pie = ['schema' => 1, 'version' => '1.5.5', 'sha256' => hash_file('sha256', $root . '/downloads/pie.phar')];
    file_put_contents($root . '/downloads/pie.json', json_encode($pie));
    $configuration = ['sapi' => '', 'shared-extensions' => 'spx'];
    $context = ['source_snapshot' => ['records' => ['ext-spx' => $source]]];
    BuildPayload::record($root, $configuration, '8.5.12', php_uname('m'), null, $context);
    BuildPayload::seal($root);
    $cached = BuildPayload::load($root)->metadata();
    BuildPayload::graft($root, $cached);
    check(BuildPayload::load($root)->metadata()['build_context'] === $context, 'Cache graft lost compiled source provenance');
    $bytes = file_get_contents($root . '/downloads/spx.zip');
    file_put_contents($root . '/downloads/spx.zip', 'changed archive', FILE_APPEND);
    try {
        BuildPayload::refreshAssets($root, $root . '/downloads');
        throw new RuntimeException('Changed SPX source was accepted');
    } catch (RuntimeException $error) {
        check(str_contains($error->getMessage(), 'differs from its recorded hash'), $error->getMessage());
    }
    check(!file_exists($root . '/packaging'), 'Failed asset recovery committed an incomplete package tree');
    file_put_contents($root . '/downloads/spx.zip', $bytes);
    $payload = BuildPayload::refreshAssets($root, $root . '/downloads');
    check(file_get_contents($root . '/packaging/spx/web-ui/index.html') === 'pinned SPX UI', 'Legacy cache did not recover pinned SPX UI');
    check(file_get_contents($root . '/packaging/spx/web-ui/js/main.js') === 'pinned script', 'SPX UI nesting was lost');
    check(!file_exists($root . '/packaging/spx/src'), 'Non-asset source files were copied');
    check(isset($payload->metadata()['packaging_assets']['packaging/spx/web-ui/index.html']), 'SPX UI is absent from the sealed asset inventory');
    unlink($root . '/downloads/spx.zip');
    BuildPayload::refreshAssets($root, $root . '/downloads');
    check(file_get_contents($root . '/packaging/spx/web-ui/index.html') === 'pinned SPX UI', 'Cached SPX assets required the source archive again');
    mkdir($root . '/share/misc/php-spx/assets/web-ui', 0755, true);
    file_put_contents($root . '/share/misc/php-spx/assets/web-ui/index.html', 'rebuilt SPX UI');
    BuildPayload::refreshAssets($root, $root . '/downloads');
    check(file_get_contents($root . '/packaging/spx/web-ui/index.html') === 'rebuilt SPX UI', 'Rebuilt SPX kept old cached assets');
    unlink($root . '/modules/spx-zts-85.so');
    BuildPayload::record($root, ['sapi' => '', 'shared-extensions' => ''], '8.5.12', php_uname('m'));
    BuildPayload::seal($root);
    BuildPayload::refreshAssets($root, $root . '/downloads');
    check(!is_dir($root . '/packaging/spx'), 'Skipped SPX retained an old asset tree');
    echo "Passed frozen SPX asset recovery, sealed inventory, reuse, replacement and skipped-module checks.\n";
} finally {
    if (is_dir($root)) {
        $iterator = new RecursiveIteratorIterator(new RecursiveDirectoryIterator($root, FilesystemIterator::SKIP_DOTS), RecursiveIteratorIterator::CHILD_FIRST);
        foreach ($iterator as $file) {
            $file->isDir() && !$file->isLink() ? rmdir($file->getPathname()) : unlink($file->getPathname());
        }
        rmdir($root);
    }
}
