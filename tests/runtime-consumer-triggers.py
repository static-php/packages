#!/usr/bin/env python3
"""Exercise native runtime triggers for custom-prefix module-only upgrades."""

import argparse
import json
import os
from pathlib import Path
import subprocess
import tempfile


ROOT = Path(__file__).resolve().parents[1]

GENERATOR = r'''<?php
declare(strict_types=1);
define('BASE_PATH', '/project');
define('TEMP_DIR', '/work/stage');
define('SPP_TYPE', $argv[1]);
require BASE_PATH . '/vendor/autoload.php';
require_once BASE_PATH . '/src/util/RuntimeServices.php';
require_once BASE_PATH . '/src/package/runtime_layout.php';
require_once BASE_PATH . '/src/step/CreatePackages.php';
mkdir(TEMP_DIR, 0755, true);
$layout = new staticphp\package\runtime_layout();
$config = $layout->getFpmConfig();
$type = SPP_TYPE;
$native = php_uname('m');
if ($native !== $argv[3]) {
    throw new RuntimeException('Native trigger validation cannot run through architecture emulation');
}
$arch = $type === 'rpm' ? $native : match ($native) {'x86_64' => 'amd64', 'aarch64' => 'arm64'};
$base = ['fpm', '-s', 'dir', '-t', $type, '--architecture', $arch,
    '--maintainer', 'Runtime trigger fixture <fixture@example.invalid>'];
$layoutArguments = $layout->getFpmExtraArgs();
if (($argv[2] ?? '') === 'omit') {
    $filtered = [];
    for ($index = 0; $index < count($layoutArguments); ++$index) {
        if ($layoutArguments[$index] === '--deb-interest-noawait'
            && ($layoutArguments[$index + 1] ?? '') === '/usr/share/php-zts/consumers') {
            ++$index;
            continue;
        }
        $value = $layoutArguments[$index];
        if (is_file($value)) {
            $contents = file_get_contents($value);
            if (str_contains($contents, '%transfiletriggerin')) {
                $value .= '-without-consumer-interest';
                file_put_contents($value, str_replace(' /usr/share/php-zts/consumers', '', $contents));
            }
        }
        $filtered[] = $value;
    }
    $layoutArguments = $filtered;
}
$arguments = [...$base, '--name', 'php-zts-runtime-layout', '--version', '1', '--iteration', '1',
    '--package', '/work/layout.' . $type, ...$layoutArguments];
foreach ($config['files'] as $source => $destination) {
    $arguments[] = ($destination === '/usr/libexec/php-zts/runtime-services' ? '/work/helper' : $source) . '=' . $destination;
}
foreach ($config['empty_directories'] as $directory) {
    $source = TEMP_DIR . '/empty-' . hash('sha256', $directory);
    mkdir($source);
    $arguments[] = $source . '/=' . $directory;
}
(new Symfony\Component\Process\Process($arguments))->setTimeout(120)->mustRun();
$modules = $type === 'rpm' ? '/usr/lib64/php-zts85/modules' : '/usr/lib/php-zts85/modules';
foreach (['one', 'two'] as $name) {
    $metadata = TEMP_DIR . '/' . $name . '.json';
    file_put_contents($metadata, json_encode(['schema' => 1, 'name' => 'php-zts85-' . $name,
        'php_stream' => '8.5', 'architecture' => $native, 'libraries' => []]) . "\n");
    foreach ([1, 2] as $iteration) {
        $source = TEMP_DIR . '/' . $name . '-' . $iteration . '.so';
        file_put_contents($source, (string) $iteration);
        $arguments = [...$base, '--name', 'php-zts85-' . $name, '--version', '1',
            '--iteration', (string) $iteration, '--package', '/work/' . $iteration . '/' . $name . '.' . $type,
            $source . '=' . $modules . '/' . $name . '.so',
            $metadata . '=/usr/share/php-zts/consumers/php-zts85-' . $name . '.json'];
        (new Symfony\Component\Process\Process($arguments))->setTimeout(120)->mustRun();
    }
}
'''


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--type', choices=['rpm', 'deb'], required=True)
    parser.add_argument('--image', required=True)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--omit-consumer-interest', action='store_true', help='Run the missing-interest negative control')
    args = parser.parse_args()
    native = subprocess.check_output(['docker', 'info', '--format', '{{.Architecture}}'], text=True).strip()
    native = {'amd64': 'x86_64', 'arm64': 'aarch64'}.get(native, native)
    if native not in ['x86_64', 'aarch64']:
        parser.error('Unsupported native Docker architecture: ' + native)
    with tempfile.TemporaryDirectory(prefix='spp-consumer-triggers-') as temporary:
        directory = Path(temporary)
        (directory / 'generate.php').write_text(GENERATOR)
        for generation in ['1', '2']:
            (directory / generation).mkdir()
        modules = '/usr/lib64/php-zts85/modules' if args.type == 'rpm' else '/usr/lib/php-zts85/modules'
        (directory / 'helper').write_text('''#!/bin/sh
set -eu
[ "$1" = finish ]
printf '%s|%s|%s\\n' "$1" "$(cat MODULES/one.so)" "$(cat MODULES/two.so)" >> /work/calls
'''.replace('MODULES', modules))
        (directory / 'helper').chmod(0o755)
        (directory / 'calls').touch()
        install = ('rpm -Uvh /work/layout.rpm /work/1/*.rpm\n: > /work/calls\nrpm -Uvh /work/2/*.rpm'
                   if args.type == 'rpm' else
                   'dpkg -i /work/layout.deb /work/1/*.deb\n: > /work/calls\ndpkg -i /work/2/*.deb')
        owner = '{}:{}'.format(os.getuid(), os.getgid())
        setup = 'set -eu\ntrap \'chown -R "$SPP_FIXTURE_OWNER" /work\' EXIT\n'
        generate = setup + 'php /work/generate.php ' + args.type
        generate += (' omit' if args.omit_consumer_interest else ' normal') + ' ' + native + '\n'
        expected_refresh = not args.omit_consumer_interest or args.type == 'rpm'
        verify = setup + install + '\n[ "$(cat /work/calls)" = "' + ('finish|2|2' if expected_refresh else '') + '" ]\n'
        runtime_image = 'debian:11' if args.type == 'deb' else args.image
        for phase, image, script in [('package', args.image, generate), ('runtime', runtime_image, verify)]:
            container = 'spp-consumer-triggers-' + directory.name.rsplit('-', 1)[-1] + '-' + phase
            project = ['-v', str(ROOT) + ':/project:ro'] if phase == 'package' else []
            try:
                result = subprocess.run(['docker', 'run', '--rm', '--name', container, '--network', 'none',
                    '-e', 'SPP_FIXTURE_OWNER=' + owner, *project, '-v', str(directory) + ':/work',
                    image, 'bash', '-c', script], capture_output=True, text=True, timeout=240)
            finally:
                subprocess.run(['docker', 'exec', container, 'chown', '-R', owner, '/work'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                subprocess.run(['docker', 'rm', '-f', container], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if result.returncode:
                raise RuntimeError(result.stdout + result.stderr)
        proof = {'schema': 1, 'type': args.type, 'image': runtime_image, 'packaging_image': args.image, 'architecture': native,
            'custom_prefix': 'php-zts85', 'module_only_upgrade': True,
            'metadata_content_unchanged': True, 'updated_modules': 2,
            'native_transaction_refreshes': int(expected_refresh),
            'refresh_observed_complete_payload': expected_refresh,
            'omitted_interest_negative_control': args.omit_consumer_interest}
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(proof, indent=2) + '\n')
        print(json.dumps(proof))


if __name__ == '__main__':
    main()
