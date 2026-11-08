<?php

declare(strict_types=1);

use staticphp\util\BuildPayload;
use Symfony\Component\Process\Process;

require dirname(__DIR__) . '/vendor/autoload.php';
require_once dirname(__DIR__) . '/src/util/BuildPayload.php';
function run(array $arguments): string { $p = new Process($arguments); $p->mustRun(); return $p->getOutput(); }
$root = trim(run(['mktemp', '-d', '/tmp/spp-payload-debuglink-XXXXXXXX']));
try {
    mkdir($root . '/bin'); mkdir($root . '/debug');
    file_put_contents($root . '/main.c', 'int main(void) { return 0; }');
    run(['cc', '-g', '-Wl,--build-id=none', $root . '/main.c', '-o', $root . '/bin/php']);
    run(['objcopy', '--only-keep-debug', $root . '/bin/php', $root . '/debug/php.debug']);
    run(['strip', '--strip-unneeded', $root . '/bin/php']);
    run(['objcopy', '--add-gnu-debuglink=' . $root . '/debug/php.debug', $root . '/bin/php']);
    BuildPayload::record($root, ['sapi' => 'cli', 'build-options' => ['with-config-file-path' => '/etc/php-zts']], '8.6.0RC4', php_uname('m'));
    $payload = BuildPayload::seal($root);
    $prepared = $payload->prepareFile($root . '/bin/php');
    if ($prepared['debug'] === null || !str_contains(run(['readelf', '--sections', $prepared['debug']]), '.debug_info')
        || !str_contains(run(['readelf', '--string-dump=.gnu_debuglink', $prepared['runtime']]), 'php-zts.debug')) {
        throw new RuntimeException('A valid GNU-debuglink CRC companion without a build ID was lost');
    }
    echo "Passed stripped PHP debug companion matching by GNU debuglink CRC when no build ID exists.\n";
} finally {
    run(['rm', '-rf', $root]);
}
