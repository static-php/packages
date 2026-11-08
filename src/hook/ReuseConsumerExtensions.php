<?php

declare(strict_types=1);

namespace staticphp\hook;

use Package\Target\php as PhpTarget;
use StaticPHP\Attribute\Package\BeforeStage;
use StaticPHP\Package\PackageInstaller;
use StaticPHP\Package\PhpExtensionPackage;
use staticphp\util\ConsumerSnapshot;
use staticphp\util\ExtMeta;

require_once dirname(__DIR__) . '/util/ConsumerSnapshot.php';
require_once dirname(__DIR__) . '/util/BuildPayload.php';

final class ReuseConsumerExtensions
{
    #[BeforeStage('php', [PhpTarget::class, 'unixBuildSharedExt'], 'php')]
    public function preserveResolvedExtensions(PackageInstaller $installer): void
    {
        $snapshot = ConsumerSnapshot::active(BUILD_ROOT_PATH);
        if ($snapshot === null) {
            return;
        }
        $snapshot->restore();
        foreach ($installer->getResolvedPackages(PhpExtensionPackage::class) as $extension) {
            $name = $extension->getExtensionName();
            if (!$extension->isBuildShared() || $snapshot->isSelected($name) || ExtMeta::isAddon($name)) {
                continue;
            }
            $snapshot->verifyExtension($name);
            $extension->addStage('build', static function (): void {});
            $extension->setBuildWithPhp(false);
            $extension->setOutput('Verified reused shared extension', BUILD_MODULES_PATH . '/' . $name . '.so');
            logger()->info('CONSUMER_REUSE verified existing shared extension ' . $name);
        }
    }
}
