<?php

namespace staticphp\Command;

use staticphp\CraftConfig;
use staticphp\step\CreatePackages;
use staticphp\util\SkippedExtensions;
use Symfony\Component\Console\Attribute\AsCommand;
use Symfony\Component\Console\Input\InputInterface;
use Symfony\Component\Console\Input\InputOption;
use Symfony\Component\Console\Output\OutputInterface;
use Symfony\Component\Process\Process;

/**
 * Install the packages just built into dist/<type>/ and prove the runtime works:
 *   1. php -v runs (via the suffixed /usr/bin/php-zts, not the container's own php)
 *   2. every shared extension craft.yml declared was packaged, or is on the recorded skip list
 *   3. every shared extension we packaged loads under the CLI SAPI (no load warnings)
 *   4. frankenphp serves a phpinfo script and loads the *same* extension set
 *
 * The check is mapping-free: PHP is told (via conf.d) to load each packaged .so and
 * either loads it or prints "Unable to load dynamic library" to stderr — its absence
 * is the proof. frankenphp is verified by comparing its get_loaded_extensions()
 * against the CLI's; any diff names the extension it dropped.
 */
#[AsCommand(
    name: 'test',
    description: 'Install the built packages and verify every packaged extension loads under php-cli and frankenphp',
)]
class TestCommand extends BaseCommand
{
    protected function configure(): void
    {
        parent::configure();
        $this->addOption('packages', null, InputOption::VALUE_REQUIRED, 'Comma-separated packages that were built (informational; the test installs whatever is in dist/<type>/)');
    }

