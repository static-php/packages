#!/usr/bin/env python3
"""Build path-specific OpenSSL overlays without rebuilding common consumers."""

import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import runpy
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET


PROFILES = {"rpm": "/etc/pki/tls", "deb": "/etc/ssl"}
MANIFEST = "shared-libraries.json"
STATE = "shared-library-build-inputs.json"
BUILD_RECIPE_SCHEMA = 2


def read(path):
    return json.loads(Path(path).read_text())


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps(value, sort_keys=True, indent=2) + "\n"
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(body)
    temporary.replace(path)


def relative(value):
    if not isinstance(value, str) or not value or Path(value).is_absolute() or ".." in Path(value).parts or value == ".":
        raise RuntimeError("Unsafe OpenSSL overlay path: " + repr(value))
    return value


def rooted(root, value):
    path = Path(root) / relative(value)
    try:
        path.resolve().relative_to(Path(root).resolve())
    except ValueError:
        raise RuntimeError("OpenSSL overlay path escapes root: " + value) from None
    return path


def file_hash(path):
    if path.is_symlink():
        return hashlib.sha256(("symlink:" + os.readlink(path)).encode()).hexdigest()
    result = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def file_record(path):
    record = {"sha256": file_hash(path), "mode": path.lstat().st_mode & 0o777}
    if path.is_symlink():
        record["symlink"] = os.readlink(path)
    return record


def copy_file(source, target):
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_symlink() or target.is_file():
        target.unlink()
    if source.is_symlink():
        target.symlink_to(os.readlink(source))
    else:
        shutil.copy2(source, target)


def mapping(value, field):
    if value == []:
        return {}
    if not isinstance(value, dict):
        raise RuntimeError("OpenSSL metadata field must be a mapping: " + field)
    return value


def runtime_assets(entry):
    value = entry.get("runtime_assets", {})
    if isinstance(value, list):
        result = {}
        for item in value:
            if not isinstance(item, dict) or not isinstance(item.get("source"), str) or not isinstance(item.get("destination"), str):
                raise RuntimeError("Invalid structured runtime asset")
            source = relative(item["source"])
            if source in result and result[source] != item["destination"]:
                raise RuntimeError("Conflicting structured runtime asset: " + source)
            result[source] = item["destination"]
        return result
    return mapping(value, "runtime_assets")


def runtime_paths(entry):
    files = entry.get("files", [])
    result = set(files if isinstance(files, list) else files.keys())
    result.update(runtime_assets(entry).keys())
    for item in result:
        relative(item)
    return result


def entry_paths(entry):
    result = runtime_paths(entry)
    result.update(relative(name) for name in mapping(entry.get("abi_dump", {}), "abi_dump").values())
    return result


def openssl_entry(root):
    manifest = read(Path(root) / MANIFEST)
    if manifest.get("schema") != 1 or manifest.get("linkage") != "shared" or manifest.get("libc") != "glibc":
        raise RuntimeError("OpenSSL variants require a glibc shared-library SDK")
    if manifest.get("pending_rebuilds"):
        raise RuntimeError("Finish pending library ABI rebuilds before producing OpenSSL variants")
    entry = manifest.get("libraries", {}).get("openssl")
    if not isinstance(entry, dict) or not entry.get("source_hash") or not entry.get("elf"):
        raise RuntimeError("The common SDK has no recorded OpenSSL build")
    for name in entry_paths(entry):
        path = rooted(root, name)
        if not path.exists():
            raise RuntimeError("Missing OpenSSL SDK file: " + name)
        expected = entry.get("file_hashes", {}).get(name)
        if expected and expected != file_hash(path):
            raise RuntimeError("Common OpenSSL SDK checksum mismatch: " + name)
    return manifest, entry


def header_digest(root):
    directory = Path(root) / "include/openssl"
    if not directory.is_dir():
        raise RuntimeError("OpenSSL development headers are missing")
    files = {str(path.relative_to(directory)): file_record(path)
             for path in sorted(directory.rglob("*")) if path.is_file() or path.is_symlink()}
    if not files:
        raise RuntimeError("OpenSSL development headers are empty")
    return digest(files)


