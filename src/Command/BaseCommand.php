<?php

namespace staticphp\Command;

use InvalidArgumentException;
use Symfony\Component\Console\Command\Command;
use Symfony\Component\Console\Input\InputInterface;
use Symfony\Component\Console\Input\InputOption;
use Symfony\Component\Console\Output\OutputInterface;

abstract class BaseCommand extends Command
{
    protected function configure(): void
    {
        $this
            ->addOption('debug', null, InputOption::VALUE_NONE, 'Print debug messages')
            ->addOption('phpv', null, InputOption::VALUE_REQUIRED, 'Specify PHP version to build', '8.4')
            ->addOption('target', null, InputOption::VALUE_REQUIRED, 'Specify the target triple for Zig (e.g., x86_64-linux-gnu, aarch64-linux-gnu)')
            ->addOption('prefix', null, InputOption::VALUE_REQUIRED, 'Specify the package prefix (e.g., -zts, -zts8.5, -zts85)', '-zts')
            ->addOption('type', null, InputOption::VALUE_REQUIRED, 'Specify package type: rpm (uses /usr/lib64), deb (uses /usr/lib), or apk (uses /usr/lib). Auto-detected from --target (musl → apk) or /etc/os-release if omitted.', null)
            ->addOption('debuginfo', null, InputOption::VALUE_NONE, 'Create debuginfo packages for APK (RPM and DEB always create debuginfo)');
    }

    protected function initialize(InputInterface $input, OutputInterface $output)
    {
        // Define build paths with PHP version
        $phpVersion = $input->getOption('phpv') ?? '8.4';
        $target = $input->getOption('target');
        $prefix = $input->getOption('prefix') ?? '-zts';
        $type = $input->getOption('type');

        // Auto-detect type if not provided
        if ($type === null) {
            $type = $this->detectType($target);
        }

        // Validate type value
        $validTypes = ['rpm', 'deb', 'apk'];
        if (!in_array($type, $validTypes, true)) {
            throw new InvalidArgumentException('Invalid --type value. Must be one of: ' . implode(', ', $validTypes));
        }

        // Check if constants are already defined
        if (defined('SPP_PHP_VERSION')) {
            echo "Constants already defined. SPP_PHP_VERSION=" . SPP_PHP_VERSION . "\n";
            return;
        }

        // Define constants
        define('SPP_PHP_VERSION', $phpVersion);
        define('SPP_TARGET', $target);
        define('SPP_PREFIX', $prefix);
        define('SPP_TYPE', $type);
        define('BUILD_ROOT_PATH', $this->getName() === 'package' && $input->getOption('buildroot') !== null ? $input->getOption('buildroot') : BASE_PATH . '/build/' . $phpVersion);
        define('BUILD_BIN_PATH', BUILD_ROOT_PATH . '/bin');
        define('BUILD_LIB_PATH', BUILD_ROOT_PATH . '/lib');
        define('BUILD_INCLUDE_PATH', BUILD_ROOT_PATH . '/include');
        define('BUILD_MODULES_PATH', BUILD_ROOT_PATH . '/modules');

        // Create necessary directories
        $this->createDirectories();
    }

    private function detectType(?string $target): string
    {
        if ($target !== null && str_contains($target, 'musl')) {
            return 'apk';
        }

        $osRelease = @parse_ini_file('/etc/os-release');
        if ($osRelease === false) {
            throw new InvalidArgumentException('Could not auto-detect package type: /etc/os-release missing. Specify --type=rpm|deb|apk.');
        }

        $id = strtolower($osRelease['ID'] ?? '');
        $idLike = strtolower($osRelease['ID_LIKE'] ?? '');
        $ids = array_filter(array_merge([$id], preg_split('/\s+/', $idLike)));

        foreach ($ids as $candidate) {
            if ($candidate === 'alpine') {
                return 'apk';
            }
            if (in_array($candidate, ['rhel', 'fedora', 'centos', 'almalinux', 'rocky'], true)) {
                return 'rpm';
            }
            if (in_array($candidate, ['debian', 'ubuntu'], true)) {
                return 'deb';
            }
        }

        throw new InvalidArgumentException('Could not auto-detect package type from /etc/os-release (ID=' . $id . '). Specify --type=rpm|deb|apk.');
    }

    protected function createDirectories(): void
    {
        $paths = [BUILD_ROOT_PATH, BUILD_BIN_PATH, BUILD_LIB_PATH, BUILD_MODULES_PATH, DIST_PATH, DIST_RPM_PATH, DIST_DEB_PATH, DIST_APK_PATH];
        foreach ($paths as $path) {
            if (!is_dir($path) && !mkdir($path, 0755, true) && !is_dir($path)) {
                throw new \RuntimeException("Failed to create directory: " . $path);
            }
        }

        // Create temporary directory
        if (file_exists(TEMP_DIR)) {
            // Clean up any existing files
            exec('rm -rf ' . escapeshellarg(TEMP_DIR . '/*'));
        } elseif (!mkdir(TEMP_DIR, 0755, true) && !is_dir(TEMP_DIR)) {
            throw new \RuntimeException("Failed to create directory: " . TEMP_DIR);
        }
    }

    protected function cleanupTempDir(OutputInterface $output): void
    {
        if (file_exists(TEMP_DIR)) {
            $output->writeln("Cleaning up temporary directory...");
            exec('rm -rf ' . escapeshellarg(TEMP_DIR));
        }
    }
}