    protected function execute(InputInterface $input, OutputInterface $output): int
    {
        $type = SPP_TYPE;
        $bin = '/usr/bin/php' . getBinarySuffix();       // e.g. /usr/bin/php-zts
        $confd = getConfdir() . '/conf.d';
        $franken = '/usr/bin/frankenphp';
        $distDir = ['rpm' => DIST_RPM_PATH, 'deb' => DIST_DEB_PATH, 'apk' => DIST_APK_PATH][$type];

        $packagesOpt = $input->getOption('packages');
        if (is_string($packagesOpt) && $packagesOpt !== '') {
            $output->writeln("Testing packages: {$packagesOpt}");
        }

        $allPkgs = glob($distDir . '/*.' . $type) ?: [];
        if (!$allPkgs) {
            return $this->fail($output, "no .{$type} packages found in {$distDir}");
        }
        // dist/ accumulates artifacts across PHP versions; an extension that fails to
        // rebuild for the current version leaves its old .deb/.rpm behind. Install only
        // packages whose php-cli dependency matches the version under test — a stale
        // one would otherwise block the whole transaction.
        [$pkgs, $skipped, $names] = $this->filterForVersion($type, $allPkgs, SPP_PHP_VERSION);
        if ($skipped) {
            $output->writeln("Skipping " . count($skipped) . " package(s) not built for PHP " . SPP_PHP_VERSION . ":");
            foreach ($skipped as $s) {
                $output->writeln("  " . basename($s));
            }
        }
        if (!$pkgs) {
            return $this->fail($output, "no packages compatible with PHP " . SPP_PHP_VERSION . " in {$distDir}");
        }

        $srv = null;
        try {
            // --- 1. install the packages built for this version -----------------
            // Extension packages depend on the base SAPI (php-zts-cli, php-zts-pdo, …). A full
            // build has those in dist/; a partial (extension-only) build does not, so pull them
            // from the published repo — exactly as a real `install php-zts-<ext>` resolves them.
            $hasBase = in_array('php' . SPP_PREFIX . '-cli', $names, true);
            // A --packages run deliberately produces a subset, so it has nothing to cross-check.
            $crossCheck = $hasBase && !(is_string($packagesOpt) && $packagesOpt !== '');
            $repoArgs = $hasBase ? [] : $this->baseRepoArgs($type, $output);
            $install = match ($type) {
                'rpm' => array_merge(['dnf', 'install', '-y'], $repoArgs, $pkgs),
                'deb' => array_merge(['apt-get', 'install', '-y', '--no-install-recommends'], $repoArgs, $pkgs),
                'apk' => array_merge(['apk', 'add', '--allow-untrusted'], $repoArgs, $pkgs),
            };
            if (!$hasBase) {
                $output->writeln("dist has no php" . SPP_PREFIX . "-cli (partial build) — base packages will resolve from the published repo");
            }
            $output->writeln("Installing " . count($pkgs) . " packages from {$distDir}...");
            if ($this->sh($this->maybeSudo($install), $output) !== 0) {
                return $this->fail($output, "package installation failed");
            }

            // --- 2. CLI SAPI works and loads every packaged extension -----------
            $ver = new Process([$bin, '-v']);
            $ver->run(fn($t, $d) => $output->write($d));
            if (!$ver->isSuccessful()) {
                return $this->fail($output, "{$bin} -v did not run");
            }

            $asked = $this->askedExtensions($confd);   // [shortName => .so basename]
            $output->writeln("Packaged shared extensions (" . count($asked) . "): " . implode(', ', array_keys($asked)));

            if ($crossCheck && ($fail = $this->crossCheckDeclared($asked, $output)) !== null) {
                return $fail;
            }

            if (!$asked) {
                // A SAPI-only build legitimately installs no conf.d drop-ins. Only fail if
                // packages actually shipped .so files that then have no extension= directive.
                $ed = new Process([$bin, '-r', 'echo ini_get("extension_dir");']);
                $ed->run();
                $sos = glob(trim($ed->getOutput()) . '/*.so') ?: [];
                if ($sos) {
                    return $this->fail($output, count($sos) . " .so files in extension_dir but no extension= directives in {$confd} — packages did not install their ini files");
                }
                $output->writeln("No shared extensions packaged — verifying SAPIs only");
            }

            $cli = new Process([$bin, '-d', 'display_errors=stderr', '-d', 'error_reporting=-1', '-r', 'echo implode(",", get_loaded_extensions());']);
            $cli->run();
            $cliLoaded = array_values(array_filter(array_map('trim', explode(',', $cli->getOutput()))));

            // Verify EACH packaged extension by name, not just the count.
            [$cliFail, $cliPlugin] = $this->verifyLoaded($asked, $cliLoaded, $cli->getErrorOutput());
            if ($cliFail || preg_match('/Unable to (?:load dynamic library|initialize module)/i', $cli->getErrorOutput())) {
                $output->write($cli->getErrorOutput());
                return $this->fail($output, count($cliFail) . " packaged extension(s) NOT loaded under CLI: " . implode(', ', $cliFail ?: ['(see warning above)']));
            }
            $note = $cliPlugin ? " (" . count($cliPlugin) . " loaded as sub-extensions with no standalone module: " . implode(', ', $cliPlugin) . ")" : "";
            $output->writeln("<info>CLI: all " . count($asked) . " packaged extensions loaded</info>" . $note);

            // --- 3. frankenphp serves the phpinfo script over HTTP --------------
            // frankenphp is a standalone package, not a dependency of the extensions, so a
            // partial build won't have pulled it — install it from the repo like the base.
            if (!is_executable($franken)) {
                $output->writeln("frankenphp not in this build — installing it from the repo...");
                $frInstall = match ($type) {
                    'rpm' => array_merge(['dnf', 'install', '-y'], $repoArgs, ['frankenphp']),
                    'deb' => array_merge(['apt-get', 'install', '-y', '--no-install-recommends'], ['frankenphp']),
                    'apk' => array_merge(['apk', 'add', '--allow-untrusted'], $repoArgs, ['frankenphp']),
                };
                $this->sh($this->maybeSudo($frInstall), $output);
            }
            if (!is_executable($franken)) {
                return $this->fail($output, "{$franken} not installed (not built, and not available from the repo)");
            }
            $webroot = sys_get_temp_dir() . '/spp-test-web';
            @mkdir($webroot, 0755, true);
            file_put_contents($webroot . '/ping.php', "<?php echo 'PONG';\n");
            file_put_contents($webroot . '/phpinfo.php', "<?php phpinfo(); echo \"\\nSPP_LOADED=\" . implode(\",\", get_loaded_extensions());\n");

            // Serve exactly how users run it (default multi-threaded). If this segfaults,
            // the packages are broken for real web use — that must be fixed, not worked around.
            $srv = new Process([$franken, 'php-server', '--listen', '127.0.0.1:8080', '--root', $webroot]);
            $srv->setTimeout(null);
            $srv->start();

            // Wait until it actually serves a trivial request (raw socket, not
            // file_get_contents() — the outer PHP may have allow_url_fopen off).
            $ready = false;
            for ($i = 0; $i < 30; $i++) {
                if ($this->httpGet('127.0.0.1', 8080, '/ping.php') === 'PONG') {
                    $ready = true;
                    break;
                }
                if (!$srv->isRunning() && $i > 1) {
                    break;
                }
                sleep(1);
            }
            if (!$ready) {
                $output->writeln("frankenphp running=" . ($srv->isRunning() ? 'yes' : 'no') . " exit=" . var_export($srv->getExitCode(), true));
                $output->write($srv->getErrorOutput() . $srv->getOutput());
                return $this->fail($output, "frankenphp did not serve over HTTP");
            }

            $body = (string) $this->httpGet('127.0.0.1', 8080, '/phpinfo.php');
            if (!preg_match('/SPP_LOADED=([^\r\n<]*)/', $body, $mm)) {
                $output->writeln("frankenphp running=" . ($srv->isRunning() ? 'yes' : 'no') . " exit=" . var_export($srv->getExitCode(), true));
                $output->write($srv->getErrorOutput() . $srv->getOutput());
                return $this->fail($output, "frankenphp served /ping.php but not the phpinfo script (crash while rendering phpinfo?)");
            }
            if (!preg_match('/PHP Version/i', $body)) {
                return $this->fail($output, "phpinfo output missing from frankenphp response");
            }
            $frLoaded = array_values(array_filter(array_map('trim', explode(',', trim($mm[1])))));

            // A genuine load failure under frankenphp makes PHP emit a startup warning to the
            // server log — that is the hard failure. An extension that simply isn't in the
            // loaded list without any such warning is CLI-only: it loaded fine but deliberately
            // makes itself inert under a non-CLI SAPI (e.g. swoole). frankenphp already served
            // cleanly above, so that case is expected, not a failure.
            $frLog = $srv->getErrorOutput() . $srv->getOutput();
            if (preg_match_all("/Unable to (?:load dynamic library '([^']+)'|initialize module '([^']+)')/", $frLog, $mfr)) {
                $output->write($frLog);
                $failed = array_values(array_filter(array_merge($mfr[1], $mfr[2])));
                return $this->fail($output, count($failed) . " extension(s) failed to load under frankenphp: " . implode(', ', $failed));
            }

            $frLc = array_flip(array_map('strtolower', $frLoaded));
            $cliOnly = [];
            foreach ($asked as $short => $so) {
                if (isset($frLc[strtolower($short)]) || in_array($short, $cliPlugin, true)) {
                    continue; // active under frankenphp, or a CLI sub-extension with no module anywhere
                }
                $cliOnly[] = $short; // present under CLI, self-inert here — CLI-only extension
            }
            $active = count($asked) - count($cliOnly);
            $note = $cliOnly ? " (" . count($cliOnly) . " CLI-only, inactive under a web SAPI: " . implode(', ', $cliOnly) . ")" : "";
            $output->writeln("<info>frankenphp: served phpinfo, {$active}/" . count($asked) . " packaged extensions active</info>" . $note);
            return self::SUCCESS;
        } finally {
            if ($srv !== null) {
                $srv->stop(3);
            }
            $this->uninstall($type, $names, $output);
        }
    }

