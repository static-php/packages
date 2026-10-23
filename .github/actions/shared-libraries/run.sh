#!/usr/bin/env bash
set -euo pipefail

command=$1
if [[ "${MODE:-full}" == incremental && ! -f downloads/build-plan.json && "$command" =~ ^(libs|classify|php)$ ]]; then
    echo 'Incremental builds require downloads/build-plan.json; run spc-download or select full mode' >&2
    exit 1
fi
if [[ "$TYPE" == apk && "${IN_BUILDER:-}" != true && "$command" != fingerprint ]]; then
    mkdir -p "$HOME/.cache/composer"
    exec docker run --rm -v "$GITHUB_WORKSPACE":/build -w /build \
        -v "$HOME/.cache/composer":/composer-cache \
        -e IN_BUILDER=true -e COMPOSER_CACHE_DIR=/composer-cache \
        -e TYPE -e MODE -e LIBRARIES -e PHP_VERSIONS -e PACKAGES -e PHPV -e ITERATION -e BUMP \
        -e IMAGE -e SOURCE_RUN_ID -e CACHE_KEY -e ALMA \
        -e FORGEJO_PASSWORD -e FORGEJO_LIBS_PASSWORD -e GITHUB_TOKEN -e TARGET -e MATRIX_ARCH \
        "$IMAGE" bash .github/actions/shared-libraries/run.sh "$command"
fi

baseline() {
    local output=$1 host=${2:-1}
    local args=(--type "$TYPE" --arch "$MATRIX_ARCH" --output "$output")
    if [[ "$TYPE" == rpm ]]; then
        args+=(--host "$RPM_HOST" --remote-directory "$RPM_DIRECTORY")
    else
        local url=https://git.henderkes.com
        [[ "$host" == 2 ]] && url=https://git.static-php.dev
        args+=(--host "$url" --owner libs --password-env FORGEJO_LIBS_PASSWORD --consumer-password-env FORGEJO_PASSWORD)
    fi
    python3 bin/shared-library-plan baseline "${args[@]}"
}

case "$command" in
    fingerprint)
        if [[ "$TYPE" == apk ]]; then
            toolchain=$(docker run --rm "$IMAGE" bash -lc 'gcc -v 2>&1; ld --version; cat /etc/os-release')
            toolchain+=$(docker image inspect "$IMAGE" --format '{{.Id}}')
        else
            toolchain=$(gcc -v 2>&1; ld --version; cat /etc/os-release)
            toolchain+="$IMAGE"
        fi
        python3 .github/actions/shared-libraries/workflow.py fingerprint --target "$TYPE-$TARGET" --toolchain "$toolchain"
        ;;
    libs)
        git config --global --add safe.directory "$PWD"
        composer install --no-interaction --prefer-dist --no-progress
        php bin/spp build --phpv=8.4 --type="$TYPE" --libs-only --debuginfo
        test -s buildroot/shared-libraries.json
        ;;
    classify)
        git config --global --add safe.directory "$PWD"
        composer install --no-interaction --prefer-dist --no-progress
        mkdir -p stage
        baseline stage/baseline
        args=(--type "$TYPE" --manifest buildroot/shared-libraries.json --root buildroot \
            --baseline stage/baseline --mode "$MODE" \
            --libraries "$LIBRARIES" --php-versions "$PHP_VERSIONS" --packages "$PACKAGES" \
            --output stage/shared-library-plan.json \
            --manifest-output buildroot/shared-libraries.json)
        if [[ -f downloads/build-plan.json ]]; then
            args+=(--request downloads/build-plan.json)
        fi
        if [[ "$TYPE" != rpm ]]; then
            baseline stage/baseline-2 2
            args+=(--peer-baseline stage/baseline-2)
        fi
        if [[ "$MODE" == full ]] || python3 -c 'import json,sys; p=json.load(open("downloads/build-plan.json")); sys.exit(not all(p.get("requests",{}).get(s,{}).get("full") for s in ["8.2","8.3","8.4","8.5","8.6"]))'; then
            args+=(--bootstrap)
        fi
        python3 bin/shared-library-plan prepare "${args[@]}"
        selected=$(python3 -c 'import json; print(",".join(json.load(open("stage/shared-library-plan.json"))["libraries"]))')
        if [[ -n "$selected" ]]; then
            flags=(--libs-only --type="$TYPE" --libraries="$selected" --debuginfo)
            if [[ -n "$ITERATION" ]]; then flags+=(--iteration="$ITERATION"); elif [[ "$BUMP" == true ]]; then flags+=(--bump); fi
            php bin/spp package "${flags[@]}"
            php bin/spp test --phpv=8.4 --prefix=-zts --type="$TYPE"
            mkdir -p stage/libraries
            cp dist/"$TYPE"/*."$TYPE" stage/libraries/
            cp dist/"$TYPE"/shared-library-consumers.json stage/libraries/
        fi
        cp buildroot/shared-libraries.json stage/shared-libraries.json
        if [[ -d buildroot/usr/share/php-zts/libraries/abi ]]; then
            mkdir -p stage/abi
            cp -a buildroot/usr/share/php-zts/libraries/abi/. stage/abi/
        fi
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
        flags=(--phpv="$PHPV" --prefix=-zts --type="$TYPE" --debuginfo)
        [[ -n "$PACKAGES" ]] && flags+=(--packages="$PACKAGES")
        if [[ -n "$ITERATION" ]]; then flags+=(--iteration="$ITERATION"); elif [[ "$BUMP" == true ]]; then flags+=(--bump); fi
        php tests/package-versions.php --type="$TYPE"
        php tests/package-iterations.php
        python3 tests/upgrade-guards.py
        php bin/spp all "${flags[@]}"
        test_flags=(--phpv="$PHPV" --prefix=-zts --type="$TYPE")
        [[ -n "$PACKAGES" ]] && test_flags+=(--packages="$PACKAGES")
        php bin/spp test "${test_flags[@]}"
        python3 .github/actions/shared-libraries/workflow.py receipt --kind consumer --php-stream "$PHPV" \
            --plan stage/shared-library-plan.json --source-run-id "$SOURCE_RUN_ID" --image "$IMAGE" \
            --output "dist/$TYPE/shared-library-build-receipt.json"
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
        python3 bin/shared-library-plan cohort --plan "$libs/shared-library-plan.json" --artifacts cohort --type "$TYPE" "${baseline_args[@]}" --output cohort-result.json
        if [[ "$TYPE" == rpm ]]; then
            python3 bin/check-rpm-upgrades --host "$RPM_HOST" --remote-directory "$RPM_DIRECTORY" --files-from rpm-upload-files "dist/$TYPE"
        else
            for host in 1 2; do
                baseline "current-baseline-$host" "$host"
                baseline_args=()
                [[ -f "current-baseline-$host/shared-libraries.json" ]] && baseline_args+=(--baseline-manifest "current-baseline-$host/shared-libraries.json")
                python3 bin/shared-library-plan cohort --plan "$libs/shared-library-plan.json" --artifacts cohort --type "$TYPE" "${baseline_args[@]}" --output "cohort-result-$host.json"
                (cd "dist/$TYPE"; python3 ../../.github/actions/shared-libraries/publish-forgejo.py check "$TYPE" "$host")
            done
        fi
        ;;
    *) echo "Unknown shared-library workflow command: $command" >&2; exit 1 ;;
esac
