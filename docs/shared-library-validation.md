# Shared library validation

The native x86_64 matrix **passed on 2026-10-23**: 25/25 verified cells across Alpine 3.21, Debian 11 and AlmaLinux 8/9/10, PHP 8.2–8.6. Arm64 execution is deferred.

The final archive gate passed: 5,495 per-cell package references, 2,775 distinct compressed archives and 2,095 independent consumer checks. Each native consumer was installed, removed and reinstalled.

| Distro | PHP source | Packages | Consumers | Loaded modules | ELF | Private RUNPATH | Result |
|---|---|---:|---:|---:|---:|---:|---|
| alpine | 8.2.33 | 224 | 88 | 114 | 329 | 302 | PASS |
| alpine | 8.3.33 | 224 | 88 | 114 | 329 | 302 | PASS |
| alpine | 8.4.24 | 224 | 88 | 114 | 329 | 303 | PASS |
| alpine | 8.5.9 | 223 | 87 | 115 | 328 | 302 | PASS |
| alpine | 8.6.0beta1 | 208 | 72 | 100 | 313 | 291 | PASS |
| debian | 8.2.33 | 223 | 87 | 114 | 329 | 302 | PASS |
| debian | 8.3.33 | 223 | 87 | 114 | 329 | 302 | PASS |
| debian | 8.4.24 | 223 | 87 | 114 | 329 | 303 | PASS |
| debian | 8.5.9 | 222 | 86 | 115 | 328 | 302 | PASS |
| debian | 8.6.0beta1 | 207 | 71 | 100 | 313 | 291 | PASS |
| alma8 | 8.2.33 | 223 | 87 | 114 | 329 | 302 | PASS |
| alma8 | 8.3.33 | 223 | 87 | 114 | 329 | 302 | PASS |
| alma8 | 8.4.24 | 223 | 87 | 114 | 329 | 303 | PASS |
| alma8 | 8.5.9 | 222 | 86 | 115 | 328 | 302 | PASS |
| alma8 | 8.6.0beta1 | 207 | 71 | 100 | 313 | 291 | PASS |
| alma9 | 8.2.33 | 223 | 87 | 114 | 329 | 302 | PASS |
| alma9 | 8.3.33 | 223 | 87 | 114 | 329 | 302 | PASS |
| alma9 | 8.4.24 | 223 | 87 | 114 | 329 | 303 | PASS |
| alma9 | 8.5.9 | 222 | 86 | 115 | 328 | 302 | PASS |
| alma9 | 8.6.0beta1 | 207 | 71 | 100 | 313 | 291 | PASS |
| alma10 | 8.2.33 | 223 | 87 | 114 | 329 | 302 | PASS |
| alma10 | 8.3.33 | 223 | 87 | 114 | 329 | 302 | PASS |
| alma10 | 8.4.24 | 223 | 87 | 114 | 329 | 303 | PASS |
| alma10 | 8.5.9 | 222 | 86 | 115 | 328 | 302 | PASS |
| alma10 | 8.6.0beta1 | 207 | 71 | 100 | 313 | 291 | PASS |

Loaded-module counts include builtins. Every cell also verifies the 27 expected builtin modules and mbregex/Argon2 capabilities.

Every PASS includes CLI, CGI, FPM and FrankenPHP requests with the expected module inventory and GD/ImageMagick PNG operations, startup-loaded libphp initialization/evaluation/shutdown, a compiled extension using installed phpize, ELF dependency/private RUNPATH audits, and four native transaction fixtures: static→shared, compatible library-only update, rejected incomplete ABI transition and successful full reverse-consumer cohort.

Production `php bin/spp test --phpv=8.6 --prefix=-zts --type=<apk|deb|rpm>` also passed independently on all five platforms, with the current TestCommand source, PNG requests, matching CLI/FrankenPHP VM kinds, install/uninstall checks and cleanup.

