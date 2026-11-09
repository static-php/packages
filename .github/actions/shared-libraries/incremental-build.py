#!/usr/bin/env python3
"""Plan and attest selective library builds against a compatible restored SDK."""

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

from workflow import stable_artifact


STATE = "shared-library-build-inputs.json"


def helper_module(filename, name):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(filename))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def compiler_runtime_scan(buildroot, manifest=None, inputs=None):
    runtime = helper_module("compiler-runtime-inputs.py", "compiler_runtime_inputs")
    inputs = inputs or runtime.compiler_runtime_inputs()
    if inputs.get("semantic", {}).get("mode") != "shared":
        raise RuntimeError("Shared SDK builds require a dynamic C++ development linker selector")
    if not manifest or not manifest.get("libraries"):
        return {"schema": 1, "compiler_runtime": inputs, "libraries": {}, "must_rebuild": [], "uncertain_stripped_libraries": []}
    return runtime.scan_sdk(buildroot, inputs, manifest)


def compact_runtime(record):
    return {"fingerprint": record["fingerprint"], "inputs": record["compiler_runtime_inputs"],
            "uses_cxx_runtime": record["uses_cxx_runtime"]}


def read(path, default=None):
    return json.loads(path.read_text()) if path.is_file() else default


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(value, sort_keys=True, indent=2) + "\n"
    if path.is_file() and path.read_text() == content:
        return
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(content)
    temporary.replace(path)


def closure(roots, graph):
    result = set(roots)
    pending = list(roots)
    while pending:
        for dependency in graph.get(pending.pop(), {}).get("depends", []):
            if dependency not in result:
                result.add(dependency)
                pending.append(dependency)
    return result


def reverse_closure(roots, graph, available):
    result = set(roots)
    while True:
        added = {name for name in available if set(graph[name].get("depends", [])) & result} - result
        if not added:
            return result
        result |= added


def requested_ids(value, graph):
    result = set()
    for name in filter(None, (item.strip() for item in value.split(","))):
        name = name.removeprefix("php-zts-lib-")
        matches = {name} if name in graph else {key for key, record in graph.items() if record.get("artifact") == name}
        if not matches:
            raise RuntimeError("Unknown managed library: " + name)
        result |= matches
    return result


def source_inputs(record, sources):
    return {name: sources[name] for name in record.get("inputs", []) if name in sources}


def changed_inputs(record, previous, sources, managed_artifacts):
    tracked = ({record.get("artifact")} | (set(record.get("inputs", [])) - managed_artifacts)) - {None}
    return any(previous.get("sources", {}).get(name) != sources[name] for name in tracked if name in sources)


def pending_rebuilds(manifest):
    pending = manifest.get("pending_rebuilds", {})
    libraries = manifest.get("libraries", {})
    if not isinstance(pending, dict):
        raise RuntimeError("Invalid pending shared-library rebuild marker")
    for consumer, dependencies in pending.items():
        if consumer not in libraries or not isinstance(dependencies, dict) or not dependencies:
            raise RuntimeError("Invalid pending shared-library consumer: " + str(consumer))
        if any(not isinstance(soname, str) or not soname or not isinstance(provider, str)
               or provider not in libraries for soname, provider in dependencies.items()):
            raise RuntimeError("Invalid pending shared-library providers: " + consumer)
    return pending


def validate_final(manifest):
    if not isinstance(manifest, dict) or manifest.get("schema") != 1 or manifest.get("linkage") != "shared":
        raise RuntimeError("Library SDK has no shared manifest")
    if pending := pending_rebuilds(manifest):
        raise RuntimeError("Shared-library SDK still requires ABI rebuilds: " + ",".join(sorted(pending)))


