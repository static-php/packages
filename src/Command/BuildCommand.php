<?php

namespace staticphp\Command;

use staticphp\step\RunSPC;
use Symfony\Component\Console\Attribute\AsCommand;
use Symfony\Component\Console\Input\InputOption;
use Symfony\Component\Console\Input\InputInterface;
use Symfony\Component\Console\Output\OutputInterface;

#[AsCommand(
    name: 'build',
    description: 'Build PHP with extensions using static-php-cli',
)]
class BuildCommand extends BaseCommand
{
    protected function configure(): void
    {
        parent::configure();
        $this
            ->addOption('packages', null, InputOption::VALUE_REQUIRED, 'Specify which packages to build (comma-separated)')
            ->addOption('libs-only', null, null, 'Build only the libraries (skip PHP and extension build)')
            ->addOption('libraries', null, InputOption::VALUE_REQUIRED, 'Build selected shared libraries (comma-separated; requires --libs-only)');
    }

    protected function execute(InputInterface $input, OutputInterface $output): int
    {
        $debug = $input->getOption('debug');
        $phpVersion = $input->getOption('phpv');
        $target = $input->getOption('target');
        $libsOnly = (bool) $input->getOption('libs-only');
        $libraries = $input->getOption('libraries');
        $libraries = $libraries === null ? null : array_values(array_filter(array_map('trim', explode(',', $libraries))));
        if ($libraries === []) {
            throw new \InvalidArgumentException('--libraries must name at least one library.');
        }
        $packagesOpt = $input->getOption('packages');
        $packages = null;
        if (is_string($packagesOpt) && $packagesOpt !== '') {
            $packages = array_values(array_filter(array_map('trim', explode(',', $packagesOpt))));
        }

        $output->writeln("Command options:");
        $output->writeln("  debug: " . ($debug ? 'true' : 'false'));
        $output->writeln("  version: {$phpVersion}");
        $output->writeln("  target: {$target}");
        if ($libsOnly) {
            $output->writeln("  libs-only: true");
        }
        if ($packages) {
            $output->writeln("  packages: " . implode(', ', $packages));
        }

        $result = RunSPC::run($debug, $phpVersion, $packages, $libsOnly, $libraries);

        if ($result) {
            $output->writeln("Build completed successfully.");
            $this->cleanupTempDir($output);
            return self::SUCCESS;
        }

        $output->writeln("Build failed.");
        $this->cleanupTempDir($output);
        return self::FAILURE;
    }
}
