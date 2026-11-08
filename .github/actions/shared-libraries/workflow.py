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


def image_digest(image):
    match = re.fullmatch(r"[^\s@]+@sha256:([a-f0-9]{64})", image)
    if match is None:
        raise RuntimeError("Publication requires an image pinned by SHA256")
    return match.group(1)


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


def fingerprint(root, target, toolchain, libraries="", build_environment=None, image=""):
    digest = hashlib.sha256(b"shared-libraries-v2\0" + target.encode() + b"\0" + toolchain.encode() + b"\0" + image.encode())
    sources = library_build_sources(root)
    digest.update(json.dumps(sources, sort_keys=True, separators=(",", ":")).encode())
    digest.update(json.dumps(csv(libraries, []), separators=(",", ":")).encode())
    if build_environment is not None:
        digest.update(json.dumps(build_environment, sort_keys=True, separators=(",", ":")).encode())
    for pattern in ("composer.json", "composer.lock", "craft.yml", "spc.registry.yml", "config/**/*", "Dockerfile.*",
                    "src/hook/**/*", "src/artifact/**/*", "src/Command/BuildCommand.php", "src/Command/BaseCommand.php",
                    "src/CraftConfig.php", "src/util/SpcAutoload.php", "src/util/TwigRenderer.php", "src/step/RunSPC.php",
                    "bin/spp", "bin/shared-library-plan", ".github/actions/shared-libraries/*"):
        for path in sorted(root.glob(pattern)):
            if path.is_file():
                digest.update(str(path.relative_to(root)).encode() + b"\0" + path.read_bytes())
    return digest.hexdigest()[:32]


def compatibility(root, target, toolchain, build_environment):
    if build_environment.get("schema") != 1 or not isinstance(build_environment.get("environment"), dict):
        raise RuntimeError("Missing rendered library build environment")
    value = {"target": target, "toolchain": toolchain, "build_environment": build_environment}
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:32]


def matrix(plans, php_versions, succeeded=None):
    includes, targets = [], []
    for path in sorted(plans.glob("*/shared-library-plan.json")):
        plan = json.loads(path.read_text())
        target = json.loads((path.parent / "target.json").read_text())
        if succeeded is not None and target["target"] not in succeeded:
            continue
        if "format_selection" in plan:
            target = dict(target, format_selection=plan["format_selection"])
        targets.append(target)
        for stream, request in sorted(plan.get("php_requests", {}).items()):
            if stream not in php_versions and not plan.get("abi_break") and not plan.get("bootstrap"):
                continue
            # An empty package list means a complete PHP build only when explicitly full.
            if not request.get("full") and not request.get("packages"):
                continue
            includes.append(dict(target, **{"php-version": stream, "packages": "" if request.get("full") else ",".join(request["packages"]),
                                          "compile": request.get("compile", True), "abi-break": bool(plan.get("abi_break")),
                                          "compile-full": bool(request.get("compile", True) and request.get("compile_full", request.get("full"))),
                                          "abi-rebuild-libraries": ",".join(plan.get("abi_rebuild_roots", [])),
                                          "abi-core-rebuild": stream in plan.get("abi_core_rebuild_streams", []),
                                          "compile-packages": "" if request.get("compile_full", request.get("full")) else ",".join(request.get("compile_packages", request.get("packages", [])))}))
    return includes, targets


def assemble(source, destination, extension):
    destination.mkdir(parents=True, exist_ok=True)
    for path in sorted(source.rglob("*." + extension)):
        target = destination / path.name
        if target.exists() and target.read_bytes() != path.read_bytes():
            raise RuntimeError("Conflicting cohort package: " + path.name)
        shutil.copy2(path, target)