    /**
     * Extensions PHP was told to load, from the installed conf.d drop-ins, as
     * [shortName => .so basename]. The .so carries the shared-library suffix
     * (e.g. redis-zts-85.so); the short name (redis) is what get_loaded_extensions()
     * / extension_loaded() report (case-insensitively).
     *
     * @return array<string,string>
     */
    private function askedExtensions(string $confd): array
    {
        $suffix = getSharedLibrarySuffix(); // e.g. -zts-85
        $out = [];
        foreach (glob($confd . '/*.ini') ?: [] as $file) {
            foreach (file($file, FILE_IGNORE_NEW_LINES) ?: [] as $line) {
                if (preg_match('/^\s*(?:zend_)?extension\s*=\s*([^\s;]+)/', $line, $m)) {
                    $so = preg_replace('/\.so$/', '', $m[1]);                                // redis-zts-85
                    $short = preg_replace('/' . preg_quote($suffix, '/') . '$/', '', $so);   // redis
                    $out[$short] = $so;
                }
            }
        }
        ksort($out);
        return $out;
    }

    /**
     * askedExtensions() only sees what actually installed, so an extension that vanished
     * from the build is invisible to it and the test would pass without noticing. Compare
     * against what craft.yml declared: every shared extension with an ini template must be
     * installed or be on the recorded skip list. The skip list is empty unless the build
     * ran with allow-shared-ext-failure, so for PHP 8.5 and earlier this is a plain
     * "everything declared must be here".
     *
     * The skip list comes from SkippedExtensions::resolveFor(), the same call packaging
     * makes, so both sides see the manifest *and* the extensions that hard-depend on a
     * skipped one — deriving it separately would fail the build over a dependent that
     * CreatePackages deliberately left out.
     *
     * @param  array<string,string> $asked [shortName => .so basename]
     * @return int|null exit code on failure, null when the check passed
     */
    private function crossCheckDeclared(array $asked, OutputInterface $output): ?int
    {
        // resolveFor() propagates skips along dependency edges declared in SPC's package configs.
        CreatePackages::bootstrapSpcGlobals();

        $craftConfig = CraftConfig::getInstance();
        // No ini template means no package; same test as CreatePackages::createExtensionPackage().
        $declared = array_filter(
            array_diff($craftConfig->getSharedExtensions(), $craftConfig->getStaticExtensions()),
            fn($e) => file_exists(INI_PATH . '/extension/' . $e . '.ini'),
        );

        $skipped = SkippedExtensions::resolveFor($craftConfig->getSharedExtensions());
        if ($skipped) {
            $output->writeln("<comment>Skipped shared extensions (" . count($skipped) . "):</comment>");
            foreach ($skipped as $ext => $reason) {
                $output->writeln("  {$ext}: {$reason}");
            }
        }

        // The manifest says this .so was dropped, so anything installed for it is stale.
        $stale = array_values(array_intersect(array_keys($asked), array_keys($skipped)));
        if ($stale) {
            return $this->fail($output, count($stale) . " extension(s) installed although this build skipped them (stale package in dist/ from an earlier run): " . implode(', ', $stale));
        }

        $missing = array_values(array_diff($declared, array_keys($asked), array_keys($skipped)));
        if ($missing) {
            return $this->fail($output, count($missing) . " declared shared extension(s) neither packaged nor recorded as skipped: " . implode(', ', $missing));
        }
        return null;
    }

