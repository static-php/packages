# Packages repo

Builds RPM/DEB/APK packages (PHP toolchain, gcc) via GitHub Actions, using reproducible Dockerfile-driven builders.

## Layout

- `bin/spp` — main PHP build CLI. Usage in workflows: `php bin/spp build --type=rpm --debuginfo --phpv=8.4 --libs-only` and `php bin/spp all --type=rpm --debuginfo --phpv=8.4 [--iteration=N] [--packages=…]`. `--libs-only` produces a `buildroot/` that gets passed to the next stage as a tarball; `all` consumes that buildroot and produces packages in `dist/<type>/`.
- `bin/createrepo_static`, `bin/forgejo-helper` — repo-side tools.
- `src/` — PHP source (`src/Command`, `src/step`, `src/package`, `src/util`, `src/patches`, `src/ini`, `CraftConfig.php`, `extension.php`, `package.php`).
- `craft.yml` — top-level static-php-cli config: extensions list, build options, SPC_* env (CFLAGS, LDFLAGS, etc.). When toolchain/optimization questions come up, look here before guessing.
- `Dockerfile.{rhel,debian,alpine}` — builders. Built by `.github/workflows/build-images.yml`.
- `.github/workflows/`:
  - `build-rpm-modular-packages.yml` — Alma 8/9/10 × x86_64/arm64 × PHP 8.2/8.3/8.4/8.5/8.6. Uploads via rsync+SSH then runs `createrepo_c` on the remote.
  - `build-deb-forgejo.yml`, `build-apk-forgejo.yml` — Debian/Alpine builds, push to Forgejo. PHP 8.2–8.6. Each PHP version publishes to its own Forgejo owner (`82`…`86`, from `getForgejoOwner()`), so there is no shared index and no default-version problem — users opt in by adding the repo for the version they want.
  - `build-images.yml` — builds the builder containers (libs-only step exists to save build time).
  - `spc-download.yml` — produces the `downloads-tarball` artifact that the build workflows pull via `dawidd6/action-download-artifact`.
  - `zizmor.yml` — workflow security audit.

## Builder containers

`Dockerfile.rhel` is matrix-built per Alma version into `ghcr.io/static-php/packages-builder-rhel-{8,9,10}`. It installs `tar`, `zstd`, `gcc-toolset-15`, `cmake` 3.31, `re2c`, `bison`, `autoconf`/`automake`/`libtool`, `fpm`, and a prebuilt `php` from `files.henderkes.com`. The autotools trio matters for pre-release PHP: a `php-8.6.0*` tag archive ships no generated `configure`, so spc runs `./buildconf --force`.

When editing `Dockerfile.rhel`, remember the per-Alma branches:

- **Alma 8/9** use `@ruby:3.3` module + `source /opt/rh/gcc-toolset-15/enable`.
- **Alma 10** uses plain `ruby` + `source /usr/lib/gcc-toolset/15-env.source`.
- **Alma 8** has no `re2c` package — built from source (4.3 tarball). 9/10 install from repo.

## PHP toolchain selection

Toolchain is chosen by **package type**, not PHP version:

- **RPM (Alma), DEB (Debian), and APK (Alpine)** → `gcc` 16 for **all** PHP versions (8.2–8.6). No `--target` is passed, so `craft.yml.twig` sets `using_gcc = not target` → `GccNativeToolchain`. For apk the template additionally sets `SPC_LIBC: musl` + `SPC_MUSL_DYNAMIC` — `Dockerfile.alpine` is a real `alpine:3.21` image (native musl gcc, not zig cross), so `bin/spp test` (`apk add`) runs in real Alpine.

