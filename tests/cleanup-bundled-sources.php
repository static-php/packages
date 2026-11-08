<?php

declare(strict_types=1);

use staticphp\hook\CleanupSources;
use StaticPHP\Package\PhpExtensionPackage;
use Symfony\Component\Process\Process;

require dirname(__DIR__) . '/vendor/autoload.php';
require_once dirname(__DIR__) . '/src/hook/CleanupSources.php';
$temporary = new Process(['mktemp', '-d', '/tmp/spp-cleanup-bundled-XXXXXXXX']);
$temporary->mustRun();
$root = trim($temporary->getOutput());
define('SOURCE_PATH', $root . '/source');
putenv('CI=1');
try {
    $directory = SOURCE_PATH . '/php-src/ext/bcmath';
    mkdir($directory . '/.libs', 0755, true);
    file_put_contents($directory . '/config.m4', 'pinned bundled source');
    file_put_contents($directory . '/.libs/bcmath.so', 'reusable object');
    $package = new class($directory) extends PhpExtensionPackage {
        public function __construct(private string $source) { parent::__construct('bcmath', extension_config: ['display-name' => 'bcmath']); }
        public function getSourceDir(): string { return $this->source; }
    };
    (new CleanupSources())->afterBuild($package);
    if (file_get_contents($directory . '/config.m4') !== 'pinned bundled source'
        || file_get_contents($directory . '/.libs/bcmath.so') !== 'reusable object') {
        throw new RuntimeException('Cleanup removed reusable sources or objects nested under php-src');
    }
    echo "Passed preservation of bundled sources and reusable extension objects during CI cleanup.\n";
} finally {
    (new Process(['rm', '-rf', $root]))->mustRun();
}