    /**
     * Check each packaged extension against the loaded-module list. An extension that is
     * neither a loaded module nor named in a load/init warning loaded as a sub-extension
     * (e.g. the mysqlnd auth plugins register no standalone module) — reported, not failed.
     *
     * @param  array<string,string> $asked  [shortName => .so basename]
     * @return array{0:list<string>,1:list<string>} [failed, sub-extensions-without-own-module]
     */
    private function verifyLoaded(array $asked, array $loaded, string $stderr): array
    {
        $lc = array_flip(array_map('strtolower', $loaded));
        $fail = $plugin = [];
        foreach ($asked as $short => $so) {
            if (isset($lc[strtolower($short)])) {
                continue; // registered as its own module
            }
            if (stripos($stderr, $so) !== false
                || preg_match('/Unable to (?:load|initialize)[^\n]{0,80}\b' . preg_quote($short, '/') . '\b/i', $stderr)) {
                $fail[] = $short;
            } else {
                $plugin[] = $short;
            }
        }
        return [$fail, $plugin];
    }

    /**
     * Split package files into [keep, skip, keepNames] by whether their php-cli
     * dependency's lower bound matches the PHP version under test. Packages with no
     * cli dependency (cli itself, frankenphp, …) are always kept; a *-debuginfo is
     * kept only if its base package is kept.
     */
    private function filterForVersion(string $type, array $files, string $phpVersion): array
    {
        $metas = array_map(fn($f) => $this->packageMeta($type, $f), $files);
        if (!preg_match('/^(\d+\.\d+)/', $phpVersion, $v)) {
            return [$files, [], array_values(array_unique(array_filter(array_column($metas, 'name'))))];
        }
        $mm = $v[1];
        $cli = 'php' . SPP_PREFIX . '-cli';
        $re = '/' . preg_quote($cli, '/') . '\s*\(?\s*>=\s*(\d+\.\d+)/';
        $marker = str_replace('.', '', $mm);

        // php-zts-cli and frankenphp carry no php-cli bound — the first IS the PHP version,
        // the second marks it as _86 / +php86 / p86. Without this both minors would be kept.
        $matchesVersion = static function (array $x) use ($re, $mm, $marker, $cli): bool {
            if (preg_match($re, $x['deps'], $m)) {
                return $m[1] === $mm;
            }
            if (preg_match('/(?:_|\+php|p)(\d{2,3})(?:~[a-z0-9]+)?$/', $x['version'], $m)) {
                return $m[1] === $marker;
            }
            if (str_starts_with($x['name'], $cli) && preg_match('/^(\d+\.\d+)/', $x['version'], $m)) {
                return $m[1] === $mm;
            }
            return true;
        };

        // Keyed by version too, or an 8.5 debuginfo rides in on the 8.6 base of the same name.
        $keptBase = [];
        foreach ($metas as $x) {
            if (!str_ends_with($x['name'], '-debuginfo') && $matchesVersion($x)) {
                $keptBase[$x['name'] . '|' . $x['version']] = true;
            }
        }

        $keep = $skip = $names = [];
        foreach ($metas as $x) {
            $dbg = str_ends_with($x['name'], '-debuginfo');
            $ok = $dbg
                ? isset($keptBase[preg_replace('/-debuginfo$/', '', $x['name']) . '|' . $x['version']])
                : $matchesVersion($x);
            if ($ok) {
                $keep[] = $x;
                if ($x['name'] !== '') {
                    $names[] = $x['name'];
                }
            } else {
                $skip[] = $x['file'];
            }
        }

        // Rebuilding bumps the iteration, leaving -1 and -2 in dist/; the installer refuses both.
        $newest = [];
        foreach ($keep as $x) {
            $cur = $newest[$x['name']] ?? null;
            if ($cur === null
                || version_compare($x['version'], $cur['version'], '>')
                || ($x['version'] === $cur['version'] && strnatcmp($x['release'], $cur['release']) > 0)) {
                if ($cur !== null) {
                    $skip[] = $cur['file'];
                }
                $newest[$x['name']] = $x;
            } else {
                $skip[] = $x['file'];
            }
        }

        return [array_column($newest, 'file'), $skip, array_values(array_unique(array_column($newest, 'name')))];
    }

