# Packages repo

Builds RPM/DEB/APK packages (PHP toolchain, gcc) via GitHub Actions, using reproducible Dockerfile-driven builders.

## Layout

- `bin/spp` — main PHP build CLI. `php bin/spp build --type=rpm --debuginfo --phpv=8.4 --libs-only [--libraries=…]` produces the reusable SDK in `buildroot/`. The glibc workflow builds and seals one PHP payload per stream and architecture, then uses `php bin/spp package --type=<rpm|deb> --phpv=… [--iteration=N] [--packages=…]` to produce archives in `dist/<type>/` without compilation. `package --buildroot=…` can select a sealed PHP payload or, with `--libs-only`, a shared library SDK.
- `bin/createrepo_static`, `bin/forgejo-helper` — repo-side tools.
- `src/` — PHP source (`src/Command`, `src/step`, `src/package`, `src/util`, `src/patches`, `src/ini`, `CraftConfig.php`, `extension.php`, `package.php`).
- `craft.yml` — top-level static-php-cli config: extensions list, build options, SPC_* env (CFLAGS, LDFLAGS, etc.). When toolchain/optimization questions come up, look here before guessing.
- `Dockerfile.{rhel,debian,alpine}` — builders. Built by `.github/workflows/build-images.yml`.
- `.github/workflows/`:
  - `build-rpm-modular-packages.yml` — one EL8 glibc producer × x86_64/arm64 × PHP 8.2–8.6; packages the same common binaries as RPM and DEB. RPM runtime tests run only on EL8. RPM publication stages and atomically promotes one canonical repository per architecture, with legacy EL9/EL10 URL aliases.
  - `build-deb-forgejo.yml` — delegates to the shared EL8 workflow; it does not compile a separate Debian payload.
  - `build-apk-forgejo.yml` — separate native Alpine/musl producer. DEB/APK publish each PHP stream to its existing Forgejo owner (`82`…`86`, from `getForgejoOwner()`), including its private libraries, so clients keep their existing version-specific repository URLs.
  - `publish-packages.yml` — publishes one selected format (`rpm`, `deb` or `apk`) from a completed default-branch `build_run_id`, without compiling or repackaging. RPM and DEB use the same shared EL8 build run; APK uses its Alpine run.
  - `publish-package-cohort.yml` — reusable publisher called by both the build workflows and `publish-packages.yml` with `secrets: inherit`. It verifies receipts, signs packages and checks current repository upgrades. Its EL8/Debian container job and Alpine host job share publication steps through a YAML anchor.
  - `build-images.yml` — builds the EL8 and Alpine producer images and Debian packaging image.
  - `spc-download.yml` — produces `downloads-inputs` for planning/cache identity, `downloads-php` for PHP/extension compilation against the SDK, and the complete `downloads-tarball` for cold SDK compilation. `source-snapshot.py` downloads inputs first from the pinned run and materializes the required source bundle only for compilation; older runs fall back to their complete artifact. Selective library compilation fetches only its recorded library sources and verifies their hashes; it does not fetch unused extension archives.
  - `zizmor.yml` — workflow security audit.

## Builder containers

On this PC, use Docker and do not install or require Podman. Keep container tooling configurable; do not enforce an engine identity. Never launch privileged containers, locally or in CI. Do not run systemd-dependent tests; they are not required. Keep PID, IPC and cgroup namespaces private. If a test cannot run unprivileged, report the limitation instead of escalating container privileges. Never share host namespaces or mount the host root, host cgroups, systemd/D-Bus sockets or container-engine sockets into an init-system test container.

Use Buildx's `docker` driver; its `docker-container` driver creates a privileged worker. Image CI pushes native architecture tags with inline cache, then resolves registry digests for the multi-architecture manifest.

Never shut down, reboot, suspend or power off the host, or stop its service manager. Init-system test commands must target a verified test container through the configured engine's exec command; never run its service or shutdown commands on the host. Other host-service changes require explicit user authorization.

