# Shared runtime libraries

PHP ZTS packages use private shared runtime libraries, one package per upstream
library, named `php-zts-lib-<library-id>`. They have upstream library versions and
package revisions independent of the selected PHP stream. Shared GCC runtime files
belong to `php-zts-lib-gcc-runtime`.

The runtime directory is `/usr/lib64/php-zts/lib` on RPM systems and
`/usr/lib/php-zts/lib` on Debian and Alpine. Consumers and libraries carry this
directory in their ELF runtime search path. These files do not replace the system's
libraries. Headers and linker metadata stay in the buildroot; runtime assets such as
providers and plugins are included with the library that needs them.

## Configure repositories and migrate

Existing Debian/Ubuntu and Alpine installations need the common library repository
**before** their first upgrade to shared PHP packages. From this checkout, run:

```sh
sudo sh bin/install-repositories 8.5
```

Pass the installed PHP stream explicitly: `8.2`, `8.3`, `8.4`, `8.5`, or `8.6`.
Compact owner names (`82`–`86`) also work. The setup is safe to rerun. It preserves
other repository entries and fails if either signing key cannot be downloaded.
Use the following commands for the relevant package manager:

| Format | Refresh and install |
| --- | --- |
| DEB | `apt-get update` then `apt-get install php-zts-cli` |
| APK | `apk update` then `apk add php-zts-cli` |
| RPM | `dnf module switch-to php-zts:static-8.5` then `dnf install php-zts-cli` |

For existing installations, refresh metadata and use the normal package-manager
upgrade command instead of installing the CLI again. Review the proposed transaction.
PHP 8.6 remains an explicitly selected prerelease stream.

DEB uses `https://pkg.henderkes.com/api/packages/<stream>/debian` and
`https://pkg.henderkes.com/api/packages/libs/debian`, both with distribution `php-zts`
and component `main`. Each owner has a separate key in `/etc/apt/keyrings`, referenced
by `signed-by` in its source file. APK uses the corresponding
`/api/packages/<owner>/alpine/main/php-zts` URLs and the signing-key filenames supplied
by Forgejo. RPM keeps the existing `rpm.henderkes.com` repository and module streams;
library packages are outside the PHP modules.

`bin/install-repositories` is the local setup script. Updating the separately hosted
`https://files.henderkes.com/install.sh` requires an authorized publication step.
Until that update is published, use the script from this checkout for the common
library repository.

## Compatible updates and ABI changes

Packages depend on the private libraries used by their own payloads, with minimum
versions and private ABI capabilities. A compatible library update can install
independently of PHP. A breaking ABI update changes its private capability and requires
the affected libraries, extensions, SAPIs, and PHP streams to upgrade together.

There is one installed ABI per library and one flat private runtime directory. The
package solver must retain the old set while an incomplete replacement set is
available. Do not force a breaking library update past unsatisfied dependencies.
Release preparation compares the previous and candidate ABI and rebuilds the reverse
dependency closure; an uncertain comparison or missing consumer blocks the transition.

## Build and release administration

Build and package libraries without compiling PHP:

```sh
php bin/spp build --type=rpm --libs-only
php bin/spp package --type=rpm --libs-only
```

Substitute `deb` or `apk` as needed. `--libraries=openssl,zlib` selects library packages;
omitting it selects the configured library set. Packaging consumes the mandatory
`buildroot/shared-libraries.json` manifest, which records library ownership, source
versions, runtime files, SONAMEs, dependencies, and build identity.

Builder images require real DWARF4 information in GCC's `libgcc_s`, `libstdc++`, and
available optional runtimes such as `libatomic` and `libgomp`, plus `COPYING3` and
`COPYING.RUNTIME` under `/opt/gcc/share/licenses/gcc`. Source toolchain builds retain
runtime debugging information while stripping compiler
binaries. An older stripped toolchain archive triggers a matching-version native
GCC source build and installs its runtime libraries without replacing the selected
compiler. This fallback builds compiler support too, so it can take substantially
longer than using a refreshed toolchain archive. `SPP_GCC_BUILD_JOBS` limits its
parallelism. `bin/build-gcc-runtime --check` verifies an installed toolchain without
downloading or rebuilding it. The toolchain workflow verifies these requirements
before preparing new archives.

Package workflows accept `mode=full|incremental` and optional `libraries` selections.
The pinned `spc-download` snapshot carries the build plan. A compatible library-only
change builds its library packages without selecting every PHP stream; an ABI break
adds affected consumers to the build. A full run retains the normal complete build.

The prepared Composer lock pins SPC revision
`d910c62cf4dbda527688038705449513ad7c0046`. This revision has been committed locally;
CI cannot fetch it until it is published to the configured SPC remote. That
publication requires separate authorization and has not been performed. Local
validation selects the checkout through `SPP_SPC_ROOT` and `SPP_SPC_BINARY`.

Before the first public release, provision a public Forgejo owner named `libs` on
**both** `git.henderkes.com` and `git.static-php.dev`, and configure the
`FORGEJO_LIBS_PASSWORD` Actions secret with upload access to that owner on both hosts.
Existing PHP owner credentials remain in use for their PHP streams. The common
owner must provide Debian and Alpine signing keys and use the same distribution,
branch, and repository names as the PHP owners. Do not expose a breaking library set
until its complete affected consumer set has passed installation and upgrade checks.

Owner creation, secret configuration, package uploads, repository publication, and
replacing the public installer are separate external administration actions requiring
explicit authorization. The repository setup script does not perform them.

For local repository fixtures, the setup script supports `SPP_FORGEJO_HOST`,
`SPP_LIBS_OWNER` (default `libs`), and `SPP_RPM_REPO_URL`. `SPP_INSTALL_ROOT` and
`SPP_INSTALL_TYPE=deb|apk|rpm` redirect configuration to a test root. Run the isolated
installer checks with `python3 tests/install-repositories.py`.
