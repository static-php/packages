<?php

namespace staticphp\package;

use RuntimeException;
use staticphp\package;
use Symfony\Component\Process\Process;

class caddyplugins implements package
{
    private const COMMON_PLUGINS = [
        // Popular Caddy modules (high stars)
        'caddy-darkweak-souin' => 'github.com/darkweak/souin', // HTTP cache system, RFC compliant
        'caddy-fabriziosalmi-caddy-waf' => 'github.com/fabriziosalmi/caddy-waf', // WAF with regex rules, IP/DNS filtering, rate limiting, GeoIP
        // 'caddy-greenpau-caddy-auth-portal' => 'github.com/greenpau/caddy-auth-portal', // Form-Based, LDAP, OAuth 2.0, SAML authentication
        'caddy-mholt-caddy-ratelimit' => 'github.com/mholt/caddy-ratelimit', // HTTP rate limiting
        'caddy-greenpau-caddy-authorize' => 'github.com/greenpau/caddy-authorize', // JWT/PASETO authorization
        // 'caddy-abiosoft-caddy-exec' => 'github.com/abiosoft/caddy-exec', // Run one-off commands
        // 'caddy-sillygod-cdp-cache' => 'github.com/sillygod/cdp-cache', // Caddy 2 proxy cache plugin
        // 'caddy-ggicci-caddy-jwt' => 'github.com/ggicci/caddy-jwt', // JWT authentication
        'caddy-sagikazarmark-caddy-fs-s3' => 'github.com/sagikazarmark/caddy-fs-s3', // AWS S3 filesystem module
        'caddy-mholt-caddy-l4' => 'github.com/mholt/caddy-l4', // Layer 4 TCP/UDP app
        'caddy-caddyserver-cache-handler' => 'github.com/caddyserver/cache-handler', // Distributed HTTP caching
        'caddy-mholt-caddy-dynamicdns' => 'github.com/mholt/caddy-dynamicdns', // Keep DNS records pointed at itself
        'caddy-mholt-caddy-webdav' => 'github.com/mholt/caddy-webdav', // WebDAV handler

        // DNS providers (caddy-dns organization)
        'caddy-caddy-dns-cloudflare' => 'github.com/caddy-dns/cloudflare',
        'caddy-caddy-dns-route53' => 'github.com/caddy-dns/route53',
        'caddy-caddy-dns-hetzner' => 'github.com/caddy-dns/hetzner',
        'caddy-caddy-dns-duckdns' => 'github.com/caddy-dns/duckdns',
        'caddy-caddy-dns-gandi' => 'github.com/caddy-dns/gandi',
        'caddy-caddy-dns-namecheap' => 'github.com/caddy-dns/namecheap',
        'caddy-caddy-dns-alidns' => 'github.com/caddy-dns/alidns',
        'caddy-caddy-dns-azure' => 'github.com/caddy-dns/azure',
        'caddy-caddy-dns-desec' => 'github.com/caddy-dns/desec',
        'caddy-caddy-dns-arvancloud' => 'github.com/caddy-dns/arvancloud',
        'caddy-caddy-dns-unifi' => 'github.com/caddy-dns/unifi',
        'caddy-caddy-dns-transip' => 'github.com/caddy-dns/transip',
        'caddy-caddy-dns-mijnhost' => 'github.com/caddy-dns/mijnhost',
        'caddy-caddy-dns-wedos' => 'github.com/caddy-dns/wedos',
        'caddy-caddy-dns-bluecat' => 'github.com/caddy-dns/bluecat',
        'caddy-caddy-dns-tencentcloud' => 'github.com/caddy-dns/tencentcloud',
        'caddy-caddy-dns-mythicbeasts' => 'github.com/caddy-dns/mythicbeasts',
        'caddy-caddy-dns-volcengine' => 'github.com/caddy-dns/volcengine',
        'caddy-caddy-dns-tecnocratica' => 'github.com/caddy-dns/tecnocratica',
        'caddy-caddy-dns-domainnameshop' => 'github.com/caddy-dns/domainnameshop',
        'caddy-caddy-dns-dnsimple' => 'github.com/caddy-dns/dnsimple',
        'caddy-caddy-dns-websupport' => 'github.com/caddy-dns/websupport',
        'caddy-caddy-dns-inwx' => 'github.com/caddy-dns/inwx',
        'caddy-caddy-dns-he' => 'github.com/caddy-dns/he',
        'caddy-caddy-dns-westcn' => 'github.com/caddy-dns/westcn',
        'caddy-caddy-dns-all-inkl' => 'github.com/caddy-dns/all-inkl',
        'caddy-caddy-dns-edgeone' => 'github.com/caddy-dns/edgeone',
        'caddy-caddy-dns-dynv6' => 'github.com/caddy-dns/dynv6',
        'caddy-caddy-dns-powerdns' => 'github.com/caddy-dns/powerdns',
        'caddy-caddy-dns-netlify' => 'github.com/caddy-dns/netlify',
        'caddy-caddy-dns-acmedns' => 'github.com/caddy-dns/acmedns',
        'caddy-caddy-dns-directadmin' => 'github.com/caddy-dns/directadmin',
        'caddy-caddy-dns-huaweicloud' => 'github.com/caddy-dns/huaweicloud',
        'caddy-caddy-dns-digitalocean' => 'github.com/caddy-dns/digitalocean',
        'caddy-caddy-dns-ovh' => 'github.com/caddy-dns/ovh',
        'caddy-caddy-dns-godaddy' => 'github.com/caddy-dns/godaddy',
        'caddy-caddy-dns-porkbun' => 'github.com/caddy-dns/porkbun',
        'caddy-caddy-dns-linode' => 'github.com/caddy-dns/linode',
        // 'caddy-caddy-dns-vercel' => 'github.com/caddy-dns/vercel',
        'caddy-caddy-dns-loopia' => 'github.com/caddy-dns/loopia',
        'caddy-caddy-dns-ionos' => 'github.com/caddy-dns/ionos',
        // 'caddy-caddy-dns-openstack-designate' => 'github.com/caddy-dns/openstack-designate',

        // Additional useful modules
        'caddy-baldinof-caddy-supervisor' => 'github.com/baldinof/caddy-supervisor', // Run and supervise background processes
        // TODO: 'caddy-lucaslorentz-caddy-docker-proxy' => 'github.com/lucaslorentz/caddy-docker-proxy/v2', // Caddy as reverse proxy for Docker
        'caddy-greenpau-caddy-security' => 'github.com/greenpau/caddy-security', // AAA plugin with MFA/2FA
        'caddy-greenpau-caddy-git' => 'github.com/greenpau/caddy-git', // Git-backed directory updates
        'caddy-caddyserver-replace-response' => 'github.com/caddyserver/replace-response', // Perform replacements in response bodies
        // 'caddy-abiosoft-caddy-yaml' => 'github.com/abiosoft/caddy-yaml', // Alternative YAML config adapter
        'caddy-caddyserver-nginx-adapter' => 'github.com/caddyserver/nginx-adapter', // Run Caddy with NGINX config
        'caddy-hslatman-caddy-crowdsec-bouncer' => 'github.com/hslatman/caddy-crowdsec-bouncer', // Block malicious traffic via CrowdSec
        'caddy-hslatman-caddy-openapi-validator' => 'github.com/hslatman/caddy-openapi-validator', // Validate requests/responses against OpenAPI spec
        // 'caddy-lindenlab-caddy-s3-proxy' => 'github.com/lindenlab/caddy-s3-proxy', // S3 proxy plugin
        'caddy-WingLim-caddy-webhook' => 'github.com/WingLim/caddy-webhook', // Serve webhooks

        // Authentication & Authorization
        // 'caddy-greenpau-caddy-auth-jwt' => 'github.com/greenpau/caddy-auth-jwt', // replaces caddy-authorize, can't use // JWT authorization plugin
        // 'caddy-casbin-caddy-authz' => 'github.com/casbin/caddy-authz', // Access control based on Casbin policies
        'caddy-authelia-caddy-forwardauth' => 'github.com/authelia/caddy-forwardauth', // Forward auth provider for Authelia
        // 'caddy-diamondburned-caddy-htmlauth' => 'github.com/diamondburned/caddy-htmlauth', // HTML authentication

        // Request/Response Processing
        'caddy-CarsonHoffman-caddy-discord-interactions-verifier' => 'github.com/CarsonHoffman/caddy-discord-interactions-verifier', // Verify Discord webhook requests
        // 'caddy-abiosoft-caddy-hmac' => 'github.com/abiosoft/caddy-hmac', // HMAC signature validation
        // 'caddy-abiosoft-caddy-json-parse' => 'github.com/abiosoft/caddy-json-parse', // Parse JSON requests
        // 'caddy-ueffel-caddy-imagefilter' => 'github.com/ueffel/caddy-imagefilter', // Transform images in various ways
        'caddy-ueffel-caddy-brotli' => 'github.com/ueffel/caddy-brotli', // Brotli compression encoder
        // 'caddy-txtdirect-txtdirect' => 'go.txtdirect.org/txtdirect', // DNS TXT-record based redirects

        // Storage & TLS
        // 'caddy-gamalan-caddy-tlsredis' => 'github.com/gamalan/caddy-tlsredis', // Redis storage for Caddy TLS data
        'caddy-pteich-caddy-tlsconsul' => 'github.com/pteich/caddy-tlsconsul', // Consul K/V storage for TLS data
        'caddy-caddyserver-ntlm-transport' => 'github.com/caddyserver/ntlm-transport', // NTLM reverse proxy transport
        'caddy-caddyserver-circuitbreaker' => 'github.com/caddyserver/circuitbreaker', // Circuit-breaker for reverse proxy

        // Config Adapters & Utilities
        'caddy-caddyserver-json5-adapter' => 'github.com/caddyserver/json5-adapter', // JSON5 config adapter
        // 'caddy-caddyserver-jsonc-adapter' => 'github.com/caddyserver/jsonc-adapter', // JSONC config adapter
        // 'caddy-caddyserver-cue-adapter' => 'github.com/caddyserver/cue-adapter', // CUE config adapter
        // 'caddy-francislavoie-caddy-hcl' => 'github.com/francislavoie/caddy-hcl', // HCL (HashiCorp Config Language) adapter
        // 'caddy-abiosoft-caddy-named-routes' => 'github.com/abiosoft/caddy-named-routes', // Named routes support

        // Logging
        'caddy-caddyserver-transform-encoder' => 'github.com/caddyserver/transform-encoder', // Custom log formats
        'caddy-leodido-caddy-jsonselect-encoder' => 'github.com/leodido/caddy-jsonselect-encoder', // Pick what to log in JSON
        // 'caddy-leodido-caddy-conditional-logging' => 'github.com/leodido/caddy-conditional-logging', // Conditional logging encoder
        'caddy-greenpau-caddy-trace' => 'github.com/greenpau/caddy-trace', // Request debugging middleware

        // Specialized Protocols & Services
        // 'caddy-hslatman-caddy-scep' => 'github.com/hslatman/caddy-scep', // Simple Certificate Enrollment Protocol (SCEP)
        // 'caddy-hslatman-caddy-est' => 'github.com/hslatman/caddy-est', // Enrollment over Secure Transport (EST)
        // 'caddy-dunglas-vulcain' => 'github.com/dunglas/vulcain', // Fast client-driven REST APIs
        // 'caddy-dunglas-mercure' => 'github.com/dunglas/mercure', // Server-sent live updates protocol

        // Development & Testing
        // 'caddy-srikrsna-csp' => 'github.com/srikrsna/csp', // Enable CSP headers
        // 'caddy-txsvc-apikit' => 'github.com/txsvc/apikit', // Wrapper around echo http server
        // 'caddy-kirsch33-caddy-realip' => 'github.com/kirsch33/realip', // Real IP module
        'caddy-lolPants-caddy-requestid' => 'github.com/lolPants/caddy-requestid', // Unique request ID placeholder
        // 'caddy-amalto-caddy-vars-regex' => 'github.com/amalto/caddy-vars-regex', // Placeholder regex
    ];

