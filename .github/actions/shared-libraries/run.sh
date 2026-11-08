#!/usr/bin/env bash
set -euo pipefail

command=$1
if [[ -n "${LIBRARIES:-}" ]]; then
    export MODE=incremental
fi
if [[ "${MODE:-full}" == incremental && ! -f downloads/build-plan.json && "$command" =~ ^(libs|classify|php)$ ]]; then
    echo 'Incremental builds require downloads/build-plan.json; run spc-download or select full mode' >&2
    exit 1
fi
if [[ "$TYPE" == apk && "${IN_BUILDER:-}" != true ]]; then
    mkdir -p "$HOME/.cache/composer"
    exec docker run --rm -v "$GITHUB_WORKSPACE":/build -w /build \
        -v "$HOME/.cache/composer":/composer-cache \
        -e IN_BUILDER=true -e COMPOSER_CACHE_DIR=/composer-cache \
        -e TYPE -e MODE -e LIBRARIES -e PHP_VERSIONS -e PACKAGES -e PHPV -e ITERATION -e BUMP \
        -e COMPILE -e COMPILE_PACKAGES -e ABI_REBUILD_LIBRARIES -e LIBRARY_ONLY \
        -e IMAGE -e SOURCE_RUN_ID -e CACHE_KEY -e ALMA \
        -e FORGEJO_PASSWORD -e GITHUB_TOKEN -e TARGET -e MATRIX_ARCH \
        "$IMAGE" bash .github/actions/shared-libraries/run.sh "$command"
fi

if ! python3 -c 'import sys; sys.exit(sys.version_info < (3, 9))'; then
    python=$(command -v python3.9)
    python_tools=$(mktemp -d "${TMPDIR:-/tmp}/spp-python-XXXXXXXX")
    ln -s "$python" "$python_tools/python3"
    export PATH="$python_tools:$PATH"
    trap 'rm -f "$python_tools/python3"; rmdir "$python_tools"' EXIT
fi

baseline() {
    local output=$1 host=${2:-1}
    local args=(--type "$TYPE" --arch "$MATRIX_ARCH" --output "$output")
    if [[ "$TYPE" == rpm ]]; then
        if [[ "${GLIBC_PIPELINE:-}" == true ]]; then
            local arch="$MATRIX_ARCH"
            [[ "$arch" == arm64 ]] && arch=aarch64
            args+=(--base-url "https://rpm.henderkes.com/$arch/el8"                 --baseline-url "https://rpm.henderkes.com/$arch/el9" --baseline-url "https://rpm.henderkes.com/$arch/el10")
        else
            args+=(--host "$RPM_HOST" --remote-directory "$RPM_DIRECTORY")
        fi
    else
        local url=https://git.henderkes.com
        [[ "$host" == 2 ]] && url=https://git.static-php.dev
        args+=(--host "$url" --password-env FORGEJO_PASSWORD --consumer-password-env FORGEJO_PASSWORD)
    fi
    python3 bin/shared-library-plan baseline "${args[@]}"
}

