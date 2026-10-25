<?php

declare(strict_types=1);

use staticphp\util\TwigRenderer;
use Symfony\Component\Process\Process;
use Symfony\Component\Yaml\Yaml;

require dirname(__DIR__) . '/vendor/autoload.php';

$options = getopt('', ['type:']);
if (isset($options['type'])) {
    define('BASE_PATH', dirname(__DIR__));
    define('SPP_TYPE', $options['type']);
    define('SPP_PREFIX', '-zts');
    define('SPP_PHP_VERSION', '8.6');
    define('SPP_TARGET', null);
    function getSharedLibrarySuffix(): string { return '-zts-86'; }
    echo TwigRenderer::renderCraftTemplate('8.6', 'x86_64', null, '8.6.0RC4');
    exit;
}

$configurations = [];
foreach (['rpm', 'deb', 'apk'] as $type) {
    $process = new Process([PHP_BINARY, __FILE__, '--type=' . $type]);
    $process->mustRun();
    $configurations[$type] = Yaml::parse($process->getOutput());
}
if ($configurations['rpm'] !== $configurations['deb']) {
    throw new RuntimeException('Package format changed common glibc compiler flags or compiled lookup paths');
}
$config = $configurations['rpm'];
if ($config['build-options']['with-shared-libdir'] !== '/usr/lib/php-zts/lib'
    || $config['extra-env']['EXTENSION_DIR'] !== '/usr/lib/php-zts/modules'
    || $config['extra-env']['PHP_BUILD_SYSTEM'] !== 'Red Hat Enterprise Linux 8'
    || !str_contains($config['extra-env']['SPC_DEFAULT_CFLAGS'], '_FORTIFY_SOURCE=2')
    || str_contains($config['extra-env']['SPC_DEFAULT_CFLAGS'], '-march=x86-64-v')
    || str_contains($config['extra-env']['SPC_DEFAULT_LDFLAGS'], 'pack-relative-relocs')
    || !str_contains($config['extra-env']['SPC_EXTRA_PHP_VARS'], 'php_cv_func_copy_file_range=yes')) {
    throw new RuntimeException('Common glibc producer lost its EL8 CPU/glibc/loader/configure baseline');
}
if ($configurations['apk']['extra-env']['SPC_LIBC'] !== 'musl'
    || isset($configurations['apk']['extra-env']['OPENSSLDIR'])
    || str_contains($configurations['apk']['extra-env']['SPC_DEFAULT_CFLAGS'], 'redhat-hardened')) {
    throw new RuntimeException('Alpine no longer uses its native musl build profile');
}
echo "Passed identical RPM/DEB EL8 build flags, canonical lookup paths and separate Alpine musl profile.\n";
