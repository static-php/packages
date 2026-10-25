<?php

namespace staticphp;

use Exception;
use staticphp\step\CreatePackages;
use staticphp\util\ExtMeta;
use staticphp\util\TwigRenderer;
use staticphp\util\ApkRuntimeAliases;

require_once __DIR__ . '/util/ApkRuntimeAliases.php';

class extension implements package
{
    private string $prefix;
    private ?array $stateScripts = null;

    public function __construct(
        private readonly string $name,
    )
    {
        $this->prefix = $this->determineExtensionPrefix();
    }

    private function determineExtensionPrefix(): string
    {
        if (!$this->isSharedExtension()) {
            return '';
        }

        if ($this->name === 'xdebug' || $this->name === 'ffi') {
            return '15-';
        }
        if (ExtMeta::isZendExtension($this->name)) {
            return '10-';
        }

        $allDependencies = $this->getExtensionDependencies($this->name);

        if (empty($allDependencies)) {
            return '20-';
        }

        return '30-';
    }

    public function getExtensionDependencies(string $extensionName, array $visited = []): array
    {
        if (ExtMeta::get($extensionName) === null) {
            return [];
        }

        $allDependencies = [];
        $visited[] = $extensionName;
        $craftConfig = CraftConfig::getInstance();

        foreach (ExtMeta::extDependencies($extensionName) as $dependency) {
            if (!in_array($dependency, $craftConfig->getSharedExtensions()) || in_array($dependency, $craftConfig->getStaticExtensions())) {
                continue;
            }

            if (in_array($dependency, $visited)) {
                continue;
            }

            if (ExtMeta::extDependencies($dependency) !== []) {
                $transitiveDeps = $this->getExtensionDependencies($dependency, $visited);

                foreach ($transitiveDeps as $transitiveDep) {
                    if (!in_array($transitiveDep, $craftConfig->getSharedExtensions()) || in_array($transitiveDep, $craftConfig->getStaticExtensions())) {
                        continue;
                    }
                    if (!in_array($transitiveDep, $allDependencies)) {
                        $allDependencies[] = $transitiveDep;
                    }
                }
            }

            $allDependencies[] = $dependency;
        }

        return $allDependencies;
    }

    public function getFpmConfig(): array
    {
        if (ExtMeta::get($this->name) === null) {
            throw new Exception("Extension configuration for '{$this->name}' not found.");
        }
        $prefix = CreatePackages::getPrefix();
        $depends = [$prefix . '-cli'];
        $seen = [];
        $ordered = [];

        $collect = function (string $name) use (&$collect, &$ordered, &$seen, $prefix): void {
            if (isset($seen[$name])) {
                return;
            }
            $seen[$name] = true;

            if (ExtMeta::get($name) === null) {
                return;
            }
            if (CreatePackages::isSkipped($name)) {
                echo "Warning: dropping suggested dependency {$prefix}-{$name} from {$prefix}-{$this->name}: " . CreatePackages::getSkippedExtensions()[$name] . "\n";
            }
            elseif (!ExtMeta::isAddon($name)) {
                $ordered[] = $prefix . '-' . $name;
            }
            foreach (ExtMeta::extDependencies($name) as $dep) {
                $collect($dep);
            }
        };

        foreach (ExtMeta::extDependencies($this->name) as $dep) {
            $collect($dep);
        }

        $depends = array_merge($depends, $ordered);

        $versionedConflicts = CreatePackages::getVersionedConflicts('-' . $this->name);
        $config = [
            'config-files' => [
                getConfdir() . '/conf.d/' . $this->prefix . $this->name . '.ini',
            ],
            'depends' => $depends,
            'provides' => [],
            'replaces' => $versionedConflicts,
            'conflicts' => $versionedConflicts,
            'apk_scripts' => defined('SPP_TYPE') && SPP_TYPE === 'apk' && $this->isSharedExtension()
                ? ['preupgrade' => $this->activationScripts()['before'], 'postupgrade' => $this->activationScripts()['after']]
                : [],
            'files' => [
                ...($this->getIniPath() ?
                    [$this->getIniPath() => getConfdir() . '/conf.d/' . $this->prefix . $this->name . '.ini']
                    : []
                ),
                ...($this->isSharedExtension() ?
                    [BUILD_MODULES_PATH . '/' . $this->name . getSharedLibrarySuffix() . '.so' => getModuledir() . '/' . $this->name . getSharedLibrarySuffix() . '.so']
                    : []
                ),
            ]
        ];
        if (defined('SPP_TYPE') && SPP_TYPE === 'apk') {
            foreach (ApkRuntimeAliases::relationships($this->getName()) as $field => $values) {
                $config[$field] = is_array($values) ? array_values(array_unique([...($config[$field] ?? []), ...$values])) : $values;
            }
        }
        return $config;
    }