def runtime_checks(includes):
    groups = {}
    for entry in includes:
        entry["runtime-checks"] = False
        entry["tls-checks"] = False
        groups.setdefault((entry["target"], entry.get("type")), []).append(entry)
    for entries in groups.values():
        selected = max(entries, key=lambda value: tuple(map(int, value["php-version"].split("."))))
        selected["tls-checks"] = True
        services = [entry for entry in entries if not entry.get("library-only") and (
            not entry.get("packages") or set(entry["packages"].split(",")) & {"cli", "cgi", "fpm", "embed", "frankenphp"})]
        if services:
            selected = max(services, key=lambda value: tuple(map(int, value["php-version"].split("."))))
            selected["runtime-checks"] = True


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



def upgrade_receipt(report, plan, packages, kind, source_run_id, image, stream, architecture, libraries_only=False):
    result = json.loads(report.read_text())
    cells = result.get("cells", [result])
    if result.get("ok") is not True or not cells or any(cell.get("ok") is not True or cell.get("type") != kind or cell.get("php_stream") != stream or cell.get("architecture") != architecture for cell in cells):
        raise RuntimeError("Native upgrade did not validate the requested package cell")
    receipt = build_receipt(plan, source_run_id, image, "libraries" if libraries_only else "consumer", None if libraries_only else stream)
    receipt.update(type=kind, architecture=architecture, probe_php_stream=stream, scope="libraries" if libraries_only else "consumer",
                   report_sha256=hashlib.sha256(report.read_bytes()).hexdigest(),
                   packages={path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in sorted(packages.glob("*." + kind))})
    if not receipt["packages"]:
        raise RuntimeError("Native upgrade receipt has no package archives")
    return receipt


