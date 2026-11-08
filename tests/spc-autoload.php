<?php

declare(strict_types=1);

use Composer\Autoload\ClassLoader;
use staticphp\util\SpcAutoload;

require dirname(__DIR__) . '/vendor/autoload.php';
require dirname(__DIR__) . '/src/util/SpcAutoload.php';

function verify(bool $condition, string $message): void
{
    if (!$condition) {
        throw new RuntimeException($message);
    }
}

function cleanTree(string $path): void
{
    if (!is_dir($path) || is_link($path)) {
        unlink($path);
        return;
    }
    foreach (new DirectoryIterator($path) as $file) {
        if (!$file->isDot()) {
            cleanTree($file->getPathname());
        }
    }
    rmdir($path);
}

$process = new Symfony\Component\Process\Process(['mktemp', '-d', '/tmp/php-zts-spc-autoload-XXXXXX']);
$process->mustRun();
$root = trim($process->getOutput());
$loader = new ClassLoader();
try {
    foreach (['StaticPHP/Config', 'StaticPHP/Util', 'Package/Library'] as $directory) {
        mkdir($root . '/src/' . $directory, 0755, true);
    }
    file_put_contents($root . '/src/StaticPHP/Config/LocalOverrideProbe.php', '<?php namespace StaticPHP\\Config; final class LocalOverrideProbe { public const SOURCE = "local"; }');
    file_put_contents($root . '/src/StaticPHP/Util/NewLocalOverrideProbe.php', '<?php namespace StaticPHP\\Util; final class NewLocalOverrideProbe {}');
    file_put_contents($root . '/src/Package/Library/LocalOverrideProbe.php', '<?php namespace Package\\Library; final class LocalOverrideProbe {}');
    $loader->addClassMap([
        'StaticPHP\\Config\\LocalOverrideProbe' => '/old/spc/src/StaticPHP/Config/LocalOverrideProbe.php',
        'Package\\Library\\LocalOverrideProbe' => '/old/spc/src/Package/Library/LocalOverrideProbe.php',
        'staticphp\\util\\ProjectProbe' => '/project/src/util/ProjectProbe.php',
    ]);
    $loader->setClassMapAuthoritative(true);
    $loader->register(true);
    SpcAutoload::configure($loader, $root);
    verify($loader->findFile('StaticPHP\\Config\\LocalOverrideProbe') === $root . '/src/StaticPHP/Config/LocalOverrideProbe.php', 'Optimized SPC class map was not replaced');
    verify($loader->findFile('Package\\Library\\LocalOverrideProbe') === $root . '/src/Package/Library/LocalOverrideProbe.php', 'Optimized recipe class map was not replaced');
    verify($loader->findFile('staticphp\\util\\ProjectProbe') === '/project/src/util/ProjectProbe.php', 'Package project namespace was overwritten');
    verify($loader->findFile('StaticPHP\\Util\\NewLocalOverrideProbe') === $root . '/src/StaticPHP/Util/NewLocalOverrideProbe.php', 'New checkout class was hidden by authoritative class map');
    verify(StaticPHP\Config\LocalOverrideProbe::SOURCE === 'local', 'SPC override loaded a vendor class');
    verify((new ReflectionClass(Package\Library\LocalOverrideProbe::class))->getFileName() === $root . '/src/Package/Library/LocalOverrideProbe.php', 'Recipe override loaded a vendor class');
    $other = $root . '/other';
    mkdir($other . '/src/StaticPHP', 0755, true);
    mkdir($other . '/src/Package', 0755, true);
    try {
        SpcAutoload::configure($loader, $other);
        throw new RuntimeException('Accepted a mixed loaded SPC implementation');
    } catch (RuntimeException $error) {
        verify(str_contains($error->getMessage(), 'before loading'), 'Unexpected override failure: ' . $error->getMessage());
    }
    echo "Passed local SPC class maps, new classes, recipe loading and mixed-version rejection.\n";
} finally {
    $loader->unregister();
    cleanTree($root);
}
