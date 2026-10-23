<?php

use staticphp\Command\TestCommand;
use staticphp\step\CreatePackages;
use Symfony\Component\Process\Process;

require dirname(__DIR__) . '/vendor/autoload.php';

$options = getopt('', ['type:', 'matrix-json']);
$types = isset($options['type']) ? [$options['type']] : ['rpm', 'deb', 'apk'];
foreach ($types as $type) {
    if (!in_array($type, ['rpm', 'deb', 'apk'], true)) {
        throw new RuntimeException("Unknown package type: {$type}");
    }
}

$runtime = new ReflectionProperty(CreatePackages::class, 'versionArch');
$command = (new ReflectionClass(TestCommand::class))->newInstanceWithoutConstructor();
$compare = new ReflectionMethod(TestCommand::class, 'comparePackageVersions');
$checks = 0;
$matrices = [];
$parseExtensionVersion = new ReflectionMethod(CreatePackages::class, 'parseExtensionVersion');
foreach ([
    '3.6.0alpha2-dev' => '3.6.0~alpha2~~dev',
    '3.6.0-beta1-dev2' => '3.6.0~beta1~~dev2',
    '3.6.0.RC10.dev9' => '3.6.0~rc10~~dev9',
    '3.6.0RC10dev10' => '3.6.0~rc10~~dev10',
    '5.1.29-dev' => '5.1.29~~dev',
    '1.1.4dev' => '1.1.4~~dev',
] as $raw => $normalized) {
    if ($parseExtensionVersion->invoke(null, 'version: ' . $raw) !== $normalized
        || CreatePackages::normalizeVersion($raw) !== $normalized
        || CreatePackages::normalizeVersion($normalized) !== $normalized) {
        throw new RuntimeException("Incorrect extension version normalization: {$raw}");
    }
}

$version = static function (string $type, string $extension, string $php) use ($runtime): string {
    static $versions = [];
    $key = $type . '|' . $extension . '|' . $php;
    if (isset($versions[$key])) {
        return $versions[$key];
    }
    $runtime->setValue(null, [CreatePackages::normalizeVersion($php), 'x86_64']);
    $version = CreatePackages::getTaggedPackageVersion($type, $extension);
    if ($type === 'apk') {
        $process = new Process(['apk', 'version', '-c', $version]);
        $process->mustRun();
        if (trim($process->getOutput()) !== '') {
            throw new RuntimeException("Invalid APK version: {$version}");
        }
    }
    return $versions[$key] = $version;
};

$less = static function (string $type, string $older, string $newer) use ($compare, $command, &$checks): void {
    if ($compare->invoke($command, $type, $older, $newer) !== -1) {
        throw new RuntimeException("Expected {$type}: {$older} < {$newer}");
    }
    $checks++;
};

$expected = ['rpm' => '3.6.0~alpha1_86.0~rc3', 'deb' => '3.6.0~alpha1+php86.0~rc3', 'apk' => '3.6.0_alpha1_p86_p0_rc3'];
$phpStages = ['8.6.0-dev', '8.6.0alpha0', '8.6.0alpha1', '8.6.0alpha2-dev', '8.6.0alpha2', '8.6.0beta1-dev2', '8.6.0beta1', '8.6.0beta2', '8.6.0beta3', '8.6.0RC1-dev', '8.6.0RC1', '8.6.0RC2', '8.6.0RC3', '8.6.0'];
$extensionStages = ['3.6.0dev', '3.6.0dev2', '3.6.0alpha0', '3.6.0alpha1', '3.6.0alpha2-dev', '3.6.0alpha2-dev2', '3.6.0alpha2', '3.6.0beta1-dev', '3.6.0beta1', '3.6.0RC1-dev', '3.6.0RC1', '3.6.0RC2', '3.6.0'];
$legacyXdebug = [
    'rpm' => ['3.6.0~dev_86~beta1', '3.6.0~dev_86~beta2', '3.6.0~alpha2_86~beta3', '3.6.0~alpha1_86~rc2', '3.6.0~alpha1_86~rc3'],
    'deb' => ['3.6.0~dev+php86~beta1-1', '3.6.0~dev+php86~beta2-1', '3.6.0~alpha2+php86~beta3-1', '3.6.0~alpha1+php86~rc2-1', '3.6.0~alpha1+php86~rc2-2', '3.6.0~alpha1+php86~rc3-1'],
    'apk' => ['3.6.0_pre_beta1_p86-r0', '3.6.0_pre_beta2_p86-r0', '3.6.0_alpha2_beta3_p86-r0', '3.6.0_alpha1_rc2_p86-r0', '3.6.0_alpha1_rc3_p86-r0'],
];