    public function getName(): string
    {
        return 'caddyplugins';
    }

    public function getFpmConfig(): array
    {
        return [];
    }

    public function getDebuginfoFpmConfig(): array
    {
        return [];
    }

    public function getFpmExtraArgs(): array
    {
        return [];
    }

    public function getLicense(): string
    {
        return 'Various';
    }

    public function getDescription(): string
    {
        return 'Common Caddy plugins built for FrankenPHP';
    }

    /**
     * Build all Caddy plugins without packaging
     */
    public function buildPlugins(): void
    {
        echo "Building Caddy plugins...\n";

        $xcaddyBin = BASE_PATH . '/pkgroot/x86_64-linux/go-xcaddy/bin/xcaddy';
        $frankenphpBin = BUILD_BIN_PATH . '/frankenphp';
        $pluginOutputDir = BUILD_ROOT_PATH . '/caddy-plugins';

        if (!file_exists($xcaddyBin)) {
            throw new RuntimeException("xcaddy not found at: {$xcaddyBin}");
        }

        if (!file_exists($frankenphpBin)) {
            throw new RuntimeException("frankenphp binary not found at: {$frankenphpBin}");
        }

        // Create plugin output directory
        if (!is_dir($pluginOutputDir) && !mkdir($pluginOutputDir, 0755, true) && !is_dir($pluginOutputDir)) {
            throw new RuntimeException(sprintf('Directory "%s" was not created', $pluginOutputDir));
        }

        // Add xcaddy to PATH
        $xcaddyPath = dirname($xcaddyBin);
        $currentPath = getenv('PATH');
        putenv("PATH={$xcaddyPath}:{$currentPath}");

        // First pass: build all plugins together to resolve versions correctly
        echo "Running initial combined build to resolve plugin versions...\n";
        $this->buildCombinedPlugins($xcaddyBin, $frankenphpBin, $pluginOutputDir);

        // Second pass: build each plugin individually with resolved versions
        foreach (self::COMMON_PLUGINS as $pluginName => $pluginImport) {
            $this->buildPlugin($pluginName, $pluginImport, $frankenphpBin, $pluginOutputDir);
        }

        echo "All Caddy plugins built successfully.\n";
    }

