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
python3 - "$GNUPGHOME/reused-signatures" "${BASH_SOURCE[0]%/*}/../../../bin/check-rpm-upgrades" <<'PYTHON'
import hashlib,json,os,runpy,sys
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
if os.environ.get('RPM_HOST') and os.environ.get('RPM_DIRECTORY'):
    guard = runpy.run_path(sys.argv[2])
    def read_file(location, missing_ok=False):
        return guard['read_remote_file'](os.environ['RPM_HOST'], os.environ['RPM_DIRECTORY'], location, missing_ok)
    published = guard['published_packages'](read_file)
    for path in sorted(Path('dist/rpm').glob('*.rpm')):
        if path.name not in reused and guard['reuse_published_signature'](path, published, read_file, os.environ.get('SIGNED_RPM_CACHE')):
            reused[path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
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
