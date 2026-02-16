<?php

namespace staticphp\Command;

use staticphp\package\caddyplugins;
use Symfony\Component\Console\Attribute\AsCommand;
use Symfony\Component\Console\Input\InputInterface;
use Symfony\Component\Console\Output\OutputInterface;

#[AsCommand(
    name: 'build-caddy',
    description: 'Build Caddy plugins using xcaddy',
)]
class BuildCaddyCommand extends BaseCommand
{
    protected function execute(InputInterface $input, OutputInterface $output): int
    {
        $output->writeln("Building Caddy plugins...");

        try {
            $caddyPlugins = new caddyplugins();

            // Just build the plugins, don't package them
            $caddyPlugins->buildPlugins();

            $output->writeln("Caddy plugins built successfully.");
            return self::SUCCESS;
        } catch (\Exception $e) {
            $output->writeln("Failed to build Caddy plugins: " . $e->getMessage());
            return self::FAILURE;
        }
    }
}
