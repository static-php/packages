#!/bin/bash
set -euo pipefail

repository=$(cd "$(dirname "$0")/.." && pwd)
temporary=$(mktemp -d)
trap 'rm -rf "$temporary"' EXIT
trap 'exit 1' HUP INT TERM
mkdir -p "$temporary/prefix/bin" "$temporary/prefix/share/licenses/gcc"
printf 'license fixture\n' > "$temporary/prefix/share/licenses/gcc/COPYING3"
printf 'exception fixture\n' > "$temporary/prefix/share/licenses/gcc/COPYING.RUNTIME"
cat > "$temporary/prefix/bin/gcc" <<'COMPILER'
#!/bin/sh
case $1 in
    -dumpfullversion) printf '16.2.0\n' ;;
    -print-file-name=libgcc_s.so.1|-print-file-name=libstdc++.so.6)
        printf '%s\n' "$GCC_RUNTIME_FIXTURE" ;;
    -print-file-name=libstdc++.so)
        printf '%s\n' "${GCC_RUNTIME_SELECTOR_FIXTURE:-$GCC_RUNTIME_FIXTURE}" ;;
    -print-file-name=*) printf '%s\n' "${1#-print-file-name=}" ;;
    *) exit 1 ;;
esac
COMPILER
chmod +x "$temporary/prefix/bin/gcc"
ln -s gcc "$temporary/prefix/bin/g++"
printf 'int exported(int value) { return value + 1; }\n' > "$temporary/runtime.c"
"${CC:-gcc}" -shared -fPIC -g "$temporary/runtime.c" -o "$temporary/runtime.so"
cp "$temporary/runtime.so" "$temporary/stripped.so"
strip --strip-debug "$temporary/stripped.so"

check() {
    SPP_GCC_PREFIX="$temporary/prefix" GCC_RUNTIME_FIXTURE="$1" \
        bash "$repository/bin/build-gcc-runtime" --check
}

check "$temporary/runtime.so"
check "$temporary/stripped.so"
mkdir -p "$temporary/prefix/lib64"
cp "$temporary/stripped.so" "$temporary/prefix/lib64/libstdc++.so.6"
printf 'INPUT(libstdc++.a)\n' > "$temporary/prefix/lib64/libstdc++.so"
if GCC_RUNTIME_SELECTOR_FIXTURE="$temporary/prefix/lib64/libstdc++.so" \
    check "$temporary/prefix/lib64/libstdc++.so.6" > "$temporary/check.log" 2>&1; then
    printf 'Incorrectly accepted static C++ development selector\n' >&2
    exit 1
fi
grep -q 'development selector must resolve to shared' "$temporary/check.log"
grep -qx 'INPUT(libstdc++.a)' "$temporary/prefix/lib64/libstdc++.so"
before=$(sha256sum "$temporary/prefix/lib64/libstdc++.so.6")
mkdir "$temporary/no-download"
printf '#!/bin/sh\nprintf "Unexpected GCC source download\\n" >&2\nexit 97\n' > "$temporary/no-download/curl"
chmod +x "$temporary/no-download/curl"
PATH="$temporary/no-download:$PATH" SPP_GCC_PREFIX="$temporary/prefix" \
    GCC_RUNTIME_FIXTURE="$temporary/prefix/lib64/libstdc++.so.6" \
    GCC_RUNTIME_SELECTOR_FIXTURE="$temporary/prefix/lib64/libstdc++.so" \
    bash "$repository/bin/build-gcc-runtime"
[[ $(readlink "$temporary/prefix/lib64/libstdc++.so") == libstdc++.so.6 ]]
[[ $(sha256sum "$temporary/prefix/lib64/libstdc++.so.6") == "$before" ]]
rm "$temporary/prefix/share/licenses/gcc/COPYING.RUNTIME"
if check "$temporary/runtime.so" > "$temporary/check.log" 2>&1; then
    printf 'Incorrectly accepted missing runtime license\n' >&2
    exit 1
fi
printf 'GCC shared selector and license validation passed.\n'
