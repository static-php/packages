#!/usr/bin/env python3
"""Track C++ link inputs only for libraries that actually use that runtime."""

import argparse
import hashlib
import json
import os
import re
import subprocess
import zlib
from pathlib import Path


RUNTIME_MARKERS = {
    "__cxa_allocate_exception", "__cxa_free_exception", "__cxa_throw",
    "__cxa_pure_virtual", "__gxx_personality_v0", "_Znwm", "_Znam",
    "_ZdlPv", "_ZdaPv", "_ZSt17__throw_bad_allocv",
    "_ZNSt8ios_base4InitC1Ev", "_ZNSt8ios_base4InitC2Ev",
    "_ZNSt8ios_base4InitD1Ev", "_ZSt4cout", "_ZSt4cerr",
}


def output(arguments):
    return subprocess.check_output(arguments, stderr=subprocess.STDOUT,
                                   env=dict(os.environ, LC_ALL="C"),
                                   universal_newlines=True).strip()


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def file_digest(path):
    result = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def compiler_file(compiler, name):
    path = Path(output([compiler, "-print-file-name=" + name]))
    if not path.is_file():
        raise RuntimeError("Compiler cannot locate " + name)
    return path


def cxx_header_inputs(compiler):
    directories = []
    searching = False
    for line in output([compiler, "-E", "-v", "-x", "c++", "/dev/null"]).splitlines():
        if line == "#include <...> search starts here:":
            searching = True
        elif line == "End of search list.":
            searching = False
        elif searching and "/c++/" in line:
            path = Path(line.strip()).resolve()
            if path.is_dir() and not any(path == parent or parent in path.parents for parent in directories):
                directories.append(path)
    result = hashlib.sha256()
    count = 0
    for index, directory in enumerate(directories):
        for path in sorted(directory.rglob("*")):
            if path.is_file():
                result.update((str(index) + ":" + str(path.relative_to(directory))).encode() + b"\0")
                result.update(bytes.fromhex(file_digest(path)))
                count += 1
    if not count:
        raise RuntimeError("Compiler C++ header search directories are missing")
    return {"sha256": result.hexdigest(), "files": count,
            "directories": [str(path) for path in directories]}


def compiler_runtime_inputs(compiler="g++"):
    selector = compiler_file(compiler, "libstdc++.so")
    runtime = compiler_file(compiler, "libstdc++.so.6")
    actual = {"selector_path": str(selector), "selector_target": str(selector.resolve()),
              "selector_sha256": file_digest(selector), "runtime_path": str(runtime),
              "runtime_sha256": file_digest(runtime)}
    runtime_symbols = set()
    for line in output(["readelf", "--wide", "--dyn-syms", str(runtime)]).splitlines():
        fields = line.split()
        if (len(fields) >= 8 and fields[0].rstrip(":").isdigit()
                and fields[4] == "GLOBAL" and fields[6] != "UND"):
            name = fields[7].split("@", 1)[0]
            if name.startswith(("_Z", "__cxa_", "__gxx_")):
                runtime_symbols.add(name)
    actual["runtime_symbols"] = sorted(runtime_symbols)
    with selector.open("rb") as source:
        header = source.read(4)
    if header == b"\x7fELF":
        dynamic = output(["readelf", "--wide", "--dynamic", str(selector)])
        names = re.findall(r"\(SONAME\).*?\[([^\]]+)\]", dynamic)
        if names != ["libstdc++.so.6"]:
            raise RuntimeError("C++ selector does not identify libstdc++.so.6")
        semantic = {"mode": "shared", "soname": names[0]}
    else:
        script = selector.read_text()
        if "libstdc++.a" in script:
            archive = compiler_file(compiler, "libstdc++.a")
            semantic = {"mode": "static", "archive_sha256": file_digest(archive),
                        "selector_script": " ".join(script.split())}
            actual["archive_path"] = str(archive)
        elif "libstdc++.so.6" in script:
            semantic = {"mode": "shared", "soname": "libstdc++.so.6"}
            actual["selector_script"] = script
        else:
            raise RuntimeError("Unrecognized C++ development linker selector")
    headers = cxx_header_inputs(compiler)
    semantic["headers_sha256"] = headers["sha256"]
    actual["headers"] = headers
    # Only a static archive contributes its implementation to the consumer itself.
    return {"schema": 1, "compiler": output([compiler, "-dumpfullversion"]),
            "target": output([compiler, "-dumpmachine"]), "semantic": semantic,
            "fingerprint": digest(semantic), "evidence": actual}