    /**
     * Build all common Caddy plugins and create packages
     */
    public function createPackages(string $packageType, array $binaryDependencies, ?string $iterationOverride = null, bool $debuginfo = false): void
    {
        echo "Building and packaging Caddy plugins...\n";

        $xcaddyBin = BASE_PATH . '/pkgroot/x86_64-linux/go-xcaddy/bin/xcaddy';
        $frankenphpBin = BUILD_BIN_PATH . '/frankenphp';
        $pluginOutputDir = BUILD_ROOT_PATH . '/caddy-plugins';

        if (!file_exists($xcaddyBin)) {
            throw new RuntimeException("xcaddy not found at: {$xcaddyBin}");
        }

        if (!file_exists($frankenphpBin)) {
            throw new RuntimeException("frankenphp binary not found at: {$frankenphpBin}");
        }

        // Create plugin output directory
        if (!is_dir($pluginOutputDir) && !mkdir($pluginOutputDir, 0755, true) && !is_dir($pluginOutputDir)) {
            throw new RuntimeException(sprintf('Directory "%s" was not created', $pluginOutputDir));
        }

        // Add xcaddy to PATH
        $xcaddyPath = dirname($xcaddyBin);
        $currentPath = getenv('PATH');
        putenv("PATH={$xcaddyPath}:{$currentPath}");

        // Get architecture
        $archProcess = new Process(['uname', '-m']);
        $archProcess->run();
        $architecture = trim($archProcess->getOutput());
        if (empty($architecture)) {
            $architecture = 'x86_64';
        }

        // First pass: build all plugins together to resolve versions correctly
        echo "Running initial combined build to resolve plugin versions...\n";
        $this->buildCombinedPlugins($xcaddyBin, $frankenphpBin, $pluginOutputDir);

        // Second pass: build each plugin individually and create package
        foreach (self::COMMON_PLUGINS as $pluginName => $pluginImport) {
            // Build the plugin .so file
            $this->buildPlugin($pluginName, $pluginImport, $frankenphpBin, $pluginOutputDir);

            // Create package for the plugin
            if ($packageType === 'rpm') {
                $this->createPluginRpmPackage($pluginName, $pluginOutputDir, $architecture, $iterationOverride);
            }
            elseif ($packageType === 'deb') {
                $this->createPluginDebPackage($pluginName, $pluginOutputDir, $architecture, $iterationOverride);
            }
            elseif ($packageType === 'apk') {
                $this->createPluginApkPackage($pluginName, $pluginOutputDir, $architecture, $iterationOverride);
            }
        }

        echo "All Caddy plugins built and packaged successfully.\n";
    }

