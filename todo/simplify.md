# Simplify the package build and update pipeline

## Objective

Make routine upstream updates stable, fast and easy to understand. The shared-library migration introduced too much orchestration and too many failure points. Successful library compilation is followed by expensive repeated work, and unrelated preparation failures prevent packages from being produced.

The user explicitly wants aggressive simplification. Preserve the necessary behavior below, but remove, merge or replace everything else that makes routine updates fragile. Do not preserve an abstraction, helper, receipt, workflow step or compatibility layer merely because it already exists. Do not add another orchestration layer to repair the existing ones.

Success means working Alpine and EL8 builds, reusable caches, reliable publication and ordinary package-manager upgrades. Passing mocked tests alone is insufficient.

## Confirmed failures

### EL8: an unchanged SDK fails after restoring successfully

- Run: https://github.com/static-php/packages/actions/runs/42482909324
- ARM64 job: https://github.com/static-php/packages/actions/runs/42482909324/job/128738640459
- Both architecture SDK jobs failed.
- ARM64 restored and extracted its cached SDK, then completed the library build step with nothing rebuilt.
- The classification step ran from about 13:03 to 13:14 UTC on 2026-11-08 despite no libraries changing.
- It eventually executed `php bin/build-payload record --root buildroot --config craft.yml ...` and failed because `craft.yml` did not exist. Compilation normally generates that temporary file; a cache hit does not.
- The cached OpenSSL profiles reported `rebuilt_profiles: []` before this failure.

The cache-hit path must work without workspace files generated only by compilation. Do not re-record an unchanged SDK. Actual library compilation already records its payload in `src/step/RunSPC.php`.

### Alpine: preinstalled go-xcaddy is not recognized

- Run: https://github.com/static-php/packages/actions/runs/42482906241
- Job: https://github.com/static-php/packages/actions/runs/42482906241/job/128741721905
- The PHP 8.4 ARM64 payload build selected PHP 8.4.27, then failed before PHP compilation.
- SPC doctor printed `go-xcaddy is not installed`, attempted `install-go-xcaddy`, then reported `Download artifact 'go-xcaddy' failed` and exited with code 69.
- Both `Dockerfile.alpine` and `Dockerfile.rhel` explicitly install go-xcaddy while building the image, set `PKG_ROOT_PATH=/opt/spc-pkgroot`, and preserve bootstrap download metadata there.

The reason the runtime build does not recognize the preinstalled tool is **not yet established**. Inspect the exact resolved image, its installed executable and package metadata, SPC's installed-artifact detection, and the environment passed to the child process. Fix the producer-image/runtime contract. Do not hide the problem by adding another routine network download or rebuilding the compiler images without evidence that their contents are wrong.

The downloaded job log is currently `/tmp/packages-apk-failure.log`; it is temporary evidence, not a repository dependency.

### Alpine: successful PHP compilation followed by a license collision

The older run's x86_64 PHP 8.4 job (`128741721952`) completed compilation, then nFPM failed because the CLI license directory mapping flattened multiple libraries' `0.txt` files onto `/usr/share/licenses/php-zts/0.txt`. Directory mappings need nFPM's tree semantics. The compiled payload was not cached because export occurred after packaging. A subsequent correction uses explicit tree mappings and exports a verified sealed payload before packaging; only a successfully completed export becomes eligible for release-cache saving, including after a packaging failure. Package tests and publication guards remain required.

## What must remain

These are behavior requirements. Their present implementations are replaceable.

