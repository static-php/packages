<?php

declare(strict_types=1);

namespace staticphp\hook;

use Package\Target\php as PhpTarget;
use StaticPHP\Attribute\Package\BeforeStage;
use StaticPHP\Package\PackageBuilder;
use StaticPHP\Package\PackageInstaller;
use StaticPHP\Package\PhpExtensionPackage;
use StaticPHP\Package\TargetPackage;
use StaticPHP\Util\SharedLibraryUtil;
use StaticPHP\Util\SPCConfigUtil;
use staticphp\util\ConsumerSnapshot;
use staticphp\util\ModuleOnlyBuild;

require_once dirname(__DIR__) . '/util/ModuleOnlyBuild.php';
require_once dirname(__DIR__) . '/util/ConsumerSnapshot.php';
require_once __DIR__ . '/PrepareReusedExtensionSources.php';

final class BuildSelectedPhpModules
{
    #[BeforeStage('php', [PhpTarget::class, 'unixBuildSharedExt'], 'php')]
    public function buildSelectedModules(TargetPackage $package, PackageInstaller $installer, PackageBuilder $builder): void
    {
        $snapshot = ConsumerSnapshot::active(BUILD_ROOT_PATH);
        if ($snapshot === null) {
            return;
        }
        $snapshot->restore();
        foreach ($installer->getResolvedPackages(PhpExtensionPackage::class) as $extension) {
            $name = $extension->getExtensionName();
            if (!$extension->isBuildShared() || !$extension->isBuildWithPhp() || !$snapshot->isSelected($name)) {
                continue;
            }
            PrepareReusedExtensionSources::prepare($extension, $installer);
            $config = new SPCConfigUtil(['libs_only_deps' => true])->config(['php']);
            $ldflags = trim($config['ldflags'] . ' ' . getenv('SPC_CMD_VAR_PHP_MAKE_EXTRA_LDFLAGS'));
            if (!preg_match('/-release\s+\S+/', $ldflags) && !str_contains($ldflags, '-avoid-version')) {
                $ldflags .= ' -avoid-version -module';
            }
            $variables = array_filter(array_merge(SharedLibraryUtil::buildEnvironment(), [
                'EXTRA_CFLAGS' => getenv('SPC_CMD_VAR_PHP_MAKE_EXTRA_CFLAGS'),
                'EXTRA_CXXFLAGS' => getenv('SPC_CMD_VAR_PHP_MAKE_EXTRA_CXXFLAGS'),
                'EXTRA_LDFLAGS' => $ldflags,
            ]));
            $output = ModuleOnlyBuild::build($package->getSourceDir(), BUILD_ROOT_PATH, $name, $builder->concurrency, $variables,
                static fn (string $source, string $target) => $builder->deployBinary($source, $target, false));
            $extension->setBuildWithPhp(false);
            $extension->addStage('build', static function (): void {});
            $extension->setOutput('Built selected module without PHP core', $output);
            logger()->info('CONSUMER_MODULE_BUILD rebuilt selected configured module ' . $name);
        }
    }
}
