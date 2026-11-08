<?php

namespace staticphp\Command;

use staticphp\step\RunSPC;
use staticphp\step\CreatePackages;
use Symfony\Component\Console\Attribute\AsCommand;
use Symfony\Component\Console\Input\InputInterface;
use Symfony\Component\Console\Input\InputOption;
use Symfony\Component\Console\Output\OutputInterface;

#[AsCommand(
    name: 'all',
    description: 'Run both build and package steps',
)]
class AllCommand extends BaseCommand
{
    protected function configure(): void
    {
        parent::configure();
        $this
            ->addOption('packages', null, InputOption::VALUE_REQUIRED, 'Specify which packages to build (comma-separated)')
            ->addOption('iteration', null, InputOption::VALUE_REQUIRED, 'Specify iteration number to use for packages (overrides auto-detected)')
            ->addOption('bump', null, InputOption::VALUE_NONE, 'Bump each package iteration to (latest published on the remote repo) + 1')
            ->addOption('libs-only', null, InputOption::VALUE_NONE, 'Package shared libraries without requiring a PHP binary')
            ->addOption('libraries', null, InputOption::VALUE_REQUIRED, 'Select shared libraries (comma-separated; requires --libs-only)');
    }

    protected function execute(InputInterface $input, OutputInterface $output): int
    {
        $debug = $input->getOption('debug');
        $packagesOpt = $input->getOption('packages');
        $iteration = $input->getOption('iteration');
        $bump = (bool)$input->getOption('bump');
        $libsOnly = (bool)$input->getOption('libs-only');
        $libraries = $input->getOption('libraries');
        $libraries = $libraries === null ? null : array_values(array_filter(array_map('trim', explode(',', $libraries))));
        if ($libraries === [] || ($libraries !== null && !$libsOnly)) {
            throw new \InvalidArgumentException('--libraries must name at least one library and requires --libs-only.');
        }
        $debuginfo = $input->getOption('debuginfo');
        $phpVersion = SPP_PHP_VERSION; // Get from constant

        if ($bump && $iteration !== null && $iteration !== '') {
            throw new \InvalidArgumentException('--bump and --iteration are mutually exclusive; pass only one.');
        }

        // Process packages option
        $packages = null;
        if (is_string($packagesOpt) && $packagesOpt !== '') {
            $packages = array_values(array_filter(array_map('trim', explode(',', $packagesOpt))));
        }

        // Run build step
        $output->writeln("Building PHP with extensions using static-php-cli...");
        $output->writeln("Using PHP version: {$phpVersion}");

        $buildResult = RunSPC::run($debug, $phpVersion, $packages, $libsOnly, $libraries);

        if (!$buildResult) {
            $output->writeln("Build failed.");
            $this->cleanupTempDir($output);
            return self::FAILURE;
        }

        // Run package step
        if ($packages) {
            $output->writeln("Creating packages for: " . implode(', ', $packages) . "...");
        } else {
            $output->writeln("Creating packages for all extensions...");
        }

        // All parameters now come from constants set by BaseCommand::initialize()
        $packageResult = CreatePackages::run($packages, $iteration, $debuginfo, $bump, $libsOnly, $libraries);

        if (!$packageResult) {
            $output->writeln("Package creation failed.");
            $this->cleanupTempDir($output);
            return self::FAILURE;
        }

        $output->writeln("All steps completed successfully.");
        $this->cleanupTempDir($output);
        return self::SUCCESS;
    }
}