case "$command" in
    glibc-classify|glibc-record|glibc-payload|glibc-package)
        git config --global --add safe.directory "$PWD"
        composer install --no-interaction --prefer-dist --no-progress
        python3 .github/actions/shared-libraries/glibc.py "${command#glibc-}"
        ;;
    fingerprint)
        git config --global --add safe.directory "$PWD"
        composer install --no-interaction --prefer-dist --no-progress >&2
        mkdir -p stage
        php .github/actions/shared-libraries/build-inputs.php "$PWD" "$TYPE" "$TARGET" > stage/library-build-environment.json
        toolchain=$(python3 .github/actions/shared-libraries/toolchain.py --build-environment stage/library-build-environment.json)
        python3 .github/actions/shared-libraries/workflow.py fingerprint --target "$TYPE-$TARGET" --toolchain "$toolchain" \
            --libraries "${LIBRARIES:-}" --image "$IMAGE" --build-environment stage/library-build-environment.json
        ;;
    libs)
        git config --global --add safe.directory "$PWD"
        composer install --no-interaction --prefer-dist --no-progress
        python3 .github/actions/shared-libraries/incremental-build.py plan --root . \
            --compatibility "$(cat stage/library-compatibility)" --libraries "${LIBRARIES:-}" \
            --output stage/library-build-plan.json
        python3 .github/actions/shared-libraries/incremental-build.py run --root . \
            --plan stage/library-build-plan.json --type "$TYPE"
        if python3 -c 'import json,sys; sys.exit(not json.load(open("stage/library-build-plan.json")).get("rebuilt_libraries"))'; then
            touch stage/sdk-rebuilt
        fi
        test -s buildroot/shared-libraries.json
        ;;
    classify)
        git config --global --add safe.directory "$PWD"
        composer install --no-interaction --prefer-dist --no-progress
        mkdir -p stage
        python3 .github/actions/shared-libraries/source-plan.py --reconcile --root . --output stage/effective-source-plan.json
        baseline stage/baseline
        args=(--type "$TYPE" --manifest buildroot/shared-libraries.json --root buildroot \
            --baseline stage/baseline --mode "$MODE" \
            --libraries "$LIBRARIES" --php-versions "$PHP_VERSIONS" --packages "$PACKAGES" \
            --output stage/shared-library-plan.json \
            --manifest-output buildroot/shared-libraries.json)
        if [[ -f stage/effective-source-plan.json ]]; then
            args+=(--request stage/effective-source-plan.json)
        fi
        if [[ "$TYPE" != rpm ]]; then
            baseline stage/baseline-2 2
            args+=(--peer-baseline stage/baseline-2)
        fi
        if [[ -z "$LIBRARIES" ]] && { [[ "$MODE" == full ]] || python3 -c 'import json,sys; from pathlib import Path; b=Path("stage/baseline/shared-libraries.json"); p=json.load(open("downloads/build-plan.json")); empty=not b.exists() or not json.loads(b.read_text()).get("libraries"); sys.exit(not (empty and all(p.get("requests",{}).get(s,{}).get("full") for s in ["8.2","8.3","8.4","8.5","8.6"])))'; }; then
            args+=(--bootstrap)
        fi
        for phase in $(seq 1 1000); do
            rebuilt=''
            if [[ -f stage/library-build-plan.json ]]; then
                rebuilt=$(python3 -c 'import json; print(",".join(json.load(open("stage/library-build-plan.json")).get("rebuilt_libraries", [])))')
            fi
            python3 bin/shared-library-plan prepare "${args[@]}" --rebuilt-libraries "$rebuilt"
            roots=$(python3 -c 'import json; p=json.load(open("stage/shared-library-plan.json")); m=json.load(open("buildroot/shared-libraries.json")); print(",".join(sorted(set(p.get("abi_rebuild_roots", [])) | {provider for dependencies in m.get("pending_rebuilds", {}).values() for provider in dependencies.values()})))')
            [[ -z "$roots" ]] && break
            python3 .github/actions/shared-libraries/incremental-build.py expand --root . \
                --plan stage/library-build-plan.json --libraries "$roots" --output stage/library-reverse-plan.json
            pending=$(python3 -c 'import json; print(",".join(json.load(open("stage/library-reverse-plan.json"))["selected"]))')
            [[ -z "$pending" ]] && break
            python3 .github/actions/shared-libraries/incremental-build.py run --root . \
                --plan stage/library-reverse-plan.json --type "$TYPE"
            cp stage/library-reverse-plan.json stage/library-build-plan.json
            touch stage/sdk-rebuilt
            if [[ "$phase" == 1000 ]]; then
                echo 'Library ABI rebuild planning did not converge' >&2
                exit 1
            fi
        done
        python3 .github/actions/shared-libraries/incremental-build.py validate-final --root .
        php bin/build-payload seal --root buildroot
        export SPP_BASELINE_MANIFESTS
        SPP_BASELINE_MANIFESTS=$(python3 -c 'import json,pathlib; print(json.dumps([str(p.resolve()) for p in pathlib.Path("stage").glob("baseline*/shared-libraries.json")]))')
        export SPP_SHARED_LIBRARY_PLAN=stage/shared-library-plan.json
        retry_args=(--plan stage/shared-library-plan.json --destination "dist/$TYPE" --manifest buildroot/shared-libraries.json --manifest-output buildroot/shared-libraries.json --output stage/publication-retries.json)
        for baseline_manifest in stage/baseline*/shared-libraries.json; do retry_args+=(--baseline-manifest "$baseline_manifest"); done
        python3 bin/shared-library-plan stage-publication-retries "${retry_args[@]}"
        selected=$(python3 -c 'import json; print(",".join(json.load(open("stage/publication-retries.json"))["remaining_libraries"]))')
        layout_required=$(python3 -c 'import json; print(str(json.load(open("stage/publication-retries.json")).get("layout_required", False)).lower())')
        if [[ -n "$selected" || "$layout_required" == true ]]; then
            flags=(--libs-only --type="$TYPE" --debuginfo)
            if [[ -n "$selected" ]]; then flags+=(--libraries="$selected"); else flags+=(--packages=runtime_layout); fi
            if [[ -n "$ITERATION" ]]; then flags+=(--iteration="$ITERATION"); elif [[ "$BUMP" == true ]]; then flags+=(--bump); fi
            php bin/spp package "${flags[@]}"
        fi
        mkdir -p stage/libraries
        find "dist/$TYPE" -maxdepth 1 -name "*.$TYPE" -exec cp -a {} stage/libraries/ \;
        if [[ -f "dist/$TYPE/shared-library-consumers.json" ]]; then cp "dist/$TYPE/shared-library-consumers.json" stage/libraries/; fi
        php bin/build-payload manifest --root buildroot --type "$TYPE" --output stage/shared-libraries.json
        if [[ -d buildroot/usr/share/php-zts/libraries/abi ]]; then
            mkdir -p stage/abi
            cp -a buildroot/usr/share/php-zts/libraries/abi/. stage/abi/
        fi
        ;;
    payload-key)
        composer install --no-interaction --prefer-dist --no-progress >&2
        python3 .github/actions/shared-libraries/payload-cache.py key --root . --stream "$PHPV"             --manifest buildroot/shared-libraries.json --compatibility stage/library-compatibility
        ;;
    prepare-runtime)
        python3 .github/actions/shared-libraries/runtime-checks.py --type "$TYPE" --payload "build/$PHPV"             --candidates "dist/$TYPE" --baseline-dir stage/published-baseline --output stage/runtime-libraries
        ;;
    freeze-baseline)
        python3 bin/freeze-migration-baseline.py --type "$TYPE" --php "$PHPV" --arch "$MATRIX_ARCH" --profile all --output stage/published-baseline
        ;;
    upload-1|upload-2)
        host=${command##*-}
        cd "dist/$TYPE"
        python3 ../../.github/actions/shared-libraries/publish-forgejo.py upload "$TYPE" "$host"
        ;;
    record)
        jq -n --arg arch "$MATRIX_ARCH" --arg alma "$ALMA" --arg target "$TARGET" --arg image "$IMAGE" --arg source "$SOURCE_RUN_ID" \
            '{arch: $arch, alma: $alma, target: $target, image: $image, source_run_id: $source}' > stage/target.json
        printf '%s\n' "$CACHE_KEY" > stage/cache-key
        python3 .github/actions/shared-libraries/workflow.py receipt --kind libraries \
            --plan stage/shared-library-plan.json --source-run-id "$SOURCE_RUN_ID" --image "$IMAGE" \
            --output stage/shared-library-build-receipt.json
        ;;
    php)
        git config --global --add safe.directory "$PWD"
        composer install --no-interaction --prefer-dist --no-progress
        cp stage/shared-libraries.json buildroot/shared-libraries.json
        if [[ -d stage/abi ]]; then
            mkdir -p buildroot/usr/share/php-zts/libraries/abi
            cp -a stage/abi/. buildroot/usr/share/php-zts/libraries/abi/
        fi
        mkdir -p dist/"$TYPE"
        if [[ -d stage/libraries ]]; then cp stage/libraries/*."$TYPE" dist/"$TYPE"/; fi
        if [[ "${LIBRARY_ONLY:-}" == true ]]; then exit 0; fi
        flags=(--phpv="$PHPV" --prefix=-zts --type="$TYPE" --debuginfo)
        [[ -n "$PACKAGES" ]] && flags+=(--packages="$PACKAGES")
        if [[ -n "$ITERATION" ]]; then flags+=(--iteration="$ITERATION"); elif [[ "$BUMP" == true ]]; then flags+=(--bump); fi
        php tests/package-versions.php --type="$TYPE"
        php tests/package-iterations.php
        python3 tests/upgrade-guards.py
        python3 .github/actions/shared-libraries/glibc.py payload
        export SPP_BASELINE_MANIFESTS
        SPP_BASELINE_MANIFESTS=$(python3 -c 'import json,pathlib; print(json.dumps([str(p.resolve()) for p in pathlib.Path("stage").glob("baseline*/shared-libraries.json")]))')
        export SPP_SHARED_LIBRARY_PLAN=stage/shared-library-plan.json
        php bin/spp package "${flags[@]}"
        if [[ -f stage/payload-cache-key ]]; then
            source_run=$(python3 -c 'import json; print(json.load(open("stage/payload-provenance.json"))["compiled_source_run_id"])')
            python3 .github/actions/shared-libraries/payload-cache.py export --root . --payload "build/$PHPV"                 --compatibility stage/library-compatibility --key "$(cat stage/payload-cache-key)"                 --seed-key "$(cat stage/payload-cache-seed-key)" --source-run-id "$source_run" --output php-payload-cache.tar.zst
        fi
        test_flags=(--phpv="$PHPV" --prefix=-zts --type="$TYPE")
        [[ -n "$PACKAGES" ]] && test_flags+=(--packages="$PACKAGES")
        php bin/spp test "${test_flags[@]}"
        python3 .github/actions/shared-libraries/workflow.py receipt --kind consumer --php-stream "$PHPV" \
            --plan stage/shared-library-plan.json --source-run-id "$SOURCE_RUN_ID" --image "$IMAGE" \
            --output "dist/$TYPE/shared-library-build-receipt.json"
        ;;
    publish-rpm)
        if [[ "$TYPE" != rpm || ! "$RPM_DIRECTORY" =~ ^/mnt/data/rpm/(x86_64|aarch64)/el8$ ]]; then
            echo 'Canonical RPM publication requires the validated architecture/el8 destination' >&2
            exit 1
        fi
        state="${RPM_DIRECTORY%/el8}/.php-zts"
        incoming=$(ssh "$RPM_HOST" "mkdir -p '$state/incoming' && mktemp -d '$state/incoming/cohort-XXXXXXXX'")
        if [[ "$incoming" != "$state/incoming/cohort-"* || ! "${incoming##*/}" =~ ^cohort-[A-Za-z0-9]+$ ]]; then
            echo 'Invalid remote cohort staging directory' >&2
            exit 1
        fi
        trap 'ssh "$RPM_HOST" "rm -rf -- '"'"'$incoming'"'"'"' EXIT
        ssh "$RPM_HOST" "mkdir -p '$incoming/packages' '$incoming/tools'"
        rsync -av --from0 --files-from=rpm-upload-files dist/rpm/ "$RPM_HOST:$incoming/packages/"
        rsync -av bin/canonical-rpm-repository bin/createrepo_static "$RPM_HOST:$incoming/tools/"
        rsync -av cohort-result.json "$RPM_HOST:$incoming/cohort.json"
        arch="${RPM_DIRECTORY%/el8}"
        arch="${arch##*/}"
        ssh "$RPM_HOST" "bash -s -- '$state' '$incoming' '$arch'" <<'REMOTE'
        set -euo pipefail
        state=$1
        incoming=$2
        arch=$3
        python=python3
        if command -v python3.9 >/dev/null 2>&1; then python=python3.9; fi
        "$python" "$incoming/tools/canonical-rpm-repository" stage \
            --state-root "$state" --incoming "$incoming/packages" --architecture "$arch" \
            --cohort-receipt "$incoming/cohort.json" \
            --legacy-root "${state%/.php-zts}/el8" --legacy-root "${state%/.php-zts}/el9" \
            --legacy-root "${state%/.php-zts}/el10" --output "$incoming/stage.json"
        generation=$("$python" -c 'import json,sys; print(json.load(open(sys.argv[1]))["generation"])' "$incoming/stage.json")
        "$python" "$incoming/tools/canonical-rpm-repository" promote \
            --state-root "$state" --generation "$generation" --activate-aliases