`Dockerfile.rhel` is built with `ALMA_VERSION=8` into `ghcr.io/static-php/packages-builder-rhel-8`, the only production glibc compiler image. It provides GCC 16.2.0 and binutils in `/opt/gcc`, `tar`, `zstd`, `cmake` 3.31, `re2c`, `bison`, `autoconf`/`automake`/`libtool`, `fpm`, and a prebuilt `php` from `files.henderkes.com`. GCC runtime assets are checked or repaired by `bin/build-gcc-runtime` for shared linkage, DWARF type information and licenses. The autotools trio matters for pre-release PHP: a `php-8.6.0*` tag archive ships no generated `configure`, so spc runs `./buildconf --force`.

`Dockerfile.debian` builds `ghcr.io/static-php/packages-packager-debian`, a Debian 12 packaging image. It consumes the sealed EL8 payload; installation, upgrade and TLS tests use separate `debian:11` containers. OpenSSL alone has two EL8-built path profiles: RPM `/etc/pki/tls` and DEB `/etc/ssl`; common PHP, extension and other library ELF bytes remain identical.

When editing retained per-Alma branches in `Dockerfile.rhel`, remember that 9/10 are not production build targets:

- **Alma 8/9** use `@ruby:3.3`; **Alma 10** uses plain `ruby`. The compiler comes from `/opt/gcc`, not gcc-toolset activation scripts.
- **Alma 8** has no `re2c` package — built from source (4.3 tarball). 9/10 install from repo.

## PHP toolchain selection

Toolchain is chosen by **producer**, not PHP version:

- **EL8 glibc (RPM and DEB payloads) and native Alpine musl (APK)** → `gcc` 16 for **all** PHP versions (8.2–8.6). No `--target` is passed, so `craft.yml.twig` sets `using_gcc = not target` → `GccNativeToolchain`. For apk the template additionally sets `SPC_LIBC: musl` + `SPC_MUSL_DYNAMIC` — `Dockerfile.alpine` is a real `alpine:3.21` image (native musl gcc, not zig cross), so `bin/spp test` (`apk add`) runs in real Alpine.

