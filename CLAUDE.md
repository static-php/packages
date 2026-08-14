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

- **Version strings carry a tilde.** `8.6.0alpha3` is normalized to `8.6.0~alpha3` before it reaches any packager, because `rpmvercmp` orders `8.6.0alpha3 > 8.6.0` but `8.6.0~alpha3 < 8.6.0`. `bin/createrepo_static` parses the `~` in both the base-package and extension regexes. Never hardcode a specific pre-release marker — the scheme must work for alpha/beta/RC alike. APK has no `~` in its version grammar, so `createApkPackage` translates it to `_` (`8.6.0_alpha3`, verified against `apk version -c/-t`). apk has no `_dev` either, so `~dev` maps to `_pre`, and a pre-release suffix forces the `p<NN>` extension suffix behind its own underscore (`5.1.29~dev` → `5.1.29_pre_p86`); plain releases keep the old `5.1.28p86` form.
- **Every package carries the pre-release marker, not just the base ones.** `php-zts-cli`/`-embed` get the PHP version itself (`8.6.0~beta1`), and everything built against them — extensions, `pie-zts`, `frankenphp` — carries the same marker inside its php tag, via `CreatePackages::getPhpVersionTag()`: `0.21.0_86~beta1` (rpm), `0.21.0+php86~beta1` (deb), `0.21.0_beta1_p86` (apk). Without it an extension's version never moves when PHP goes alpha3 → beta1, so a `.so` built against the older libphp stays installed and loads into the newer one. rpm/deb put the marker behind the digits so `_86~alpha3` still outranks `_85`; apk has no tilde and rejects the bare post-suffix once a pre-release suffix is present (`6.2.0_rc2p86`), so there the marker goes in front and forces the `_p` form. Against a GA release the tag is byte-identical to what it always was (`0.21.0_85`), so nothing changes for 8.2–8.5. `bin/createrepo_static` keys the module stream on the tag's digits (`php_stream()`) and `TestCommand::filterForVersion` reads them back the same way — both tolerate the marker on either side.
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
- `iteration` — passed through as `--iteration=…`.
- `debug_tmate` — opens tmate on failure (only after build step; tmate binary is downloaded inline because Alma doesn't ship it).

When the user asks to "rerun for X only", they mean these inputs.

## Operational notes

- Packages are uploaded via `rsync` over SSH to `${{ secrets.DEB_SERVER_IP }}:/mnt/data/{rpm|deb|apk}/${RPM_ARCH}/${TARGET_DIR}/`. RPM `update-repo` job ssh-runs `createrepo_static && createrepo_c --update . && modifyrepo_c …` on the remote.
- RPM signing: GPG key from `secrets.DEB_GPG_PRIVATE_KEY` + passphrase `DEB_GPG_PASSWORD`, loaded into `~/.rpmmacros` and used by `rpmsign --addsign`.
- Cache: composer cache is the only persistent cache (`actions/cache` keyed on `composer.lock`).
- `buildroot-*` artifacts are uploaded with `retention-days: 1` — they're cheap intermediates.

## Comments

Match the comment density of the file you are editing. Both repos are sparsely commented: a comment earns its place only when it records something the code cannot say — an upstream bug, a non-obvious ordering constraint, why a workaround exists.

Don't narrate. No comment restating the line below it, no explaining what a well-named call does, no multi-line rationale on a one-line change, and no "changed X to Y" or "fixed Z" notes — that belongs in the commit message. When in doubt, leave it out; the diff and the commit message carry the reasoning.

## Don'ts

- Don't delete `src/hook/Frankenphp.php` because its patches "landed upstream" — spc builds frankenphp's newest *release tarball*, and both the PHP 8.6 `PG(output_handler)` fix and the `do_php_cli()` switch are still only on `main`; v1.12.7 carries neither, and without the hook `frankenphp.c` fails to compile against 8.6. Check the tag, not the branch. The hook is self-disabling — it no-ops when `php_globals.h` still declares `char *output_handler` and when the source already has the fix — so it costs nothing to keep until a release ships it.
- Don't switch `tar --zstd` to pipes in the deb/apk workflows — they don't run on Alma.
- Don't skip pinning new actions to SHAs.
- Don't add `container: <alpine image>` to the apk jobs — musl containers can't run JS actions on arm64. Keep the host + `docker run` pattern.
- Don't replace the dynamic-matrix filter pattern with `if: needs.X.result == 'success'` — that fails closed for the whole matrix when any entry fails.
- Don't `git push` or `gh pr create` unless explicitly asked.
