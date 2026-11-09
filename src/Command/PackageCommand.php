<?php

namespace staticphp\Command;

use staticphp\step\CreatePackages;
use Symfony\Component\Console\Attribute\AsCommand;
use Symfony\Component\Console\Input\InputInterface;
use Symfony\Component\Console\Input\InputOption;
use Symfony\Component\Console\Output\OutputInterface;

#[AsCommand(
    name: 'package',
    description: 'Create packages for all extensions or specific packages',
)]
class PackageCommand extends BaseCommand
{
    protected function configure(): void
    {
        parent::configure();
        $this
            ->addOption('packages', null, InputOption::VALUE_REQUIRED, 'Specify which packages to build (comma-separated)')
            ->addOption('iteration', null, InputOption::VALUE_REQUIRED, 'Specify iteration number to use for packages (overrides auto-detected)')
            ->addOption('bump', null, InputOption::VALUE_NONE, 'Bump each package iteration to (latest published on the remote repo) + 1')
            ->addOption('libs-only', null, InputOption::VALUE_NONE, 'Package shared libraries without requiring a PHP binary')
            ->addOption('buildroot', null, InputOption::VALUE_REQUIRED, 'Shared library SDK directory (requires --libs-only)')
            ->addOption('library-manifest', null, InputOption::VALUE_REQUIRED, 'Classified library metadata for the SDK (requires --libs-only)')
            ->addOption('libraries', null, InputOption::VALUE_REQUIRED, 'Select shared libraries (comma-separated; requires --libs-only)');
    }

    protected function execute(InputInterface $input, OutputInterface $output): int
    {
        $packageNames = $input->getOption('packages');
        $iteration = $input->getOption('iteration');
        $bump = (bool)$input->getOption('bump');
        $libsOnly = (bool)$input->getOption('libs-only');
        $buildroot = $input->getOption('buildroot');
        if ($buildroot !== null && !$libsOnly) {
            throw new \InvalidArgumentException('--buildroot requires --libs-only.');
        }
        $libraries = $input->getOption('libraries');
        $libraries = $libraries === null ? null : array_values(array_filter(array_map('trim', explode(',', $libraries))));
        if ($libraries === [] || ($libraries !== null && !$libsOnly)) {
            throw new \InvalidArgumentException('--libraries must name at least one library and requires --libs-only.');
        }
        $debuginfo = $input->getOption('debuginfo');

        if ($bump && $iteration !== null && $iteration !== '') {
            throw new \InvalidArgumentException('--bump and --iteration are mutually exclusive; pass only one.');
        }

        if ($packageNames) {
            $packageNames = explode(',', $packageNames);
            $output->writeln("Creating packages for: " . implode(', ', $packageNames) . "...");
        } else {
            $output->writeln("Creating packages for all extensions...");
        }

        // All parameters now come from constants set by BaseCommand::initialize()
        $result = CreatePackages::run($packageNames, $iteration, $debuginfo, $bump, $libsOnly, $libraries, $buildroot, $input->getOption('library-manifest'));

        if ($result) {
            $output->writeln("Package creation completed successfully.");
            $this->cleanupTempDir($output);
            return self::SUCCESS;
        }

        $output->writeln("Package creation failed.");
        $this->cleanupTempDir($output);
        return self::FAILURE;
    }
}