- **Existing user upgrades and repository URLs.** DEB/APK users keep their PHP-specific owners/repositories (`82` through `86`), including private libraries. A user with only owner `86` configured must upgrade without adding another repository, account or credential. There is no separate libraries user or password; do not reintroduce `FORGEJO_LIBS_PASSWORD`.
- **Package identity and native version ordering.** Keep existing package names and real upstream versions. Never publish a downgrade. Preserve prerelease ordering, the initial PHP prerelease ABI distinction, revision selection and historical upgrade compatibility described in `AGENTS.md`. Use native RPM/DEB/APK comparators. Publication must still check every affected stream's actual native versions; sharing a dependency graph does not justify inventing or copying native package versions between streams/formats.
- **One common glibc compilation.** EL8 is the glibc producer. Its PHP/extension/common-library binaries feed both RPM and DEB; do not restore a second Debian compiler pipeline. OpenSSL's RPM `/etc/pki/tls` and DEB `/etc/ssl` profiles remain necessary. Preserve their correct compiled paths and common public ABI.
- **Native Alpine compilation.** APK remains a separate musl producer. Keep JavaScript actions on the host and invoke the Alpine image with Docker; an ARM64 Alpine `container:` job cannot run those actions.
- **One reusable library SDK per producer and architecture.** Libraries are PHP-independent. Do not compile or store the same library SDK once per PHP stream. PHP 8.4 currently triggers SDK compilation; PHP 8.5 is the canonical published dependency inventory. Those roles are different.
- **Real ABI safety when outputs change.** An actual changed library must get the necessary typed ABI comparison against its published baseline. Genuine ABI changes rebuild affected libraries and PHP consumers before publication. Use the existing cached classification for unchanged libraries; do not walk, hash or parse all cached ABI XML on every run. Do not silently declare a changed ABI compatible to remove work.
- **Selective updates and no-op behavior.** Reuse compatible SDKs and PHP payloads. Compile only changed inputs and genuine dependents. An unchanged manual library selection compiles nothing. Full PHP requests can reuse the SDK. `bump=false` must remain the default; a revision-only request must not become a compilation request.
- **Coherent publication.** Keep architecture isolation, fail-fast disabled across independent targets, native install/upgrade/load checks, package signing and downgrade guards. Prevent incomplete ABI transitions from being published. Preserve canonical RPM promotion and legacy URLs, and primary Forgejo publication with a best-effort secondary mirror. Build/test-only runs must not publish or update release caches. Publish-only runs must not compile or repackage.
- **Correct source and artifact reuse.** A build must use its selected source snapshot and report the actual PHP release. Keep necessary integrity and compatibility checks, but perform them at the appropriate boundary once. Keep builder-installed tools available across all subprocesses. A restored artifact must be usable without a previous workspace.
- **Existing safety/platform constraints.** Follow `AGENTS.md`: Docker locally, no privileged containers or host namespace sharing, no systemd-dependent tests, native GCC toolchains, EL8-compatible tar/zstd pipes, pinned actions, PHP 8.6 shared-extension skip manifest semantics and prerelease protections.

## What should become much simpler

1. **Make the common path obvious:** select inputs, restore compatible outputs, rebuild what actually changed, package, test, publish. Avoid layered plans, mutable metadata rewrites and repeated convergence passes where a direct dependency decision is sufficient.
2. **Do nothing expensive when nothing rebuilt.** No library ABI tool invocations or XML parsing, and no published PHP consumer downloads. Necessary native publication/version checks can still run. Log a short, clear explanation that the compiled SDK was reused.
3. **Read the consumer dependency graph once.** Read PHP **8.5 only**, not 8.2 through 8.6. For the shared EL8 producer, read it once and reuse it for RPM and DEB; their common binaries depend on the same libraries. Alpine needs its own native graph. Only fetch the graph when libraries actually rebuilt. Keep per-stream native publication checks separate from this graph.
4. **Reuse tools already in the image.** Routine PHP builds should not download/reinstall go-xcaddy or other preinstalled build tools. Align SPC's executable paths and installed-package metadata with the image, instead of creating competing setup paths.
5. **Keep simple persistent caches.** The current release is `cache`, with `buildroot-apk-{arch}.tar.zst` and `buildroot-glibc-alma8-{arch}.tar.zst`, plus their JSON metadata. Keep source/compiler compatibility and archive integrity in metadata rather than putting long input/checksum strings in filenames. Update the existing target cache after a successful partial rebuild. No new weekly drafts or forced Monday library rebuilds.
6. **Preserve the already compiled SDKs.** Do not change cache keys/compatibility casually, delete useful archives, or start a two-hour cold rebuild to test orchestration edits. If a future workflow expects a renamed asset, make the already uploaded asset available under that name and verify lookup before pushing or dispatching. The previous cache migration and obsolete-cache deletion are already complete; do not repeat them.
7. **Reduce the number of independent moving parts.** Inspect overlapping responsibilities in `glibc.py`, `run.sh`, `workflow.py`, `source-plan.py`, `incremental-build.py`, `payload-cache.py`, `openssl-variants.py`, `bin/shared-library-plan` and the workflows. Collapse duplicate control flow and calculations. Remove redundant copies, baseline reads, extraction, validation and metadata refreshes. Fewer steps alone is not enough if the same complexity is hidden inside one enormous helper.
8. **Simplify tests along with production code.** Retain useful native regression and upgrade coverage. Replace implementation-shaped tests with checks of required behavior. Verify an actual restored SDK and an actual preinstalled tool, not only mocked commands that cannot expose the observed failures.