    /**
     * Build all plugins together in one pass to resolve versions
     */
    private function buildCombinedPlugins(string $xcaddyBin, string $frankenphpBin, string $outputDir): void
    {
        if (file_exists($outputDir . '/combined-plugins.so')) return;
        echo "Building all plugins together to resolve dependency versions...\n";

        // Build command with all plugins
        $withArgs = [];
        foreach (self::COMMON_PLUGINS as $pluginName => $pluginImport) {
            $withArgs[] = '--with';
            $withArgs[] = $pluginImport;
        }

        $process = new Process(array_merge([
            $xcaddyBin,
            'build-plugin',
            '--caddy', $frankenphpBin,
            '--plugin-dir', $outputDir,
        ], $withArgs, [
            '--output', $outputDir . '/combined-plugins.so',
        ]), env: ['XCADDY_SKIP_CLEANUP' => '1']);

        $process->setTimeout(null);
        $process->run(function ($type, $buffer) {
            echo $buffer;
        });

        if (!$process->isSuccessful()) {
            throw new RuntimeException("Failed to build combined plugins for version resolution: " . $process->getErrorOutput());
        }
    }

    /**
     * Build a single Caddy plugin
     */
    private function buildPlugin(string $pluginName, string $pluginImport, string $frankenphpBin, string $outputDir): void
    {
        $outputFile = "{$outputDir}/{$pluginName}.so";

        // Skip if plugin already exists
        if (file_exists($outputFile)) {
            echo "Plugin {$pluginName} already exists, skipping: {$outputFile}\n";
            return;
        }

        echo "Building plugin: {$pluginName} ({$pluginImport})...\n";

        $process = new Process([
            'xcaddy',
            'build-plugin',
            '--caddy', $frankenphpBin,
            '--plugin-dir', $outputDir,
            '--with', $pluginImport,
            '--output', $outputFile,
        ]);

        $process->setTimeout(null);
        $process->run(function ($type, $buffer) {
            echo $buffer;
        });

        if (!$process->isSuccessful()) {
            throw new RuntimeException("Failed to build plugin {$pluginName}: " . $process->getErrorOutput());
        }

        if (!file_exists($outputFile)) {
            throw new RuntimeException("Plugin {$pluginName} was not created at {$outputFile}");
        }

        echo "Plugin {$pluginName} built successfully: {$outputFile}\n";

        // Verify plugin loads correctly
        $this->verifyPlugin($frankenphpBin, $outputDir, $pluginName);
    }