def common_identity(sdk, entry):
    state_path = sdk / STATE
    state = read(state_path) if state_path.is_file() else {}
    return digest({"source_hash": entry["source_hash"], "version": entry["version"],
                   "runtime_libdir": entry["runtime_libdir"], "headers": header_digest(sdk),
                   "compatibility": state.get("compatibility"),
                   "openssl_inputs": state.get("libraries", {}).get("openssl", {})})


def fingerprint(root, sdk, environment, entry, profile):
    state_path = sdk / STATE
    state = read(state_path) if state_path.is_file() else {}
    actual = state.get("libraries", {}).get("openssl", {})
    recipe = Path(root) / "vendor/crazywhalecc/static-php-cli/src/Package/Library/openssl.php"
    if not recipe.is_file():
        raise RuntimeError("Cannot identify the installed OpenSSL build recipe")
    values = {name: value for name, value in environment.get("environment", {}).items()
              if name not in ("OPENSSLDIR", "SPC_EXTRA_PHP_VARS")}
    return digest({"schema": 1, "source_hash": entry["source_hash"], "version": entry["version"],
                   "architecture": environment["architecture"], "environment": values,
                   "options": environment.get("options", {}), "implementation": environment.get("implementation", {}),
                   "compatibility": state.get("compatibility"), "recipe": actual.get("recipe_hash"),
                   "recipe_file": file_hash(recipe), "headers": header_digest(sdk),
                   "compiled_dependency_inputs": actual.get("dependency_signatures", {}),
                   "openssldir": PROFILES[profile], "build_recipe_schema": BUILD_RECIPE_SCHEMA})


def valid_overlay(directory, expected=None):
    try:
        receipt = read(directory / "overlay.json")
        if (receipt.get("schema") != 1 or receipt.get("library") != "openssl"
                or receipt.get("profile") not in PROFILES or receipt.get("openssldir") != PROFILES[receipt["profile"]]
                or expected is not None and receipt.get("input_fingerprint") != expected
                or receipt.get("abi", {}).get("types_verified") is not True):
            return None
        if not isinstance(receipt.get("files"), dict) or not receipt["files"]:
            return None
        for name, record in receipt["files"].items():
            if file_record(rooted(directory / "files", name)) != record:
                return None
        if not entry_paths(receipt["entry"]) <= receipt["files"].keys():
            return None
        return receipt
    except (OSError, ValueError, KeyError, RuntimeError):
        return None


def export_overlay(sdk, entry, destination, profile, key, abi, identity=None):
    with tempfile.TemporaryDirectory(prefix=".openssl-overlay-", dir=destination.parent) as temporary:
        stage = Path(temporary)
        files = set(entry_paths(entry))
        for name in list(files):
            debug = "debug/" + name + ".debug"
            if rooted(sdk, debug).is_file():
                files.add(debug)
        records = {}
        for name in sorted(files):
            source = rooted(sdk, name)
            copy_file(source, rooted(stage / "files", name))
            records[name] = file_record(source)
        exported = copy.deepcopy(entry)
        exported.pop("package_versions", None)
        receipt = {"schema": 1, "library": "openssl", "profile": profile,
                   "openssldir": PROFILES[profile], "input_fingerprint": key, "compiled_input_fingerprint": key,
                   "source_hash": entry["source_hash"], "version": entry["version"],
                   "runtime_libdir": entry["runtime_libdir"], "architecture": read(sdk / MANIFEST)["architecture"], "files": records,
                   "headers_sha256": header_digest(sdk), "common_identity": identity or common_identity(sdk, entry), "entry": exported, "abi": abi}
        write(stage / "overlay.json", receipt)
        stage.chmod(0o755)
        if destination.exists():
            shutil.rmtree(destination)
        stage.rename(destination)
        return receipt