foreach ($types as $type) {
    $revision = match ($type) {'deb' => '-1', 'apk' => '-r0', default => ''};
    $stableTag = match ($type) {'deb' => '+php82.35', 'apk' => 'p82.35', default => '_82.35'};
    foreach (['1.13.1', '0.13.0'] as $upstream) {
        $stable = $version($type, $upstream, '8.2.35');
        if ($stable !== $upstream . $stableTag) {
            throw new RuntimeException("Unexpected stable {$type} version: {$stable}");
        }
        $published = $stable . ($type === 'apk' ? '_p0' : '+ext');
        $less($type, $stable . $revision, $published . $revision);
        $nextUpstream = $upstream === '1.13.1' ? '1.13.2' : '0.13.1';
        $less($type, $published . $revision, $version($type, $nextUpstream, '8.2.35') . $revision);
        $less($type, $published . $revision, $version($type, $upstream, '8.2.36') . $revision);
    }
    $latest = $version($type, '3.6.0alpha1', '8.6.0RC3');
    if ($latest !== $expected[$type]) {
        throw new RuntimeException("Unexpected {$type} version: {$latest}");
    }
    $developmentExpected = match ($type) {
        'rpm' => '3.6.0~alpha2~~dev0_86.0~rc4',
        'deb' => '3.6.0~alpha2~~dev0+php86.0~rc4',
        'apk' => '3.6.0_alpha2_alpha0_alpha00_p86_p0_rc4',
    };
    $development = $version($type, '3.6.0alpha2-dev', '8.6.0RC4');
    if ($development !== $developmentExpected) {
        throw new RuntimeException("Unexpected development {$type} version: {$development}");
    }
    $release = $version($type, '3.6.0', '8.6.0RC3');
    foreach ($legacyXdebug[$type] as $older) {
        $less($type, $older, $release . $revision);
    }

    foreach ($phpStages as $php) {
        foreach (array_slice($extensionStages, 1) as $i => $extension) {
            $less($type, $version($type, $extensionStages[$i], $php), $version($type, $extension, $php));
        }
    }
    foreach (array_slice($phpStages, 1) as $i => $php) {
        foreach ($extensionStages as $extension) {
            $less($type, $version($type, $extension, $phpStages[$i]), $version($type, $extension, $php));
        }
    }
    $less($type, $version($type, '3.6.0alpha1', '8.6.0'), $version($type, '3.6.0alpha2', '8.6.0RC3'));

    foreach (['8.2.30', '8.3.30', '8.4.20', '8.5.10'] as $php) {
        $suffix = str_replace('.', '', substr($php, 0, 3));
        $legacy = match ($type) {'deb' => "5.1.28+php{$suffix}", 'apk' => "5.1.28p{$suffix}", default => "5.1.28_{$suffix}"};
        $less($type, $legacy, $version($type, '5.1.28', $php));
        $less($type, $version($type, '3.6.0dev', $php), $version($type, '3.6.0alpha1', $php));
        foreach ($legacyXdebug[$type] as $older) {
            $oldSuffix = $type === 'deb' ? 'php86' : ($type === 'apk' ? 'p86' : '_86');
            $newSuffix = $type === 'deb' ? 'php' . $suffix : ($type === 'apk' ? 'p' . $suffix : '_' . $suffix);
            $less($type, str_replace($oldSuffix, $newSuffix, $older), $version($type, '3.6.0', $php) . $revision);
        }
    }

    $phaseVersions = [];
    foreach (['dev', 'alpha1', 'beta1', 'RC1', ''] as $extensionPhase) {
        foreach (['dev', 'alpha1', 'beta1', 'RC1', ''] as $phpPhase) {
            $new = $version($type, '3.6.0' . $extensionPhase, '8.6.0' . $phpPhase);
            $phaseVersions[] = $new;
            $phpMarker = $phpPhase === '' ? '' : '~' . strtolower($phpPhase);
            $extensionMarker = $extensionPhase === '' ? '' : '~' . strtolower($extensionPhase);
            $tag = match ($type) {
                'deb' => '+php86' . $phpMarker,
                'rpm' => '_86' . $phpMarker,
                'apk' => $phpMarker . ($extensionMarker . $phpMarker === '' ? 'p86' : '_p86'),
            };
            $old = '3.6.0' . $extensionMarker . $tag;
            if ($type === 'apk') {
                $old = str_replace(['~dev', '~'], ['_pre', '_'], $old);
            }
            $less($type, $old . $revision, $release . $revision);
            $matrices[$type][] = ['php' => $phpPhase ?: 'release', 'extension' => $extensionPhase ?: 'release', 'before' => $old, 'after' => $new];
        }
    }
    foreach ($phaseVersions as $i => $older) {
        foreach (array_slice($phaseVersions, $i + 1) as $newer) {
            $less($type, $older, $newer);
        }
    }
    $less($type, $version($type, '3.6.0alpha1', '8.6.0'), $version($type, '3.6.1dev', '8.6.0'));
    $less($type, $version($type, '3.6.0dev', '8.6.0'), $version($type, '3.6.0dev', '9.0.0alpha1'));
    $less($type, $version($type, '3.6.0dev', '9.9.0'), $version($type, '3.6.0dev', '10.0.0alpha1'));
    $less($type, $version($type, '3.6.0dev', '8.6.0'), $version($type, '3.6.0dev', '8.6.1alpha1'));
    $less($type, $version($type, '3.6.0dev', '8.6.9'), $version($type, '3.6.0dev', '8.6.10alpha1'));
    foreach (['alpha', 'beta', 'RC'] as $phase) {
        $less($type, $version($type, '3.6.0' . $phase . '9', '8.6.0RC3'), $version($type, '3.6.0' . $phase . '10', '8.6.0RC3'));
        $less($type, $version($type, '3.6.0dev', '8.6.0' . $phase . '9'), $version($type, '3.6.0dev', '8.6.0' . $phase . '10'));
        $less($type, $version($type, '3.6.0' . $phase . '1', '9.9.9'), $version($type, '3.6.0' . $phase . '2-dev', '8.6.0RC4'));
        $less($type, $version($type, '3.6.0' . $phase . '2-dev', '9.9.9'), $version($type, '3.6.0' . $phase . '2-dev2', '8.6.0RC4'));
        $less($type, $version($type, '3.6.0' . $phase . '2-dev9', '9.9.9'), $version($type, '3.6.0' . $phase . '2-dev10', '8.6.0RC4'));
        $less($type, $version($type, '3.6.0' . $phase . '2-dev10', '9.9.9'), $version($type, '3.6.0' . $phase . '2', '8.6.0RC4'));
    }
}

if (isset($options['matrix-json'])) {
    echo json_encode(['checks' => $checks, 'matrix' => $matrices], JSON_PRETTY_PRINT | JSON_THROW_ON_ERROR) . "\n";
} else {
    echo "Passed {$checks} native package-version comparisons (" . implode(', ', $types) . ").\n";
}
