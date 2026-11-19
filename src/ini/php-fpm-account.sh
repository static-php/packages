#!/bin/sh
set -e
getent group frankenphp >/dev/null || groupadd --system frankenphp
getent passwd frankenphp >/dev/null || useradd --system --gid frankenphp --home-dir /var/lib/frankenphp --shell /sbin/nologin --comment 'PHP application server' frankenphp
