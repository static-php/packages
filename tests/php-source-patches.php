<?php

declare(strict_types=1);

use Package\Target\php as PhpTarget;
use staticphp\hook\Php86Performance;
use StaticPHP\Package\TargetPackage;
use Symfony\Component\Process\Process;

require dirname(__DIR__) . '/vendor/autoload.php';
require_once dirname(__DIR__) . '/src/hook/Php86Performance.php';

final class PatchPreflightTarget extends TargetPackage
{
    public function __construct(private string $source)
    {
        parent::__construct('php', 'target');
    }

    public function getSourceDir(): string
    {
        return $this->source;
    }
}

function runPreflight(array $arguments): string
{
    $process = new Process($arguments);
    $process->setTimeout(null);
    $process->mustRun();
    return $process->getOutput();
}

function removePreflight(string $path): void
{
    if (!is_dir($path) || is_link($path)) {
        unlink($path);
        return;
    }
    foreach (new DirectoryIterator($path) as $file) {
        if (!$file->isDot()) {
            removePreflight($file->getPathname());
        }
    }
    rmdir($path);
}

$options = getopt('', ['downloads:', 'versions:']);
$downloads = $options['downloads'] ?? dirname(__DIR__) . '/downloads';
$versions = explode(',', $options['versions'] ?? '8.5.9,8.6.0beta1,8.6.0RC2');
$patch = dirname(__DIR__) . '/config/patches/php86-performance-prs.patch';
preg_match_all('/^diff --git a\/(\S+) b\//m', file_get_contents($patch), $paths);
$temporary = trim(runPreflight(['mktemp', '-d', '/tmp/php-zts-source-patches-XXXXXX']));
try {
    foreach ($versions as $version) {
        if (!preg_match('/^8\.[0-9]+\.[0-9]+(?:(?:alpha|beta|RC)[0-9]+)?$/D', $version)) {
            throw new RuntimeException("Invalid PHP version: {$version}");
        }
        $archives = array_values(array_filter([
            $downloads . '/php-' . $version . '.tar.gz',
            $downloads . '/php-' . $version . '.tar.xz',
        ], 'is_file'));
        if (count($archives) !== 1) {
            throw new RuntimeException("Expected exactly one cached PHP {$version} archive in {$downloads}");
        }
        $source = $temporary . '/' . $version;
        mkdir($source);
        runPreflight(['tar', '-xf', $archives[0], '--strip-components=1', '-C', $source]);
        if (PhpTarget::getPHPVersion($source) !== $version) {
            throw new RuntimeException("PHP source version does not match {$version}");
        }
        $hashes = static function () use ($source, $paths): array {
            $hashes = [];
            foreach ($paths[1] as $file) {
                $hashes[$file] = hash_file('sha256', $source . '/' . $file);
            }
            return $hashes;
        };
        $before = $hashes();
        $target = new PatchPreflightTarget($source);
        $hook = new Php86Performance();
        $applied = $hook->patchBeforeBuildconf($target);
        $eligible = str_starts_with($version, '8.6.') && version_compare($version, '8.6.0RC2', '>=');
        if ($applied !== $eligible || ($hashes() !== $before) !== $eligible) {
            throw new RuntimeException("Unexpected PHP performance patch behavior for {$version}");
        }
        if ($eligible) {
            $after = $hashes();
            if (!$hook->patchBeforeBuildconf($target) || $hashes() !== $after) {
                throw new RuntimeException("PHP {$version} performance patch is not idempotent");
            }
            runPreflight(['patch', '--dry-run', '-p1', '-s', '-f', '-R', '-i', $patch, '-d', $source]);
        }
        echo 'Passed PHP ' . $version . ': ' . ($eligible ? 'performance patch applies and is idempotent' : 'unsupported performance patch leaves source unchanged') . ".\n";
    }
} finally {
    removePreflight($temporary);
}