def public_symbols(path):
    result = subprocess.check_output(["readelf", "--wide", "--dyn-syms", str(path)], text=True, env=dict(os.environ, LC_ALL="C"))
    symbols = []
    for line in result.splitlines():
        fields = line.split()
        if len(fields) >= 8 and fields[0].rstrip(":").isdigit() and fields[6] != "UND" and fields[4] in ("GLOBAL", "WEAK") and fields[5] in ("DEFAULT", "PROTECTED"):
            symbols.append([fields[3], fields[4], fields[5], fields[7]])
    return sorted(symbols)


def abi_inventory(sdk, entry, output):
    sonames = {}
    symbols = {}
    dumps = {}
    for name, elf in sorted(entry["elf"].items()):
        soname = elf.get("soname")
        if not soname:
            continue
        if soname in sonames and sonames[soname] != name:
            raise RuntimeError("Duplicate OpenSSL SONAME: " + soname)
        sonames[soname] = name
        symbols[soname] = public_symbols(rooted(sdk, name))
        dump = output / (soname + ".xml")
        dump.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["abidw", "--no-corpus-path", "--no-show-locs", "--debug-info-dir", str(sdk / "debug"),
                        "--debug-info-dir", str(sdk / "usr/lib/debug"), "--out-file", str(dump), str(rooted(sdk, name))], check=True)
        corpus = ET.parse(dump).getroot()
        if corpus.tag != "abi-corpus" or not any(list(item) for item in corpus.findall("abi-instr")):
            raise RuntimeError("OpenSSL ABI corpus has no type information: " + soname)
        dumps[soname] = dump
    if len(sonames) != 2 or not any(re.fullmatch(r"libcrypto\.so\.[0-9]+", name) for name in sonames) or not any(re.fullmatch(r"libssl\.so\.[0-9]+", name) for name in sonames):
        raise RuntimeError("Unexpected OpenSSL SONAME set: " + ",".join(sonames))
    return {"sonames": sorted(sonames), "symbols_sha256": digest(symbols), "types_verified": True}, dumps


def compare_abi(common, variant, common_dumps, variant_dumps):
    if common != variant:
        raise RuntimeError("OpenSSL profile changed public SONAMEs or symbols")
    for name in common_dumps:
        result = subprocess.run(["abidiff", "--no-default-suppression", str(common_dumps[name]), str(variant_dumps[name])], capture_output=True, text=True)
        if result.returncode:
            raise RuntimeError("OpenSSL profile changed public type ABI: " + name + "\n" + (result.stdout + result.stderr)[-5000:])


def compiled_plugin_directories(sdk, version, runtime):
    major = re.match(r'^v?(\d+)\.', version)
    if major is None:
        raise RuntimeError('Cannot determine OpenSSL plugin interface version: ' + version)
    probes = [("-m", "module", "ossl-modules")]
    if int(major[1]) < 4:
        probes.insert(0, ("-e", "engine", "engines-3"))
    directories = {}
    for flag, label, suffix in probes:
        text = subprocess.check_output([str(sdk / "bin/openssl"), "version", flag], text=True,
                                       env=dict(os.environ, LD_LIBRARY_PATH=str(sdk / "lib")))
        matches = re.findall(r'"([^"]+)"', text)
        if matches != [runtime + "/" + suffix]:
            raise RuntimeError("Original compiled OpenSSL plugin directory differs from its provenance: " + text.strip())
        directories[label] = matches[0]
    return directories


def probe_directory(sdk, expected):
    program = sdk / "bin/openssl"
    if not program.is_file():
        raise RuntimeError("OpenSSL build has no version probe binary")
    result = subprocess.check_output([str(program), "version", "-d"], text=True,
                                     env=dict(os.environ, LD_LIBRARY_PATH=str(sdk / "lib")))
    if result.strip() != 'OPENSSLDIR: "' + expected + '"':
        raise RuntimeError("OpenSSL has the wrong compiled configuration path: " + result.strip())