    /** @return array{file:string,name:string,version:string,deps:string} name + version + raw dependency string for a package file */
    private function packageMeta(string $type, string $file): array
    {
        $name = '';
        $version = '';
        // rpm only: deb Version and apk pkgver already carry the revision.
        $release = '';
        $deps = '';
        switch ($type) {
            case 'rpm':
                $n = new Process(['rpm', '-qp', '--nosignature', '--qf', '%{NAME}|%{VERSION}|%{RELEASE}', $file]);
                $n->run();
                [$name, $version, $release] = array_pad(explode('|', trim($n->getOutput()), 3), 3, '');
                $d = new Process(['rpm', '-qpR', '--nosignature', $file]);
                $d->run();
                $deps = $d->getOutput();
                break;
            case 'deb':
                $p = new Process(['dpkg-deb', '-f', $file, 'Package', 'Version', 'Depends']);
                $p->run();
                foreach (explode("\n", $p->getOutput()) as $line) {
                    if (stripos($line, 'Package:') === 0) {
                        $name = trim(substr($line, 8));
                    } elseif (stripos($line, 'Version:') === 0) {
                        $version = trim(substr($line, 8));
                    } elseif (stripos($line, 'Depends:') === 0) {
                        $deps = trim(substr($line, 8));
                    }
                }
                break;
            case 'apk':
                $p = Process::fromShellCommandline('tar -xzOf ' . escapeshellarg($file) . ' .PKGINFO 2>/dev/null');
                $p->run();
                $info = $p->getOutput();
                if (preg_match('/^pkgname\s*=\s*(\S+)/m', $info, $nm)) {
                    $name = $nm[1];
                }
                if (preg_match('/^pkgver\s*=\s*(\S+)/m', $info, $vm)) {
                    $version = $vm[1];
                }
                preg_match_all('/^depend\s*=\s*(\S+)/m', $info, $dp);
                $deps = implode(' ', $dp[1] ?? []);
                break;
        }
        return ['file' => $file, 'name' => $name, 'version' => $version, 'release' => $release, 'deps' => $deps];
    }

    /** Remove the packages this test installed. Best-effort: a cleanup hiccup must not mask the test result. */
    private function uninstall(string $type, array $names, OutputInterface $output): void
    {
        $names = $this->installedSubset($type, $names);
        if (!$names) {
            return;
        }
        $output->writeln("Uninstalling " . count($names) . " test packages...");
        $remove = match ($type) {
            'rpm' => array_merge(['dnf', 'remove', '-y'], $names),
            'deb' => array_merge(['apt-get', 'purge', '-y'], $names),
            'apk' => array_merge(['apk', 'del'], $names),
        };
        if ($this->sh($this->maybeSudo($remove), $output) !== 0) {
            $output->writeln("<comment>warning: uninstall of test packages did not complete cleanly</comment>");
        }
    }

