<?php

declare(strict_types=1);

use staticphp\package;
use staticphp\step\CreatePackages;
use Symfony\Component\Process\Process;

require dirname(__DIR__) . '/vendor/autoload.php';

final class LicenseTreeFixture implements package
{
    public function getName(): string { return 'php-zts-cli'; }
    public function getFpmConfig(): array { return []; }
    public function getDebuginfoFpmConfig(): array { return []; }
    public function getFpmExtraArgs(): array { return []; }
    public function getLicense(): string { return 'MIT'; }
    public function getDescription(): string { return 'Nested license packaging regression'; }
}

$root = sys_get_temp_dir() . '/spp-apk-license-' . bin2hex(random_bytes(8));
define('BASE_PATH', dirname(__DIR__));
define('TEMP_DIR', $root . '/tmp');
define('DIST_APK_PATH', $root . '/dist');
define('BUILD_BIN_PATH', $root . '/bin');
foreach ([TEMP_DIR, DIST_APK_PATH, BUILD_BIN_PATH, $root . '/license/one', $root . '/license/two'] as $path) {
    mkdir($path, 0755, true);
}
try {
    file_put_contents($root . '/license/one/0.txt', 'first license');
    file_put_contents($root . '/license/two/0.txt', 'second license');
    symlink('one/0.txt', $root . '/license/current');
    $method = new ReflectionMethod(CreatePackages::class, 'createApkWithNfpm');
    $method->invoke(null, new LicenseTreeFixture(), 'php-zts-cli', '8.4.27', 'x86_64', '1', [
        'files' => [$root . '/license/' => '/usr/share/licenses/php-zts/'],
    ], false);
    $inspect = new Process(['python3', '-c', <<<'PY'
import gzip, sys, tarfile
with gzip.open(sys.argv[1], 'rb') as stream, tarfile.open(fileobj=stream, mode='r|', ignore_zeros=True) as archive:
    found = {}
    for member in archive:
        name = member.name.lstrip('./')
        if name.startswith('usr/share/licenses/php-zts/'):
            found[name] = ('link:' + member.linkname) if member.issym() else archive.extractfile(member).read().decode() if member.isfile() else 'directory'
    assert found['usr/share/licenses/php-zts/one/0.txt'] == 'first license', found
    assert found['usr/share/licenses/php-zts/two/0.txt'] == 'second license', found
    assert found['usr/share/licenses/php-zts/current'] == 'link:one/0.txt', found
    assert 'usr/share/licenses/php-zts/0.txt' not in found, found
PY, DIST_APK_PATH . '/php-zts-cli-8.4.27-r1.x86_64.apk']);
    $inspect->mustRun();
    if (in_array('--native', $argv, true)) {
        $install = new Process([getenv('CONTAINER_ENGINE') ?: 'docker', 'run', '--rm', '-i', '--network', 'none',
            'alpine:3.24', 'sh', '-c', 'cat > /tmp/fixture.apk && apk add --no-network --allow-untrusted /tmp/fixture.apk']);
        $install->setInput(fopen(DIST_APK_PATH . '/php-zts-cli-8.4.27-r1.x86_64.apk', 'rb'));
        $install->mustRun();
        echo $install->getOutput();
    }
    echo "Passed real APK packaging with duplicate license basenames and a preserved symlink.\n";
} finally {
    (new Process(['rm', '-rf', $root]))->mustRun();
}