def copy_sdk(sdk, target, manifest):
    ownership = read(sdk / ".shared-library-ownership.json")
    wanted = {"openssl", "gcc-runtime"} & manifest["libraries"].keys()
    while True:
        expanded = wanted | {item for name in wanted for field in ("depends", "dlopen_dependencies", "asset_dependencies")
                             for item in manifest["libraries"][name].get(field, []) if item in manifest["libraries"]}
        if wanted == expanded:
            break
        wanted = expanded
    for name in wanted:
        paths = entry_paths(manifest["libraries"][name]) | set(ownership["libraries"][name]["files"])
        for relative_name in paths:
            copy_file(rooted(sdk, relative_name), rooted(target, relative_name))
    for directory in ["include/openssl", "lib/pkgconfig", "lib/cmake/OpenSSL"]:
        if (sdk / directory).is_dir():
            shutil.copytree(sdk / directory, target / directory, dirs_exist_ok=True, symlinks=True)
    for name in ["include/zlib.h", "include/zconf.h", "bin/pkg-config", "bin/pkgconf"]:
        if (sdk / name).exists():
            copy_file(sdk / name, target / name)
    if (sdk / ".build.json").is_file():
        cache = read(sdk / ".build.json")
        write(target / ".build.json", {name: record for name, record in cache.items() if name in wanted and name != "openssl"})
    for name in [MANIFEST, ".shared-library-ownership.json"]:
        data = read(sdk / name)
        data["libraries"] = {key: item for key, item in data.get("libraries", {}).items() if key in wanted}
        data.pop("pending_rebuilds", None)
        write(target / name, data)


def produce(root, sdk, environment, output, profile):
    manifest, _ = openssl_entry(sdk)
    temporary_root = output.parent
    temporary_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".openssl-build-", dir=temporary_root) as temporary:
        work = Path(temporary)
        target = work / "buildroot"
        target.mkdir()
        copy_sdk(sdk, target, manifest)
        downloads = work / "downloads"
        downloads.mkdir()
        cached = read(root / "downloads/.cache.json").get("openssl", {}).get("source", {})
        filename = cached.get("filename")
        if cached.get("cache_type") != "archive" or not isinstance(filename, str) or Path(filename).name != filename or cached.get("hash") != manifest["libraries"]["openssl"]["source_hash"]:
            raise RuntimeError("OpenSSL archive cache does not match the common SDK's pinned source")
        archive = root / "downloads" / filename
        source_hash = hashlib.sha1()
        with archive.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                source_hash.update(block)
        if source_hash.hexdigest() != cached["hash"]:
            raise RuntimeError("OpenSSL archive checksum does not match the common SDK")
        for path in (root / "downloads").iterdir():
            if path.name == ".cache.json":
                shutil.copy2(path, downloads / path.name)
            else:
                (downloads / path.name).symlink_to(path)
        env = {str(name): str(value) for name, value in environment["environment"].items()}
        env["OPENSSLDIR"] = PROFILES[profile]
        craft = {"php-version": "8.4", "extensions": [], "shared-extensions": [], "libs": ["openssl"], "sapi": [],
                 "build-options": {"with-shared-libs": True, "with-shared-libdir": manifest["runtime_libdir"], "with-suggests": False},
                 "craft-options": {"doctor": False, "download": False, "build": True}, "extra-env": env}
        write(work / "craft.json", craft)
        process_env = dict(os.environ, BUILD_ROOT_PATH=str(target), BUILD_INCLUDE_PATH=str(target / "include"),
                           BUILD_LIB_PATH=str(target / "lib"), BUILD_BIN_PATH=str(target / "bin"),
                           BUILD_MODULES_PATH=str(target / "modules"), SOURCE_PATH=str(work / "source"),
                           DOWNLOAD_PATH=str(downloads), SPC_LOGS_DIR=str(work / "logs"))
        binary = Path(os.environ.get("SPP_SPC_BINARY", str(root / "vendor/bin/spc"))).resolve()
        subprocess.run([str(binary), "craft", str(work / "craft.json"), "--libs-only"], cwd=root, env=process_env, check=True)
        actual, entry = openssl_entry(target)
        if entry["source_hash"] != manifest["libraries"]["openssl"]["source_hash"]:
            raise RuntimeError("Variant OpenSSL source drifted from the common SDK")
        probe_directory(target, PROFILES[profile])
        if header_digest(target) != header_digest(sdk):
            raise RuntimeError("OpenSSL profiles have different public headers")
        common_abi, common_dumps = abi_inventory(sdk, manifest["libraries"]["openssl"], work / "abi-common")
        variant_abi, variant_dumps = abi_inventory(target, entry, work / "abi-variant")
        compare_abi(common_abi, variant_abi, common_dumps, variant_dumps)
        # Reuse the common typed ABI metadata because the path-only profiles have identical ABI.
        for field in ("abi_epoch", "abi_capability", "abi_capabilities", "abi_dump", "abi_dump_hashes", "abi_empty"):
            if field in manifest["libraries"]["openssl"]:
                entry[field] = copy.deepcopy(manifest["libraries"]["openssl"][field])
        for name in mapping(entry.get("abi_dump", {}), "abi_dump").values():
            copy_file(rooted(sdk, name), rooted(target, name))
        return export_overlay(target, entry, output, profile, fingerprint(root, sdk, environment, manifest["libraries"]["openssl"], profile), variant_abi, common_identity(sdk, manifest["libraries"]["openssl"]))


