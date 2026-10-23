#!/usr/bin/env python3
"""Small, locally testable pieces of the shared-library workflow."""

import argparse
import hashlib
import json
import re
import shutil
import subprocess
from pathlib import Path


STREAMS = ["8.2", "8.3", "8.4", "8.5", "8.6"]


def csv(value, default):
    return sorted(set(part.strip() for part in value.split(",") if part.strip())) if value else default


def emit(name, value):
    encoded = json.dumps(value, separators=(",", ":")) if not isinstance(value, str) else value
    print(name + "=" + encoded)


def stable_artifact(value):
    if not isinstance(value, dict):
        return value
    result = dict(value)
    if isinstance(result.get("source"), dict):
        result["source"] = {key: item for key, item in result["source"].items() if key != "time"}
    if isinstance(result.get("binary"), dict):
        result["binary"] = {platform: {key: item for key, item in record.items() if key != "time"} if isinstance(record, dict) else record for platform, record in result["binary"].items()}
    return result


def library_build_sources(root):
    sources = json.loads((root / "downloads/.cache.json").read_text())
    plan_path = root / "downloads/build-plan.json"
    inventory = json.loads(plan_path.read_text()).get("build_inputs") if plan_path.exists() else None
    if inventory is not None:
        fields = ["managed_library_artifacts", "library_tool_artifacts", "irrelevant_artifacts"]
        if inventory.get("schema") != 1 or any(not isinstance(inventory.get(field), list) or any(not isinstance(name, str) for name in inventory[field]) for field in fields):
            raise RuntimeError("Invalid library build input inventory")
        required = set(inventory["managed_library_artifacts"]) | set(inventory["library_tool_artifacts"])
        excluded = set(inventory["irrelevant_artifacts"]) - required
        sources = {name: value for name, value in sources.items() if name not in excluded}
    return {name: stable_artifact(value) for name, value in sources.items()}


def fingerprint(root, target, toolchain):
    digest = hashlib.sha256(b"shared-libraries-v1\0" + target.encode() + b"\0" + toolchain.encode())
    sources = library_build_sources(root)
    digest.update(json.dumps(sources, sort_keys=True, separators=(",", ":")).encode())
    for pattern in ("composer.json", "composer.lock", "craft.yml", "spc.registry.yml", "config/**/*", "Dockerfile.*",
                    "src/hook/**/*", "src/artifact/**/*", "src/Command/BuildCommand.php", "src/Command/BaseCommand.php",
                    "src/CraftConfig.php", "src/util/SpcAutoload.php", "src/util/TwigRenderer.php", "src/step/RunSPC.php",
                    "bin/spp", "bin/shared-library-plan", ".github/actions/shared-libraries/*"):
        for path in sorted(root.glob(pattern)):
            if path.is_file():
                digest.update(str(path.relative_to(root)).encode() + b"\0" + path.read_bytes())
    return digest.hexdigest()[:32]


def matrix(plans, php_versions, succeeded=None):
    includes, targets = [], []
    for path in sorted(plans.glob("*/shared-library-plan.json")):
        plan = json.loads(path.read_text())
        target = json.loads((path.parent / "target.json").read_text())
        if succeeded is not None and target["target"] not in succeeded:
            continue
        targets.append(target)
        for stream, request in sorted(plan.get("php_requests", {}).items()):
            if stream not in php_versions and not plan.get("abi_break") and not plan.get("bootstrap"):
                continue
            # An empty package list means a complete PHP build only when explicitly full.
            if not request.get("full") and not request.get("packages"):
                continue
            includes.append(dict(target, **{"php-version": stream, "packages": "" if request.get("full") else ",".join(request["packages"])}))
    return includes, targets


def assemble(source, destination, extension):
    destination.mkdir(parents=True, exist_ok=True)
    for path in sorted(source.rglob("*." + extension)):
        target = destination / path.name
        if target.exists() and target.read_bytes() != path.read_bytes():
            raise RuntimeError("Conflicting cohort package: " + path.name)
        shutil.copy2(path, target)


def build_receipt(plan, source_run_id, image, kind, php_stream=None):
    if not re.fullmatch(r"[0-9]+", str(source_run_id)) or not re.fullmatch(r".+@sha256:[a-f0-9]{64}", image):
        raise RuntimeError("Build receipt requires the resolved source run and builder image digest")
    if kind not in ["libraries", "consumer"] or (kind == "consumer" and php_stream not in STREAMS) or (kind == "libraries" and php_stream is not None):
        raise RuntimeError("Invalid build receipt kind or PHP stream")
    return {"schema": 1, "kind": kind, "source_run_id": str(source_run_id), "image": image,
            "plan_sha256": hashlib.sha256(plan.read_bytes()).hexdigest(), "php_stream": php_stream}


