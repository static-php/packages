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
        -e BUILDER_HOST_UID="$(id -u)" -e BUILDER_HOST_GID="$(id -g)" \
        -e TYPE -e MODE -e LIBRARIES -e PHP_VERSIONS -e PACKAGES -e PHPV -e ITERATION -e BUMP -e PUBLISH \
        -e COMPILE -e COMPILE_PACKAGES -e ABI_REBUILD_LIBRARIES -e LIBRARY_ONLY -e EXPORT_PAYLOAD_CACHE \
        -e IMAGE -e SOURCE_RUN_ID -e CACHE_KEY -e ALMA \
        -e FORGEJO_PASSWORD -e GITHUB_TOKEN -e TARGET -e MATRIX_ARCH \
        "$IMAGE" bash .github/actions/shared-libraries/run.sh "$command"
fi

if [[ "$TYPE" == apk ]]; then
    if [[ -n "${BUILDER_HOST_UID:-}" && -n "${BUILDER_HOST_GID:-}" ]]; then
        builder_workspace=$PWD
        restore_workspace_owner() {
            local status=$?
            trap - EXIT
            chown -R "$BUILDER_HOST_UID:$BUILDER_HOST_GID" "$builder_workspace" || exit $?
            if [[ -n "${COMPOSER_CACHE_DIR:-}" && -d "$COMPOSER_CACHE_DIR" ]]; then
                chown -R "$BUILDER_HOST_UID:$BUILDER_HOST_GID" "$COMPOSER_CACHE_DIR" || exit $?
            fi
            exit "$status"
        }
        trap restore_workspace_owner EXIT
    fi
    # The host extracts source repositories before the root-run Alpine builder mounts them.
    for repository in "$PWD"/downloads/*/.git; do
        [[ -e "$repository" ]] || continue
        git config --global --add safe.directory "${repository%/.git}"
    done
fi

if ! python3 -c 'import sys; sys.exit(sys.version_info < (3, 9))'; then
    python=$(command -v python3.9)
    python_tools=$(mktemp -d "${TMPDIR:-/tmp}/spp-python-XXXXXXXX")
    ln -s "$python" "$python_tools/python3"
    export PATH="$python_tools:$PATH"
    trap 'rm -f "$python_tools/python3"; rmdir "$python_tools"' EXIT
fi

case "$command" in
    native-inventories)
        python3 .github/actions/shared-libraries/glibc.py native-inventories
        ;;
    classify|glibc-classify|glibc-record|glibc-payload|glibc-package|glibc-inline-package)
        git config --global --add safe.directory "$PWD"
        composer install --no-interaction --prefer-dist --no-progress
        python3 .github/actions/shared-libraries/glibc.py "${command#glibc-}"
        ;;
    fingerprint)
        git config --global --add safe.directory "$PWD"
        composer install --no-interaction --prefer-dist --no-progress >&2
        php bin/spp --version >/dev/null
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
    payload-key)
        composer install --no-interaction --prefer-dist --no-progress >&2
        php bin/spp --version >/dev/null
        python3 .github/actions/shared-libraries/payload-cache.py key --root . --stream "$PHPV"             --manifest buildroot/shared-libraries.json --compatibility stage/library-compatibility
        ;;
    prepare-runtime)
        python3 .github/actions/shared-libraries/runtime-checks.py --type "$TYPE" --payload "build/$PHPV"             --candidates "dist/$TYPE" --baseline-dir stage/published-baseline --output stage/runtime-libraries
        ;;
    freeze-baseline)
        baseline_stage=stage
        if [[ "$TYPE" != apk ]]; then baseline_stage="stage/$TYPE"; fi
        baseline_args=(--packages "${PACKAGES:-}")
        if [[ "${LIBRARY_ONLY:-}" == true ]]; then baseline_args+=(--library-plan "$baseline_stage/shared-library-plan.json"); fi
        python3 bin/freeze-migration-baseline.py --type "$TYPE" --php "$PHPV" --arch "$MATRIX_ARCH" --profile all --output stage/published-baseline \
            --inventory "$baseline_stage/native-inventories/$PHPV.json" "${baseline_args[@]}"
        ;;
    upload-1|upload-2)
        host=${command##*-}
        cd "dist/$TYPE"
        python3 ../../.github/actions/shared-libraries/publish-forgejo.py upload "$TYPE" "$host"
        ;;
    record)
        jq -n --arg type "$TYPE" --arg arch "$MATRIX_ARCH" --arg alma "$ALMA" --arg target "$TARGET" --arg image "$IMAGE" --arg source "$SOURCE_RUN_ID" \
            '{type: $type, arch: $arch, alma: $alma, target: $target, image: $image, source_run_id: $source}' > stage/target.json
        if [[ -f stage/payload-provenance.json ]]; then
            python3 -c 'import json,pathlib; p=pathlib.Path("stage/target.json"); t=json.loads(p.read_text()); t["payload_stream"]=json.load(open("stage/payload-provenance.json"))["php_stream"]; p.write_text(json.dumps(t))'
        fi
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
        if [[ -d stage/libraries ]]; then find stage/libraries -maxdepth 1 -name "*.$TYPE" -exec cp -a {} "dist/$TYPE/" \;; fi
        if [[ "${LIBRARY_ONLY:-}" == true ]]; then exit 0; fi
        flags=(--phpv="$PHPV" --prefix=-zts --type="$TYPE" --debuginfo)
        [[ -n "$PACKAGES" ]] && flags+=(--packages="$PACKAGES")
        if [[ -n "$ITERATION" ]]; then flags+=(--iteration="$ITERATION"); elif [[ "$BUMP" == true ]]; then flags+=(--bump); fi
        python3 .github/actions/shared-libraries/glibc.py payload
        if [[ "${EXPORT_PAYLOAD_CACHE:-false}" == true && -f stage/payload-cache-key ]]; then
            source_run=$(python3 -c 'import json; print(json.load(open("stage/payload-provenance.json"))["compiled_source_run_id"])')
            python3 .github/actions/shared-libraries/payload-cache.py export --root . --payload "build/$PHPV"                 --compatibility stage/library-compatibility --key "$(cat stage/payload-cache-key)"                 --seed-key "$(cat stage/payload-cache-seed-key)" --source-run-id "$source_run" --output php-payload-cache-export.tar.zst
            mv php-payload-cache-export.tar.zst php-payload-cache-ready.tar.zst
        fi
        export SPP_BASELINE_MANIFESTS
        SPP_BASELINE_MANIFESTS=$(python3 -c 'import json,pathlib; print(json.dumps([str(p.resolve()) for pattern in ["baseline/shared-libraries.json", "published-baseline/baseline.json"] for p in pathlib.Path("stage").glob(pattern)]))')
        export SPP_SHARED_LIBRARY_PLAN=stage/shared-library-plan.json
        php bin/spp package "${flags[@]}"
        python3 .github/actions/shared-libraries/glibc.py test-dependencies
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
            (cd "dist/$TYPE"; python3 ../../.github/actions/shared-libraries/publish-forgejo.py check "$TYPE" 1)
        fi
        ;;
    *) echo "Unknown shared-library workflow command: $command" >&2; exit 1 ;;
esac