**Alpine jobs do NOT use `container:` — they run on the glibc host and invoke the image via `docker run`.** A musl `container:` breaks JavaScript actions (checkout/cache/download-artifact/tmate) on **arm64**: GitHub only ships a musl Node for x64, so an arm64 musl container errors with "JavaScript Actions in Alpine containers are only supported on x64 Linux runners." So `build-apk-forgejo.yml` keeps checkout/cache/artifact steps on the host and wraps native Alpine work in Docker (as root — `bin/spp`'s `maybeSudo` no-ops at euid 0, so `apk add` works). A GHCR `docker login` step is required since manual `docker run` isn't auto-authenticated the way `container:` is. Glibc producer jobs use `container:`; format packaging and native upgrade tests run from the host through Docker. Format packaging consumes only the sealed payload and SDK metadata; it does not download source tarballs. Unchanged selected PHP build/test requests restore only the selected outputs, the required test hosts and their library closure. Changed-source/ABI rebuilds retain the full SDK for compilation, then build/test-only selections discard unrelated payload files before sealing and format packaging. Full cache archives are checksum-verified before selection; all retained ELF/debug identities stay sealed, and these packaging subsets cannot replace a complete PHP release cache.

The `build-libs` job builds one PHP-independent shared SDK per producer and architecture, using PHP 8.4 as the canonical library trigger. Both producers use the same Python classification and library packaging flow in `.github/actions/shared-libraries/glibc.py`; Alpine keeps its SDK in place, while EL8 uses separate RPM/DEB SDK paths for OpenSSL profiles and concurrent packaging. A single requested PHP stream restores/builds in the SDK job, avoiding an SDK archive transfer and a second source download. EL8 uses `.github/actions/php-payload`; selected build/test-only PHP requests with unpublished native dependencies also package RPM/DEB concurrently from isolated sealed payload paths there, then transfer archives for separate native installation checks. This avoids another payload transfer and packaging builder. Publication-enabled requests retain the separate packaging path. Alpine uses the same `.github/actions/apk-packages` build/validation action as its multi-stream jobs. Multiple streams retain parallel producer jobs; `shared-sdk-buildroot-glibc-alma8-{arch}` feeds those jobs, whose `shared-payload-glibc-alma8-{arch}-php{stream}` artifact fans out to RPM/DEB packaging. Alpine uses `shared-sdk-buildroot-apk-{arch}`. EL8 library RPM/DEB packaging runs concurrently. Unchanged explicit library or consumer selections whose library dependency closure excludes OpenSSL read the sealed common SDK directly and pass separate classified metadata with `--library-manifest=…`; they do not copy or reseal the SDK. Rebuilt libraries and OpenSSL selections use separate format SDK paths (`package --libs-only --buildroot=…`). Unchanged OpenSSL-dependent library selections project those paths to the selected dependency closure while preserving raw/prepared file identities and native profile verification. The common SDK stays in place. Compatible seeds select only changed library inputs and genuine ABI dependents; unchanged manual selections compile nothing. SDKs and sealed PHP payloads use one persistent GitHub **release**, `cache`, via `.github/actions/buildroot-cache`. Each target has a simple archive name and one JSON file containing its input identity, compatibility and checksum. Successful updates replace that target's cache; older weekly assets remain readable during migration. `publish=false` does not write release caches. Build/test-only runs use independent concurrency groups; only publication-enabled runs share the producer lock. Before the initial shared-library migration is published, explicit library or PHP package selections produce build/test-only plans: CI installs them with local SDK dependency packages in fresh native containers, without fetching old PHP archives. Focused install/load checks use one fresh container and install the selected local archives directly through the native package manager; they do not build a temporary repository or commit an intermediate runtime image. Independent-install and upgrade-fixture suites retain their repository-based path. Extension selections package cached CLI, FrankenPHP and its embed dependency for load checks; SAPI selections add only CLI when required. Native metadata detects whether the selected PHP stream has migrated to shared linkage, independently of whether private libraries already exist. Unchanged PHP-only build/test requests reuse the SDK ABI and read the native CLI capability without downloading unrelated library archives. These test dependencies are separate from candidate archives. The cohort guard rejects these unpublished subsets; they cannot be promoted through publish-only runs. Published-library updates retain their real ABI and native upgrade baselines. Manual library requests read published metadata for the selected and actually rebuilt dependency closure. Only rebuilt major/minor upgrades read the relevant ABI references; consumer discovery waits until an ABI transition is detected. Native library-only upgrades load the affected libraries and actual reverse consumers, without fetching static PHP hosts that do not consume them.

## Pre-release PHP and `allow-shared-ext-failure`

PHP 8.6 is still a pre-release, so upstream resolves to whatever `php-8.6.0*` tag is newest (alpha/beta/RC). Consequences:

- **Skip PHP 8.6.0RC1** because of its upstream packaging issue. The upstream probe excludes that exact tag, and `src/artifact/PhpSource.php` falls back to beta3 if SPC selects RC1 (explicit RC1 requests fail). RC2 and later resolve normally.

- **Version strings carry a tilde.** PHP and extension prereleases normalize to `~alphaN`, `~betaN`, and `~rcN`; development snapshots use `~~dev` so they sort below alpha instead of between beta and rc. APK translates these to `_alphaN`, `_betaN`, `_rcN`, and `_alpha0_alpha0` for dev. The old `_pre` mapping incorrectly placed dev above beta. Never hardcode a particular prerelease marker.
- **Independent versions identify the PHP stream and initial prerelease ABI.** `CreatePackages::getTaggedPackageVersion()` builds stable versions as `1.13.6_82` (RPM), `1.13.6+php82` (DEB), or `1.13.6p82` (APK). FrankenPHP and compiled extensions append PHP's phase only during the initial `x.y.0` prerelease cycle: `1.13.6_86~rc5`, `1.13.6+php86~rc5`, `1.13.6p86_rc5`. Their own prerelease stays before the PHP tag: `3.2.17~rc1_86~rc5`, `3.2.17~rc1+php86~rc5`, `3.2.17_rc1_p86_rc5`. After GA, even a patch RC such as `8.6.1RC1` uses only `86`. PIE always uses the stream-only tag and has no compiled PHP ABI dependency. PHP-versioned packages keep the full PHP version, including patch RCs. Forgejo owner IDs stay unchanged (`82`…`86`). For the same upstream release, retain the highest published structure (including old PHP patch tags and `+ext` / `_p0` boundaries), but update its PHP phase to the current ABI phase; remove that phase at GA and for PIE. Advance the revision when required, and adopt the shorter structure at the package's next upstream release without adding an epoch. Numeric upstream rollbacks remain forbidden. The shared-runtime capability distinguishes initial PHP prereleases even when the module API number is unchanged; GA and subsequent patch releases/RCs share the stable capability.
- **Keep existing package names and real upstream versions.** AMQP and igbinary retain their original APK names; no `-shared` replacements, versioned name aliases or invented numeric prefixes. Malformed historical APK versions containing `~` block corrected spellings of the same numeric upstream version, even with higher revisions. A real higher numeric upstream release can upgrade normally (for example, AMQP 2.2.1 after 2.2.0 or igbinary 3.2.18 after 3.2.17). Until then, preflight rejects lower candidates. It also checks the actual upstream version and phase separately. `tests/apk-version-migration.php --native` validates this future upgrade path with hypothetical higher-upstream metadata fixtures on PHP 8.2–8.5.
- **Use native version comparators.** `TestCommand` selects packages with `dpkg`, `apk version`, and `rpmdev-vercmp` (available in all Alma builders), not PHP's `version_compare()`. The separate `packaging-checks.yml` workflow runs `tests/package-versions.php` and packaging regressions when code changes; package rebuild jobs do not rerun those suites. It checks both prerelease phase sequences, stable-ABI patch/RC invariance, PIE's stream-only tags, PHP stream transitions and migration from historical spellings. `tests/php-prerelease-abi.php --type=<rpm|deb|apk>` checks generated native package versions and ABI relationships. `tests/createrepo-versions.py` checks RPM parsing and module artifacts for historical and current tags.
- **Never publish a lower candidate.** `bin/forgejo-helper upload` paginates the full authenticated package list and checks DEB/APK archive headers against published versions for the same name and architecture before uploading any file. Forgejo stores APK filenames without architecture suffixes, so read published APK `.PKGINFO` headers through the file-ID download links; signed APKs may contain multiple gzip/tar members. A failed per-package verification skips that package and its matching runtime/debuginfo files, while unrelated packages continue uploading. Failure to list the registry still aborts the batch. `bin/check-rpm-upgrades` reads published primary metadata over the existing SSH connection and compares RPM epochs/versions/releases for the same name, architecture and PHP stream. Standalone guards skip lower candidates and their matching runtime/debuginfo files, then report exit code 3. Shared publication workflows require each selected PHP stream and its libraries to pass preflight before uploading that stream: RPM uses `--strict-cohort` and a NUL-delimited rsync allowlist, while DEB/APK preflight every existing stream owner on `git.henderkes.com` only. A rejected candidate blocks its PHP stream; publication continues for other validated streams. DEB/APK libraries go only to that stream owner. RPM retains older library providers in the active repository so unchanged streams retain their ABI dependencies. Both guards use native package comparators and have regression coverage in `tests/upgrade-guards.py`.
- **A pre-release must never become the default module stream.** RPM puts every PHP version in one repo and separates them with modularity, so the highest stream would otherwise be installed unasked. `PRERELEASE_STREAMS` at the top of `bin/createrepo_static` lists the streams excluded from `modulemd-defaults`; drop an entry once that stream goes GA. The stream is still published, so users opt in with `dnf module switch-to php-zts:static-8.6`. Deb and apk need no equivalent — each version is its own Forgejo repo.
- **Not every shared extension compiles or loads yet.** `craft.yml`'s `build-options.allow-shared-ext-failure` tells static-php-cli to skip such an extension instead of aborting the whole build.

The flag is emitted by exactly one condition, in `src/util/TwigRenderer.php`: `'allow_shared_ext_failure' => version_compare($phpVersion, '8.6', '>=')`, consumed by `{% if allow_shared_ext_failure %}` inside `build-options:` in `config/templates/craft.yml.twig`. For ≤ 8.5 the key is simply absent, so spc's own default (`false`) applies and a shared-extension failure stays fatal exactly as before. Keep the comparison in PHP — Twig's `>=` on strings is lexical and breaks at `8.10`. Do not add the version rule to the workflows; they stay version-agnostic.

**Manifest contract.** When the option is honoured, spc writes `buildroot/skipped-shared-extensions.json` on **every** run, even when nothing was skipped — an absent file means "spc too old", an empty `skipped` array means "nothing failed":

```json
{
  "schema": 1,
  "generated_at": "2026-08-07T22:00:00+00:00",
  "php_version_id": 80600,
  "allow_shared_ext_failure": true,
  "skipped": [
    {"package": "ext-imagick", "extension": "imagick", "phase": "build", "exception": "StaticPHP\\Exception\\ExecutionException", "message": "…"}
  ]
}
```

No leading dot in the filename: `src/step/RunSPC.php` now copies the buildroot with `cp -a src/. dst/` (dotfiles included, symlinks preserved), but the manifest is deliberately not a dotfile so it survives a copy that isn't. Key on `extension` (short name), not `package`. The packaging step subtracts these from the shared-extension list, and a shared extension with no `.so` and no skip record is still a hard error: **no `.so` ⇒ no subpackage ⇒ no `conf.d` drop-in**.

## AlmaLinux 8 tar quirk

**Alma 8 ships GNU tar 1.30** — `tar --zstd` is unsupported (the flag was added in tar 1.31). Pipe through the standalone `zstd` binary instead (it's in `Dockerfile.rhel`):

```bash
# Pack
tar -cf - buildroot | zstd -o buildroot.tar.zst
# Unpack
zstd -dc buildroot.tar.zst | tar -xf -
```

The Alpine workflow still uses `tar --zstd` — that is **fine**; those steps run on the host. The shared glibc workflow uses pipes for its EL8 stages, including the payload later packaged as DEB.

## Matrix isolation pattern

Setup resolves the shared multi-architecture builder/packager image tags once before emitting the architecture matrix; image resolution has no per-architecture jobs or artifact transfer.

Per-target failures must not cascade. Pattern used in `build-rpm-modular-packages.yml`:

1. **Fan-out job** uses `strategy.fail-fast: false` and an explicit, parseable `name:` such as `Build libs shared (alma8-{arch})` or `Build payload (alma8-{arch} php{stream})`. The name format is the contract — downstream filters parse it.
2. **Filter job** runs with `if: ${{ !cancelled() }}` and `permissions: actions: read`. It calls `gh api repos/$GITHUB_REPOSITORY/actions/runs/$GITHUB_RUN_ID/jobs --paginate --jq …`, selects successful terminal job names (including reusable-workflow prefixes), and sed-extracts target/stream identities. `.github/actions/shared-libraries/workflow.py` combines these with downloaded target plans to emit `{include: [...]}` matrices and `any` booleans. Its `matrix` and `packages` commands cover SDK and common-payload stages; one successful payload fans out only to the requested formats.
3. **Downstream job** does `needs: [filter-job]`, gates with `if: ${{ !cancelled() && needs.filter-job.outputs.any == 'true' }}`, and uses `matrix: ${{ fromJson(needs.filter-job.outputs.matrix) }}`.

Why `!cancelled()` not `success()`: upstream matrix entries are allowed to fail; without `!cancelled()` the filter job wouldn't run at all.

When adding a new downstream stage to any workflow, follow this pattern rather than relying on `needs.*.result` (which collapses across the whole matrix).

## Validating workflow changes locally

Before pushing, validate YAML and the actual workflow/filter contracts:

```bash
# YAML syntax
python3 -c "import yaml,sys; yaml.safe_load(open(sys.argv[1]))" .github/workflows/build-rpm-modular-packages.yml

python3 tests/glibc-workflow-entrypoints.py
python3 tests/workflow-shared-libraries.py
python3 tests/glibc-pipeline.py
python3 tests/package-publication.py
```

The Plan/Explore agents can also read large workflow files, but for these targeted edits prefer Read + Edit directly.

## Workflow style conventions

- 4-space indentation throughout (including step bodies).
- Steps written as `-   name:` (3 spaces between dash and key). Match this when adding new steps.
- All third-party `uses:` actions are pinned to commit SHAs with a `# vX` trailing comment. Keep that style; don't switch to floating tags.
- `FORCE_JAVASCRIPT_ACTIONS_TO_NODE24: true` is set in container jobs because the GH-hosted Node in Alma containers is too old.
- Producer/package jobs use `ubuntu-24.04${{ matrix.arch == 'arm64' && '-arm' || '' }}`; image jobs use explicit `ubuntu-24.04-arm` for arm64.
- Matrix arch is `arm64`/`x86_64`; the glibc entrypoint also accepts `amd64` as an x86_64 input alias. RPM tooling wants `aarch64`, while DEB archive headers use `arm64`/`amd64`; keep these conversions in the shared workflow helpers.

## Build inputs

The shared glibc workflow, its DEB wrapper and the Alpine workflow accept these `workflow_dispatch` inputs:

- `mode` — `full` or `incremental`; a full PHP request still reuses a compatible library SDK.
- `libraries` — comma-separated library IDs for a selective library-only update. Unchanged selected inputs reuse compiled outputs; add `bump=true` to raise only the selected package revisions. ABI changes include genuine dependent rebuilds.
- `php_versions` (e.g., `8.2,8.5`) — empty = all.
- `downloads_run_id` — pins the `spc-download` source snapshot; automatic dispatches pass their own run ID, and empty preserves the download action's latest-artifact lookup.
- `architectures` — `x86_64,arm64`; the glibc workflow and DEB wrapper also accept `amd64` for x86_64. EL8 is the fixed glibc producer; there is no `alma_versions` input.
- `packages` — passed through to `bin/spp` as `--packages=…`; a PHP package selection does not force packaging every unchanged SDK library. Selective native upgrade baselines download only those consumers, CLI/FPM/FrankenPHP and their managed dependency closure; library-only updates add genuine reverse consumers from native repository metadata. Full requests retain complete baseline coverage. Native upgrade tests load ordinary extensions together and isolate VM instrumentation extensions; they do not repeat every ordinary extension in its own install. The full published version inventory remains metadata for revision and downgrade checks.
- `iteration` — passed through as `--iteration=…` and overrides automatic bumping. `bump` defaults to false, including scheduled `spc-download` dispatches; only an explicit manual `bump=true` requests forced revision bumps. Forgejo revision queries paginate the full authenticated registry and cache each page.
- `publish` — defaults to false: build/test only, without package publication or release-cache writes. Scheduled `spc-download` dispatches also keep it false; publication requires an explicit manual request.
- `debug_tmate` — opens tmate on failure (only after build step; tmate binary is downloaded inline because Alma doesn't ship it).

When the user asks to "rerun for X only", they mean these inputs.

`RunSPC` resolves minor versions through the snapshot's `downloads/.spc-meta.json` and verifies the built binary before packaging. CI requires this metadata; explicit full-version requests remain explicit. Keep the PHP stream used for paths, package tags and Forgejo owners separate from the pinned source release.

`spc-download` restores source archives, checks upstream identities with SPC `download --update`, and downloads only changed or invalid sources. Pinned PHP archives and PIE reuse their verified bytes. Source caches are independent of compiled release caches. `spc-download` runs incremental requests daily. Only an explicit manual `force_full` requests every PHP stream; there is no automatic Monday full rebuild. Compatible library SDKs rebuild only changed inputs. ABI analysis is limited to actually rebuilt libraries crossing a major/minor version boundary; patch and same-version rebuilds skip ABI tools and retain the last checked ABI reference. Unknown version schemes and new libraries without a reference use ABI analysis. An unchanged library never invokes ABI tools. Automatic dispatch triggers the shared glibc workflow once plus Alpine, not a separate DEB compiler pipeline. Upload downgrade guards still apply.

## Operational notes

Native upgrade jobs use published metadata captured once per architecture in the SDK job, passed through its existing artifacts. RPM index downloads are shared across PHP streams. The native-baseline action restores archive caches by format, architecture and stream, verifies every retained archive against the captured checksums, and downloads only missing selected packages. Build-only selections skip this baseline path. Cold baseline downloads use two workers; package fan-out is capped at four glibc jobs and two Alpine jobs. Transient repository errors retry three times and report the URL. Inventory failures are recorded per stream so they do not fail unrelated targets.

A complete republish must not be blocked solely by an already-broken published baseline. Baseline capture records missing/ambiguous managed dependencies as defects. If the old installation or load check fails, full-cohort validation starts a clean native client and verifies the selected replacement packages, their exact versions, ELF dependencies, SAPI/extension loading and an unchanged upgrade. Its receipt explicitly records fresh replacement coverage and the original baseline failure. A failed upgrade from a working baseline, candidate installation/load failure, incomplete replacement, tampered inputs or failed cleanup still blocks publication. Partial updates continue requiring their existing baseline. TLS checks still test the replacement; service migration checks are unavailable when the old installation cannot be established.

- RPM archives are staged via `rsync` over SSH beneath `/mnt/data/rpm/{x86_64|aarch64}/.php-zts/`. The checked-out `bin/canonical-rpm-repository` and `bin/createrepo_static` are copied with the validated cohort; `stage` and `promote --activate-aliases` publish one complete generation while retaining cached legacy URLs. DEB/APK use `git.henderkes.com` alone for published versions, revision selection, ABI baselines and publication preflight. After publishing there, they attempt the secondary Forgejo mirror without failing the workflow if that mirror fails.
- RPM signing: GPG key from `secrets.DEB_GPG_PRIVATE_KEY` + passphrase `DEB_GPG_PASSWORD`, loaded into `~/.rpmmacros` and used by `rpmsign --addsign`.
- `actions/cache` stores Composer downloads keyed on `composer.lock`; reusable compiled SDKs and PHP payloads use the release cache described above.
- `shared-sdk-*` and `shared-payload-*` artifacts use `retention-days: 1` as intermediates. SDK archives are transferred only when the plan requests separate PHP jobs; library-only validation consumes native archives and metadata, without restoring sources or an SDK. Package cohorts, validation receipts and target plans are retained for seven days for independent publication. Publish-only runs pin artifact IDs and the recorded builder/packager digests, and share per-format/per-target publication locks with normal builds. Missing or failed PHP streams are skipped independently within each architecture and format; each selected stream must pass every publication guard. Original build-plan and receipt identities remain unchanged when selecting a stream. Publication downloads only native package artifacts, never SDK or source artifacts. Publishing preserves the built versions/revisions and does not populate release caches.

## Comments

Match the comment density of the file you are editing. Both repos are sparsely commented: a comment earns its place only when it records something the code cannot say — an upstream bug, a non-obvious ordering constraint, why a workaround exists.

Don't narrate. No comment restating the line below it, no explaining what a well-named call does, no multi-line rationale on a one-line change, and no "changed X to Y" or "fixed Z" notes — that belongs in the commit message. When in doubt, leave it out; the diff and the commit message carry the reasoning.

## Don'ts

- Don't wire an extension patch with `#[PatchBeforeBuild]` — spc emits that only from `PackageBuilder::buildPackage()`, which refuses anything that isn't a `LibraryPackage`, so it reaches libraries and targets but never `#[Extension]` packages. Extensions go `buildShared()` → `runStage()`, which emits stage hooks: use `#[BeforeStage('ext-<name>', 'phpizeForUnix')]` to land before phpize/configure/make. A hook wired the wrong way is silent — the extension just fails and `allow-shared-ext-failure` skips it.
- SPC uses bundled `php-src/ext/zip` for PHP 8.5+ and archived PECL zip for older PHP versions. The former PHP 8.6 PECL zip hook and patch are no longer needed.
- Don't switch the Alpine workflow's host-side `tar --zstd` to pipes solely for the EL8 tar quirk.
- Don't skip pinning new actions to SHAs.
- Don't add `container: <alpine image>` to the apk jobs — musl containers can't run JS actions on arm64. Keep the host + `docker run` pattern.
- Don't replace the dynamic-matrix filter pattern with `if: needs.X.result == 'success'` — that fails closed for the whole matrix when any entry fails.
- Don't `git push` or `gh pr create` unless explicitly asked.
