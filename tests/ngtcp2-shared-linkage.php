<?php

declare(strict_types=1);

use staticphp\hook\Ngtcp2;
use staticphp\util\SpcAutoload;
use StaticPHP\Package\LibraryPackage;
use StaticPHP\Package\PackageBuilder;

$loader = require dirname(__DIR__) . '/vendor/autoload.php';
SpcAutoload::configure($loader);
require dirname(__DIR__) . '/src/hook/Ngtcp2.php';
$options = getopt('', ['source:']);
$temporary = !isset($options['source']);
$root = $options['source'] ?? sys_get_temp_dir() . '/ngtcp2-linkage-' . bin2hex(random_bytes(8));
$path = $root . '/crypto/ossl/Makefile.in';
if ($temporary) {
    mkdir(dirname($path), 0755, true);
    file_put_contents($path, "OPENSSL_LIBS = @OPENSSL_LIBS@\nlibngtcp2_crypto_ossl_la_LIBADD = ../../lib/libngtcp2.la @OPENSSL_LIBS@\n");
}
try {
    $package = new class($root) extends LibraryPackage {
        public function __construct(private readonly string $source) { parent::__construct('ngtcp2', 'library'); }
        public function getSourceDir(): string { return $this->source; }
    };
    new PackageBuilder(['with-shared-libs' => false]);
    $hook = new Ngtcp2();
    if ($hook->patchSharedOpenSsl($package)) {
        throw new RuntimeException('Static build was modified');
    }
    new PackageBuilder(['with-shared-libs' => true]);
    if (!$hook->patchSharedOpenSsl($package) || $hook->patchSharedOpenSsl($package)
        || str_contains(file_get_contents($path), '@OPENSSL_LIBS@')
        || !str_contains(file_get_contents($path), '-L$(libdir) -lssl -lcrypto')) {
        throw new RuntimeException('Shared OpenSSL link patch failed or was not idempotent');
    }
    echo "Passed ngtcp2 shared-link patch, static-build isolation and idempotence.\n";
} finally {
    if ($temporary) {
        unlink($path);
        rmdir(dirname($path));
        rmdir($root . '/crypto');
        rmdir($root);
    }
}
