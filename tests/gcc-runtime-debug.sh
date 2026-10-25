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
    -print-file-name=libatomic.so.1)
        printf '%s\n' "${GCC_RUNTIME_OPTIONAL_FIXTURE:-libatomic.so.1}" ;;
    -print-file-name=*) printf '%s\n' "${1#-print-file-name=}" ;;
    *) exit 1 ;;
esac
COMPILER
chmod +x "$temporary/prefix/bin/gcc"
ln -s gcc "$temporary/prefix/bin/g++"
printf 'int exported(int value) { return value + 1; }\n' > "$temporary/runtime.c"
"${CC:-gcc}" -shared -fPIC -g -gdwarf-4 "$temporary/runtime.c" -o "$temporary/dwarf4.so"
"${CC:-gcc}" -shared -fPIC -g -gdwarf-5 "$temporary/runtime.c" -o "$temporary/dwarf5.so"
cp "$temporary/dwarf4.so" "$temporary/stripped.so"
strip --strip-debug "$temporary/stripped.so"

check() {
    SPP_GCC_PREFIX="$temporary/prefix" GCC_RUNTIME_FIXTURE="$1" \
        GCC_RUNTIME_OPTIONAL_FIXTURE="${2-}" bash "$repository/bin/build-gcc-runtime" --check
}

check "$temporary/dwarf4.so"
check "$temporary/dwarf4.so" "$temporary/dwarf4.so"
for invalid in dwarf5 stripped; do
    if check "$temporary/$invalid.so" > "$temporary/check.log" 2>&1; then
        printf 'Incorrectly accepted %s runtime\n' "$invalid" >&2
        exit 1
    fi
    grep -q "Invalid GCC runtime DWARF4: $temporary/$invalid.so" "$temporary/check.log"
done
if check "$temporary/dwarf4.so" "$temporary/dwarf5.so" > "$temporary/check.log" 2>&1; then
    printf 'Incorrectly accepted DWARF5 optional runtime\n' >&2
    exit 1
fi
mkdir -p "$temporary/prefix/lib64"
cp "$temporary/dwarf4.so" "$temporary/prefix/lib64/libstdc++.so.6"
printf 'INPUT(libstdc++.a)\n' > "$temporary/prefix/lib64/libstdc++.so"
if GCC_RUNTIME_SELECTOR_FIXTURE="$temporary/prefix/lib64/libstdc++.so" \
    check "$temporary/prefix/lib64/libstdc++.so.6" > "$temporary/check.log" 2>&1; then
    printf 'Incorrectly accepted static C++ development selector\n' >&2
    exit 1
fi
grep -q 'development selector must resolve to shared' "$temporary/check.log"
grep -qx 'INPUT(libstdc++.a)' "$temporary/prefix/lib64/libstdc++.so"
before=$(sha256sum "$temporary/prefix/lib64/libstdc++.so.6")
SPP_GCC_PREFIX="$temporary/prefix" \
    GCC_RUNTIME_FIXTURE="$temporary/prefix/lib64/libstdc++.so.6" \
    GCC_RUNTIME_SELECTOR_FIXTURE="$temporary/prefix/lib64/libstdc++.so" \
    bash "$repository/bin/build-gcc-runtime"
[[ $(readlink "$temporary/prefix/lib64/libstdc++.so") == libstdc++.so.6 ]]
[[ $(sha256sum "$temporary/prefix/lib64/libstdc++.so.6") == "$before" ]]
if [[ -f /etc/alpine-release ]]; then
    readelf --debug-dump=info "$temporary/dwarf4.so" | awk '
        /^[[:space:]]*Version:/ { versions[$2]++ }
        END { exit !(versions[4] && versions[5]) }
    '
    printf 'Mixed musl startup assembly DWARF5 and runtime C DWARF4 accepted.\n'
fi
printf 'GCC shared selector and runtime DWARF validation passed.\n'