def selection(inventory, sources, manifest, state, compatibility, libraries="", invalid=(), changed_artifacts=(), deployment_import=None, compiler_runtime=None):
    graph = inventory.get("libraries")
    if not isinstance(graph, dict) or not graph:
        raise RuntimeError("Library inventory has no dependency/recipe graph")
    pending = pending_rebuilds(manifest or {})
    sources = {name: stable_artifact(value) for name, value in sources.items()}
    manual = bool(libraries.strip())
    requested = requested_ids(libraries, graph) if manual else set()
    existing = set((manifest or {}).get("libraries", {})) & graph.keys()
    imported = isinstance(deployment_import, dict) and deployment_import.get("deployment_compatibility") == compatibility and deployment_import.get("compilation_compatibility") == (state or {}).get("compatibility")
    compatible = isinstance(state, dict) and state.get("schema") == 1 and (state.get("compatibility") == compatibility or imported)
    if not compatible:
        if manual:
            raise RuntimeError("Selected library builds require a compatible recorded buildroot; create or restore the library SDK first")
        result = {"schema": 1, "compatibility": compatibility, "full": True, "manual": False,
                "requested": [], "selected": [], "reused": [], "reason": "No compatible recorded library SDK",
                "inventory": inventory, "effective_sources": sources}
        if compiler_runtime is not None:
            result["compiler_runtime_scan"] = compiler_runtime
        return result
    recorded = state.get("libraries", {})
    if not existing or not existing <= recorded.keys():
        raise RuntimeError("Restored SDK input state does not cover its managed libraries")
    managed_artifacts = {record.get("artifact") for record in graph.values()} - {None}
    scope = requested if manual else existing
    selected = set(pending) | (set(invalid) & (closure(requested, graph) if manual else existing))
    for name in scope & existing:
        current = graph[name]
        previous = recorded[name]
        if any(previous.get(field) != current.get(field) for field in ["artifact", "depends", "inputs", "recipe_hash"]) or changed_inputs(current, previous, sources, managed_artifacts):
            selected.add(name)
    if manual:
        selected |= closure(requested, graph) - existing
    else:
        required = set(inventory.get("required_libraries", []))
        if not required <= graph.keys():
            raise RuntimeError("Required library inventory contains unknown managed libraries")
        selected |= closure(required, graph) - existing
    if imported:
        selected |= {name for name in deployment_import.get("must_rebuild", []) if name in existing
                     and recorded[name].get("compiled_compatibility") != compatibility}
    if compiler_runtime is not None:
        selected |= (set(compiler_runtime.get("must_rebuild", [])) | set(compiler_runtime.get("uncertain_stripped_libraries", []))) & existing
        for name in existing:
            current = compiler_runtime.get("libraries", {}).get(name)
            if current is None:
                raise RuntimeError("Managed library has no compiler runtime inspection: " + name)
            previous = recorded[name].get("compiler_runtime")
            if previous is None:
                accepted = (deployment_import or {}).get("libraries", {}).get(name, {}).get("compiler_runtime", {})
                backfill = imported and deployment_import.get("cpp_headers_verified") and accepted.get("fingerprint") == current["fingerprint"]
                if current["uses_cxx_runtime"] and not backfill:
                    selected.add(name)
            elif previous.get("fingerprint") != current["fingerprint"]:
                selected.add(name)
    while True:
        previous = set(selected)
        dependencies = closure(selected, graph)
        selected |= dependencies - existing
        selected |= {name for name in dependencies & existing if name in invalid}
        own_artifacts = {graph[name].get("artifact") for name in selected} - {None}
        selected |= {name for name in existing if graph[name].get("artifact") in own_artifacts}
        if selected == previous:
            break
    reused = existing - selected
    effective = dict(state.get("sources", {}))
    effective.update(sources)
    for name in selected:
        artifact = graph[name].get("artifact")
        if artifact and artifact not in sources:
            effective.pop(artifact, None)
    if manual:
        # Keep unselected sources at their compiled versions while the selected roots build.
        for name in reused:
            artifact = graph[name].get("artifact")
            old = recorded[name].get("sources", {})
            if artifact and artifact in old:
                effective[artifact] = old[artifact]
    result = {"schema": 1, "compatibility": compatibility, "full": False, "manual": manual,
            "requested": sorted(requested), "selected": sorted(selected), "reused": sorted(reused),
            "rebuilt_libraries": [],
            "reason": "Explicit library selection" if manual else "Changed library build inputs",
            "inventory": inventory, "effective_sources": effective, "previous_state": state}
    if imported:
        result["deployment_import"] = deployment_import
    if compiler_runtime is not None:
        result["compiler_runtime_scan"] = compiler_runtime
    return result