    /**
     * Verify that a plugin loads correctly
     */
    private function verifyPlugin(string $frankenphpBin, string $pluginDir, string $pluginName): void
    {
        echo "Verifying plugin {$pluginName} loads correctly...\n";

        $ldLibraryPath = 'LD_LIBRARY_PATH=' . BUILD_LIB_PATH;
        $process = new Process([
            'sh', '-c',
            $ldLibraryPath . ' ' . $frankenphpBin . ' list-modules --plugin-dir ' . $pluginDir
        ]);
        $process->setTimeout(30);
        $process->run();

        if (!$process->isSuccessful()) {
            echo "Warning: Failed to verify plugin {$pluginName}: " . $process->getErrorOutput() . "\n";
            echo "Output: " . $process->getOutput() . "\n";
        }
        else {
            $output = $process->getOutput();

            // Extract only the non-standard modules (after "Standard modules:" line)
            $lines = explode("\n", $output);
            $afterStandard = false;
            $nonStandardModules = [];

            foreach ($lines as $line) {
                if (strpos($line, 'Standard modules:') !== false) {
                    $afterStandard = true;
                    continue;
                }
                if ($afterStandard && trim($line) !== '') {
                    $nonStandardModules[] = trim($line);
                }
            }

            if (!empty($nonStandardModules)) {
                echo "Plugin {$pluginName} loaded successfully.\n";
                echo "Non-standard modules detected:\n";
                foreach ($nonStandardModules as $module) {
                    echo "  - {$module}\n";
                }
            }
            else {
                echo "Plugin {$pluginName} verification succeeded (no non-standard modules detected).\n";
            }
        }
    }