def elf_identity(path, scratch):
    dynamic = subprocess.check_output(["readelf", "--wide", "--dynamic", str(path)], text=True, env=dict(os.environ, LC_ALL="C"))
    text = scratch / "text-section"
    subprocess.run(["objcopy", "--dump-section", ".text=" + str(text), str(path), os.devnull], check=True, capture_output=True)
    return {"text_sha256": file_hash(text), "needed": sorted(re.findall(r"\(NEEDED\).*?\[([^]]+)\]", dynamic)),
            "soname": re.findall(r"\(SONAME\).*?\[([^]]+)\]", dynamic),
            "symbols_sha256": digest(public_symbols(path))}


def relocated_environment(previous, current):
    previous, current = copy.deepcopy(previous), copy.deepcopy(current)
    for value in (previous, current):
        for field in ("type", "target"):
            value.pop(field, None)
        value.get("options", {}).pop("with-shared-libdir", None)
    if previous != current:
        raise RuntimeError("Profile relocation cannot change recorded compiler, recipe or build flags")


def relocate(root, sdk, environment, compiled_environment, source, output):
    manifest, common = openssl_entry(sdk)
    assert_el8(manifest)
    if source.resolve() == output.resolve():
        raise RuntimeError("Relocate a copy of the verified profiles; preserve the original cache")
    relocated_environment(compiled_environment, environment)
    if environment.get("options", {}).get("with-shared-libdir") != manifest["runtime_libdir"]:
        raise RuntimeError("Relocated environment does not match the adopted SDK")
    output.mkdir(parents=True, exist_ok=True)
    receipts = {}
    with tempfile.TemporaryDirectory(prefix=".openssl-relocate-", dir=output.parent) as temporary:
        work = Path(temporary)
        common_abi, common_dumps = abi_inventory(sdk, common, work / "abi-common")
        for profile in PROFILES:
            original = valid_overlay(source / profile)
            if original is None or original["profile"] != profile:
                raise RuntimeError("Original verified OpenSSL profile is missing: " + profile)
            source_index = read(source / "variants.json")
            if source_index.get("profiles", {}).get(profile, {}).get("receipt_sha256") != file_hash(source / profile / "overlay.json"):
                raise RuntimeError("Original OpenSSL profile index differs from its receipt")
            original_key = fingerprint(root, sdk, compiled_environment, common, profile)
            if original["input_fingerprint"] != original_key:
                raise RuntimeError("Original compiled environment does not reproduce the profile fingerprint")
            if original["source_hash"] != common["source_hash"] or original["version"] != common["version"] or original["headers_sha256"] != header_digest(sdk):
                raise RuntimeError("Original profile differs from the adopted SDK source or headers")
            target = work / profile
            target.mkdir()
            shutil.copytree(sdk / "include/openssl", target / "include/openssl", symlinks=True)
            for name in original["files"]:
                copy_file(rooted(source / profile / "files", name), rooted(target, name))
            entry = copy.deepcopy(original["entry"])
            old_runtime, new_runtime = original["runtime_libdir"], manifest["runtime_libdir"]
            proof = {}
            for name in entry["elf"]:
                path = rooted(target, name)
                before = elf_identity(path, work)
                paths = subprocess.check_output(["patchelf", "--print-rpath", str(path)], text=True).strip().split(":")
                preserved = []
                for value in filter(None, paths):
                    if value == old_runtime:
                        continue
                    if not re.fullmatch(r"(?:\$ORIGIN|\$\{ORIGIN\})(?:/[A-Za-z0-9_.+-]+)*", value):
                        raise RuntimeError("Profile has an unexpected original runtime path: " + value)
                    preserved.append(value)
                subprocess.run(["patchelf", "--set-rpath", ":".join([new_runtime, *preserved]), str(path)], check=True)
                after = elf_identity(path, work)
                if before != after:
                    raise RuntimeError("RPATH relocation changed OpenSSL code or ABI inputs: " + name)
                entry["elf"][name].update(runpath=[new_runtime, *preserved], rpath=[], sha256=file_hash(path))
                proof[name] = before
            entry["runtime_libdir"] = new_runtime
            entry["runtime_assets"] = {name: new_runtime + value[len(old_runtime):] if value.startswith(old_runtime + "/") else value
                                       for name, value in runtime_assets(entry).items()}
            entry["file_hashes"] = {name: file_hash(rooted(target, name)) for name in entry.get("files", [])}
            for field in ("abi_epoch", "abi_capability", "abi_capabilities", "abi_dump", "abi_dump_hashes", "abi_empty"):
                if field in common:
                    entry[field] = copy.deepcopy(common[field])
                else:
                    entry.pop(field, None)
            for name in mapping(entry.get("abi_dump", {}), "abi_dump").values():
                copy_file(rooted(sdk, name), rooted(target, name))
            target_manifest = dict(manifest, libraries={"openssl": entry})
            write(target / MANIFEST, target_manifest)
            copy_file(sdk / "bin/openssl", target / "bin/openssl")
            for name in entry_paths(manifest["libraries"].get("zlib", {})):
                copy_file(rooted(sdk, name), rooted(target, name))
            probe_directory(target, PROFILES[profile])
            private_directories = compiled_plugin_directories(target, common['version'], old_runtime)
            abi, dumps = abi_inventory(target, entry, work / ("abi-" + profile))
            compare_abi(common_abi, abi, common_dumps, dumps)
            deployment_key = fingerprint(root, sdk, environment, common, profile)
            receipt = export_overlay(target, entry, output / profile, profile, deployment_key, abi, common_identity(sdk, common))
            old_root = str(Path(old_runtime).parent)
            new_root = str(Path(new_runtime).parent)
            receipt["compiled_input_fingerprint"] = original.get("compiled_input_fingerprint", original["input_fingerprint"])
            receipt["deployment_fingerprint"] = deployment_key
            receipt["relocation"] = {"schema": 1, "source_receipt_sha256": file_hash(source / profile / "overlay.json"),
                                     "source_runtime_libdir": old_runtime, "target_runtime_libdir": new_runtime,
                                     "typed_abi_revalidated": True, "elf_identity": proof,
                                     "compiled_engine_directory": private_directories.get("engine"),
                                     "compiled_module_directory": private_directories["module"],
                                     "required_layout_alias": {"path": new_root, "target": old_root} if profile == "rpm" else {"path": old_root, "target": new_root}}
            write(output / profile / "overlay.json", receipt)
            receipts[profile] = receipt
    index = {"schema": 1, "library": "openssl", "architecture": manifest["architecture"], "source_hash": common["source_hash"],
             "version": common["version"], "profiles": {name: {"input_fingerprint": value["input_fingerprint"],
                 "compiled_input_fingerprint": value["compiled_input_fingerprint"], "deployment_fingerprint": value["deployment_fingerprint"],
                 "receipt_sha256": file_hash(output / name / "overlay.json")} for name, value in receipts.items()}}
    write(output / "variants.json", index)
    return {"complete": True, "rebuilt_profiles": [], "relocated_profiles": list(PROFILES), "original_profiles_preserved": True}