def expansion(previous, manifest, state, roots):
    pending = pending_rebuilds(manifest)
    graph = previous["inventory"]["libraries"]
    combined = {name: dict(record) for name, record in graph.items()}
    for name, record in state.get("libraries", {}).items():
        if name in combined:
            combined[name]["depends"] = sorted(set(record.get("depends", [])) | set(combined[name].get("depends", [])))
    for name, record in manifest["libraries"].items():
        combined.setdefault(name, {"depends": []})
        combined[name]["depends"] = sorted(set(combined[name]["depends"]) | set(record.get("depends", [])))
    available = set(manifest["libraries"]) & graph.keys()
    requested = requested_ids(roots, combined)
    requested |= {provider for dependencies in pending.values() for provider in dependencies.values()}
    rebuilt = set(previous.get("rebuilt_libraries", []))
    selected = reverse_closure(requested, combined, available) - rebuilt - requested
    selected = {name for name in selected if any(
        state["libraries"][name].get("dependency_signatures", {}).get(dependency)
        != dependency_signature(dependency, manifest, state["libraries"])
        for dependency in closure([name], combined) & requested
    )}
    if not set(pending) <= available:
        raise RuntimeError("No managed build recipe for pending ABI consumers: " + ",".join(sorted(set(pending) - available)))
    selected |= pending.keys()
    selected |= closure(selected, graph) - available
    return dict(previous, full=False, selected=sorted(selected), reused=sorted(available - selected),
                reason="ABI-affected library rebuild", previous_state=state,
                rebuilt_libraries=sorted(rebuilt))


def signature(path):
    if path.is_symlink():
        return hashlib.sha256(("symlink:" + os.readlink(path)).encode()).hexdigest()
    return hashlib.sha256(path.read_bytes()).hexdigest()


def safe_file(root, relative):
    if not isinstance(relative, str) or Path(relative).is_absolute() or ".." in Path(relative).parts:
        raise RuntimeError("Invalid SDK ownership path: " + repr(relative))
    path = root / relative
    try:
        path.resolve(strict=True).relative_to(root.resolve())
    except (OSError, ValueError):
        raise RuntimeError("Missing or escaping SDK output: " + relative)
    return path


def file_snapshot(root, manifest, names):
    result = {}
    for name in names:
        entry = manifest["libraries"][name]
        files = entry.get("files", [])
        files = files.keys() if isinstance(files, dict) else files
        for relative in files:
            path = safe_file(root, relative)
            actual = signature(path)
            expected = entry.get("file_hashes", {}).get(relative)
            if not expected or actual != expected:
                raise RuntimeError("Restored SDK output checksum mismatch: " + name + ":" + relative)
            stat = path.lstat()
            result[relative] = {"sha256": actual, "mode": stat.st_mode}
            # Alpine ldconfig recreates unchanged SONAME symlinks.
            if not path.is_symlink():
                result[relative]["mtime_ns"] = stat.st_mtime_ns
    return result


def invalid_libraries(root, manifest):
    invalid = set()
    for name in manifest.get("libraries", {}):
        try:
            file_snapshot(root, manifest, [name])
        except RuntimeError:
            invalid.add(name)
    return invalid


def record_state(plan, manifest):
    graph = plan["inventory"]["libraries"]
    previous = plan.get("previous_state", {}).get("libraries", {})
    actual = {}
    for name in manifest["libraries"]:
        if name not in graph:
            continue
        if not plan["full"] and name in plan["reused"]:
            actual[name] = previous[name]
        else:
            actual[name] = dict(graph[name], sources=source_inputs(graph[name], plan["effective_sources"]))
            actual[name]["compiled_compatibility"] = plan["compatibility"]
        runtime = plan.get("compiler_runtime_scan", {}).get("libraries", {}).get(name)
        if runtime is not None:
            if name in plan.get('reused', []) and actual[name].get('compiler_runtime') == compact_runtime(runtime):
                continue
            # This is inspection of actual linkage; it does not relabel reused recipe inputs.
            actual[name] = dict(actual[name], compiler_runtime=compact_runtime(runtime),
                                compiler_runtime_attestation="artifact-scan" if name in plan.get("reused", []) else "rebuilt")
    combined = {name: {"depends": sorted(set(record.get("depends", [])) | set(graph.get(name, {}).get("depends", [])))}
                for name, record in manifest["libraries"].items()}
    for name, record in actual.items():
        if not plan["full"] and name in plan["reused"]:
            continue
        dependencies = closure([name], combined) - {name}
        record["dependency_signatures"] = {
            dependency: dependency_signature(dependency, manifest, actual)
            for dependency in sorted(dependencies) if dependency in manifest["libraries"]
        }
    relevant = {artifact for record in actual.values()
                for artifact in [record.get("artifact"), *record.get("inputs", [])]} - {None}
    sources = {name: value for name, value in plan["effective_sources"].items() if name in relevant}
    compilation = plan.get("previous_state", {}).get("compatibility") if plan.get("deployment_import") else plan["compatibility"]
    result = {"schema": 1, "compatibility": compilation, "sources": sources, "libraries": actual}
    if plan.get("deployment_import"):
        result["deployment_compatibility"] = plan["compatibility"]
    return result