## Working tree at the original handoff

The default branch at the original handoff had commit `8a777776e095aed2dd90e31d945ffd94b84be4b1` (`Use one persistent incremental build cache`). The two failed runs above used it. Consult the current worktree and commit history for subsequent fixes.

The implementation changes present at that handoff were:

- `bin/shared-library-plan`: restrict ABI analysis/extraction to explicitly rebuilt libraries; reuse stored classifications for unchanged ones; canonical PHP 8.5 consumer inventory; skip-consumer option and graph projection.
- `.github/actions/shared-libraries/glibc.py`: skip consumer scans for no-op SDK builds; share the DEB dependency graph with RPM; use cached OpenSSL profiles before planning; remove the redundant `record --config craft.yml` command that caused the EL8 failure.
- `.github/actions/shared-libraries/run.sh`: pass actual rebuilt-library selections and skip consumer discovery for no-op builds.
- `.github/actions/shared-libraries/openssl-variants.py`: retain a cached ABI comparison when applying a profile.
- Regression additions in `tests/abi-dump-cache.py`, `tests/glibc-pipeline.py`, `tests/openssl-variants.py` and `tests/publication-baselines.py`.

These changes are **not a completed simplification**. Review, simplify or replace them. In particular, the unchanged-library path fails fast if the published ABI baseline differs and no matching cached comparison is available; make sure its recovery behavior is usable and does not create another unexpected failure for existing caches.

On resuming the active goal, the go-xcaddy detection problem was traced to shallow metadata merging and SPC requiring a downloaded binary archive before checking the installed directory. `bin/spp` now merges image binary records by platform and registers tools physically installed under the image's package root as local binaries. It preserves pinned source records. `tests/builder-tool-cache.php` passed against the real EL8 image with networking disabled, for both foreign-platform and stale-native snapshot metadata; the installed xcaddy executable also ran successfully. This fix still requires real Alpine ARM64 CI verification.

Installed-tool metadata is initialized before SDK/PHP cache identities are selected, remains idempotent, and retains the image's original archive hash. This prevents the build's later tool setup from changing the inputs used to select its payload cache key.

The four required workflow suites passed locally: `glibc-workflow-entrypoints.py`, `workflow-shared-libraries.py`, `glibc-pipeline.py` and `package-publication.py`. ABI, library-plan, OpenSSL-profile and publication-baseline suites, including the empty-SONAME regression, also passed with the EL8 tools in an unprivileged local container. This is supporting evidence, not proof that either CI pipeline works.

There is an unrelated preexisting staged-add/worktree-delete entry at `.github/actions/publish-packages/action.yml` (`AD` in `git status`). **Do not stage, commit or discard it incidentally.** Untracked `__pycache__` directories are also present.

Existing local evidence includes `build/local-el8-cache-20261108/`, the APK SDK archives under `build/cache-goal-42468709817-{arm64,x86_64}/`, and `build/cache-migration/` containing completed cache-renaming verification. These are ignored local files and may not exist in another checkout. The GitHub `cache` release is the authoritative remote SDK cache; all four SDK archives were preserved in place during renaming.

## Completion criteria

- [ ] Explain and fix the preinstalled go-xcaddy detection failure using the actual image/runtime environment.
- [ ] A restored unchanged SDK succeeds without root `craft.yml`, ABI XML analysis or consumer archive scans.
- [ ] One genuine library update rebuilds only that library and genuine dependents; a no-op selection compiles nothing.
- [ ] Consumer discovery reads only PHP 8.5 and runs once per relevant producer/architecture, shared across RPM/DEB.
- [ ] Existing SDK caches are reused by the new workflow without manual invalidation or a forced cold build.
- [ ] Routine incremental upstream updates produce working RPM, DEB and APK packages, including PHP/extension load and native upgrade checks.
- [ ] Existing version-specific DEB/APK repository users upgrade without configuration changes; RPM legacy URLs keep working.
- [ ] Coherent publication and downgrade protections remain effective without duplicated orchestration.
- [ ] Both real CI pipelines finish successfully, and a subsequent unchanged update demonstrates the fast reuse path.
- [ ] Delete superseded logic and update `AGENTS.md` to describe the simplified implementation. Report remaining limitations honestly; do not call this done because the local test suite is green.