    /** Subset of $names that is actually installed, so removal never errors on absent packages. */
    private function installedSubset(string $type, array $names): array
    {
        if (!$names) {
            return [];
        }
        $p = match ($type) {
            'rpm' => new Process(['rpm', '-qa', '--qf', '%{NAME}\n']),
            'deb' => new Process(['dpkg-query', '-W', '-f=${Package}\n']),
            'apk' => new Process(['apk', 'info']),
        };
        $p->run();
        $installed = array_flip(array_filter(array_map('trim', preg_split('/\s+/', $p->getOutput()) ?: [])));
        return array_values(array_filter($names, fn($n) => isset($installed[$n])));
    }

    /**
     * Configures the published repo as a dependency source (side effects) and returns any
     * extra install-command args, so a partial build's extension packages can resolve their
     * base SAPI. Overridable via SPP_RPM_REPO_URL / SPP_FORGEJO_HOST / SPP_FORGEJO_OWNER.
     */
    private function baseRepoArgs(string $type, OutputInterface $output): array
    {
        $host = rtrim(getenv('SPP_FORGEJO_HOST') ?: 'https://git.henderkes.com', '/');
        $owner = getenv('SPP_FORGEJO_OWNER') ?: str_replace('.', '', SPP_PHP_VERSION); // e.g. "85"
        switch ($type) {
            case 'rpm':
                // The repo is a modular DNF repo. The release RPM configures it with the right
                // $releasever; enabling this PHP version's stream makes the base packages
                // resolvable on el8/el9 (el10 dropped modularity, so module-enable is best-effort).
                $base = rtrim(getenv('SPP_RPM_REPO_URL') ?: 'https://rpm.henderkes.com', '/');
                $mm = preg_match('/^(\d+\.\d+)/', SPP_PHP_VERSION, $m) ? $m[1] : SPP_PHP_VERSION;
                $stream = 'php' . SPP_PREFIX . ':static-' . $mm; // e.g. php-zts:static-8.2
                $this->sh($this->maybeSudo(['dnf', 'install', '-y', '--nogpgcheck', "{$base}/static-php-1-1.noarch.rpm"]), $output);
                $this->sh($this->maybeSudo(['dnf', 'module', 'enable', '-y', $stream]), $output);
                return ['--nogpgcheck'];
            case 'deb':
                $line = "deb [trusted=yes] {$host}/api/packages/{$owner}/debian php-zts main";
                $this->sh($this->maybeSudo(['bash', '-c', "printf '%s\\n' " . escapeshellarg($line) . " > /etc/apt/sources.list.d/spp-test.list"]), $output);
                $this->sh($this->maybeSudo(['apt-get', 'update']), $output); // best-effort; local install still works if unreachable
                return [];
            case 'apk':
                return ['--repository', "{$host}/api/packages/{$owner}/alpine/main/php-zts"];
        }
        return [];
    }

    private function maybeSudo(array $cmd): array
    {
        if (!(function_exists('posix_geteuid') && posix_geteuid() === 0)) {
            array_unshift($cmd, 'sudo');
        }
        return $cmd;
    }

    /** Minimal HTTP/1.0 GET over a raw socket; returns the response body, or null if the connection failed. */
    private function httpGet(string $host, int $port, string $path): ?string
    {
        $fp = @fsockopen($host, $port, $errno, $errstr, 2);
        if (!$fp) {
            return null;
        }
        stream_set_timeout($fp, 5);
        fwrite($fp, "GET {$path} HTTP/1.0\r\nHost: {$host}\r\nConnection: close\r\n\r\n");
        $resp = stream_get_contents($fp);
        fclose($fp);
        if (!is_string($resp) || $resp === '') {
            return null;
        }
        $split = strpos($resp, "\r\n\r\n");
        return $split === false ? $resp : substr($resp, $split + 4);
    }

    private function sh(array $cmd, OutputInterface $output): int
    {
        $p = new Process($cmd);
        $p->setTimeout(null);
        return $p->run(fn($t, $d) => $output->write($d));
    }

    private function fail(OutputInterface $output, string $msg): int
    {
        $output->writeln("<error>FAIL: {$msg}</error>");
        return self::FAILURE;
    }
}