def assert_el8(manifest):
    os_release = Path("/etc/os-release").read_text()
    distribution = re.search(r'^ID=["\']?([^"\'\n]+)', os_release, re.M)
    version = re.search(r'^VERSION_ID=["\']?([^"\'\n]+)', os_release, re.M)
    if not distribution or distribution.group(1) not in ("almalinux", "rhel", "centos", "rocky", "ol") or not version or version.group(1).split(".")[0] != "8":
        raise RuntimeError("OpenSSL glibc path variants must be compiled on EL8")
    arch = {"amd64": "x86_64", "arm64": "aarch64"}.get(platform.machine(), platform.machine())
    if manifest.get("architecture") != arch:
        raise RuntimeError("OpenSSL variants must use the common SDK's native architecture")


def build(root, sdk, environment, output):
    manifest, entry = openssl_entry(sdk)
    assert_el8(manifest)
    if (environment.get("architecture") != manifest["architecture"] or environment.get("environment", {}).get("SPC_LIBC") != "glibc"
            or environment.get("options", {}).get("with-shared-libdir") != manifest["runtime_libdir"]):
        raise RuntimeError("Variant build environment does not match the glibc SDK")
    output.mkdir(parents=True, exist_ok=True)
    probe_directory(sdk, PROFILES["rpm"])
    keys = {profile: fingerprint(root, sdk, environment, entry, profile) for profile in PROFILES}
    receipts = {profile: valid_overlay(output / profile, key) for profile, key in keys.items()}
    rebuilt = []
    if receipts["rpm"] is None or receipts["rpm"]["files"] != {name: file_record(rooted(sdk, name)) for name in receipts["rpm"]["files"]}:
        with tempfile.TemporaryDirectory(prefix=".openssl-abi-", dir=output.parent) as temporary:
            abi, _ = abi_inventory(sdk, entry, Path(temporary))
            receipts["rpm"] = export_overlay(sdk, entry, output / "rpm", "rpm", keys["rpm"], abi)
    if receipts["deb"] is None:
        receipts["deb"] = produce(root, sdk, environment, output / "deb", "deb")
        rebuilt.append("deb")
    if receipts["rpm"]["abi"] != receipts["deb"]["abi"] or receipts["rpm"]["headers_sha256"] != receipts["deb"]["headers_sha256"]:
        raise RuntimeError("Cached OpenSSL variants disagree on public headers or ABI")
    index = {"schema": 1, "library": "openssl", "architecture": manifest["architecture"], "source_hash": entry["source_hash"],
             "version": entry["version"], "profiles": {name: {"input_fingerprint": value["input_fingerprint"],
                 "receipt_sha256": file_hash(output / name / "overlay.json")} for name, value in receipts.items()}}
    write(output / "variants.json", index)
    return {"complete": True, "rebuilt_profiles": rebuilt, "reused_common_rpm": True, "profiles": list(PROFILES)}