def dependency_signature(name, manifest, actual):
    return {"source_hash": manifest["libraries"].get(name, {}).get("source_hash"),
            "recipe_hash": actual.get(name, {}).get("recipe_hash")}


def execute(root, buildroot, plan, package_type, command=None):
    cache_path = root / "downloads/.cache.json"
    original = cache_path.read_bytes()
    manifest = read(buildroot / "shared-libraries.json", {})
    if not plan["full"]:
        ownership = read(buildroot / ".shared-library-ownership.json", {})
        for name in plan["reused"]:
            owned = ownership.get("libraries", {}).get(name)
            if not isinstance(owned, dict) or owned.get("source_hash") != manifest["libraries"][name].get("source_hash"):
                raise RuntimeError("Restored SDK ownership is incomplete or stale: " + name)
    if not plan['full'] and not plan['selected']:
        current = read(buildroot / 'shared-libraries.json')
        validate_final(current)
        write(buildroot / STATE, record_state(plan, current))
        print('Unchanged SDK: compilation and repeat output/runtime scans skipped', flush=True)
        return {'schema': 1, 'complete': True, 'full': False, 'manual': plan['manual'],
                'selected': [], 'reused': plan['reused'], 'built': False,
                'rebuilt_libraries': plan['rebuilt_libraries'], 'unchanged_output_count': 0, 'command': [],
                'state_sha256': hashlib.sha256((buildroot / STATE).read_bytes()).hexdigest()}
    protected = file_snapshot(buildroot, manifest, plan["reused"]) if not plan["full"] else {}
    selected = plan["selected"]
    ran = bool(plan["full"] or selected)
    if command is None:
        command = ["php", "bin/spp", "build", "--phpv=8.4", "--type=" + package_type, "--libs-only", "--debuginfo"]
        if not plan["full"]:
            command.append("--libraries=" + ",".join(selected))
    try:
        if ran:
            subprocess.run(['python3', str(Path(__file__).with_name('source-snapshot.py')), 'materialize', '--root', str(root)], check=True)
            spec = importlib.util.spec_from_file_location("pinned_library_source", Path(__file__).with_name("pinned-source.py"))
            pinned = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(pinned)
            graph = plan["inventory"]["libraries"]
            force = graph if plan["full"] else selected
            artifacts = {graph[name].get("artifact") for name in force} - {None}
            artifacts &= plan["effective_sources"].keys()
            pinned.ensure_sources(root, plan["effective_sources"], sorted(artifacts))
            write(cache_path, plan["effective_sources"])
            subprocess.run(command, cwd=root, check=True)
            observed = read(cache_path)
            for name in force:
                artifact = graph[name].get("artifact")
                if artifact and artifact in observed:
                    plan["effective_sources"][artifact] = stable_artifact(observed[artifact])
    finally:
        cache_path.write_bytes(original)
    current = read(buildroot / "shared-libraries.json")
    if not isinstance(current, dict) or current.get("schema") != 1 or current.get("linkage") != "shared":
        raise RuntimeError("Library build produced no shared SDK manifest")
    pending = pending_rebuilds(current)
    if not plan["full"]:
        observed = file_snapshot(buildroot, current, plan["reused"])
        if observed != protected:
            changed = sorted(name for name in observed.keys() | protected.keys() if observed.get(name) != protected.get(name))
            raise RuntimeError("A selected build changed unselected library outputs: " + ", ".join(changed[:10]))
    if not plan["full"]:
        for name in plan["reused"]:
            for field in ["source_hash", "version"]:
                if current["libraries"][name].get(field) != manifest["libraries"][name].get(field):
                    raise RuntimeError("A selected build changed unselected library metadata: " + name)
    if plan["full"]:
        selected = sorted(set(current["libraries"]) & plan["inventory"]["libraries"].keys())
        plan["selected"] = selected
    if unresolved := set(selected) & pending.keys():
        raise RuntimeError("Rebuilt libraries still require ABI rebuilds: " + ",".join(sorted(unresolved)))
    for name in selected:
        artifact = plan["inventory"]["libraries"][name].get("artifact")
        source = plan["effective_sources"].get(artifact, {}).get("source", {})
        if artifact and current["libraries"][name].get("source_hash") != source.get("hash"):
            raise RuntimeError("Built library does not match its pinned source: " + name)
    plan["rebuilt_libraries"] = sorted(set(plan.get("rebuilt_libraries", [])) | set(selected))
    if "compiler_runtime_scan" in plan:
        plan["compiler_runtime_scan"] = compiler_runtime_scan(buildroot, current, plan["compiler_runtime_scan"]["compiler_runtime"])
        if selected_static := set(selected) & set(plan["compiler_runtime_scan"].get("must_rebuild", [])):
            raise RuntimeError("Rebuilt libraries still embed the static C++ runtime: " + ",".join(sorted(selected_static)))
    state = record_state(plan, current)
    write(buildroot / STATE, state)
    return {"schema": 1, "complete": True, "full": plan["full"], "manual": plan["manual"],
            "selected": selected, "reused": plan["reused"], "built": ran,
            "rebuilt_libraries": plan["rebuilt_libraries"],
            "unchanged_output_count": len(protected), "command": command if ran else [],
            "state_sha256": hashlib.sha256((buildroot / STATE).read_bytes()).hexdigest()}


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="action", required=True)
    prepare = sub.add_parser("plan")
    prepare.add_argument("--root", type=Path, default=Path("."))
    prepare.add_argument("--buildroot", type=Path)
    prepare.add_argument("--compatibility", required=True)
    prepare.add_argument("--libraries", default="")
    prepare.add_argument("--request", type=Path)
    prepare.add_argument("--output", type=Path, required=True)
    prepare.add_argument("--compiler-runtime-scan", type=Path)
    run = sub.add_parser("run")
    run.add_argument("--root", type=Path, default=Path("."))
    run.add_argument("--buildroot", type=Path)
    run.add_argument("--plan", type=Path, required=True)
    run.add_argument("--type", choices=("rpm", "deb", "apk"), required=True)
    run.add_argument("--command", nargs=argparse.REMAINDER)
    expand = sub.add_parser("expand")
    expand.add_argument("--root", type=Path, default=Path("."))
    expand.add_argument("--buildroot", type=Path)
    expand.add_argument("--plan", type=Path, required=True)
    expand.add_argument("--libraries", required=True)
    expand.add_argument("--output", type=Path, required=True)
    final = sub.add_parser("validate-final")
    final.add_argument("--root", type=Path, default=Path("."))
    final.add_argument("--buildroot", type=Path)
    args = parser.parse_args()
    root = args.root.resolve()
    buildroot = (args.buildroot or root / "buildroot").resolve()
    if args.action == "plan":
        inventory = json.loads(subprocess.check_output(["php", str(Path(__file__).with_name("inventory.php")), str(root)], cwd=root, text=True))
        manifest = read(buildroot / "shared-libraries.json", {})
        sources = read(root / "downloads/.cache.json")
        request = read(args.request or root / "downloads/build-plan.json", {})
        state = read(buildroot / STATE)
        importer = helper_module("sdk-import.py", "sdk_import")
        imported = importer.validate(buildroot, state, args.compatibility) if state is not None else None
        runtime = read(args.compiler_runtime_scan) if args.compiler_runtime_scan else compiler_runtime_scan(buildroot, manifest)
        plan = selection(inventory, sources, manifest, state, args.compatibility,
                         args.libraries, invalid_libraries(buildroot, manifest), request.get("libraries", []), imported, runtime)
        write(args.output, plan)
        print(json.dumps({key: plan[key] for key in ["full", "manual", "requested", "selected", "reused", "reason"]}, sort_keys=True))
    elif args.action == "expand":
        plan = expansion(read(args.plan), read(buildroot / "shared-libraries.json"), read(buildroot / STATE), args.libraries)
        write(args.output, plan)
        print(json.dumps({key: plan[key] for key in ["selected", "rebuilt_libraries", "reused"]}, sort_keys=True))
    elif args.action == "validate-final":
        manifest = read(buildroot / "shared-libraries.json", {})
        validate_final(manifest)
        print(json.dumps({"complete": True, "pending_rebuilds": []}, sort_keys=True))
    else:
        plan = read(args.plan)
        if plan.get("schema") != 1:
            raise RuntimeError("Unsupported incremental library build plan")
        result = execute(root, buildroot, plan, args.type, args.command)
        write(args.plan, plan)
        write(args.plan.with_name("library-build-result.json"), result)
        print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
