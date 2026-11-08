# Simplify the package build and update pipeline

## Objective

Make routine upstream updates stable, fast and easy to understand. The shared-library migration introduced too much orchestration and too many failure points. Successful library compilation is followed by expensive repeated work, and unrelated preparation failures prevent packages from being produced.

The user explicitly wants aggressive simplification. Preserve the necessary behavior below, but remove, merge or replace everything else that makes routine updates fragile. Do not preserve an abstraction, helper, receipt, workflow step or compatibility layer merely because it already exists. Do not add another orchestration layer to repair the existing ones.

This is a simplification assignment, not a request to keep patching the current design until one run happens to pass. First identify the smallest pipeline that satisfies the requirements, then replace the redundant paths and delete their superseded code. Prefer fewer independently maintained contracts and one source of truth for each decision. The specific helper files, plan schemas, intermediate artifacts, job boundaries and metadata formats are not requirements.

Success means working Alpine and EL8 builds, reusable caches, reliable publication and ordinary package-manager upgrades. Passing mocked tests alone is insufficient.

## Current handoff status

Verified on 2026-11-08 at commit `d32002f`:

- [EL8 run `42499442120`](https://github.com/static-php/packages/actions/runs/42499442120): both shared SDK jobs succeeded and saved their caches. PHP payload jobs for 8.2–8.6 on both architectures are still running.
- [Alpine run `42500878781`](https://github.com/static-php/packages/actions/runs/42500878781): both shared SDK jobs succeeded and saved their caches. PHP jobs for 8.2–8.6 on both architectures are still running.
- The targeted ngtcp2 repair rebuilt only ngtcp2. Both architectures of both producers confirm 67 cached ABI classifications reused and one library analyzed. The four canonical SDK archives and JSON receipts are present on the `cache` release. This verifies the narrow repair, not the overall design or successful publication.
- Full package installation, load, upgrade and publication results remain unverified for these runs. Preserve their in-flight work and saved caches; inspect their eventual results before launching replacements.

The immediate assignment is to simplify the design, not accumulate another sequence of symptom fixes. The detailed failure history below explains why the current contracts are fragile; it is evidence for deleting duplication, not a specification to retain every workaround. Prioritize the unchanged update path and the one-library update path. Both must be understandable without tracing repeated planning, grafting, classification and metadata rewrites across numerous helpers.

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

Subsequent CI evidence from job `128746788330` in run `42485596531` confirms the no-op ABI fix: `rebuilt_libraries: []`, cached classifications reused for all 68 libraries, and zero libraries analyzed for both RPM and DEB. No PHP consumer archives were scanned. That run was later superseded for a separate tool/cache-key correction, so it does not prove overall pipeline success.

Its remaining long SDK step was package creation: gRPC RPM debuginfo took approximately ten minutes under explicit xz compression. Debuginfo now uses gzip; ordinary RPM/DEB packages retain xz. The native archive tools verify the compression and can decode the resulting packages. This trades larger debug archives for faster packaging. First publication still needs library archives; subsequent unchanged updates should reuse already published archives.

### Alpine: preinstalled go-xcaddy is not recognized

- Run: https://github.com/static-php/packages/actions/runs/42482906241
- Job: https://github.com/static-php/packages/actions/runs/42482906241/job/128741721905
- The PHP 8.4 ARM64 payload build selected PHP 8.4.27, then failed before PHP compilation.
- SPC doctor printed `go-xcaddy is not installed`, attempted `install-go-xcaddy`, then reported `Download artifact 'go-xcaddy' failed` and exited with code 69.
- Both `Dockerfile.alpine` and `Dockerfile.rhel` explicitly install go-xcaddy while building the image, set `PKG_ROOT_PATH=/opt/spc-pkgroot`, and preserve bootstrap download metadata there.

Follow-up investigation traced detection to shallow metadata merging and SPC requiring the downloaded binary archive before checking its installed directory. `bin/spp` now merges the image's binary metadata by platform and registers physically installed tools as local binaries, retaining the original archive hash. An offline test against the real EL8 image passed. Treat this as an implemented correction requiring end-to-end verification, not a reason to preserve the surrounding setup complexity. Routine builds must use the image's installed tool; do not add another network download or rebuild the compiler images without evidence that their contents are wrong.

The downloaded job log is currently `/tmp/packages-apk-failure.log`; it is temporary evidence, not a repository dependency.

### Alpine: successful PHP compilation followed by a license collision

The older run's x86_64 PHP 8.4 job (`128741721952`) completed compilation, then nFPM failed because the CLI license directory mapping flattened multiple libraries' `0.txt` files onto `/usr/share/licenses/php-zts/0.txt`. Directory destinations must omit their trailing slash to preserve nesting. The first correction used nFPM's explicit `tree` type; nFPM 2.44 then produced directory-heavy APKs rejected by Alpine 3.21 as `package file format error`. Ordinary directory mappings with the normalized destination pass actual `apk add` in `tests/apk-license-tree.php --native`, including duplicate basenames and symlinks.

The original compiled payload was not cached because export occurred after packaging. Export now runs before packaging; only a successfully completed sealed export becomes eligible for release-cache saving, including after a packaging/test failure. This preserved the later run's compiled PHP outputs despite its installation failures. Package tests and publication guards remain required.

### Interrupted cache replacement erased an existing SDK

EL8 job `128752715161` missed a genuinely compatible x86_64 cache and started a cold build. Its requested compiler compatibility exactly matched the existing receipt. The original archive had been deleted by a superseded run's save, which was canceled during replacement upload, leaving only an unusable `starter` asset. The verified local archive still matched the old receipt's full checksum and was recovered without compilation.

Cache writes must preserve the current SDK until the replacement is fully uploaded and its receipt is committed. The replacement implementation stages uploads, restores by committed asset ID, keeps a receipt backup during the switch, and finishes with the same readable canonical filenames. It removes obsolete staging assets after success. Tests interrupt each transaction boundary and partial uploads. A matching receipt with a missing archive must fail visibly instead of silently triggering a cold rebuild.

### PHP 8.6 complete-cache profile drifted from the actual build

Both PHP 8.6 Alpine builds compiled successfully but could not export their payloads: the cache's expected profile required Swoole even though `craft.yml.twig` deliberately excludes it for PHP 8.6. Those two outputs were not saved; the eight PHP 8.2–8.5 payloads were saved. The expected profile now applies the exclusion, and a test compares it with the real rendered configurations for RPM, DEB and APK. That check runs during fingerprint preparation, before library or PHP compilation. Preserve the rule that deliberately filtered builds cannot overwrite a complete cache; remove duplicated profile interpretation where possible.

### Cached PHP packaging depended on the old source tree

Run `42493776131` restored PHP 8.3/8.5 payloads with `changed_components: []`, then packaging failed to detect AMQP's version. The version probe loaded unsuffixed module names, while the actual files are named `amqp-zts-85.so`, etc. Fresh builds had hidden this through a source-header fallback. The probe now uses actual module paths and the correct `zend_extension` directive where required. `tests/extension-version-probe.php --payload=PATH --stream=8.5 --type=apk` passed all 81 available shared extensions from the real cached Alpine payload in an offline native container without a PHP source tree. A restored payload must support packaging independently of compilation's workspace.

The subsequent native packaging check found SPX's web UI was also omitted from the PHP cache. Asset refresh now records it under `packaging/spx/web-ui`, and cache export retains it with its module. Existing caches recover the UI from the already downloaded source archive, verified against the compiled source lock; cache graft preserves that provenance. Rebuilt SPX replaces its old UI, while skipped SPX retains no old UI. The real cached Alpine PHP 8.5 payload then created AMQP, MySQLi, Xdebug, SPX, CLI and devel APKs successfully without compilation. `tests/extension-assets.php` covers legacy recovery, archive mismatch rejection, sealed inventory, replacement and reuse. This native check does not replace CI installation, upgrade and publication validation.

### EL8 selected PHP cache keys before initializing their inputs

Job `128776908029` in run `42493778798` compiled and sealed PHP 8.3 successfully, then failed export with `PHP cache export inputs do not match its requested key`. Its key step ran the Python helper directly before Composer installation. The later build initialized vendor files and installed-tool metadata, changing the inputs. A local checkout without vendor files reproduced the exact CI seed `77b55af48ca17f39355467ac0a91606d`; populating vendor changed its core fingerprint, while repeated initialized reads were stable. EL8 now uses the same `run.sh payload-key` preparation already used by Alpine. The existing sealed common-payload upload also runs before release-cache export, preserving completed compilation if export fails. Downstream jobs still require a successful producer job. The failed PHP 8.3 output was not saved; do not claim it can be reused. The four SDK caches remain available; this key-ordering correction does not require library recompilation.

### ngtcp2's crypto adapter lost its OpenSSL dependencies

Alpine ARM64 PHP 8.6 job `128773504109` completed compilation, packaging and native installation, then its loader check failed on `libngtcp2_crypto_ossl.so`. Its sealed PHP payload was saved successfully despite the test failure. The adapter had unresolved OpenSSL symbols and no `DT_NEEDED` entries for libssl/libcrypto; both cached producers had the defect. Reproducing the original link showed libtool discarding SPC's absolute `.so` arguments. `src/hook/Ngtcp2.php` changes the shared adapter's generated Makefile template to normal `-lssl -lcrypto` flags. The real hook passed an EL8 GCC 16 build and relocation checks. A native musl SDK audit went from one failing object out of 237 to zero after rebuilding ngtcp2 with the hook. Recipe comparison and the real selective planner selected only ngtcp2, reusing the other 66 libraries; SDK compiler compatibility and PHP compilation inputs stayed unchanged. Existing SDKs should receive this targeted repair, not a cold library rebuild. CI still needs to verify typed ABI classification, cache updates and full package tests for both architectures.

Alpine x86_64 SDK job `128789019049` then rebuilt exactly ngtcp2 in 31 seconds, but the isolation guard rejected unselected outputs because it compared SONAME symlink timestamps. Native Alpine `ldconfig -n` recreates unchanged symlinks: a real zlib link retained the same target while its timestamp changed. The guard now compares symlink targets and modes, retaining byte, mode and timestamp checks for regular files. Regression checks reject changed link targets and regular-file timestamp changes. Genuine differences also report affected paths rather than only a generic error. Do not restore timestamp/inode comparisons for symlinks or weaken actual output checks to handle this case.

The corrected Alpine jobs `128790898718` and `128790898725` passed that guard and confirmed the intended ABI scope: 67 cached classifications reused and only ngtcp2 analyzed. Planning then failed at `package_versions.pop(type, None)` because SPC's PHP associative-array round-trip changed an empty map from `{}` to `[]`. The planner normalizes only an empty list back to a map at its manifest boundary and rejects populated lists. Native PHP round-trip tests preserve real per-format versions. This is a representation correction, not permission to discard published versions or weaken publication checks.

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
9. **Avoid unnecessary SDK transfers.** If an unchanged request needs neither library compilation nor SDK contents, use its metadata and existing published outputs. Extract the SDK when a PHP build or actual library update needs it. A cache hit is not itself a reason to download, unpack, reclassify, repack and upload the same multi-gigabyte tree.

## How to approach the replacement

- Trace one unchanged update and one changed-library update from source selection through publication. List each expensive operation and the concrete requirement it serves. Delete operations without one; consolidate repeated operations that serve the same requirement.
- Define the decision once: no change, changed PHP/extensions, changed libraries with compatible ABI, or changed libraries requiring dependent rebuilds. Downstream stages should consume that decision rather than rediscover it through another plan, baseline fetch or metadata rewrite.
- Keep compatibility checks tied to actual compiler/source inputs. Orchestration-only edits must not invalidate compiled SDKs. Do not bypass real compiler incompatibility to force cache reuse.
- Before pushing a cache-layout change or dispatching CI, verify that the already uploaded SDKs resolve through the future lookup path. Preserve the working archive throughout replacement, including cancellation. Never use a cold rebuild as the default recovery for a broken cache transaction.
- Verify the narrow path locally first, using the real native tool or builder where needed. Then use CI to validate the replacement. Do not repeatedly launch all streams and architectures while a known preparation or packaging failure guarantees failure.
- Deliver the simpler implementation, deletion of superseded logic, updated documentation and measured evidence: which outputs were reused, which libraries actually rebuilt, how many ABI comparisons ran, and where time was spent. Do not substitute a large new framework or a growing collection of workaround steps.

## Working tree at the original handoff

The default branch at the original handoff had commit `8a777776e095aed2dd90e31d945ffd94b84be4b1` (`Use one persistent incremental build cache`). The two failed runs above used it. Consult the current worktree and commit history for subsequent fixes.

The implementation changes present at that handoff were:

- `bin/shared-library-plan`: restrict ABI analysis/extraction to explicitly rebuilt libraries; reuse stored classifications for unchanged ones; canonical PHP 8.5 consumer inventory; skip-consumer option and graph projection.
- `.github/actions/shared-libraries/glibc.py`: skip consumer scans for no-op SDK builds; share the DEB dependency graph with RPM; use cached OpenSSL profiles before planning; remove the redundant `record --config craft.yml` command that caused the EL8 failure.
- `.github/actions/shared-libraries/run.sh`: pass actual rebuilt-library selections and skip consumer discovery for no-op builds.
- `.github/actions/shared-libraries/openssl-variants.py`: retain a cached ABI comparison when applying a profile.
- Regression additions in `tests/abi-dump-cache.py`, `tests/glibc-pipeline.py`, `tests/openssl-variants.py` and `tests/publication-baselines.py`.

These changes are **not a completed simplification**. Review, simplify or replace them. In particular, the unchanged-library path fails fast if the published ABI baseline differs and no matching cached comparison is available; make sure its recovery behavior is usable and does not create another unexpected failure for existing caches.

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
