#!/usr/bin/env python3
"""Create one source-snapshot request shared by all package formats."""

import argparse
import hashlib
import json
import subprocess
from pathlib import Path

from workflow import stable_artifact


STREAMS = ("8.2", "8.3", "8.4", "8.5", "8.6")
TOOLS = {"php-src", "php-micro", "cmake", "ninja", "pkg-config", "pkgconf", "re2c", "bison", "go", "rust", "zig", "gcc", "composer"}


def identity(root):
    digest = hashlib.sha256()
    for pattern in ("composer.json", "composer.lock", "craft.yml", "spc.registry.yml", "Dockerfile.*", "bin/*", "src/**/*", "config/**/*", ".github/actions/shared-libraries/*"):
        for path in sorted(root.glob(pattern)):
            if path.is_file():
                digest.update(str(path.relative_to(root)).encode() + b"\0" + path.read_bytes())
    return digest.hexdigest()


def plan(previous, current, previous_meta, current_meta, force=False, configuration="", library_ids=None, build_inputs=None):
    changed = sorted(key for key in previous.keys() | current.keys() if stable_artifact(previous.get(key)) != stable_artifact(current.get(key)))
    packages = [key.removeprefix("ext-") for key in changed if key.startswith("ext-") or key == "frankenphp"]
    if previous_meta.get("pie_version") != current_meta.get("pie_version"):
        packages.append("pie")
    libraries = [key for key in changed if (key in library_ids if library_ids is not None else key not in TOOLS and not key.startswith("ext-") and key != "frankenphp")]
    library_build_inputs = []
    if build_inputs is not None:
        managed = set(build_inputs["managed_library_artifacts"])
        required = managed | set(build_inputs["library_tool_artifacts"])
        excluded = set(build_inputs["irrelevant_artifacts"]) - required
        library_build_inputs = [key for key in changed if key not in managed and key not in excluded]
    full = force or previous_meta.get("shared_configuration") != configuration
    requests = {}
    for stream in STREAMS:
        stream_full = full or previous_meta.get("php_versions", {}).get(stream) != current_meta.get("php_versions", {}).get(stream)
        if stream_full or packages:
            requests[stream] = {"full": bool(stream_full), "packages": [] if stream_full else sorted(set(packages))}
    return {"schema": 1, "requests": requests, "libraries": libraries, "library_build_inputs": library_build_inputs, "configuration": configuration}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument("--force-full", action="store_true")
    args = parser.parse_args()
    root = args.root.resolve()
    read = lambda name: json.loads((root / name).read_text())
    metadata = read("downloads/.spc-meta.json")
    configuration = identity(root)
    inventory = json.loads(subprocess.check_output(["php", str(Path(__file__).with_name("inventory.php")), str(root)], cwd=root, universal_newlines=True))
    library_ids = set(inventory["managed_library_artifacts"])
    request = plan(read(".download-state/lock.prev.json"), read("downloads/.cache.json"), read(".download-state/meta.prev.json"), metadata, args.force_full, configuration, library_ids, inventory)
    request["build_inputs"] = inventory
    metadata["shared_configuration"] = configuration
    (root / "downloads/.spc-meta.json").write_text(json.dumps(metadata, sort_keys=True) + "\n")
    (root / "downloads/build-plan.json").write_text(json.dumps(request, sort_keys=True, indent=2) + "\n")
    print("any_change=" + str(bool(request["requests"] or request["libraries"] or request["library_build_inputs"])).lower())
    print("changed_libraries=" + ",".join(request["libraries"]))


if __name__ == "__main__":
    main()
