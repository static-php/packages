# Simplify the package build and update pipeline

## Assignment

Make routine upstream updates stable, fast and understandable. The shared-library migration introduced too much orchestration: cache restoration, planning, ABI classification, packaging and publication have overlapping state and failure points. Hours of successful compilation can be followed by an unrelated metadata or packaging failure.

Simplify the design aggressively. The behavior requirements below are necessary; the current helpers, schemas, receipts, intermediate artifacts and job boundaries are replaceable. Delete superseded logic. Do not hide the same complexity inside one enormous script or add another framework around it. A sequence of isolated fixes that eventually produces one green run does not fulfill this assignment.

The two most important paths are an unchanged update and an update to one library. Make both easy to follow, cheap and reliable. Preserve already compiled outputs while doing this work.

## Reported failures and why they matter

- **Unchanged EL8 SDK still ran expensive classification and failed.** [Run 42482909324, job 128738640459](https://github.com/static-php/packages/actions/runs/42482909324/job/128738640459) restored its SDK, rebuilt no libraries, then spent roughly eleven minutes in classification. It failed when a redundant payload-recording command required root `craft.yml`, which exists after compilation but not necessarily after restoration. A cache hit must be usable independently of compilation's old workspace.
- **Alpine tried to download a tool already installed in its builder.** [Run 42482906241, job 128741721905](https://github.com/static-php/packages/actions/runs/42482906241/job/128741721905) reported `go-xcaddy is not installed`, tried installation, and failed downloading the artifact before PHP compilation. Both builder Dockerfiles install it under the configured package root. Tool detection, metadata and executable paths must agree; routine builds must not reinstall it.
- **Repeated consumer discovery was unnecessary.** Reading PHP 8.2–8.6 consumers duplicates the same library dependency inventory. Reading it independently for RPM and DEB duplicates it again. Doing either when no library rebuilt wastes time without changing a decision.
- **Cache management became its own fragile subsystem.** Long input/checksum filenames, seed aliases and weekly release generations obscured the reusable target SDK. One interrupted replacement deleted a compatible archive before its replacement finished, causing an avoidable cold build. Keep the previous usable cache until replacement succeeds.
- **Restoration did not reproduce the build environment.** Later failures involved cache keys selected before tool initialization, extension version probes relying on source trees, missing SPX UI assets, a PHP 8.6 cache profile disagreeing with the rendered build, and host extraction changing sealed file permissions. Cache contents and input identities need a single clear contract.
- **Packaging and publication multiplied expensive work.** Examples include slow debug compression, conflicting independently generated `Architecture: all` DEBs, repeated downloads of identical library archives, an optional Forgejo Git association failing uploads, SSH trust written to the wrong home, and native baselines omitting the DEB `all` index or two-component upstream versions such as memcache `8.2`. Required native upgrade and identity guards exposed real bugs; do not remove those guards to make a run green.

Several targeted fixes already exist. At handoff, local HEAD is `ba09739`; inspect the actual checkout before changing anything. Relevant repairs include installed-tool metadata initialization, no-op ABI reuse, atomic cache replacement, sealed packaging assets, deterministic runtime-layout DEBs, in-process verified archive identity reuse, complete native dependency baselines and RPM version parsing. Review or replace their implementations rather than reintroducing the failures they prevent.

Earlier native CI evidence demonstrated all ten Alpine build/test targets passing from cached PHP payloads, all ten EL8 PHP payloads reused without compilation, and unchanged SDKs reporting 68 cached classifications and zero analyzed libraries. A genuine ngtcp2 repair previously rebuilt only ngtcp2, with 67 cached classifications and one analyzed library per producer/architecture. These establish behavior worth preserving, not completion of this simplification or proof of successful publication.

CI state changes. Inspect current runs and actual terminal logs before dispatching, canceling or claiming success. Useful references are [Alpine source 42509033185](https://github.com/static-php/packages/actions/runs/42509033185), [Alpine publication recovery 42518472445](https://github.com/static-php/packages/actions/runs/42518472445), [EL8 source 42517652884](https://github.com/static-php/packages/actions/runs/42517652884) and [EL8 recovery 42526301093](https://github.com/static-php/packages/actions/runs/42526301093). This document does not assert their current live status. A green upload step with `continue-on-error` is insufficient evidence of successful publication.

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

## Simplification priorities

1. **One obvious control flow:** select and initialize inputs, restore compatible outputs, rebuild changed inputs and genuine dependents, package, test, publish. Each decision should have one authoritative implementation. Remove repeated planning, metadata rewriting, convergence passes and workspace reconstruction where a direct decision suffices.
2. **An unchanged SDK does no ABI work.** Reuse cached classification. Do not invoke ABI tools, parse cached ABI XML or download published PHP consumer archives when no library actually rebuilt. Report clearly that compilation and ABI analysis were skipped. Keep necessary publication/version checks.
3. **Read the consumer graph only when needed.** Use PHP **8.5 only**. For EL8, obtain the dependency graph once per architecture and share it across RPM/DEB. Alpine requires its own native graph. Analyze only regenerated libraries and genuinely affected dependents. This graph is distinct from per-stream native package versions, which publication must still verify.
4. **Use the builder's installed tools.** Verify actual go-xcaddy executable detection in the image, including SPC metadata and subprocess environment. Do not paper over detection errors with another download or an unnecessary image rebuild.
5. **Keep one persistent cache per target.** The release is `cache`; SDK names are `buildroot-apk-{arch}.tar.zst` and `buildroot-glibc-alma8-{arch}.tar.zst`, with JSON metadata. Reuse and update that SDK after partial rebuilds. Keep compatibility and integrity data in metadata. Do not restore checksum-heavy asset names, weekly drafts, forced Monday library rebuilds, or a separate libs account/password.
6. **Preserve useful compiled work.** Do not invalidate compiler compatibility merely because orchestration changed. Do not delete usable archives before replacements are committed. If names must change, make existing uploaded assets available under the future names and verify lookup before pushing a workflow that expects them. The earlier cache rename is already complete; do not repeat it.
7. **Avoid unnecessary transfers and packaging.** If an unchanged operation needs only metadata and existing packages, do not download/extract a multi-gigabyte SDK. Extract when actual compilation or packaging needs its contents. Avoid repeatedly downloading the same immutable published archive. Reuse validated cohorts for publication retries without compilation or repackaging.
8. **Keep library optimization separate from PHP optimization.** Native GCC library defaults in `config/templates/craft.yml.twig` currently omit LTO; PHP 8.6 has separate LTO flags. Verify actual library compiler commands if build time suggests otherwise. Do not accidentally apply PHP's flags to all shared libraries or change compiler options just to test orchestration.
9. **Simplify tests with the implementation.** Keep meaningful native regression, ABI and upgrade coverage. Replace tests that merely mirror helper internals. Validate restored outputs and installed tools in actual unprivileged native containers, including an offline restoration path.

## Where to start

Read `AGENTS.md`, the two producer workflows and their call paths. Inspect overlapping responsibilities in:

- `.github/actions/shared-libraries/{run.sh,glibc.py,workflow.py,source-plan.py,incremental-build.py,payload-cache.py,openssl-variants.py}`
- `.github/actions/buildroot-cache/` and `bin/shared-library-plan`
- `bin/build-payload`, `bin/forgejo-helper`, `bin/freeze-migration-baseline.py` and the publication workflows
- `bin/spp`, `src/step/RunSPC.php`, `src/Command/CreatePackages.php`, the craft template and builder Dockerfiles

Write down the smallest flow satisfying the required behavior before editing. Identify duplicated decisions and state, then remove them rather than preserving the current structure by default. Start with the unchanged path and one-library path. Retain necessary source, ABI and package integrity evidence at clear boundaries; checksums themselves are useful, repeatedly recomputing/downloading everything is not.

Run the four workflow checks required by `AGENTS.md` plus targeted native checks for changed behavior. Validate one representative target before fanning out while a known failure guarantees wasted work. Do not cancel useful live work or restart compilation because a publication-only retry is needed. Separate observation timeouts from actual job failures.

There is an unrelated staged-add/worktree-delete entry at `.github/actions/publish-packages/action.yml`. Preserve it; never stage, commit or discard it incidentally. Leave untracked `__pycache__` directories alone. Ignored local evidence such as `build/local-el8-cache-20261108/` may help; the remote `cache` release is authoritative and another checkout may lack local evidence.

## Acceptance criteria

- [ ] The implementation is materially simpler: duplicated control flow and superseded helpers/contracts are deleted, and `AGENTS.md` describes the resulting pipeline.
- [ ] Preinstalled go-xcaddy is detected in the actual builders without another download.
- [ ] A restored unchanged SDK succeeds without compilation-generated workspace files, library compilation, ABI analysis or consumer scans.
- [ ] A real single-library update rebuilds only changed inputs and genuine dependents; ABI comparisons cover only regenerated libraries requiring comparison. An unchanged manual selection compiles nothing.
- [ ] Consumer discovery uses PHP 8.5 only, runs only after a library rebuild, and is shared between RPM/DEB per architecture.
- [ ] Existing uploaded SDKs and PHP payloads survive the transition and are reused without forced cold builds. Interrupted cache writes preserve the previous usable cache.
- [ ] Actual Alpine and EL8 CI builds, native install/load/upgrade/TLS checks and required primary publication succeed for both architectures. RPM/DEB continue sharing the same common compiled bytes.
- [ ] Existing PHP-specific DEB/APK repository users and legacy RPM URLs continue upgrading without configuration changes. Native version ordering, signing, identity checks and coherent ABI publication remain enforced.
- [ ] A subsequent unchanged update demonstrates the fast path in actual logs. Report elapsed time, reused outputs, rebuilt libraries, ABI comparison counts and consumer downloads; local green tests alone do not prove success.

Deliver the simpler implementation and measured evidence. Report remaining limitations honestly. Do not substitute another large orchestration layer or claim completion after fixing only the latest symptom.
