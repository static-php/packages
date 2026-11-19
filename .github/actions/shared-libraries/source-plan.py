#!/usr/bin/env python3
"""Create one source-snapshot request shared by all package formats."""

import argparse
import hashlib
import importlib.util
import json
import re
import runpy
import subprocess
import sys
from pathlib import Path

from workflow import stable_artifact


STREAMS = ("8.2", "8.3", "8.4", "8.5", "8.6")
TOOLS = {"php-src", "php-micro", "cmake", "ninja", "pkg-config", "pkgconf", "re2c", "bison", "go", "rust", "zig", "gcc", "composer"}
CORE_EXTENSIONS = set('calendar,ctype,dom,exif,filter,iconv,libxml,mbregex,mbstring,opcache,openssl,password-argon2,pcntl,phar,posix,readline,session,simplexml,sockets,sodium,tokenizer,xml,xmlreader,xmlwriter,zlib'.split(','))
def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def core_components(root):
    path = root / 'config/templates/craft.yml.twig'
    if not path.is_file():
        return CORE_EXTENSIONS
    text = path.read_text()
    match = re.search(r"set\s+base_extensions_str\s*=\s*['\"]([^'\"]*)['\"]", text)
    if match:
        return {name.strip().replace('_', '-') for name in match[1].split(',') if name.strip()}
    if 'base_extensions_str' in text:
        raise RuntimeError('Cannot resolve compiled-in extension selection for source planning')
    return CORE_EXTENSIONS