**Alpine jobs do NOT use `container:` — they run on the glibc host and invoke the image via `docker run`.** A musl `container:` breaks JavaScript actions (checkout/cache/download-artifact/tmate) on **arm64**: GitHub only ships a musl Node for x64, so an arm64 musl container errors with "JavaScript Actions in Alpine containers are only supported on x64 Linux runners." So `build-apk-forgejo.yml` keeps checkout/cache/artifact steps on the host and wraps only the build/test/forgejo steps in `docker run --rm -v "$GITHUB_WORKSPACE":/build … "$IMAGE" bash -lc '…'` (as root — `bin/spp`'s `maybeSudo` no-ops at euid 0, so `apk add` works). A GHCR `docker login` step is required since manual `docker run` isn't auto-authenticated the way `container:` is. The rpm/deb jobs still use `container:` — their images are glibc, so this doesn't apply; don't convert them.

The `build-libs-gcc` job builds one lib set per (alma, arch) with `phpv: "8.4"` as the canonical trigger (libs are PHP-version-independent). The downstream `build` step reuses that single buildroot (`buildroot-rpm-alma{V}-{arch}-gcc` / `buildroot-deb-{arch}-gcc` / `buildroot-apk-{arch}`) for every PHP version. Buildroots ship as `cache-YYYY-WW` GitHub **release** assets via the `.github/actions/buildroot-cache` composite action (pruned by `buildroot-cache-cleanup.yml`).

## Pre-release PHP and `allow-shared-ext-failure`

PHP 8.6 is still a pre-release, so upstream resolves to whatever `php-8.6.0*` tag is newest (alpha/beta/RC). Consequences:

- **Skip PHP 8.6.0RC1** because of its upstream packaging issue. The upstream probe excludes that exact tag, and `src/artifact/PhpSource.php` falls back to beta3 if SPC selects RC1 (explicit RC1 requests fail). RC2 and later resolve normally.

- **Version strings carry a tilde.** PHP and extension prereleases normalize to `~alphaN`, `~betaN`, and `~rcN`; development snapshots use `~~dev` so they sort below alpha instead of between beta and rc. APK translates these to `_alphaN`, `_betaN`, `_rcN`, and `_alpha0_alpha0` for dev. The old `_pre` mapping incorrectly placed dev above beta. Never hardcode a particular prerelease marker.
- **Independently versioned packages compare PHP before their own prerelease.** `CreatePackages::getTaggedPackageVersion()` builds `3.6.0_806.0~rc3+ext~alpha1` (rpm), `3.6.0+php806.0~rc3+ext~alpha1` (deb), or `3.6.0p806.0_rc3_p0_alpha1` (apk). The order is extension numeric version, PHP major/minor/patch and prerelease, then extension prerelease. The `+ext` / `_p0` boundary keeps PHP GA above its prereleases even when the extension moves backwards from release to dev. Both phase sequences are `dev < alpha < beta < rc < release`; a later PHP prerelease wins even if the extension moves alpha2 to alpha1. The PHP minor is padded to two digits (`806`, `810`, `900`) so 8.10 sorts below 9.0, and the patch follows after a dot. Forgejo owner IDs stay unchanged (`86`, etc.). The new format applies to extensions, PIE and FrankenPHP across all PHP streams, including GA, so the next rebuild upgrades old metadata. Packages whose version equals PHP itself keep the full PHP version. Numeric upstream rollbacks still need a rollback version or epoch.
- **Use native version comparators.** `TestCommand` selects packages with `dpkg`, `apk version`, and `rpmdev-vercmp` (available in all Alma builders), not PHP's `version_compare()`. All three workflows run `tests/package-versions.php` for their format before building. It checks all 25 PHP/extension phase combinations, migration past legacy builds, numbered prereleases and PHP version transitions. `tests/createrepo-versions.py` checks RPM parsing and module artifacts for the legacy and current formats.
- **Never publish a lower candidate.** `bin/forgejo-helper upload` paginates the full authenticated package list and checks DEB/APK archive headers against published versions for the same name and architecture before uploading any file. `bin/check-rpm-upgrades` reads published primary metadata over the existing SSH connection and compares RPM epochs/versions/releases for the same name, architecture and PHP stream. Repository read failures or a lower candidate abort the batch before upload. Both guards use native package comparators and have regression coverage in `tests/upgrade-guards.py`.
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

The deb/apk forgejo workflows still use `tar --zstd` — that is **fine**; they don't run on Alma. Don't "fix" them.

## Matrix isolation pattern

Per-Alma failures must not cascade. Pattern used in `build-rpm-modular-packages.yml`:

1. **Fan-out job** uses `strategy.fail-fast: false` and an explicit, parseable `name:` like `Build libs gcc (alma{V} {arch})` or `Build (alma{V} {arch} php{V})`. The name format is the contract — downstream filters parse it.
2. **Filter job** runs with `if: ${{ !cancelled() }}` and `permissions: actions: read`. It calls `gh api repos/$GITHUB_REPOSITORY/actions/runs/$GITHUB_RUN_ID/jobs --paginate --jq …`, filters successful jobs by name regex, sed-extracts the tuple, then jq-builds an `{include: [...]}` matrix and an `any` boolean. Both go to `$GITHUB_OUTPUT`.
3. **Downstream job** does `needs: [filter-job]`, gates with `if: ${{ !cancelled() && needs.filter-job.outputs.any == 'true' }}`, and uses `matrix: ${{ fromJson(needs.filter-job.outputs.matrix) }}`.

Why `!cancelled()` not `success()`: upstream matrix entries are allowed to fail; without `!cancelled()` the filter job wouldn't run at all.

When adding a new downstream stage to any workflow, follow this pattern rather than relying on `needs.*.result` (which collapses across the whole matrix).

## Validating workflow changes locally

Before pushing, validate YAML and simulate the jq filters:

```bash
# YAML syntax
python3 -c "import yaml,sys; yaml.safe_load(open(sys.argv[1]))" .github/workflows/build-rpm-modular-packages.yml

# Simulate the compute-build-gcc-matrix jq with a mocked "succeeded" (alma arch) string
FULL_MATRIX='{"php-version":["8.2","8.3","8.4","8.5","8.6"],"alma":["8","9","10"],"arch":["x86_64","arm64"]}'
succeeded=$'9 x86_64\n9 arm64\n10 x86_64\n10 arm64'
jq -nc --argjson m "$FULL_MATRIX" --arg s "$succeeded" '
  ($s | split("\n") | map(select(length > 0))) as $set |
  [ $m["php-version"][] as $p | $m.alma[] as $a | $m.arch[] as $r |
    select($set | index("\($a) \($r)")) |
    {"php-version": $p, alma: $a, arch: $r}
  ]'
```

The Plan/Explore agents can also read large workflow files, but for these targeted edits prefer Read + Edit directly.

## Workflow style conventions

- 4-space indentation throughout (including step bodies).
- Steps written as `-   name:` (3 spaces between dash and key). Match this when adding new steps.
- All third-party `uses:` actions are pinned to commit SHAs with a `# vX` trailing comment. Keep that style; don't switch to floating tags.
- `FORCE_JAVASCRIPT_ACTIONS_TO_NODE24: true` is set in container jobs because the GH-hosted Node in Alma containers is too old.
- `runs-on` for arm64: `ubuntu-24.04-arm` (RPM) or `ubuntu-24.04${{ matrix.arch == 'arm64' && '-arm' || '' }}` (DEB style).
- Matrix arch is `arm64`/`x86_64` in inputs; the RPM tooling wants `aarch64` — the conversion happens in the "Set architecture variables" step (`MATRIX_ARCH` env → `RPM_ARCH`).

## Build inputs

All build-* workflows accept comma-separated overrides via `workflow_dispatch`:

- `php_versions` (e.g., `8.2,8.5`) — empty = all.
- `alma_versions` (RPM only).
- `architectures` (`x86_64,arm64` for RPM/APK; `amd64,arm64` for DEB).
- `packages` — passed through to `bin/spp` as `--packages=…`.
- `iteration` — passed through as `--iteration=…` and overrides automatic bumping. `bump` now defaults to true in the package workflows; scheduled runs also bump. Forgejo revision queries paginate the full authenticated registry and cache each page.
- `debug_tmate` — opens tmate on failure (only after build step; tmate binary is downloaded inline because Alma doesn't ship it).

When the user asks to "rerun for X only", they mean these inputs.

## Operational notes

- Packages are uploaded via `rsync` over SSH to `${{ secrets.DEB_SERVER_IP }}:/mnt/data/{rpm|deb|apk}/${RPM_ARCH}/${TARGET_DIR}/`. RPM `update-repo` checks out the current code and pipes `bin/createrepo_static` into remote `python3 -`, followed by `createrepo_c --update . && modifyrepo_c …`; no separately installed server script needs updating.
- RPM signing: GPG key from `secrets.DEB_GPG_PRIVATE_KEY` + passphrase `DEB_GPG_PASSWORD`, loaded into `~/.rpmmacros` and used by `rpmsign --addsign`.
- Cache: composer cache is the only persistent cache (`actions/cache` keyed on `composer.lock`).
- `buildroot-*` artifacts are uploaded with `retention-days: 1` — they're cheap intermediates.

## Comments

Match the comment density of the file you are editing. Both repos are sparsely commented: a comment earns its place only when it records something the code cannot say — an upstream bug, a non-obvious ordering constraint, why a workaround exists.

Don't narrate. No comment restating the line below it, no explaining what a well-named call does, no multi-line rationale on a one-line change, and no "changed X to Y" or "fixed Z" notes — that belongs in the commit message. When in doubt, leave it out; the diff and the commit message carry the reasoning.

## Don'ts

- Don't wire an extension patch with `#[PatchBeforeBuild]` — spc emits that only from `PackageBuilder::buildPackage()`, which refuses anything that isn't a `LibraryPackage`, so it reaches libraries and targets but never `#[Extension]` packages. Extensions go `buildShared()` → `runStage()`, which emits stage hooks: use `#[BeforeStage('ext-<name>', 'phpizeForUnix')]` to land before phpize/configure/make. A hook wired the wrong way is silent — the extension just fails and `allow-shared-ext-failure` skips it.
- SPC uses bundled `php-src/ext/zip` for PHP 8.5+ and archived PECL zip for older PHP versions. The former PHP 8.6 PECL zip hook and patch are no longer needed.
- Don't switch `tar --zstd` to pipes in the deb/apk workflows — they don't run on Alma.
- Don't skip pinning new actions to SHAs.
- Don't add `container: <alpine image>` to the apk jobs — musl containers can't run JS actions on arm64. Keep the host + `docker run` pattern.
- Don't replace the dynamic-matrix filter pattern with `if: needs.X.result == 'success'` — that fails closed for the whole matrix when any entry fails.
- Don't `git push` or `gh pr create` unless explicitly asked.