REMOTE
        ;;
    assemble|publish)
        libs="cohort/shared-libs-$TYPE-$TARGET"
        python3 .github/actions/shared-libraries/workflow.py prune-consumers --source cohort --library-artifact "$libs"
        if [[ ! -d "dist/$TYPE" ]]; then
            python3 .github/actions/shared-libraries/workflow.py assemble --source cohort --destination "dist/$TYPE" --type "$TYPE"
        fi
        if [[ "$command" == assemble ]]; then exit 0; fi
        baseline current-baseline
        baseline_args=()
        [[ -f current-baseline/shared-libraries.json ]] && baseline_args+=(--baseline-manifest current-baseline/shared-libraries.json)
        python3 bin/shared-library-plan cohort --plan "$libs/shared-library-plan.json" --artifacts "dist/$TYPE" --type "$TYPE" "${baseline_args[@]}" --output cohort-result.json
        if [[ "$TYPE" == rpm ]]; then
            python3 bin/check-rpm-upgrades --host "$RPM_HOST" --remote-directory "$RPM_DIRECTORY"                 --baseline-directory "${RPM_DIRECTORY%/el8}/el9" --baseline-directory "${RPM_DIRECTORY%/el8}/el10"                 --strict-cohort --files-from rpm-upload-files "dist/$TYPE"
        else
            for host in 1 2; do
                baseline "current-baseline-$host" "$host"
                baseline_args=()
                [[ -f "current-baseline-$host/shared-libraries.json" ]] && baseline_args+=(--baseline-manifest "current-baseline-$host/shared-libraries.json")
                python3 bin/shared-library-plan cohort --plan "$libs/shared-library-plan.json" --artifacts "dist/$TYPE" --type "$TYPE" "${baseline_args[@]}" --output "cohort-result-$host.json"
                (cd "dist/$TYPE"; python3 ../../.github/actions/shared-libraries/publish-forgejo.py check "$TYPE" "$host")
            done
        fi
        ;;
    *) echo "Unknown shared-library workflow command: $command" >&2; exit 1 ;;
esac
