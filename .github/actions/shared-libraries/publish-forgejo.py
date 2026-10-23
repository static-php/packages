#!/usr/bin/env python3
"""Preflight a complete cohort, then publish consumers before its libraries."""

import json
import os
import re
import runpy
import sys
from pathlib import Path


def main():
    action, package_type, host_number = sys.argv[1:]
    root = Path(__file__).resolve().parents[3]
    helper = runpy.run_path(str(root / "bin/forgejo-helper"))
    registry_type = {"deb": "debian", "apk": "alpine"}[package_type]
    host = helper["host" + host_number]
    owners = {}
    for manifest in (root / "cohort").rglob("shared-library-consumers.json"):
        consumers = json.loads(manifest.read_text()).get("consumers", {})
        for archive in manifest.parent.glob("*." + package_type):
            key = helper["package_metadata"](archive, registry_type)
            consumer = consumers.get(key[0].removesuffix("-debuginfo"), {})
            stream = consumer.get("php_version") or consumer.get("php_stream")
            if stream:
                match = re.match(r"^(\d+)\.(\d+)", stream)
                if not match:
                    raise RuntimeError("Invalid consumer PHP stream: " + str(stream))
                owners[key] = "".join(match.groups())
    groups = {}
    for path in sorted(Path(".").glob("*." + package_type)):
        key = helper["package_metadata"](path, registry_type)
        name = key[0]
        owner = "libs" if name.startswith("php-zts-lib-") else owners.get(key)
        if not owner:
            raise RuntimeError("Package has no consumer ownership record: " + name)
        groups.setdefault(owner, []).append(path)
    if not groups:
        raise RuntimeError("No cohort packages to publish")
    # A breaking library cannot be visible before the consumers that require it.
    for owner in sorted(groups, key=lambda value: value == "libs"):
        password = os.environ["FORGEJO_LIBS_PASSWORD" if owner == "libs" else "FORGEJO_PASSWORD"]
        remote = helper["get_remote_packages"](host, owner, password, registry_type)
        allowed = helper["verify_upload_versions"](host, owner, password, registry_type, groups[owner], remote)
        if len(allowed) != len(groups[owner]):
            raise RuntimeError("A downgrade guard rejected part of the cohort")
        if action == "upload":
            for path in groups[owner]:
                status = helper["upload_packages"](host, owner, password, registry_type, path.name)
                if status:
                    raise RuntimeError("Cohort upload failed: " + path.name)
        elif action != "check":
            raise RuntimeError("Unknown publication action: " + action)


if __name__ == "__main__":
    main()
