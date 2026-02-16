<?php

namespace staticphp\Command;

use staticphp\package\caddyplugins;
use Symfony\Component\Console\Attribute\AsCommand;
use Symfony\Component\Console\Input\InputInterface;
use Symfony\Component\Console\Input\InputOption;
use Symfony\Component\Console\Output\OutputInterface;

#[AsCommand(
    name: 'package-caddy',
    description: 'Build and package Caddy plugins',
)]
class PackageCaddyCommand extends BaseCommand
{
    protected function configure(): void
    {
        parent::configure();
        $this
            ->addOption('iteration', null, InputOption::VALUE_REQUIRED, 'Specify iteration number to use for packages (overrides default)');
    }

    protected function execute(InputInterface $input, OutputInterface $output): int
    {
        $iteration = $input->getOption('iteration');
        $debuginfo = $input->getOption('debuginfo');
        $type = $input->getOption('type');

        $output->writeln("Building and packaging Caddy plugins for {$type}...");

        try {
            $caddyPlugins = new caddyplugins();

            // Build and package plugins based on package type
            $caddyPlugins->createPackages($type, [], $iteration, $debuginfo);

            $output->writeln("Caddy plugins built and packaged successfully.");
            $this->cleanupTempDir($output);
            return self::SUCCESS;
        } catch (\Exception $e) {
            $output->writeln("Failed to build/package Caddy plugins: " . $e->getMessage());
            $this->cleanupTempDir($output);
            return self::FAILURE;
        }
    }
}
