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

The Debian 12 image `ghcr.io/static-php/packages-packager-debian` packages the
sealed EL8 payload. Installation, upgrade and TLS tests use separate
`debian:11` containers.

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
with their owning library. OpenSSL 3's `libcrypto.so.3` links to the private
`libz.so.1`; `libssl.so.3` links to `libcrypto.so.3`.

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

Extension packages keep their original names on every format, including Alpine.
Runtime and debuginfo packages upgrade in place; no `-shared` replacement names
or versioned compatibility aliases are used.

## Package versions

FrankenPHP, PIE and independently versioned extensions use their own upstream
version followed by the PHP stream: `1.13.6_82` for RPM, `1.13.6+php82` for DEB,
and `1.13.6p82` for APK. FrankenPHP and compiled extensions also include PHP's
phase during the initial `x.y.0` prerelease cycle: `1.13.6_86~rc5`,
`1.13.6+php86~rc5`, or `1.13.6p86_rc5`. Their own prerelease precedes the PHP
tag, for example `3.2.17~rc1_86~rc5` or `3.2.17_rc1_p86_rc5`.
After PHP reaches GA, stable patches and patch RCs such as `8.6.1RC1` keep
the stream-only tag. PIE always uses the stream-only tag. PHP-versioned
packages keep the full PHP version, including patch RCs.

Compiled consumers require the shared-runtime capability for the exact initial
PHP prerelease, even if two prereleases report the same module API number.
GA and subsequent patches/patch RCs share the stable capability. PIE has no
compiled PHP ABI requirement.

For an already published upstream release, packaging retains its historical
structure and advances the revision when needed for an ordinary upgrade. The
PHP phase within that structure still advances with the runtime, and is removed
at GA or for PIE. Each package switches to the shorter tag at its next upstream
release, without an epoch. Historical tags remain readable by repository and
upgrade tooling.

Malformed historical APK versions containing `~`, such as `3.2.17~rc1_82-r1`,
block corrected spellings of the same numeric upstream version: APK treats
revision changes after the malformed part as equal, and valid spellings sort
lower. A real higher numeric upstream release can upgrade normally: for example,
`3.2.18p82-r0` sorts above `3.2.17~rc1_82-r1`, whereas `3.2.17p82-r999` does not.
The published blockers affect AMQP on PHP 8.5 at git.henderkes.com and igbinary
on PHP 8.2–8.5 at both Forgejo hosts. Package names and real upstream versions
stay unchanged; no synthetic prefixes are added. Until a real higher upstream
release is available, preflight rejects lower candidates. Valid historical
versions continue using ordinary revision bumps. Preflight checks the real
source version and prerelease phase as well as native ordering.

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

To publish those tested archives later, dispatch `publish-packages.yml` with
`format=rpm`, `format=deb` or `format=apk` and `build_run_id=<package-build-run>`.
Each dispatch publishes only its selected format. RPM and DEB refer to the same
shared EL8 run; APK refers to its native Alpine run. This does not compile,
repackage, change package revisions or save release caches. Set `bump=true` on
the original build if revision bumps are needed.

The selected run must have completed on the default branch. Complete architecture
cohorts can be published even if another architecture failed. Missing or expired
artifacts exclude that architecture; no complete cohort is an error. Plans,
packages and their validation receipts are retained for seven days in new builds.
The publisher uses the recorded image digests and immutable artifact IDs, checks
native upgrade receipts against archive hashes, signs RPMs, and reruns the cohort
and current repository version guards before uploading. Concurrent
publication to the same format and architecture is serialized across workflows.

Each producer and architecture caches its whole SDK buildroot as a weekly release
asset. Daily builds restore a compatible SDK, check source and build inputs,
rebuild changed libraries and genuine ABI dependents, and save the whole updated
SDK. They compile the whole SDK only when no compatible seed exists. Monday's
full PHP request still reuses a compatible SDK.

To bump just the selected library package revisions, add `-f bump=true` to the
example above. An unchanged compatible SDK is repackaged without recompilation.
Changed sources or recipes rebuild the selected libraries and required dependents.

The full matrix covers unique sets: RPM10 + DEB10 + APK10, across five PHP streams
and two native architectures. PHP-independent library/ABI checks are reused.
RPM runtime validation runs on EL8 only. Debian uses Debian 11 and APK uses
Alpine 3.21.

`bin/freeze-migration-baseline.py` freezes real published archives and their
checksums. `tests/migration-upgrade-containers.py` installs those packages through
a signed disposable HTTP repository, runs ordinary upgrades, verifies actual
version advancement/dependencies/features/configuration, and checks a second
no-op upgrade. Its local mirror key is established before the old installation;
production signing/download verification remains a separate publication gate.
`--profile all` and independent extension profiles require a complete frozen
baseline. `--library-only` checks that PHP versions remain unchanged.

Systemd-dependent service tests are not required and are excluded from CI.
Alpine retains unprivileged OpenRC service checks through
`tests/runtime-service-containers.py`, which defaults to Docker; `--container-engine`
selects an alternative engine. The harness rejects privileged containers and uses
private namespaces without host cgroup, systemd/D-Bus or engine socket mounts.
Service commands run only through the selected engine's exec command in tracked test containers;
host shutdown and service-manager commands are rejected by the harness.
The checks cover requests and mapped executable/library identities across core
and library-only upgrades, including stopped/disabled state. Native arm64 validation requires the
GitHub runners; local x86_64 results do not establish arm64 or minimum-kernel
compatibility.

GCC assets must contain genuine DWARF type information, licenses and a development
selector that links shared libstdc++. `bin/build-gcc-runtime --check` verifies them;
it accepts DWARF4 and DWARF5. Repair builds matching target runtimes only when
needed. A validated SDK import records actual old compiler/recipe identities and
deployment relocation separately,
so compatible C-only libraries survive a layout or runtime-asset repair.

The SPC lock pins `dfc33a7963cad4135a8353b34b0d87e1a49a6589` on
`feat/el8-shared-libraries`. This reconstructs the missing prerequisite with
opt-in shared-library support; static builds keep their existing defaults.
[Reconstruction checks](spc-reconstruction-validation.json) record the current
EL8/Alpine checks separately from historical migration validation. The current
EL8 SDK includes all 67 ordinary libraries with typed ABI verification. PHP
consumer and package upgrade coverage is recorded in that report; historical
results do not validate this reconstruction. Remote CI needs the pinned revision
available from its configured source. No push, package upload, repository promotion
or public installer replacement is performed by local preparation. Those operations
require explicit authorization.