def verify_upgrades(source, library_artifact, kind):
    target = json.loads((library_artifact / "target.json").read_text())
    plan_path = library_artifact / "shared-library-plan.json"
    plan = json.loads(plan_path.read_text())
    wanted = {stream for stream, request in plan.get("php_requests", {}).items() if request.get("full") or request.get("packages")}
    libraries_only = not wanted and bool(plan.get("libraries"))
    validated = set()
    for path in source.glob("*/shared-library-upgrade-receipt.json"):
        data = json.loads(path.read_text())
        scope = data.get("scope")
        stream = data.get("probe_php_stream")
        expected = upgrade_receipt(path.parent / "native-upgrade-receipt.json", plan_path, path.parent, kind,
                                   target["source_run_id"], target["image"], stream, target["arch"], scope == "libraries")
        if data != expected:
            raise RuntimeError("Native upgrade receipt does not match its payload/plan: " + str(path))
        if scope == "consumer" and stream in wanted:
            validated.add(stream)
        elif scope == "libraries" and libraries_only:
            validated.add("libraries")
    missing = ({"libraries"} if libraries_only else wanted) - validated
    if missing:
        raise RuntimeError("Missing successful native ordinary-upgrade cells: " + ", ".join(sorted(missing)))


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command")
    sub.required = True
    setup = sub.add_parser("setup")
    setup.add_argument("--type", choices=("rpm", "deb", "apk", "glibc"), required=True)
    setup.add_argument("--architectures", default="")
    setup.add_argument("--alma-versions", default="")
    setup.add_argument("--php-versions", default="")
    setup.add_argument("--resolve-images", action="store_true")
    fp = sub.add_parser("fingerprint")
    fp.add_argument("--target", required=True)
    fp.add_argument("--toolchain", required=True)
    fp.add_argument("--libraries", default="")
    fp.add_argument("--image", default="")
    fp.add_argument("--build-environment", type=Path, required=True)
    validate = sub.add_parser("validate-source")
    validate.add_argument("--mode", choices=("full", "incremental"), required=True)
    mat = sub.add_parser("matrix")
    mat.add_argument("--plans", type=Path, required=True)
    mat.add_argument("--php-versions", default="")
    mat.add_argument("--succeeded")
    packages = sub.add_parser("packages")
    packages.add_argument("--plans", type=Path, required=True)
    packages.add_argument("--succeeded", required=True)
    packages.add_argument("--php-versions", default="")
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
    native = sub.add_parser("upgrade-receipt")
    native.add_argument("--report", type=Path, required=True)
    native.add_argument("--plan", type=Path, required=True)
    native.add_argument("--packages", type=Path, required=True)
    native.add_argument("--type", required=True)
    native.add_argument("--source-run-id", required=True)
    native.add_argument("--image", required=True)
    native.add_argument("--php-stream", required=True)
    native.add_argument("--architecture", required=True)
    native.add_argument("--library-only", dest="libraries_only", action="store_true")
    native.add_argument("--output", type=Path, required=True)
    mode = sub.add_parser("upgrade-mode")
    mode.add_argument("--baseline", type=Path, required=True)
    verify_native = sub.add_parser("verify-upgrades")
    verify_native.add_argument("--source", type=Path, required=True)
    verify_native.add_argument("--library-artifact", type=Path, required=True)
    verify_native.add_argument("--type", required=True)
    prune = sub.add_parser("prune-consumers")
    prune.add_argument("--source", type=Path, required=True)
    prune.add_argument("--library-artifact", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "setup":
        archs = csv(args.architectures, ["amd64", "arm64"] if args.type == "deb" else ["x86_64", "arm64"])
        archs = ["arm64" if arch == "aarch64" else "x86_64" if args.type == "glibc" and arch == "amd64" else arch for arch in archs]
        if any(arch not in (["amd64", "arm64"] if args.type == "deb" else ["x86_64", "arm64"]) for arch in archs):
            raise RuntimeError("Unsupported architecture")
        alms = ["8"] if args.type == "glibc" else csv(args.alma_versions, ["8", "9", "10"]) if args.type == "rpm" else [""]
        if args.type == "rpm" and any(alma not in ["8", "9", "10"] for alma in alms):
            raise RuntimeError("Unsupported Alma version")
        values = [{"arch": arch, "alma": alma, "target": ("alma" + alma + "-" if alma else "") + arch} for alma in alms for arch in archs]
        images = {}
        for value in values:
            image = "ghcr.io/static-php/" + {
                "rpm": "packages-builder-rhel-" + value["alma"],
                "glibc": "packages-builder-rhel-8",
                "deb": "packages-packager-debian",
                "apk": "packages-builder-alpine",
            }[args.type] + ":latest"
            if args.resolve_images:
                if image not in images:
                    result = subprocess.check_output(["docker", "buildx", "imagetools", "inspect", image, "--format", "{{json .Manifest}}"], universal_newlines=True)
                    digest = json.loads(result)["digest"]
                    if not re.fullmatch("sha256:[a-f0-9]{64}", digest):
                        raise RuntimeError("Invalid builder image digest")
                    images[image] = image.rsplit(":", 1)[0] + "@" + digest
                image = images[image]
            value["image"] = image
            if args.type == "glibc":
                debian = "ghcr.io/static-php/packages-packager-debian:latest"
                if args.resolve_images:
                    if debian not in images:
                        result = subprocess.check_output(["docker", "buildx", "imagetools", "inspect", debian, "--format", "{{json .Manifest}}"], universal_newlines=True)
                        digest = json.loads(result)["digest"]
                        if not re.fullmatch("sha256:[a-f0-9]{64}", digest):
                            raise RuntimeError("Invalid packaging image digest")
                        images[debian] = debian.rsplit(":", 1)[0] + "@" + digest
                    debian = images[debian]
                value["package_images"] = {"rpm": image, "deb": debian}
        emit("matrix", {"include": values})
        emit("php-versions", csv(args.php_versions, STREAMS))
    elif args.command == "fingerprint":
        environment = json.loads(args.build_environment.read_text())
        emit("fingerprint", fingerprint(Path("."), args.target, args.toolchain, args.libraries, environment, args.image))
        compatible = compatibility(Path("."), args.target, args.toolchain, environment)
        emit("compatibility", compatible)
        args.build_environment.with_name("library-compatibility").write_text(compatible + "\n")
    elif args.command == "validate-source":
        if args.mode == "incremental" and not Path("downloads/build-plan.json").is_file():
            raise RuntimeError("Incremental builds require downloads/build-plan.json; run spc-download or select full mode")
    elif args.command == "matrix":
        includes, targets = matrix(args.plans, csv(args.php_versions, STREAMS), None if args.succeeded is None else args.succeeded.splitlines())
        for target in targets:
            if target.get("type") != "apk" or any(value["target"] == target["target"] for value in includes):
                continue
            path = next(path for path in args.plans.glob("*/shared-library-plan.json") if json.loads((path.parent / "target.json").read_text())["target"] == target["target"])
            if json.loads(path.read_text()).get("libraries"):
                includes.append(dict(target, **{"php-version": "8.4", "packages": "", "compile": False, "compile-packages": "", "library-only": True}))
        runtime_checks([entry for entry in includes if entry.get("type") == "apk"])
        emit("matrix", {"include": includes})
        emit("any", str(bool(includes)).lower())
        publishing = [dict(target, type=kind, **{"package-image": target["package_images"][kind],
                      "package-image-digest": image_digest(target["package_images"][kind])}) for target in targets for kind in ["rpm", "deb"]
                      if kind not in target.get("format_selection", {}) or target["format_selection"][kind].get("libraries") or any(request.get("full") or request.get("packages") for request in target["format_selection"][kind].get("php_requests", {}).values())] if any("package_images" in target for target in targets) else targets
        emit("publish-matrix", {"include": publishing})
        emit("publish-any", str(bool(publishing)).lower())
    elif args.command == "packages":
        values, target_values = matrix(args.plans, csv(args.php_versions, STREAMS))
        succeeded = set(args.succeeded.splitlines())
        includes = []
        for value in values:
            if value["target"] + " " + value["php-version"] not in succeeded:
                continue
            for kind in ["rpm", "deb"]:
                selection = value.get("format_selection", {}).get(kind)
                request = selection.get("php_requests", {}).get(value["php-version"]) if selection is not None else None
                if selection is not None and not (request and (request.get("full") or request.get("packages"))):
                    continue
                packages = "" if request and request.get("full") else ",".join(request.get("packages", [])) if request else value["packages"]
                includes.append(dict(value, type=kind, packages=packages, **{"package-image": value["package_images"][kind], "library-only": False}))
        for target in target_values:
            path = next(path for path in args.plans.glob("*/shared-library-plan.json") if json.loads((path.parent / "target.json").read_text())["target"] == target["target"])
            plan = json.loads(path.read_text())
            for kind in ["rpm", "deb"]:
                selection = plan.get("format_selection", {}).get(kind, plan)
                if not any(request.get("full") or request.get("packages") for request in selection.get("php_requests", {}).values()) and selection.get("libraries"):
                    includes.append(dict(target, type=kind, **{"package-image": target["package_images"][kind], "php-version": "8.4", "packages": "", "library-only": True}))
        runtime_checks(includes)
        emit("matrix", {"include": includes})
        emit("any", str(bool(includes)).lower())
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
    elif args.command == "upgrade-receipt":
        value = upgrade_receipt(args.report, args.plan, args.packages, args.type, args.source_run_id, args.image, args.php_stream, args.architecture, args.libraries_only)
        args.output.write_text(json.dumps(value, sort_keys=True) + "\n")
    elif args.command == "upgrade-mode":
        baseline = json.loads(args.baseline.read_text())
        cli = next((item for item in baseline.get("packages", []) if item.get("name") == "php-zts-cli"), None)
        if cli is None:
            raise RuntimeError("Frozen baseline has no CLI identity")
        print("shared" if any(name.startswith("php-zts-lib-") for name in cli.get("requires", [])) else "static")
    elif args.command == "verify-upgrades":
        verify_upgrades(args.source, args.library_artifact, args.type)
    elif args.command == "prune-consumers":
        prune_consumers(args.source, args.library_artifact)


if __name__ == "__main__":
    main()