def matching_debug(path, debug):
    first = re.search(r"Build ID:\s*(\S+)", output(["readelf", "--notes", str(path)]))
    second = re.search(r"Build ID:\s*(\S+)", output(["readelf", "--notes", str(debug)]))
    if first is not None and second is not None:
        return first.group(1) == second.group(1)
    data = bytearray()
    for line in output(["readelf", "--wide", "--hex-dump=.gnu_debuglink", str(path)]).splitlines():
        fields = line.split(None, 1)
        if len(fields) != 2 or not re.fullmatch(r"0x[0-9a-fA-F]+", fields[0]):
            continue
        for word in fields[1][:35].split():
            if not re.fullmatch(r"[0-9a-fA-F]{8}", word):
                return False
            data.extend(bytes.fromhex(word))
    end = data.find(b"\0")
    if end < 0:
        return False
    offset = (end + 4) & ~3
    if len(data) != offset + 4:
        return False
    with path.open("rb") as source:
        endian = "little" if source.read(6)[5] == 1 else "big"
    expected = int.from_bytes(data[offset:], endian)
    actual = 0
    with debug.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            actual = zlib.crc32(block, actual)
    return actual == expected


def inspect_elf(path, debug=None, runtime_symbols=()):
    symbols = output(["readelf", "--wide", "--symbols", str(path)])
    debug_companion = None
    if "Symbol table '.symtab'" not in symbols and debug is not None:
        if matching_debug(path, debug):
            symbols += "\n" + output(["readelf", "--wide", "--symbols", str(debug)])
            debug_companion = str(debug)
    markers = RUNTIME_MARKERS | set(runtime_symbols)
    defined = set()
    undefined = set()
    cxx_symbols = False
    for line in symbols.splitlines():
        fields = line.split()
        if len(fields) < 8 or not fields[0].rstrip(":").isdigit():
            continue
        name = fields[7].split("@", 1)[0]
        cxx_symbols |= name.startswith("_Z")
        if name in markers:
            (undefined if fields[6] == "UND" else defined).add(name)
    dynamic = output(["readelf", "--wide", "--dynamic", str(path)])
    needed = re.findall(r"\(NEEDED\).*?\[([^\]]+)\]", dynamic)
    cpp_language = False
    if not cxx_symbols and not defined and not undefined and "libstdc++.so.6" not in needed:
        language_source = debug if debug_companion is not None else path
        language = output(["readelf", "--debug-dump=info", "--dwarf-depth=1", str(language_source)])
        cpp_language = re.search(r"DW_AT_language[^\n]*C(?:\+\+|_plus_plus)", language) is not None
    return {"needed": needed, "defined_runtime_markers": sorted(defined),
            "undefined_runtime_markers": sorted(undefined),
            "has_symbol_table": "Symbol table '.symtab'" in symbols,
            "has_cxx_symbols": cxx_symbols, "has_cxx_language": cpp_language,
            "debug_companion": debug_companion}


def debug_file(root, relative, recorded):
    candidates = []
    if relative in recorded:
        candidates.append(root / recorded[relative])
    candidates.extend([root / "debug" / (relative + ".debug"),
                       root / "debug" / (Path(relative).name + ".debug"),
                       root / "payload/debug" / Path(relative).parent / (Path(relative).name + ".debug")])
    for path in candidates:
        if path.is_file() and not path.is_symlink():
            try:
                path.resolve().relative_to(root)
            except ValueError:
                raise RuntimeError("Escaping debug companion: " + str(path))
            return path
    return None


