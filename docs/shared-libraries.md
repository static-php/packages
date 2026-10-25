# Shared runtime libraries

PHP ZTS uses private shared libraries named `php-zts-lib-<library-id>`. Their
versions and revisions are independent of PHP. Shared compiler runtimes belong
to `php-zts-lib-gcc-runtime`; the host still supplies glibc, its loader and NSS.

## One glibc producer

EL8 is the only glibc compilation platform, with native x86_64 and arm64 jobs.
One PHP-independent SDK feeds all PHP streams. Each PHP/core/extension/FrankenPHP
payload is compiled once per stream and architecture, then packaged as RPM and
DEB without changing its common ELF bytes. EL9, EL10 and Debian do not compile
separate production payloads. Alpine retains its separate native-musl producer.

The logical runtime directory is `/usr/lib/php-zts/lib`; modules use
`/usr/lib/php-zts/modules`. RPM retains its real `/usr/lib64/php-zts` tree and a
package-owned canonical alias. DEB uses the real canonical tree and a compatibility
alias for retained libraries with compiled legacy lib64 plugin paths. The runtime
layout package owns these aliases, detects conflicting existing paths, and is a
hard dependency of libraries and PHP consumers. The embed DSO lives in the private
library tree, with legacy public linker names preserved.

OpenSSL has two EL8-built runtime profiles: `/etc/pki/tls` for RPM and `/etc/ssl`
for DEB. Both use the same source/toolchain, headers, SONAMEs and verified symbol
and type ABI. PHP, cURL and FrankenPHP remain common. Profile changes are isolated
from unrelated library build inputs. Runtime data, engines and providers stay
with their owning library.

`bin/build-payload` records and seals source versions, architecture, enabled and
skipped extensions, PHP module ABI, build configuration, common ELF/debug hashes
and packaging assets. Package-only stages consume that record instead of
regenerating compiler settings from their host. Package revision state lives in
sidecars rather than rewriting the compiled SDK.

## Existing installations

Keep existing repository URLs, signing keys, package names and PHP module streams.
Private library packages are available in each existing DEB/APK owner `82`–`86`;
clients do not add a separate `libs` repository or rerun an installer.

| Format | Ordinary upgrade |
| --- | --- |
| DEB | `apt update && apt upgrade` |
| DEB scripts | `apt-get update && apt-get upgrade --with-new-pkgs` |
| RPM | `dnf upgrade` |
| APK | `apk update && apk upgrade` |

Plain `apt-get upgrade` can hold the update because it refuses new dependency
packages. PHP 8.6 remains an opt-in prerelease stream. Supported baselines are
Debian 11+, Alpine 3.21+, and EL8–10.

Fileinfo and cURL are shared extensions. RPM/DEB SAPIs recommend both; APK uses
native `install_if` metadata tied to the shared-runtime capability. Defaults retain
these features, while explicit weak-dependency/selection opt-outs remain possible.

New modules require the matching shared PHP generation and module ABI. Core
migration constraints prevent an installed old extension from being silently left
in an incompatible mixed set. A published extension missing from the candidate
cohort blocks that migration; `allow-shared-ext-failure` does not authorize loss
of an installed feature.

## One RPM repository

Each architecture has one canonical `<arch>/el8/` repository, one RPM archive set,
and one primary/module metadata generation. Existing `<arch>/el9/` and
`<arch>/el10/` URL subtrees alias that repository. `createrepo_static` records EL8
only as a build requirement, not a runtime platform restriction.

New RPM releases are plain integers, without `.el8`, `.el9` or `.el10` suffixes.
The initial migration compares native EVRs against all three historical baselines:
removing a suffix at the same revision would sort lower. Legacy extension Version
spellings are preserved when necessary for the first shared replacement.

`bin/canonical-rpm-repository` stages a complete immutable primary/module generation
and promotes it atomically under a lock. Alias cutover retains historical RPM and
hash-named metadata URLs for cached clients. Stream names/defaults stay unchanged;
module publication versions increase monotonically.

## Incremental builds and validation

The glibc entrypoint is `build-rpm-modular-packages.yml`; it produces both formats.
The DEB workflow delegates to it rather than starting a second compiler pipeline.
For example, after the branch is available to Actions:

```sh
gh workflow run build-rpm-modular-packages.yml \
    -f libraries=curl,zlib -f architectures=x86_64 -f publish=false
```

`libraries` selects a library-only incremental request. Source/version changes
build only those libraries and genuine ABI dependents. OpenSSL expands to its
required profiles. Compatible library updates do not rebuild PHP. Package-only
changes reuse sealed PHP payloads; extension-only changes reuse the verified core.
`bump` defaults to false; explicit iterations take precedence. `publish=false`
builds and tests without repository or release-cache writes.

To bump just the selected library package revisions, add `-f bump=true` to the
example above. An unchanged compatible SDK is repackaged without recompilation.
Changed sources or recipes rebuild the selected libraries and required dependents.

The full matrix covers unique sets: RPM10 + DEB10 + APK10, across five PHP streams
and two native architectures. PHP-independent library/ABI checks are reused.
Additional EL9/EL10 and newer-client checks focus on compatibility and unchanged
legacy URLs instead of repeating the complete extension/profile matrix.

`bin/freeze-migration-baseline.py` freezes real published archives and their
checksums. `tests/migration-upgrade-containers.py` installs those packages through
a signed disposable HTTP repository, runs ordinary upgrades, verifies actual
version advancement/dependencies/features/configuration, and checks a second
no-op upgrade. Its local mirror key is established before the old installation;
production signing/download verification remains a separate publication gate.
`--profile all` and independent extension profiles require a complete frozen
baseline. `--library-only` checks that PHP versions remain unchanged.

`tests/runtime-service-containers.py` exercises real systemd/OpenRC service state,
requests and mapped executable/library identities across core and library-only
upgrades, including stopped/disabled state. Native arm64 validation requires the
GitHub runners; local x86_64 results do not establish arm64 or minimum-kernel
compatibility.

GCC assets must contain genuine DWARF4 type information, licenses and a development
selector that links shared libstdc++. `bin/build-gcc-runtime --check` verifies them;
repair builds matching target runtimes only when needed. A validated SDK import
records actual old compiler/recipe identities and deployment relocation separately,
so compatible C-only libraries survive a layout or runtime-asset repair.

The SPC lock currently pins local revision
`fbff6898d85b56a172aedc47051fab09939c2a97`; remote CI needs that revision available
from its configured source. No push, package upload, repository promotion or
public installer replacement is performed by local preparation. Those operations
require explicit authorization.