def prune_consumers(source, library_artifact):
    receipt_name = "shared-library-build-receipt.json"
    library = json.loads((library_artifact / receipt_name).read_text())
    target = json.loads((library_artifact / "target.json").read_text())
    expected = build_receipt(library_artifact / "shared-library-plan.json", target.get("source_run_id", ""), target.get("image", ""), "libraries")
    if library != expected:
        raise RuntimeError("Library artifact has an invalid build receipt")
    for path in sorted(source.glob("shared-consumers-*")):
        if path.is_symlink() or not path.is_dir():
            raise RuntimeError("Invalid consumer artifact directory: " + str(path))
        stream = re.search(r"-php([0-9]+\.[0-9]+)$", path.name)
        wanted = dict(expected, kind="consumer", php_stream=stream.group(1) if stream else None)
        receipt = path / receipt_name
        try:
            matching = stream is not None and stream.group(1) in STREAMS and receipt.is_file() and json.loads(receipt.read_text()) == wanted
        except (OSError, ValueError):
            matching = False
        if not matching:
            print("::warning::Ignoring stale consumer artifact " + path.name)
            shutil.rmtree(path)


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command")
    sub.required = True
    setup = sub.add_parser("setup")
    setup.add_argument("--type", choices=("rpm", "deb", "apk"), required=True)
    setup.add_argument("--architectures", default="")
    setup.add_argument("--alma-versions", default="")
    setup.add_argument("--php-versions", default="")
    setup.add_argument("--resolve-images", action="store_true")
    fp = sub.add_parser("fingerprint")
    fp.add_argument("--target", required=True)
    fp.add_argument("--toolchain", required=True)
    validate = sub.add_parser("validate-source")
    validate.add_argument("--mode", choices=("full", "incremental"), required=True)
    mat = sub.add_parser("matrix")
    mat.add_argument("--plans", type=Path, required=True)
    mat.add_argument("--php-versions", default="")
    mat.add_argument("--succeeded")
    targets = sub.add_parser("targets")
    targets.add_argument("--directory", type=Path, required=True)
    targets.add_argument("--succeeded", required=True)
    merge = sub.add_parser("assemble")
    merge.add_argument("--source", type=Path, required=True)
    merge.add_argument("--destination", type=Path, required=True)
    merge.add_argument("--type", required=True)
    receipt = sub.add_parser("receipt")
    receipt.add_argument("--plan", type=Path, required=True)
    receipt.add_argument("--source-run-id", required=True)
    receipt.add_argument("--image", required=True)
    receipt.add_argument("--kind", required=True)
    receipt.add_argument("--php-stream")
    receipt.add_argument("--output", type=Path, required=True)
    prune = sub.add_parser("prune-consumers")
    prune.add_argument("--source", type=Path, required=True)
    prune.add_argument("--library-artifact", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "setup":
        archs = csv(args.architectures, ["amd64", "arm64"] if args.type == "deb" else ["x86_64", "arm64"])
        archs = ["arm64" if arch == "aarch64" else arch for arch in archs]
        if any(arch not in (["amd64", "arm64"] if args.type == "deb" else ["x86_64", "arm64"]) for arch in archs):
            raise RuntimeError("Unsupported architecture")
        alms = csv(args.alma_versions, ["8", "9", "10"]) if args.type == "rpm" else [""]
        if args.type == "rpm" and any(alma not in ["8", "9", "10"] for alma in alms):
            raise RuntimeError("Unsupported Alma version")
        values = [{"arch": arch, "alma": alma, "target": ("alma" + alma + "-" if alma else "") + arch} for alma in alms for arch in archs]
        images = {}
        for value in values:
            image = "ghcr.io/static-php/packages-builder-" + ({"rpm": "rhel-" + value["alma"], "deb": "debian", "apk": "alpine"}[args.type]) + ":latest"
            if args.resolve_images:
                if image not in images:
                    result = subprocess.check_output(["docker", "buildx", "imagetools", "inspect", image, "--format", "{{json .Manifest}}"], universal_newlines=True)
                    digest = json.loads(result)["digest"]
                    if not re.fullmatch("sha256:[a-f0-9]{64}", digest):
                        raise RuntimeError("Invalid builder image digest")
                    images[image] = image.rsplit(":", 1)[0] + "@" + digest
                image = images[image]
            value["image"] = image
        emit("matrix", {"include": values})
        emit("php-versions", csv(args.php_versions, STREAMS))
    elif args.command == "fingerprint":
        emit("fingerprint", fingerprint(Path("."), args.target, args.toolchain))
    elif args.command == "validate-source":
        if args.mode == "incremental" and not Path("downloads/build-plan.json").is_file():
            raise RuntimeError("Incremental builds require downloads/build-plan.json; run spc-download or select full mode")
    elif args.command == "matrix":
        includes, targets = matrix(args.plans, csv(args.php_versions, STREAMS), None if args.succeeded is None else args.succeeded.splitlines())
        emit("matrix", {"include": includes})
        emit("any", str(bool(includes)).lower())
        emit("publish-matrix", {"include": targets})
        emit("publish-any", str(bool(targets)).lower())
    elif args.command == "targets":
        succeeded = set(args.succeeded.splitlines())
        values = [json.loads(path.read_text()) for path in sorted(args.directory.glob("*/target.json"))]
        values = [value for value in values if value["target"] in succeeded]
        emit("matrix", {"include": values})
        emit("any", str(bool(values)).lower())
    elif args.command == "assemble":
        assemble(args.source, args.destination, args.type)
    elif args.command == "receipt":
        args.output.write_text(json.dumps(build_receipt(args.plan, args.source_run_id, args.image, args.kind, args.php_stream), sort_keys=True) + "\n")
    elif args.command == "prune-consumers":
        prune_consumers(args.source, args.library_artifact)


if __name__ == "__main__":
    main()