def active_components(root):
    template = root / 'config/templates/craft.yml.twig'
    if not template.is_file() or 'base_shared_str' not in template.read_text():
        return None
    spec = importlib.util.spec_from_file_location('source_payload_profile', Path(__file__).with_name('payload-profile.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    result = {}
    for stream in STREAMS:
        profile = module.expected_profile(root, stream)
        result[stream] = {name.replace('_', '-') for name in profile['shared'] + profile['extensions']}
        if 'frankenphp' in profile['sapis']:
            result[stream].update(['frankenphp', 'go', 'go-xcaddy'])
    return result


def component_used(name, stream, artifact=False, active=None):
    version = tuple(int(value) for value in stream.split('.'))
    if active is not None and name.replace('_', '-') not in active[stream]:
        return False
    return not (version >= (8, 5) and (name == 'memcache' or artifact and name == 'zip'))


def php_archive_inputs(root, metadata, verify=False):
    frozen_path = root / 'downloads/.php-sources.json'
    if frozen_path.is_file():
        frozen = json.loads(frozen_path.read_text())
        if frozen.get('schema') != 1 or not isinstance(frozen.get('sources'), dict):
            raise RuntimeError('Invalid frozen PHP source inventory')
        actual = {}
        for stream, version in metadata.get('php_versions', {}).items():
            record = frozen['sources'].get(version, {})
            if not record:
                actual[stream] = {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                                  for path in sorted((root / 'downloads').glob('php-' + version + '.tar.*')) if path.is_file()}
                continue
            name = record.get('source', {}).get('filename', '')
            if not name or Path(name).name != name:
                raise RuntimeError('Missing pinned PHP source archive for ' + version)
            path = root / 'downloads' / name
            checksum = hashlib.sha256(path.read_bytes()).hexdigest()
            if checksum != record.get('archive_sha256'):
                raise RuntimeError('Pinned PHP source checksum changed: ' + name)
            actual[stream] = {name: checksum}
    else:
        actual = {stream: {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                           for path in sorted((root / 'downloads').glob('php-' + version + '.tar.*')) if path.is_file()}
                  for stream, version in metadata.get('php_versions', {}).items()}
    expected = metadata.get('php_source_hashes')
    if verify and expected is not None and expected != actual:
        raise RuntimeError('Pinned PHP source archives changed or are missing')
    return actual


def extension_source_inputs(root):
    path = root / 'downloads/.php-sources.json'
    if not path.is_file():
        return {}
    value = json.loads(path.read_text()).get('extensions', {})
    if not isinstance(value, dict) or any(not isinstance(records, dict) for records in value.values()):
        raise RuntimeError('Invalid frozen extension source inventory')
    return value


def extension_patch_inputs(root, sources, overrides):
    directory = root / 'config/patches/php86-extensions'
    manifest = directory / 'patches.json'
    patches = {}
    if manifest.is_file():
        for name, versions in json.loads(manifest.read_text()).items():
            record = overrides.get('8.6', {}).get(name, sources.get(name, {}).get('source', {}))
            files = versions.get(record.get('version'), [])
            if files:
                patches[name] = [hashlib.sha256((directory / file).read_bytes()).hexdigest() for file in files]
    return {'8.6': patches}


def stream_artifacts(artifacts, metadata, stream):
    overrides = metadata.get('extension_sources', {}).get(stream, {})
    names = {name for name in artifacts if name.startswith('ext-')} | overrides.keys()
    result = {}
    for name in names:
        value = overrides[name] if name in overrides else (artifacts.get(name) or {}).get('source')
        component = stable_artifact({'source': value})
        patches = metadata.get('extension_patches', {}).get(stream, {}).get(name)
        if patches:
            component['patches'] = patches
        result[name.removeprefix('ext-')] = component
    for name in ('frankenphp',):
        if name in artifacts:
            result[name] = stable_artifact(artifacts[name])
    return result


def source_delta(before, after):
    for field in ('version', 'filename', 'hash'):
        old = (before or {}).get(field)
        new = (after or {}).get(field)
        if old != new:
            return f"{old or '(none)'} -> {new or '(none)'}"
    return 'source inputs changed'


def artifact_delta(before, after):
    old = before.get('source') if isinstance(before, dict) else None
    new = after.get('source') if isinstance(after, dict) else None
    return source_delta(old, new)


def plan(previous, current, previous_meta, current_meta, force=False, library_ids=None,
         compiled_in=None, active=None, log=None):
    compiled_in = CORE_EXTENSIONS if compiled_in is None else compiled_in
    changed = sorted(key for key in previous.keys() | current.keys() if stable_artifact(previous.get(key)) != stable_artifact(current.get(key)))
    package_only = ["pie"] if any(previous_meta.get(key) != current_meta.get(key) for key in ['pie_version', 'pie_sha256']) else []
    libraries = [key for key in changed if (key in library_ids if library_ids is not None else key not in TOOLS and not key.startswith("ext-") and key != "frankenphp")]
    library_build_inputs = []
    full = force
    requests, package_requests = {}, {}
    if log is not None:
        if force:
            print('Full rebuild requested for all PHP streams', file=log)
        for name in libraries:
            change = artifact_delta(previous.get(name), current.get(name))
            print(f'Library {name} {change}', file=log)
        if package_only:
            old = {'version': previous_meta.get('pie_version'), 'hash': previous_meta.get('pie_sha256')}
            new = {'version': current_meta.get('pie_version'), 'hash': current_meta.get('pie_sha256')}
            print(f'PIE {source_delta(old, new)} (package only, all PHP streams)', file=log)
    for stream in STREAMS:
        previous_artifacts = stream_artifacts(previous, previous_meta, stream)
        current_artifacts = stream_artifacts(current, current_meta, stream)
        stream_changes = {name for name in previous_artifacts.keys() | current_artifacts.keys()
                          if previous_artifacts.get(name) != current_artifacts.get(name)}
        stream_changes = {'frankenphp' if name in ('go', 'go-xcaddy') else name for name in stream_changes}
        core_changes = {name for name in stream_changes if name.replace('_', '-') in compiled_in}
        old_php = previous_meta.get('php_versions', {}).get(stream)
        new_php = current_meta.get('php_versions', {}).get(stream)
        archive_changed = previous_meta.get('php_source_hashes', {}).get(stream, {}) != current_meta.get('php_source_hashes', {}).get(stream, {})
        stream_full = full or bool(core_changes) or old_php != new_php or archive_changed
        stream_packages = {name for name in stream_changes if component_used(name, stream, artifact=True, active=active)}
        if log is not None:
            if old_php != new_php:
                print(f"PHP {stream} release {old_php or '(none)'} -> {new_php or '(none)'} (full build)", file=log)
            elif archive_changed:
                print(f'PHP {stream} source archive changed (full build)', file=log)
            for name in sorted(stream_packages | core_changes):
                change = artifact_delta(previous_artifacts.get(name), current_artifacts.get(name))
                scope = ' (full build)' if name in core_changes else ''
                print(f'PHP {stream} / {name} {change}{scope}', file=log)
        if stream_full or stream_packages:
            requests[stream] = {"full": bool(stream_full), "packages": [] if stream_full else sorted(stream_packages)}
        if package_only:
            package_requests[stream] = {"full": False, "packages": package_only}
    component_inputs = {}
    for stream in STREAMS:
        component_sources = stream_artifacts(current, current_meta, stream)
        values = {name: digest(value) for name, value in component_sources.items()
                  if component_used(name, stream, artifact=True, active=active) and name.replace('_', '-') not in compiled_in}
        component_inputs[stream] = values
    base_inputs = {stream: digest({"php": current_meta.get("php_versions", {}).get(stream),
                                  "php_source": current_meta.get("php_source_hashes", {}).get(stream),
                                  'core_artifacts': {name: value for name, value in stream_artifacts(current, current_meta, stream).items() if name.replace('_', '-') in compiled_in}}) for stream in STREAMS}
    return {"schema": 1, "requests": requests, "package_requests": package_requests,
            'source_comparison_available': bool(previous_meta.get('php_versions')), 'force_full': bool(force),
            'format_package_requests': {}, 'library_package_requests': [], 'variant_package_requests': {},
            "libraries": sorted(set(libraries)), "library_build_inputs": library_build_inputs, "payload_core_inputs": base_inputs,
            "payload_component_inputs": component_inputs,
            "payload_inputs": {stream: digest({"core": base_inputs[stream], "components": component_inputs[stream]}) for stream in STREAMS}}


def reconcile(original, sources, metadata, inventory, compiled_in=None, active=None):
    effective = plan(sources, sources, metadata, metadata, library_ids=set(inventory['managed_library_artifacts']), compiled_in=compiled_in, active=active)
    for stream, request in original.get('requests', {}).items():
        current = effective['requests'].setdefault(stream, {'full': False, 'packages': []})
        current['full'] |= bool(request.get('full'))
        current['packages'] = [] if current['full'] else sorted(set(current['packages']) | set(request.get('packages', [])))
    for stream, request in original.get('package_requests', {}).items():
        if 'pie' in request.get('packages', []):
            effective['package_requests'][stream] = {'full': False, 'packages': ['pie']}
    effective['libraries'] = sorted(set(effective['libraries']) | set(original.get('libraries', [])))
    effective['build_inputs'] = inventory
    return effective


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument("--force-full", action="store_true")
    parser.add_argument('--reconcile', action='store_true', help='Preserve the immutable pinned source update selection')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    root = args.root.resolve()
    read = lambda name: json.loads((root / name).read_text())
    metadata = read("downloads/.spc-meta.json")
    metadata["php_source_hashes"] = php_archive_inputs(root, metadata, verify=args.reconcile)
    metadata['extension_sources'] = extension_source_inputs(root)
    if not args.reconcile:
        metadata['extension_patches'] = extension_patch_inputs(root, read('downloads/.cache.json'), metadata['extension_sources'])
    inventory = json.loads(subprocess.check_output(["php", str(Path(__file__).with_name("inventory.php")), str(root)], cwd=root, universal_newlines=True))
    library_ids = set(inventory["managed_library_artifacts"])
    if args.reconcile:
        if args.output is None:
            parser.error('--reconcile requires --output; pinned snapshot files must remain immutable')
        original = read('downloads/build-plan.json')
        if not original.get('source_comparison_available'):
            snapshot = read('downloads/.source-snapshot.json')
            helper = runpy.run_path(str(Path(__file__).with_name('source-state.py')))
            helper['restore'](root, snapshot['repository'], snapshot['run_id'], allow_empty=original.get('force_full', False))
            original = plan(read('.download-state/lock.prev.json'), read('downloads/.cache.json'),
                            read('.download-state/meta.prev.json'), metadata, force=original.get('force_full', False),
                            library_ids=library_ids, compiled_in=core_components(root), active=active_components(root))
        request = reconcile(original, read('downloads/.cache.json'), metadata, inventory, core_components(root), active_components(root))
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(request, sort_keys=True, indent=2) + '\n')
        print('effective-plan=' + str(args.output))
        return
    request = plan(read(".download-state/lock.prev.json"), read("downloads/.cache.json"), read(".download-state/meta.prev.json"), metadata,
                   force=args.force_full, library_ids=library_ids,
                   compiled_in=core_components(root), active=active_components(root), log=sys.stderr)
    request["build_inputs"] = inventory
    for field in ('shared_configuration', 'shared_package_configuration', 'shared_library_package_configuration',
                  'shared_variant_configuration', 'shared_format_package_configuration'):
        metadata.pop(field, None)
    (root / "downloads/.spc-meta.json").write_text(json.dumps(metadata, sort_keys=True) + "\n")
    (root / "downloads/build-plan.json").write_text(json.dumps(request, sort_keys=True, indent=2) + "\n")
    print("any_change=" + str(bool(request["requests"] or request["package_requests"] or request["libraries"] or request["library_build_inputs"]
                                   or request['library_package_requests'] or request['variant_package_requests'] or request['format_package_requests'])).lower())
    print("changed_libraries=" + ",".join(request["libraries"]))


if __name__ == "__main__":
    main()
