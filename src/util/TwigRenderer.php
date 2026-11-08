<?php

namespace staticphp\util;

use Exception;
use RuntimeException;
use Twig\Environment;
use Twig\Loader\FilesystemLoader;

class TwigRenderer
{
    /**
     * Renders any Twig template with the given variables
     *
     * @param string $templateName Template file name (e.g., 'pie-wrapper.twig')
     * @param array $variables Variables to pass to the template
     * @return string The rendered template content
     * @throws RuntimeException If there's an error rendering the template
     */
    public static function render(string $templateName, array $variables = []): string
    {
        $loader = new FilesystemLoader(BASE_PATH . '/config/templates');
        $twig = new Environment($loader);

        try {
            return $twig->render($templateName, $variables);
        } catch (Exception $e) {
            throw new RuntimeException("Error rendering template {$templateName}: " . $e->getMessage());
        }
    }

    /**
     * Renders any Twig template file with the given variables
     *
     * @param string $filePath Full path to the template file
     * @param array $variables Variables to pass to the template
     * @return string The rendered template content
     * @throws RuntimeException If there's an error rendering the template
     */
    public static function renderFile(string $filePath, array $variables = []): string
    {
        $directory = dirname($filePath);
        $fileName = basename($filePath);

        $loader = new FilesystemLoader($directory);
        $twig = new Environment($loader);

        try {
            return $twig->render($fileName, $variables);
        } catch (Exception $e) {
            throw new RuntimeException("Error rendering template file {$filePath}: " . $e->getMessage());
        }
    }

    /**
     * Renders a Twig template with the given variables
     *
     * @param string $phpVersion PHP version to use in the template
     * @param string|null $arch Architecture to use in the template (defaults to detected architecture)
     * @return string The rendered template content
     * @throws RuntimeException If there's an error rendering the template
     */
    public static function renderCraftTemplate(string $phpVersion = '8.4', ?string $arch = null, ?array $packages = null, ?string $sourceVersion = null, ?array $libraries = null): string
    {
        // Detect architecture if not provided
        if ($arch === null) {
            $arch = str_contains(php_uname('m'), 'x86_64') ? 'x86_64' : 'aarch64';
        }

        // Use Twig to render the craft.yml template
        $loader = new FilesystemLoader(BASE_PATH . '/config/templates');
        $twig = new Environment($loader);

        // Prepare template variables
        // Get the binary suffix (e.g., "-zts", "-nts", "-zts8.5")
        $binarySuffix = defined('SPP_PREFIX') ? SPP_PREFIX : '-zts';
        $prefix = "php$binarySuffix";
        $packageType = defined('SPP_TYPE') ? SPP_TYPE : 'rpm';
        $majorOsVersion = $packageType === 'apk' ? '99' : '8';


        $sharedLibrarySuffix = getSharedLibrarySuffix();
        $templateVars = [
            'php_version' => $phpVersion,
            'php_source_version' => $sourceVersion ?? $phpVersion,
            'php_version_nodot' => str_replace('.', '', $phpVersion),
            // Must stay off for <= 8.5. version_compare, not Twig's lexical >=, so it survives 8.10.
            'allow_shared_ext_failure' => version_compare($phpVersion, '8.6', '>='),
            'target' => SPP_TARGET,
            'arch' => $arch,
            'os' => $majorOsVersion,
            'prefix' => $prefix,
            'release_suffix' => str_replace_first('-', '', $sharedLibrarySuffix),
            'confdir' => '/etc/' . $prefix,
            'type' => $packageType === 'apk' ? 'apk' : 'rpm',
            'moduledir' => '/usr/lib/' . $prefix . '/modules',
            'shared_libdir' => '/usr/lib/php-zts/lib',
            'filter_libraries' => $libraries,
            // Optional filter: when provided, craft.yml will include only selected packages
            // across extensions/shared-extensions/sapi, while always including cli SAPI.
            'filter_packages' => $packages,
            'ci' => (bool)getenv('CI'),
        ];

        try {
            return ltrim($twig->render('craft.yml.twig', $templateVars));
        } catch (Exception $e) {
            throw new RuntimeException("Error rendering craft.yml template: " . $e->getMessage());
        }
    }
}
