<?php

namespace staticphp;

use Exception;
use RuntimeException;
use Symfony\Component\Yaml\Yaml;
use staticphp\util\TwigRenderer;
use staticphp\util\BuildPayload;

require_once __DIR__ . '/util/BuildPayload.php';

class CraftConfig
{
    private static $instance = null;
    private $craftConfig;
    private $extensions = [];
    private $sharedExtensions = [];
    private $sapis = [];

    private function __construct()
    {
        $this->loadConfig();
    }

    public static function getInstance(): self
    {
        if (self::$instance === null) {
            self::$instance = new self();
        }
        return self::$instance;
    }

    private function loadConfig()
    {
        $phpVersion = defined('SPP_PHP_VERSION') ? SPP_PHP_VERSION : '8.4';

        try {
            $payload = defined('BUILD_ROOT_PATH') ? BuildPayload::load(BUILD_ROOT_PATH) : null;
            $this->craftConfig = $payload !== null
                ? $payload->configuration()
                : Yaml::parse(TwigRenderer::renderCraftTemplate($phpVersion));
        } catch (Exception $e) {
            throw new RuntimeException("Error rendering or parsing craft.yml template: " . $e->getMessage());
        }

        if (isset($this->craftConfig['extensions'])) {
            $extensions = $this->craftConfig['extensions'];
            if (is_string($extensions)) {
                $extensions = explode(',', $extensions);
            }
            $this->extensions = array_map('trim', $extensions);
        }

        if (isset($this->craftConfig['shared-extensions'])) {
            $sharedExtensions = $this->craftConfig['shared-extensions'];
            if (is_string($sharedExtensions)) {
                $sharedExtensions = explode(',', $sharedExtensions);
            }
            $this->sharedExtensions = array_map('trim', $sharedExtensions);
        }

        if (isset($this->craftConfig['sapi'])) {
            $sapis = $this->craftConfig['sapi'];
            if (is_string($sapis)) {
                $sapis = explode(',', $sapis);
            }
            $this->sapis = array_map('trim', $sapis);
        }
    }

    public function getStaticExtensions(): array
    {
        return $this->extensions;
    }

    public function getSharedExtensions(): array
    {
        return $this->sharedExtensions;
    }

    public function getSapis(): array
    {
        return $this->sapis;
    }

    public function getRawConfig(): array
    {
        return $this->craftConfig;
    }
}