def apply(sdk, variants, profile):
    manifest, old = openssl_entry(sdk)
    receipt = valid_overlay(variants / profile)
    if receipt is None or receipt["profile"] != profile:
        raise RuntimeError("OpenSSL variant is missing or failed integrity/ABI validation")
    index = read(variants / "variants.json")
    if index.get("profiles", {}).get(profile, {}).get("receipt_sha256") != file_hash(variants / profile / "overlay.json"):
        raise RuntimeError("OpenSSL variant receipt does not match the sealed profile index")
    if (receipt["source_hash"] != old["source_hash"] or receipt["version"] != old["version"]
            or receipt["runtime_libdir"] != manifest["runtime_libdir"] or receipt.get("architecture") != manifest["architecture"]
            or receipt["headers_sha256"] != header_digest(sdk) or receipt.get("common_identity") != common_identity(sdk, old)):
        raise RuntimeError("OpenSSL overlay does not match this common SDK")
    if runtime_paths(receipt["entry"]) != runtime_paths(old):
        raise RuntimeError("OpenSSL profile changed the common runtime/asset ownership set")
    allowed = entry_paths(old) | {"debug/" + name + ".debug" for name in entry_paths(old)}
    if not receipt["files"].keys() <= allowed:
        raise RuntimeError("OpenSSL profile contains undeclared runtime files")
    other_owners = {name for identifier, entry in manifest["libraries"].items() if identifier != "openssl" for name in entry_paths(entry)}
    if set(receipt["files"]) & other_owners:
        raise RuntimeError("OpenSSL variant would overwrite another library's files")
    old_paths = entry_paths(old)
    retained_abi = set(mapping(old.get('abi_dump', {}), 'abi_dump').values()) if not receipt['entry'].get('abi_dump') else set()
    for name in list(old_paths):
        debug = "debug/" + name + ".debug"
        if rooted(sdk, debug).is_file():
            old_paths.add(debug)
    for name in sorted(old_paths - receipt["files"].keys() - retained_abi):
        path = rooted(sdk, name)
        if path.is_symlink() or path.is_file():
            path.unlink()
    for name in sorted(receipt["files"]):
        copy_file(rooted(variants / profile / "files", name), rooted(sdk, name))
    entry = copy.deepcopy(receipt["entry"])
    for field in ("abi_epoch", "abi_capability", "abi_capabilities", "abi_comparison", "package_versions"):
        if field in old:
            entry[field] = copy.deepcopy(old[field])
    if retained_abi:
        for field in ('abi_dump', 'abi_dump_hashes', 'abi_empty'):
            if field in old:
                entry[field] = copy.deepcopy(old[field])
    manifest["libraries"]["openssl"] = entry
    manifest["build_identity"] = digest({key: value for key, value in manifest.items() if key != "build_identity"})
    write(sdk / MANIFEST, manifest)
    marker = {"schema": 1, "profile": profile, "input_fingerprint": receipt["input_fingerprint"],
              "overlay_receipt_sha256": file_hash(variants / profile / "overlay.json"),
              "modified_files": sorted(old_paths | receipt["files"].keys()), "files": receipt["files"]}
    write(sdk / "openssl-variant.json", marker)
    return {"complete": True, "profile": profile, "modified_files": marker["modified_files"]}


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="action", required=True)
    producer = sub.add_parser("build")
    producer.add_argument("--root", type=Path, default=Path("."))
    producer.add_argument("--buildroot", type=Path, required=True)
    producer.add_argument("--environment", type=Path, required=True)
    producer.add_argument("--output", type=Path, required=True)
    relocation = sub.add_parser("relocate")
    relocation.add_argument("--root", type=Path, default=Path("."))
    relocation.add_argument("--buildroot", type=Path, required=True)
    relocation.add_argument("--environment", type=Path, required=True)
    relocation.add_argument("--compiled-environment", type=Path, required=True)
    relocation.add_argument("--variants", type=Path, required=True)
    relocation.add_argument("--output", type=Path, required=True)
    select = sub.add_parser("apply")
    select.add_argument("--buildroot", type=Path, required=True)
    select.add_argument("--variants", type=Path, required=True)
    select.add_argument("--type", choices=tuple(PROFILES), required=True)
    args = parser.parse_args()
    if args.action == "build":
        result = build(args.root.resolve(), args.buildroot.resolve(), read(args.environment), args.output.resolve())
    elif args.action == "relocate":
        result = relocate(args.root.resolve(), args.buildroot.resolve(), read(args.environment), read(args.compiled_environment),
                          args.variants.resolve(), args.output.resolve())
    else:
        result = apply(args.buildroot.resolve(), args.variants.resolve(), args.type)
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    try:
        main()
    except (OSError, RuntimeError, ValueError, KeyError, subprocess.CalledProcessError, ET.ParseError) as error:
        print("openssl-variants: " + str(error), file=sys.stderr)
        sys.exit(1)