    protected function getIniPath(): ?string
    {
        $iniPath = INI_PATH . '/extension/' . $this->name . '.ini';
        if (!file_exists($iniPath)) {
            return null;
        }

        $tempIniPath = TEMP_DIR . '/' . $this->prefix . $this->name . '.ini';

        // Get the dynamic prefix for path replacements
        $prefix = CreatePackages::getPrefix();

        $iniContent = TwigRenderer::renderFile($iniPath, [
            'type' => defined('SPP_TYPE') ? SPP_TYPE : 'rpm',
            'binary_suffix' => getBinarySuffix(),
            'shared_library_suffix' => getSharedLibrarySuffix(),
            'is_shared' => $this->isSharedExtension(),
        ]);

        $iniContent = preg_replace(
            [
                '#/usr/share/php[^/]*/#',
                '#/usr/local/share/php[^/]*/#',
            ],
            [
                '/usr/share/' . $prefix . '/',
                '/usr/local/share/' . $prefix . '/',
            ],
            $iniContent
        );
        file_put_contents($tempIniPath, $iniContent);

        return $tempIniPath;
    }

    public function isSharedExtension(): bool
    {
        $craftConfig = CraftConfig::getInstance();
        return in_array($this->name, $craftConfig->getSharedExtensions()) && !in_array($this->name, $craftConfig->getStaticExtensions());
    }

    public function getFpmExtraArgs(): array
    {
        if (!$this->isSharedExtension()) {
            return [];
        }
        $scripts = $this->activationScripts();
        return ['--before-install', $scripts['before'], '--before-upgrade', $scripts['before'],
            '--after-install', $scripts['after'], '--after-upgrade', $scripts['after']];
    }

    private function activationScripts(): array
    {
        if ($this->stateScripts === null) {
            $ini = getConfdir() . '/conf.d/' . $this->prefix . $this->name . '.ini';
            $this->stateScripts = self::writeActivationScripts($ini,
                '/run/php-zts-module-state/' . hash('sha256', $ini));
        }
        return $this->stateScripts;
    }

    private static function writeActivationScripts(string $ini, string $state): array
    {
        $variables = 'ini=' . escapeshellarg($ini) . "\nstate=" . escapeshellarg($state) . "\n"
            . 'disabled="$ini.disabled"' . "\n";
        $before = <<<'SH'
umask 077
if [ -f "$disabled" ] && { [ ! -e "$ini" ] || [ -f "$state/disabled" ]; }; then
    mkdir -p "$state"
    : > "$state/disabled"
elif [ -f "$state/disabled" ]; then
    rm -f "$state/disabled"
    rmdir "$state" 2>/dev/null || :
fi
SH;
        $after = <<<'SH'
if [ -f "$state/disabled" ]; then
    if [ -f "$disabled" ]; then rm -f "$ini"; fi
    rm -f "$state/disabled"
    rmdir "$state" 2>/dev/null || :
fi
SH;
        $scripts = [];
        foreach (['before' => $before, 'after' => $after] as $stage => $body) {
            $path = TEMP_DIR . '/extension-state-' . hash('sha256', $ini) . '-' . $stage . '.sh';
            if (file_put_contents($path, "#!/bin/sh\nset -eu\n" . $variables . $body . "\n") === false || !chmod($path, 0755)) {
                throw new Exception('Cannot stage shared extension activation state script');
            }
            $scripts[$stage] = $path;
        }
        return $scripts;
    }

    public function getDebuginfoFpmConfig(): array
    {
        if (!$this->isSharedExtension()) {
            return [];
        }
        $sharedLibrarySuffix = getSharedLibrarySuffix();
        $src = BUILD_ROOT_PATH . '/debug/' . $this->name . $sharedLibrarySuffix . '.so.debug';
        if (!file_exists($src)) {
            return [];
        }
        $targetSo = getModuledir() . '/' . $this->name . $sharedLibrarySuffix . '.so';
        $target = '/usr/lib/debug' . $targetSo . '.debug';
        $config = [
            'depends' => [$this->getName()],
            'files' => [
                $src => $target,
            ],
        ];
        if (defined('SPP_TYPE') && SPP_TYPE === 'apk') {
            $config = array_merge($config, ApkRuntimeAliases::relationships($this->getName() . '-debuginfo'));
        }
        return $config;
    }

    public function getName(): string
    {
        $logical = CreatePackages::getPrefix() . '-' . $this->name;
        return defined('SPP_TYPE') && SPP_TYPE === 'apk' ? ApkRuntimeAliases::physicalName($logical) : $logical;
    }

    public function getLicense(): string
    {
        if ($this->name === 'xdebug') {
            return 'Xdebug-1.03';
        }
        return 'PHP-3.01';
    }

    public function getDescription(): string
    {
        return ucfirst($this->name) . ' extension for PHP';
    }
}
