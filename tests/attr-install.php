<?php

declare(strict_types=1);

use staticphp\hook\Attr;
use staticphp\util\SpcAutoload;
use StaticPHP\Package\LibraryPackage;
use Symfony\Component\Process\Process;

$loader = require dirname(__DIR__) . '/vendor/autoload.php';
SpcAutoload::configure($loader);
require dirname(__DIR__) . '/src/hook/Attr.php';
$options = getopt('', ['source:']);
$temporary = !isset($options['source']);
$root = $options['source'] ?? sys_get_temp_dir() . '/attr-install-' . bin2hex(random_bytes(8));
$path = $root . '/Makefile.am';
if ($temporary) {
    mkdir($root, 0755, true);
    file_put_contents($path, <<<'MAKE'
.PHONY: install-attributes_h install-data
install-attributes_h:
	@mkdir header.lock
	@sleep 0.05
	@rmdir header.lock
install-data:
	@$(MAKE) --no-print-directory -f Makefile.am install-attributes_h
MAKE);
}
try {
    $package = new class($root) extends LibraryPackage {
        public function __construct(private readonly string $source) { parent::__construct('attr', 'library'); }
        public function getSourceDir(): string { return $this->source; }
    };
    $hook = new Attr();
    if (!$hook->serializeInstallation($package) || $hook->serializeInstallation($package)) {
        throw new RuntimeException('attr installation patch was not applied exactly once');
    }
    if ($temporary) {
        for ($attempt = 0; $attempt < 5; ++$attempt) {
            (new Process(['make', '--no-print-directory', '-j8', '-f', 'Makefile.am', 'install-attributes_h', 'install-data'], $root))->mustRun();
        }
    }
    echo "Passed attr overlapping installation targets and patch idempotence.\n";
} finally {
    if ($temporary) {
        @rmdir($root . '/header.lock');
        unlink($path);
        rmdir($root);
    }
}