    /**
     * Create RPM package for a Caddy plugin
     */
    private function createPluginRpmPackage(string $pluginName, string $pluginOutputDir, string $architecture, ?string $iterationOverride): void
    {
        $packageName = "caddyplugin-{$pluginName}";
        $pluginFile = "{$pluginOutputDir}/{$pluginName}.so";

        if (!file_exists($pluginFile)) {
            echo "Plugin file not found, skipping package creation: {$pluginFile}\n";
            return;
        }

        // Extract description from plugin comment
        $description = $this->getPluginDescription($pluginName);

        $version = '1.0.0';
        $iteration = $iterationOverride ?? '1';

        $distVersion = $this->getDistVersion();
        $rpmRelease = $distVersion !== '' ? "{$iteration}.{$distVersion}" : $iteration;
        $packageFile = DIST_RPM_PATH . "/{$packageName}-{$version}-{$rpmRelease}.{$architecture}.rpm";

        $fpmArgs = [
            'fpm',
            '-s', 'dir',
            '-t', 'rpm',
            '--rpm-compression', 'xz',
            '-p', $packageFile,
            '-n', $packageName,
            '-v', $version,
            '--iteration', $rpmRelease,
            '--architecture', $architecture,
            '--description', $description,
            '--license', 'Various',
            '--depends', 'frankenphp',
            $pluginFile . '=/var/lib/caddy/modules/' . basename($pluginFile),
        ];

        $process = new Process($fpmArgs);
        $process->setTimeout(null);
        $process->run(function ($type, $buffer) {
            echo $buffer;
        });

        if (!$process->isSuccessful()) {
            throw new RuntimeException("Failed to create RPM package for {$pluginName}: " . $process->getErrorOutput());
        }

        echo "RPM package created: {$packageFile}\n";
    }

    /**
     * Create DEB package for a Caddy plugin
     */
    private function createPluginDebPackage(string $pluginName, string $pluginOutputDir, string $architecture, ?string $iterationOverride): void
    {
        $packageName = "{$pluginName}";
        $pluginFile = "{$pluginOutputDir}/{$pluginName}.so";

        if (!file_exists($pluginFile)) {
            echo "Plugin file not found, skipping package creation: {$pluginFile}\n";
            return;
        }

        // Extract description from plugin comment
        $description = $this->getPluginDescription($pluginName);

        $version = '1.0.0';
        $iteration = $iterationOverride ?? '1';

        // Convert system architecture to Debian architecture naming
        $debArch = match ($architecture) {
            'x86_64' => 'amd64',
            'aarch64' => 'arm64',
            default => $architecture,
        };

        $packageFile = DIST_DEB_PATH . "/{$packageName}_{$version}-{$iteration}_{$debArch}.deb";

        $fpmArgs = [
            'fpm',
            '-s', 'dir',
            '-t', 'deb',
            '--deb-compression', 'xz',
            '-p', $packageFile,
            '-n', $packageName,
            '-v', $version,
            '--iteration', $iteration,
            '--architecture', $debArch,
            '--description', $description,
            '--license', 'Various',
            '--depends', 'frankenphp',
            $pluginFile . '=/var/lib/caddy/modules/' . basename($pluginFile),
        ];

        $process = new Process($fpmArgs);
        $process->setTimeout(null);
        $process->run(function ($type, $buffer) {
            echo $buffer;
        });

        if (!$process->isSuccessful()) {
            throw new RuntimeException("Failed to create DEB package for {$pluginName}: " . $process->getErrorOutput());
        }

        echo "DEB package created: {$packageFile}\n";
    }

