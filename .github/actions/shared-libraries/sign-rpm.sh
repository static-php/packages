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
python3 - "$GNUPGHOME/reused-signatures" <<'PYTHON'
import hashlib,json,sys
from pathlib import Path
reused = {}
for receipt in Path('cohort').glob('shared-libs-rpm-*/publication-retries.json'):
    for item in json.loads(receipt.read_text()).get('copied_archives', []):
        name = Path(item['path']).name
        path = Path('dist/rpm') / name
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest != item['sha256']:
            raise RuntimeError('Exact publication retry bytes changed before signing: ' + name)
        reused[name] = digest
Path(sys.argv[1]).write_bytes(b''.join(name.encode() + b'\0' for name in sorted(reused)))
PYTHON
declare -A existing_signatures=()
while IFS= read -r -d '' name; do existing_signatures["$name"]=true; done < "$GNUPGHOME/reused-signatures"
for archive in dist/rpm/*.rpm; do
    if [[ "${existing_signatures[${archive##*/}]:-}" == true ]]; then continue; fi
    unsigned_hash=$(sha256sum "$archive" | cut -d ' ' -f 1)
    if [[ -n "${SIGNED_RPM_CACHE:-}" && -f "$SIGNED_RPM_CACHE/$unsigned_hash" ]]; then
        cp "$SIGNED_RPM_CACHE/$unsigned_hash" "$archive"
        continue
    fi
    rpmsign --define "_gpg_name $fingerprint" --define "_gpg_path $GNUPGHOME" \
        --define '__gpg /usr/bin/gpg' --addsign "$archive"
    if [[ -n "${SIGNED_RPM_CACHE:-}" ]]; then cp "$archive" "$SIGNED_RPM_CACHE/$unsigned_hash"; fi
done
