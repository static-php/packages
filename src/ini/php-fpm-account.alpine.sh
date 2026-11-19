#!/bin/sh
set -e
getent group frankenphp >/dev/null || addgroup -S frankenphp
getent passwd frankenphp >/dev/null || adduser -S -D -H -h /var/lib/frankenphp -s /sbin/nologin -G frankenphp -g 'PHP application server' frankenphp