    /**
     * Create APK package for a Caddy plugin
     */
    private function createPluginApkPackage(string $pluginName, string $pluginOutputDir, string $architecture, ?string $iterationOverride): void
    {
        $packageName = "caddyplugin-{$pluginName}";
        $pluginFile = "{$pluginOutputDir}/{$pluginName}.so";

        if (!file_exists($pluginFile)) {
            echo "Plugin file not found, skipping package creation: {$pluginFile}\n";
            return;
        }

        // Extract description from plugin comment
        $description = $this->getPluginDescription($pluginName);

        $version = '1.0.0';
        $iteration = $iterationOverride ?? '1';

        $nfpmConfig = [
            'name' => $packageName,
            'arch' => $architecture,
            'platform' => 'linux',
            'version' => $version,
            'release' => $iteration,
            'section' => 'default',
            'priority' => 'optional',
            'maintainer' => 'Marc Henderkes <pkg@henderkes.com>',
            'description' => $description,
            'vendor' => 'Marc Henderkes',
            'homepage' => 'https://apks.henderkes.com',
            'license' => 'Various',
            'depends' => ['frankenphp'],
            'contents' => [
                [
                    'src' => $pluginFile,
                    'dst' => '/var/lib/caddy/modules/' . basename($pluginFile),
                ],
            ],
        ];

        $nfpmConfigFile = TEMP_DIR . "/nfpm-{$packageName}.yaml";
        if (!yaml_emit_file($nfpmConfigFile, $nfpmConfig, YAML_UTF8_ENCODING)) {
            throw new RuntimeException("Failed to write YAML file: {$nfpmConfigFile}");
        }

        $outputFile = DIST_APK_PATH . "/{$packageName}-{$version}-r{$iteration}.{$architecture}.apk";
        $nfpmProcess = new Process([
            'nfpm', 'package',
            '--config', $nfpmConfigFile,
            '--packager', 'apk',
            '--target', $outputFile
        ]);
        $nfpmProcess->setTimeout(null);
        $nfpmProcess->run(function ($type, $buffer) {
            echo $buffer;
        });

        if (!$nfpmProcess->isSuccessful()) {
            throw new RuntimeException("nfpm package creation failed for {$pluginName}: " . $nfpmProcess->getErrorOutput());
        }

        @unlink($nfpmConfigFile);

        echo "APK package created: {$outputFile}\n";
    }

    /**
     * Get plugin description from the COMMON_PLUGINS array comment
     */
    private function getPluginDescription(string $pluginName): string
    {
        // Default description if not found
        $defaultDescription = "Caddy plugin: {$pluginName}";

        // Try to extract description from source code comments
        $reflection = new \ReflectionClass($this);
        $source = file_get_contents($reflection->getFileName());

        // Match the plugin line with comment
        $pattern = '/[\'"]' . preg_quote($pluginName, '/') . '[\'"]\\s*=>\\s*[\'"][^\'"]+[\'"],?\\s*\\/\\/\\s*(.+)/';
        if (preg_match($pattern, $source, $matches)) {
            return trim($matches[1]);
        }

        return $defaultDescription;
    }

    /**
     * Get distribution version for RPM filenames (e.g., "el9", "el8", "fc39")
     */
    private function getDistVersion(): string
    {
        if (!file_exists('/etc/os-release')) {
            return '';
        }

        $osRelease = parse_ini_file('/etc/os-release');
        if (!$osRelease || !isset($osRelease['ID'], $osRelease['VERSION_ID'])) {
            return '';
        }

        $id = $osRelease['ID'];
        $versionId = $osRelease['VERSION_ID'];

        // Extract major version number
        if (preg_match('/^(\d+)/', $versionId, $matches)) {
            $majorVersion = $matches[1];
        }
        else {
            return '';
        }

        // Map distribution ID to prefix
        $distMap = [
            'rhel' => 'el',
            'centos' => 'el',
            'rocky' => 'el',
            'almalinux' => 'el',
            'fedora' => 'fc',
        ];

        $prefix = $distMap[$id] ?? '';
        return $prefix !== '' ? $prefix . $majorVersion : '';
    }
}
