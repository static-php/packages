#!/usr/bin/env bash
set -euo pipefail

GNUPGHOME=$(mktemp -d)
export GNUPGHOME
trap 'gpgconf --kill gpg-agent || true; rm -rf "$GNUPGHOME"' EXIT
chmod 700 "$GNUPGHOME"
printf '%s\n' allow-loopback-pinentry > "$GNUPGHOME/gpg-agent.conf"
printf '%s' "$RPM_SIGNING_KEY" | gpg --batch --yes --import
fingerprint=$(gpg --batch --with-colons --list-secret-keys | awk -F: '$1 == "fpr" {print $10; exit}')
test -n "$fingerprint"
printf '%s\n' warmup > "$GNUPGHOME/warmup"
gpg --batch --yes --pinentry-mode loopback --passphrase-fd 0 --local-user "$fingerprint" \
    --sign --output /dev/null "$GNUPGHOME/warmup" <<< "$RPM_SIGNING_PASSWORD"
for archive in dist/rpm/*.rpm; do
    rpmsign --define "_gpg_name $fingerprint" --define "_gpg_path $GNUPGHOME" \
        --define '__gpg /usr/bin/gpg' --addsign "$archive"
done
