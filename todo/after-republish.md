# Cleanup after republishing

## Prerequisites

- [x] Repackage and publish libraries and their PHP consumers together for every
  supported format, stream and architecture. Every package losing synthetic
  `php-zts-lib-*-abi-N` requirements/provides must have a strictly newer native version
  or revision, including CLI, affected SAPIs/extensions and matching debuginfo packages.
  Replacing archives at the same version does not update installed dependencies.
  Set `bump=true` for this repackaging run, leave `iteration` and package/library filters
  empty, and verify the resulting versions and headers before publication.
  `bump=false` preserves revisions. Unchanged binaries can be reused.
  Primary publication completed in [RPM/DEB run 43204217976](https://github.com/static-php/packages/actions/runs/43204217976)
  and [APK run 43193640698](https://github.com/static-php/packages/actions/runs/43193640698).
- [ ] Replace reusable SDK, PHP payload and OpenSSL overlay caches, and verify their
  metadata no longer contains the retired fields below. OpenSSL receipts must contain
  `sonames`. Publish-only, repackage-only and `publish=false` runs do not refresh caches.
  Retire old build artifacts that could republish packages with synthetic dependencies.

## Remove once published metadata and caches are clean

- [ ] Delete retired-field stripping from `bin/shared-library-plan`,
  `src/util/SharedLibraries.php` and `.github/actions/shared-libraries/glibc.py`.
  The fields are `abi_epoch`, `abi_capability`, `abi_capabilities`, `abi_dump`,
  `abi_dump_hashes`, `abi_empty`, `abi_reference_version` and `abi_comparison`.
- [ ] Delete legacy OpenSSL receipt handling in
  `.github/actions/shared-libraries/openssl-variants.py`: XML-file filtering,
  `abi_*`/`abi` stripping and acceptance of receipts without `sonames`.
- [ ] Simplify cached identity/proof handling in `src/util/BuildPayload.php` and
  `.github/actions/shared-libraries/payload-cache.py` that discards old epoch fields.
  Keep the library-name/SONAME comparison and payload integrity checks.
- [ ] Remove fixtures and tests for these retired metadata formats with the code.
  Keep SONAME-change and native installation/upgrade coverage.

## Other deletions and their conditions

- [ ] After cold SDK rebuilds for both producers and architectures replace every
  imported seed, delete `bin/adopt-shared-sdk`,
  `.github/actions/shared-libraries/sdk-import.py`, their tests, and their integration
  in `incremental-build.py` and `bin/export-library-sdk`. Verify no cache needs a
  `shared-library-deployment-compatibility.json` receipt. Full PHP mode can reuse an
  SDK, so a full PHP build alone does not meet this condition.
- [ ] Delete retired `tests/build-shared-matrix.py` together with its dependent tests
  in `tests/shared-runtime-harness.py`, after checking for remaining callers.
  This cleanup does not need to wait for publication.
- [ ] Once support for pinned older download runs ends, remove the missing-artifact
  fallbacks in `.github/actions/shared-libraries/source-snapshot.py`.
  Keep `downloads-tarball`: cold SDK builds still use it.

Republishing does not retire installed clients. Keep package-version migration rules,
required runtime/linker path aliases, published providers needed by older consumers,
and native upgrade guards until their actual compatibility requirements end.