def scan_sdk(root, inputs, manifest=None):
    root = Path(root).resolve()
    manifest = manifest or json.loads((root / "shared-libraries.json").read_text())
    if manifest.get("schema") != 1 or not isinstance(manifest.get("libraries"), dict):
        raise RuntimeError("Invalid shared-library ownership manifest")
    semantic = inputs.get("semantic")
    if (inputs.get("schema") != 1 or not isinstance(semantic, dict)
            or semantic.get("mode") not in ("shared", "static")
            or inputs.get("fingerprint") != digest(semantic)):
        raise RuntimeError("Invalid compiler runtime input record")
    result = {}
    uncertain = []
    for name, entry in sorted(manifest["libraries"].items()):
        if name == "gcc-runtime":
            continue
        evidence = []
        for relative in entry.get("files", []):
            path = root / relative
            try:
                path.resolve(strict=True).relative_to(root)
            except (OSError, ValueError):
                raise RuntimeError("Missing or escaping library output: " + relative)
            if path.is_symlink() or not path.is_file():
                continue
            with path.open("rb") as source:
                if source.read(4) != b"\x7fELF":
                    continue
            companion = debug_file(root, relative, manifest.get("debug_files", {}))
            details = inspect_elf(path, companion, inputs.get("evidence", {}).get("runtime_symbols", []))
            evidence.append(dict(details, file=relative))
        embedded = any(item["defined_runtime_markers"] for item in evidence)
        uses_runtime = embedded or any(item["undefined_runtime_markers"] or
                                      "libstdc++.so.6" in item["needed"] for item in evidence)
        uses_headers = uses_runtime or any(item["has_cxx_symbols"] or item["has_cxx_language"] for item in evidence)
        unresolved = any(not item["has_symbol_table"] and
                         not item["defined_runtime_markers"] and
                         not item["undefined_runtime_markers"] and
                         "libstdc++.so.6" not in item["needed"] for item in evidence)
        if unresolved and not uses_runtime:
            uncertain.append(name)
        relevant = inputs["semantic"] if uses_runtime else (
            {"headers_sha256": inputs["semantic"].get("headers_sha256")} if uses_headers else None)
        result[name] = {"uses_cxx_runtime": uses_runtime,
                        "uses_cxx_headers": uses_headers,
                        "embedded_cxx_runtime": embedded,
                        "compiler_runtime_inputs": relevant,
                        "fingerprint": digest({"schema": 1, "cxx_runtime": relevant}),
                        "files": evidence}
    return {"schema": 1, "compiler_runtime": inputs,
            "libraries": result,
            "affected_static_libraries": [name for name, value in result.items()
                                          if value["embedded_cxx_runtime"]],
            "must_rebuild": [name for name, value in result.items()
                             if value["embedded_cxx_runtime"] and inputs["semantic"]["mode"] == "shared"],
            "uncertain_stripped_libraries": uncertain}


def scan_payload(root, inputs, files=None):
    root = Path(root)
    allowed = None if files is None else set(files)
    groups = {}
    for directory in ("bin", "lib", "modules"):
        parent = root / directory
        if not parent.is_dir():
            continue
        for path in sorted(parent.iterdir()):
            if allowed is not None and str(path.relative_to(root)) not in allowed:
                continue
            if path.is_symlink() or not path.is_file():
                continue
            with path.open("rb") as source:
                if source.read(4) != b"\x7fELF":
                    continue
            if directory == "bin" and path.name == "frankenphp":
                component = "frankenphp"
            elif (directory == "bin" and path.name.startswith("php")) or (
                    directory == "lib" and path.name.startswith("libphp")):
                component = "core"
            elif directory == "modules" and path.name.endswith(".so"):
                component = re.sub(r"-(?:zts|nts)(?:-\d+)?$", "", path.stem)
            else:
                continue
            groups.setdefault(component, {"files": []})["files"].append(str(path.relative_to(root)))
    metadata = root / "build-payload.json"
    prepared = json.loads(metadata.read_text()).get("prepared", {}) if metadata.is_file() else {}
    debug_files = {relative: value["debug"]["path"] for relative, value in prepared.items()
                   if isinstance(value, dict) and isinstance(value.get("debug"), dict)
                   and isinstance(value["debug"].get("path"), str)}
    result = scan_sdk(root, inputs, {"schema": 1, "libraries": groups, "debug_files": debug_files})
    return {"schema": 1, "compiler_runtime": inputs,
            "consumers": result["libraries"],
            "must_rebuild": result["must_rebuild"],
            "uncertain_stripped_consumers": result["uncertain_stripped_libraries"]}


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command")
    sub.required = True
    collect = sub.add_parser("toolchain")
    collect.add_argument("--compiler", default="g++")
    collect.add_argument("--require-shared", action="store_true")
    collect.add_argument("--output", type=Path)
    scan = sub.add_parser("scan")
    scan.add_argument("--root", type=Path, required=True)
    scan.add_argument("--inputs", type=Path)
    scan.add_argument("--compiler", default="g++")
    scan.add_argument("--output", type=Path)
    payload = sub.add_parser("scan-payload")
    payload.add_argument("--root", type=Path, required=True)
    payload.add_argument("--inputs", type=Path)
    payload.add_argument("--compiler", default="g++")
    payload.add_argument("--files", type=Path)
    payload.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.command == "toolchain":
        value = compiler_runtime_inputs(args.compiler)
        if args.require_shared and value["semantic"]["mode"] != "shared":
            raise RuntimeError("Shared library builds require dynamic libstdc++ selection")
    else:
        inputs = json.loads(args.inputs.read_text()) if args.inputs else compiler_runtime_inputs(args.compiler)
        value = scan_payload(args.root, inputs, json.loads(args.files.read_text()) if args.files else None) if args.command == "scan-payload" else scan_sdk(args.root, inputs)
    content = json.dumps(value, sort_keys=True, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(content)
    else:
        print(content, end="")


if __name__ == "__main__":
    main()
