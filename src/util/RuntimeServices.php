<?php

declare(strict_types=1);

namespace staticphp\util;

use RuntimeException;

final class RuntimeServices
{
    private const PATHS = ['/usr/lib/php-zts', '/usr/lib64/php-zts', '/usr/share/php-zts/services', '/usr/share/php-zts/consumers', '/usr/bin', '/usr/sbin', '/usr/lib/systemd/system'];

    public static function layoutConfig(string $type): array
    {
        $files = [BASE_PATH . '/src/runtime/runtime-services' => '/usr/libexec/php-zts/runtime-services'];
        if ($type === 'apk') {
            $files[BASE_PATH . '/src/runtime/apk-commit-hook'] = '/etc/apk/commit_hooks.d/php-zts-runtime';
        }
        return ['files' => $files];
    }

    public static function layoutArgs(string $type): array
    {
        $refresh = '/usr/libexec/php-zts/runtime-services finish';
        if ($type === 'deb') {
            $script = self::script('runtime-layout-postinst', "case \"\${1-}\" in triggered) {$refresh} ;; esac\n");
            $args = ['--after-install', $script, '--deb-interest-noawait', 'php-zts-runtime'];
            foreach (self::PATHS as $path) {
                $args[] = '--deb-interest-noawait';
                $args[] = $path;
            }
            return $args;
        }
        if ($type === 'rpm') {
            // FPM has no file-trigger option; its posttrans body can contain the native spec section.
            $script = self::script('runtime-layout-posttrans', $refresh . "\n\n%transfiletriggerin -- " . implode(' ', self::PATHS) . "\n" . $refresh . "\n");
            return ['--rpm-posttrans', $script];
        }
        return [];
    }

    public static function descriptor(string $kind, string $service, string $executable, string $configuration): array
    {
        if (!in_array($kind, ['fpm', 'frankenphp'], true)
            || !preg_match('/^[a-zA-Z0-9_.-]+$/D', $service)
            || !str_starts_with($executable, '/') || !str_starts_with($configuration, '/')
            || strpbrk($executable . $configuration, "|\r\n") !== false) {
            throw new RuntimeException('Invalid managed PHP service descriptor');
        }
        $path = TEMP_DIR . '/service-' . $service;
        $modules = function_exists('getModuledir') ? \getModuledir() : '/usr/lib/php-zts/modules';
        file_put_contents($path, implode('|', [$kind, $service, $executable, $configuration, $modules]) . "\n");
        return [$path => '/usr/share/php-zts/services/' . $service];
    }

    public static function serviceArgs(string $type, string $service, bool $frankenphp = false): array
    {
        $setup = self::script($service . '-setup-' . $type, self::accountSetup($type) . self::unitSetup($type, $service, $frankenphp));
        $remove = self::script($service . '-remove-' . $type, self::removeService($type, $service));
        $args = ['--before-install', $setup, '--before-upgrade', $setup, '--before-remove', $remove];
        if ($type === 'deb') {
            $args[] = '--after-install';
            $args[] = self::script($service . '-ready-deb', self::debianTrigger());
        }
        return $args;
    }

    public static function apkScripts(string $service, bool $frankenphp = false): array
    {
        return ['preupgrade' => self::script($service . '-apk-preupgrade', self::accountSetup('apk')),
            'postupgrade' => self::script($service . '-apk-postupgrade', self::unitSetup('apk', $service, $frankenphp))];
    }

    public static function frankenPostInstall(string $type, string $service, string $executable, bool $upgrade = false): string
    {
        $fresh = '';
        if ($type === 'deb') {
            $fresh = str_replace('@service@', $service, <<<'SH'
if [ "${1-}" = configure ] && [ -z "${2-}" ]; then
    if deb-systemd-helper --quiet was-enabled @service@.service; then
        deb-systemd-helper unmask @service@.service >/dev/null || :
        deb-systemd-helper enable @service@.service >/dev/null || :
        mkdir -p /run/php-zts-runtime/start
        : > /run/php-zts-runtime/start/@service@
    else
        deb-systemd-helper update-state @service@.service >/dev/null || :
    fi
fi

SH);
        } elseif ($type === 'apk' && !$upgrade) {
            $fresh = 'if command -v rc-update >/dev/null 2>&1; then rc-update add ' . escapeshellarg($service) . ' default; fi' . "\n"
                . 'mkdir -p /run/php-zts-runtime/start; : > /run/php-zts-runtime/start/' . $service . "\n";
        } elseif ($type === 'rpm' && !$upgrade) {
            $fresh = 'if [ -d /run/systemd/system ] && command -v systemctl >/dev/null 2>&1; then systemctl preset ' . escapeshellarg($service . '.service') . ' || :; fi' . "\n";
        }
        return self::script($service . ($upgrade ? '-upgrade-' : '-postinstall-') . $type, self::accountSetup($type)
            . 'mkdir -p /var/lib/frankenphp /var/log/frankenphp' . "\n"
            . 'chown frankenphp:frankenphp /var/lib/frankenphp /var/log/frankenphp' . "\n"
            . 'chmod 0755 /var/lib/frankenphp /var/log/frankenphp' . "\n"
            . 'if command -v setcap >/dev/null 2>&1; then setcap cap_net_bind_service=+ep ' . escapeshellarg($executable) . ' || :; fi' . "\n"
            . ($type === 'rpm' ? self::selinuxSetup($executable) : '')
            . self::unitSetup($type, $service, true) . $fresh . ($type === 'deb' ? self::debianTrigger() : ''));
    }