| Library baseline | Owners | ELF | SONAME ABI corpora | Verified empty ABI | License owners | Runtime/debug packages |
|---|---:|---:|---:|---:|---:|---:|
| alpine | 68 | 244 | 240 | 2 | 68 | 136 |
| debian | 68 | 244 | 240 | 2 | 68 | 136 |
| alma8 | 68 | 244 | 240 | 2 | 68 | 136 |
| alma9 | 68 | 244 | 240 | 2 | 68 | 136 |
| alma10 | 68 | 244 | 240 | 2 | 68 | 136 |

Alpine runtime/debug APK metadata was repackaged with `version_schema: none`: all 68 runtime headers now match their literal declared versions and all 68 debug dependencies match exactly. The 136-package native solver installation passed. Every library payload byte, mode and symlink remained unchanged, including 488 runtime/debug ELF objects, 79 license files and 240 ABI XML files.

All five Alpine PHP generations were rebuilt normally with `-Wl,-z,stack-size=0x80000`; the actual FrankenPHP ELF headers declare a non-executable 512 KiB stack. This resolves the ImageMagick PNG worker crash. A normal C negative control failed 3/3 at the old default stack, and the ordinary linker-flag control passed 3/3 without changing library ELF bytes. PHP 8.6 uses only the exact three validated master-recipe removals from the reviewed template; saved source/private hashes and frozen commits are included in the JSON.

PHP 8.6.0beta1 intentionally records 15 unsupported shared extensions on every platform: ds, ev, igbinary, msgpack, memcached, mongodb, mysqlnd_ed25519, mysqlnd_parsec, sqlsrv, pdo_sqlsrv, redis, spx, swoole, xlswriter and yac. None has a native subpackage or loaded module in those cells. Matching pinned-source Alma 8 diagnostics corroborate PHP API/header/version-guard failures and serializer cascades; the original Alpine compiler stderr was not retained. Stable PHP streams skip no extensions.

SPC is locally committed at `d910c62cf4dbda527688038705449513ad7c0046` and the Composer source/dist references use that exact unpublished commit. The frozen PIE payload is 1.5.3 while the original download metadata says 1.4.10; that provenance difference is preserved. Public pushes, package publication, remote repository/secret provisioning and hosted installer publication were not performed.

[The portable JSON](shared-library-validation.json) records source/helper generations, per-cell native/manifest/provenance hashes, all consumer archive hashes, deduplicated library archive inventories, skipped-extension corroboration, production CI proof, regression/source checks and retired attempt history. The four older ROOT-helper passes are distinguished from later v2 helper passes; observer attachment limits are explicit. Per-cell temporary resources and the final task build directory were removed. All 13 task image references and 106 remaining task cache records were removed; all 480 pre-existing cache records and unrelated image aliases were preserved. [Cleanup evidence](shared-library-cleanup.json) records the final checks.

Final checker certificate SHA-256: `34a9e564f90efd0d67a4201a704134808a9c93a0de0b74d2fd10d04d3de92d26`.

Portable JSON SHA-256: `1a86a4881cce1ee580dce1229560c79d38f4f9833d8be07643a89afac437c742`.

## Reproduce checks

Validate a real package set and its build manifest with:

```sh
python3 -B tests/shared-runtime-containers.py --os alma8 --php 8.4 \
    --artifacts /path/to/dist/rpm \
    --manifest /path/to/buildroot/shared-libraries.json
```

The reusable build driver is `tests/build-shared-matrix.py`. With its five prepared
`php-zts-shared-test-<distro>-debug` builder images and the pinned downloads available,
`--jobs 1 --build-concurrency 8 --abi-jobs 1` runs the full matrix one distro at a time.
The completed validation used temporary bounded recovery runners, retaining only
verified packages between compilation and runtime checks.

Final host checks passed: 46 workflow tests, 38 production VM/PNG regressions,
5 installer tests, 5 repository-version tests, revision/autoload fixtures and syntax
checks. Upgrade guards passed 12 checks and skipped two host package-tool checks;
all three native package formats were exercised in the container matrix. The SPC
shared-library group passed 51 tests/437 assertions and configuration tests passed
96 tests/188 assertions. Its unrelated full-suite ZipTest state failure was reproduced on the original
revision; the isolated ZipTest passed. ABI planner tests passed 39/39 in all five
builders.
