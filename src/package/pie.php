<?php

namespace staticphp\package;

use RuntimeException;
use staticphp\package;
use staticphp\step\CreatePackages;
use staticphp\util\BuildPayload;
use staticphp\util\TwigRenderer;

class pie implements package
{
    public function getName(): string
    {
        // Return pie with the suffix (e.g., "pie-zts", "pie-zts8.5", "pie-zts85")
        return 'pie' . getBinarySuffix();
    }

    public function getVersion(): string
    {
        return $this->frozenArtifact()[1];
    }

    public function getFpmConfig(): array
    {
        [$pharSource, $wrapperSource] = $this->prepareArtifacts();

        $prefix = CreatePackages::getPrefix();

        // Get versioned conflicts for pie packages (pie-zts8.0, pie-zts8.1, etc.)
        // Replace the 'php' prefix from conflicts with 'pie'
        $phpConflicts = CreatePackages::getVersionedConflicts('');
        $versionedConflicts = [];
        foreach ($phpConflicts as $conflict) {
            // Replace 'php' with 'pie' (e.g., php-zts8.5 -> pie-zts8.5, php-nts85 -> pie-nts85)
            $versionedConflicts[] = str_replace('php', 'pie', $conflict);
        }

        return [
            'depends' => [
                $prefix . '-cli',
                $prefix . '-devel',
            ],
            'provides' => [],
            'replaces' => $versionedConflicts,
            'conflicts' => $versionedConflicts,
            'files' => [
                $pharSource => getSharedir() . '/pie.phar',
                $wrapperSource => '/usr/bin/pie' . getBinarySuffix(),
            ],
        ];
    }

    public function getDebuginfoFpmConfig(): array
    {
        return [];
    }

    public function getFpmExtraArgs(): array
    {
        return [];
    }

    public function getLicense(): string
    {
        return 'BSD-3-Clause';
    }

    public function getDescription(): string
    {
        return 'PHP Installer for Extensions (PIE)';
    }

    private function prepareArtifacts(): array
    {
        [$pharPath] = $this->frozenArtifact();

        // Render the pie wrapper script using Twig template
        $binarySuffix = getBinarySuffix();
        $wrapperPath = TEMP_DIR . '/pie' . $binarySuffix;

        $wrapperContents = TwigRenderer::render('pie-wrapper.twig', [
            'binary_suffix' => $binarySuffix,
            'sharedir' => getSharedir(),
        ]);

        file_put_contents($wrapperPath, $wrapperContents);
        chmod($wrapperPath, 0755);

        return [$pharPath, $wrapperPath];
    }

    private function frozenArtifact(): array
    {
        $payloadRoot = defined('BUILD_ROOT_PATH') ? BUILD_ROOT_PATH : '';
        $sealedMetadata = $payloadRoot . '/packaging/pie/metadata.json';
        if ($payloadRoot !== '' && (is_file($sealedMetadata) || is_file($payloadRoot . '/packaging/pie/pie.phar'))) {
            $payload = BuildPayload::load($payloadRoot, true);
            if (!$payload->isSealed()) {
                throw new RuntimeException('PIE packaging requires a sealed producer payload');
            }
            $metadataPath = $sealedMetadata;
            $pharPath = $payloadRoot . '/packaging/pie/pie.phar';
            $metadata = $this->readMetadata($metadataPath);
        } else {
            $metadataPath = BASE_PATH . '/downloads/.spc-meta.json';
            $snapshot = $this->readMetadata($metadataPath);
            $metadata = ['version' => $snapshot['pie_version'] ?? null, 'sha256' => $snapshot['pie_sha256'] ?? null];
            $pharPath = BASE_PATH . '/downloads/pie.phar';
        }
        $version = preg_replace('/^[vV](?=\d)/', '', (string) ($metadata['version'] ?? ''));
        $sha256 = $metadata['sha256'] ?? null;
        if (!preg_match('/^\d+\.\d+\.\d+[A-Za-z0-9.+~-]*$/D', $version)
            || !is_string($sha256) || !preg_match('/^[a-f0-9]{64}$/D', $sha256)
            || !is_file($pharPath)) {
            throw new RuntimeException('PIE must be frozen with its version and SHA256 in the source snapshot before packaging');
        }
        if (!hash_equals($sha256, hash_file('sha256', $pharPath))) {
            throw new RuntimeException('Frozen PIE archive SHA256 mismatch: ' . $pharPath);
        }
        return [$pharPath, $version];
    }

    private function readMetadata(string $path): array
    {
        if (!is_file($path)) {
            throw new RuntimeException('Missing frozen PIE metadata: ' . $path);
        }
        $metadata = json_decode((string) file_get_contents($path), true, 512, JSON_THROW_ON_ERROR);
        if (!is_array($metadata)) {
            throw new RuntimeException('Invalid frozen PIE metadata: ' . $path);
        }
        return $metadata;
    }
}