    private static function debianTrigger(): string
    {
        return 'if [ "${1-}" = configure ]; then dpkg-trigger --no-await php-zts-runtime; fi' . "\n";
    }

    private static function selinuxSetup(string $executable): string
    {
        return str_replace('@binary@', escapeshellarg($executable), <<<'SH'
if [ -x /usr/sbin/getsebool ]; then setsebool -P httpd_can_network_connect on || :; fi
if [ -x /usr/sbin/semanage ] && [ -x /usr/sbin/restorecon ]; then
    semanage fcontext --add --type httpd_exec_t @binary@ 2>/dev/null || :
    semanage fcontext --add --type httpd_sys_content_t '/usr/share/frankenphp(/.*)?' 2>/dev/null || :
    semanage fcontext --add --type httpd_config_t '/etc/frankenphp(/.*)?' 2>/dev/null || :
    semanage fcontext --add --type httpd_var_lib_t '/var/lib/frankenphp(/.*)?' 2>/dev/null || :
    semanage fcontext --add --type httpd_sys_rw_content_t '/var/lib/frankenphp(/.*\.db)' 2>/dev/null || :
    restorecon -r @binary@ /usr/share/frankenphp /etc/frankenphp /var/lib/frankenphp || :
fi
if [ -x /usr/sbin/semanage ]; then
    semanage port --add --type http_port_t --proto udp 80 2>/dev/null || :
    semanage port --add --type http_port_t --proto udp 443 2>/dev/null || :
    semanage port --add --type http_port_t --proto tcp 2019 2>/dev/null || :
fi

SH);
    }

    private static function accountSetup(string $type): string
    {
        if ($type === 'apk') {
            return <<<'SH'
if ! getent group frankenphp >/dev/null 2>&1; then addgroup -S frankenphp; fi
if ! getent passwd frankenphp >/dev/null 2>&1; then
    adduser -S -D -H -h /var/lib/frankenphp -s /sbin/nologin -G frankenphp -g 'PHP application server' frankenphp
fi

SH;
        }
        return <<<'SH'
if ! getent group frankenphp >/dev/null 2>&1; then groupadd --system frankenphp; fi
if ! getent passwd frankenphp >/dev/null 2>&1; then
    useradd --system --gid frankenphp --home-dir /var/lib/frankenphp --shell /sbin/nologin --comment 'PHP application server' frankenphp
fi

SH;
    }

    private static function unitSetup(string $type, string $service, bool $frankenphp): string
    {
        if ($type === 'apk') {
            return '';
        }
        return "if [ -d /run/systemd/system ] && command -v systemctl >/dev/null 2>&1; then systemctl daemon-reload || :; fi\n";
    }

    private static function removeService(string $type, string $service): string
    {
        if ($type === 'apk') {
            return 'if command -v rc-service >/dev/null 2>&1; then rc-service ' . escapeshellarg($service) . ' stop || :; fi' . "\n"
                . 'if command -v rc-update >/dev/null 2>&1; then rc-update del ' . escapeshellarg($service) . ' default >/dev/null 2>&1 || :; fi' . "\n";
        }
        $condition = $type === 'rpm' ? '[ "${1-}" = 0 ]' : '[ "${1-}" = remove ] || [ "${1-}" = deconfigure ]';
        return 'if { ' . $condition . '; } && [ -d /run/systemd/system ]; then' . "\n"
            . '    if command -v deb-systemd-invoke >/dev/null 2>&1; then deb-systemd-invoke stop ' . escapeshellarg($service . '.service') . ' || :;' . "\n"
            . '    else systemctl stop ' . escapeshellarg($service . '.service') . ' || :; fi' . "\nfi\n";
    }

    private static function script(string $name, string $contents): string
    {
        $path = TEMP_DIR . '/' . $name . '.sh';
        if (file_put_contents($path, "#!/bin/sh\nset -e\n" . $contents) === false || !chmod($path, 0755)) {
            throw new RuntimeException('Cannot stage PHP runtime service script');
        }
        return $path;
    }
}
