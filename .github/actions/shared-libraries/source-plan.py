#!/usr/bin/env python3
"""Create one source-snapshot request shared by all package formats."""

import argparse
import ast
import hashlib
import importlib.util
import json
import re
import subprocess
from pathlib import Path

from workflow import stable_artifact


STREAMS = ("8.2", "8.3", "8.4", "8.5", "8.6")
TOOLS = {"php-src", "php-micro", "cmake", "ninja", "pkg-config", "pkgconf", "re2c", "bison", "go", "rust", "zig", "gcc", "composer"}
CORE_EXTENSIONS = set('calendar,ctype,dom,exif,filter,iconv,libxml,mbregex,mbstring,opcache,openssl,password-argon2,pcntl,phar,posix,readline,session,simplexml,sockets,sodium,tokenizer,xml,xmlreader,xmlwriter,zlib'.split(','))
MODULE_ONLY_EXTENSIONS = {'mysqli', 'mysqlnd', 'shmop', 'soap', 'sqlite3', 'xsl', 'xhprof'}
SPC = "vendor/crazywhalecc/static-php-cli/"
COMPILE_PATTERNS = (
    "spc.registry.yml", "Dockerfile.rhel", "Dockerfile.alpine", "bin/spp",
    "src/artifact/**/*.php", "src/Command/BuildCommand.php", "src/Command/BaseCommand.php",
    "src/CraftConfig.php", "src/util/TwigRenderer.php", "src/util/SpcAutoload.php", "src/step/RunSPC.php",
    "src/hook/**/*.php", "config/templates/craft.yml.twig", "config/patches/*.patch",
    SPC + "src/StaticPHP/Package/Package*.php", SPC + "src/StaticPHP/Package/PhpExtensionPackage.php",
    SPC + "src/StaticPHP/Package/TargetPackage.php", SPC + "src/StaticPHP/Runtime/**/*.php",
    SPC + "src/StaticPHP/Toolchain/**/*.php", SPC + "src/StaticPHP/Registry/**/*.php",
    SPC + "src/StaticPHP/Util/SourcePatcher.php", SPC + "src/StaticPHP/Util/Pgo/**/*.php",
    SPC + "src/StaticPHP/Util/PkgConfigUtil.php", SPC + "src/StaticPHP/Util/SPCConfigUtil.php",
    SPC + "src/StaticPHP/Util/GlobalEnvManager.php", SPC + "src/StaticPHP/Util/GlobalPathTrait.php",
    SPC + "src/StaticPHP/Util/SharedLibraryUtil.php", SPC + "src/StaticPHP/Util/System/**/*.php",
    SPC + "src/globals/functions*.php",
    SPC + "src/Package/Target/php.php", SPC + "src/Package/Target/php/unix.php",
    SPC + "src/Package/Target/php/pgo.php",
)
PACKAGE_PATTERNS = (
    "src/package/**/*.php", "src/ini/**/*", "src/extension.php", "src/package.php",
    "src/step/CreatePackages.php", "src/util/SharedLibraries.php", "src/util/BuildPayload.php",
    "src/util/**/*.php",
    "config/templates/phpmod.twig", "config/templates/pie-wrapper.twig", "config/static-php.repo",
    "bin/apk-control-metadata", "bin/runtime-services", "bin/migration-cutoffs",
)


def file_identity(root, patterns):
    paths = {path for pattern in patterns for path in root.glob(pattern) if path.is_file()}
    return {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest() for path in sorted(paths)}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def referenced_patches(root, files):
    result = {}
    directories = [root / 'config/patches', root / (SPC + 'src/globals/patch'), root / (SPC + 'patches')]
    for relative in files:
        path = root / relative
        if path.suffix != '.php':
            continue
        for literal in re.findall(r"['\"]([^'\"\n]+\.patch)['\"]", path.read_text()):
            literal = re.sub(r'\{?\$[A-Za-z_][A-Za-z0-9_]*\}?', '*', literal)
            candidates = [path.parent / literal, root / literal.lstrip('/')]
            candidates += [directory / literal.lstrip('/') for directory in directories]
            for candidate in candidates:
                for patch in candidate.parent.glob(candidate.name):
                    if patch.is_file() and patch.resolve().is_relative_to(root.resolve()):
                        result[str(patch.relative_to(root))] = hashlib.sha256(patch.read_bytes()).hexdigest()
    return result


def dependency_identity(root):
    lock = root / "composer.lock"
    if not lock.is_file():
        return {}
    data = json.loads(lock.read_text())
    return {package["name"]: {key: package.get(key) for key in ("version", "source", "dist")}
            for package in data.get("packages", [])
            if package.get("name") in {"twig/twig", "symfony/process", "symfony/yaml"}}


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


def extension_definitions(root):
    result = {}
    for directory in [SPC + 'config/pkg/ext', 'config/pkg/ext']:
        for suffix in ['yml', 'yaml', 'json']:
            for path in sorted((root / directory).glob('*.' + suffix)):
                text = path.read_text()
                if suffix == 'json':
                    blocks = {name.removeprefix('ext-'): json.dumps(value, sort_keys=True)
                              for name, value in json.loads(text).items() if name.startswith('ext-')}
                else:
                    matches = list(re.finditer(r'^ext-([A-Za-z0-9_-]+):[^\n]*\n', text, re.M))
                    blocks = {match[1]: text[match.start():matches[index + 1].start() if index + 1 < len(matches) else len(text)]
                              for index, match in enumerate(matches)}
                for name, block in blocks.items():
                    addon = re.search(r'''arg-type["']?\s*:\s*["']?none\b''', block)
                    display = re.search(r'''display-name["']?\s*:\s*["']?([A-Za-z0-9_-]+)''', block)
                    result[name] = {'path': str(path.relative_to(root)), 'text': block,
                                    'hash': hashlib.sha256(block.encode()).hexdigest(),
                                    'build_with_php': bool(re.search(r'(?:build-with-php["\']?\s*:\s*)true\b', block, re.I)),
                                    'external': bool(re.search(r'(?:^\s+source:\s*$|["\']source["\']\s*:)', block, re.M)),
                                    'parent': display[1] if addon and display and display[1] != name else None}
    return result


def shared_components(root):
    definitions = extension_definitions(root)
    template = root / 'config/templates/craft.yml.twig'
    if template.is_file():
        match = re.search(r"set\s+base_shared_str\s*=\s*['\"]([^'\"]*)['\"]", template.read_text())
        if match:
            return {definitions.get(name.strip(), {}).get('parent') or name.strip() for name in match[1].split(',') if name.strip()}
    return {entry.get('parent') or name for name, entry in definitions.items()} - core_components(root) or MODULE_ONLY_EXTENSIONS


def module_only_components(root):
    definitions = extension_definitions(root)
    configured = {name for name, entry in definitions.items() if entry['build_with_php']} if definitions else MODULE_ONLY_EXTENSIONS
    return configured & shared_components(root)


def switch_cases(text):
    switch = re.search(r'\bswitch\s*\(.*?\)\s*\{', text, re.S)
    if switch is None:
        return {}
    opening, position, depth, quote = switch.end() - 1, switch.end(), 1, None
    while position < len(text) and depth:
        character = text[position]
        if quote:
            if character == '\\':
                position += 2
                continue
            if character == quote:
                quote = None
        elif character in "'\"":
            quote = character
        elif text.startswith('//', position):
            position = text.find('\n', position)
            if position < 0:
                break
            continue
        elif text.startswith('/*', position):
            end = text.find('*/', position + 2)
            if end < 0:
                break
            position = end + 2
            continue
        elif character == '{':
            depth += 1
        elif character == '}':
            depth -= 1
        position += 1
    if depth:
        raise RuntimeError('Cannot resolve extension preparation switch for source planning')
    closing = position - 1
    body = text[opening + 1:closing]
    labels = list(re.finditer(r'''^[ \t]*case\s+['"]([A-Za-z0-9_-]+)['"]\s*:[ \t]*$''', body, re.M))
    if (re.search(r'^[ \t]*default\s*:', body, re.M)
            or len(labels) != len(re.findall(r'^[ \t]*case\b', body, re.M))):
        raise RuntimeError('Cannot resolve extension-specific source preparation branches')
    result, grouped = {}, []
    for index, label in enumerate(labels):
        grouped.append(label[1])
        branch = body[label.end():labels[index + 1].start() if index + 1 < len(labels) else len(body)]
        if not branch.strip():
            continue
        for name in grouped:
            prefix = re.sub(r'''^[ \t]*#\[BeforeStage\(['"]ext-([^'"]+)['"][^\n]*\n''',
                            lambda match: match[0] if match[1] == name else '', text[:opening + 1], flags=re.M)
            result[name] = prefix + '\ncase ' + repr(name) + ':' + branch + text[closing:]
        grouped = []
    return result


def scoped_hook_inputs(root):
    result = {}
    for path in root.glob('src/hook/*.php'):
        relative, text = str(path.relative_to(root)), path.read_text()
        if path.name == 'BuildSelectedPhpModules.php':
            actors = module_only_components(root)
        elif path.name == 'ReuseConsumerExtensions.php':
            actors = shared_components(root)
        elif path.name == 'PrepareReusedExtensionSources.php':
            cases = switch_cases(text)
            if not cases:
                raise RuntimeError('Cannot resolve extension preparation actors for source planning')
            result[relative] = {name: hashlib.sha256(value.encode()).hexdigest() for name, value in cases.items()}
            continue
        elif path.name == 'FrankenPhp86.php':
            actors = {'frankenphp'}
        elif path.name in {'Rar.php', 'Ev.php', 'Ds.php'}:
            actors = {path.stem.lower()}
        else:
            actors = set(re.findall(r'''\#\[(?:Extension|BeforeStage)\(['"](?:ext-)?([A-Za-z0-9_-]+)['"]''', text))
            actors.discard('php')
        if actors:
            result[relative] = {name: hashlib.sha256(text.encode()).hexdigest() for name in actors}
    return result


def identity(root):
    files = file_identity(root, COMPILE_PATTERNS)
    if 'bin/spp' in files:
        # Packaging destinations do not change the compiler inputs of existing payloads.
        launcher = (root / 'bin/spp').read_bytes().replace(
            b"define('DIST_PATH', getenv('SPP_DIST_PATH') ?: BASE_PATH . '/dist');",
            b"const DIST_PATH = BASE_PATH . '/dist';")
        files['bin/spp'] = hashlib.sha256(launcher).hexdigest()
    scoped = scoped_hook_inputs(root)
    files = {name: value for name, value in files.items()
             if name not in scoped
             and not name.startswith("config/patches/franken")}
    for path in root.glob(SPC + 'src/Package/Extension/**/*.php'):
        if path.stem.replace('_', '-') in core_components(root):
            files[str(path.relative_to(root))] = hashlib.sha256(path.read_bytes()).hexdigest()
    for name, entry in extension_definitions(root).items():
        if name.replace('_', '-') in core_components(root):
            files[entry['path'] + '#' + name] = entry['hash']
    files.update(referenced_patches(root, files))
    return digest({"schema": 2, "files": files, "dependencies": dependency_identity(root)})


def package_identity(root):
    files = file_identity(root, PACKAGE_PATTERNS)
    for name in ['src/package/library.php', 'src/util/ModuleOnlyBuild.php', 'src/util/ConsumerSnapshot.php']:
        files.pop(name, None)
    return digest({"schema": 1, "files": files})


def library_package_identity(root):
    return digest(file_identity(root, ['src/package/library.php', 'src/util/SharedLibraries.php']))


def format_package_identities(root):
    return {}


def variant_identities(root):
    path = root / '.github/actions/shared-libraries/openssl-variants.py'
    if not path.is_file():
        return {}
    tree = ast.parse(path.read_text())
    body, profiles, recipe_schema = [], {}, None
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id == 'PROFILES' for target in node.targets):
            profiles = ast.literal_eval(node.value)
        elif isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id == 'BUILD_RECIPE_SCHEMA' for target in node.targets):
            recipe_schema = ast.literal_eval(node.value)
            body.append(node)
        else:
            body.append(node)
    engine = recipe_schema if recipe_schema is not None else digest(ast.dump(ast.Module(body=body, type_ignores=[]), include_attributes=False))
    return {profile: digest({'engine': engine, 'openssldir': directory}) for profile, directory in profiles.items()}


def component_identities(root):
    paths = {}
    for pattern, prefix in [("config/pkg/ext/ext-*.yml", "ext-"), (SPC + "src/Package/Extension/**/*.php", "")]:
        for path in root.glob(pattern):
            if path.is_file():
                name = path.relative_to(root / (SPC + "src/Package/Extension")).parts[0] if prefix == "" else path.stem
                name = name.removesuffix(".php").removeprefix(prefix)
                paths.setdefault(name, {})[str(path.relative_to(root))] = hashlib.sha256(path.read_bytes()).hexdigest()
    for name, patterns in {"rar": ["src/hook/Rar.php"], "frankenphp": ["src/hook/FrankenPhp86.php", "config/patches/franken*.patch", SPC + "src/Package/Target/php/frankenphp.php"]}.items():
        paths.setdefault(name, {}).update(file_identity(root, patterns))
    definitions = extension_definitions(root)
    for name, entry in definitions.items():
        key = entry['path'] + '#' + name if entry['path'].startswith(SPC) else entry['path']
        paths.setdefault(entry.get('parent') or name, {}).setdefault(key, entry['hash'])
    for name, entry in definitions.items():
        if entry.get('parent') and name in paths:
            paths.setdefault(entry['parent'], {}).update(paths.pop(name))
    for relative, actors in scoped_hook_inputs(root).items():
        for name, value in actors.items():
            paths.setdefault(name, {})[relative] = value
    for relative, actors in [('src/util/ModuleOnlyBuild.php', module_only_components(root)),
                             ('src/util/ConsumerSnapshot.php', shared_components(root) | {'frankenphp'})]:
        if (root / relative).is_file():
            value = hashlib.sha256((root / relative).read_bytes()).hexdigest()
            for name in actors:
                paths.setdefault(name, {})[relative] = value
    repair = file_identity(root, ['.github/actions/shared-libraries/rehydrate-php-extensions.py'])
    active = active_components(root)
    selected = set().union(*active.values()) if active is not None else None
    result = {}
    for name, files in paths.items():
        if (not files or name.replace('_', '-') in core_components(root)
                or selected is not None and name.replace('_', '-') not in selected):
            continue
        values = {}
        for stream in STREAMS:
            inputs = dict(files, **referenced_patches(root, files))
            bundled = not definitions.get(name, {}).get('external', True)
            if name == 'zip':
                bundled = tuple(map(int, stream.split('.'))) >= (8, 5)
            if bundled:
                inputs.update(repair)
            values[stream] = digest(inputs)
        result[name] = next(iter(values.values())) if len(set(values.values())) == 1 else {'streams': values}
    return result


def resolved_components(components, stream):
    return {name: value['streams'][stream] if isinstance(value, dict) and 'streams' in value else value
            for name, value in components.items()}


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


def stream_artifacts(artifacts, metadata, stream):
    overrides = metadata.get('extension_sources', {}).get(stream, {})
    names = {name for name in artifacts if name.startswith('ext-')} | overrides.keys()
    result = {}
    for name in names:
        value = overrides[name] if name in overrides else (artifacts.get(name) or {}).get('source')
        result[name.removeprefix('ext-')] = stable_artifact({'source': value})
    for name in ('frankenphp', 'go', 'go-xcaddy'):
        if name in artifacts:
            result[name] = stable_artifact(artifacts[name])
    return result


def plan(previous, current, previous_meta, current_meta, force=False, configuration="", library_ids=None, build_inputs=None,
         package_configuration=None, components=None, library_package_configuration=None, variants=None, compiled_in=None, active=None,
         format_package_configuration=None):
    compiled_in = CORE_EXTENSIONS if compiled_in is None else compiled_in
    changed = sorted(key for key in previous.keys() | current.keys() if stable_artifact(previous.get(key)) != stable_artifact(current.get(key)))
    package_only = ["pie"] if any(previous_meta.get(key) != current_meta.get(key) for key in ['pie_version', 'pie_sha256']) else []
    libraries = [key for key in changed if (key in library_ids if library_ids is not None else key not in TOOLS and not key.startswith("ext-") and key != "frankenphp")]
    library_build_inputs = []
    if build_inputs is not None:
        recipes = {name: item.get("recipe_hash") for name, item in build_inputs.get("libraries", {}).items()}
        previous_recipes = previous_meta.get("shared_library_recipes", {})
        libraries += [name for name in recipes.keys() | previous_recipes.keys() if recipes.get(name) != previous_recipes.get(name)]
        managed = set(build_inputs["managed_library_artifacts"])
        required = managed | set(build_inputs["library_tool_artifacts"])
        excluded = set(build_inputs["irrelevant_artifacts"]) - required
        library_build_inputs = [key for key in changed if key not in managed and key not in excluded]
    full = force or previous_meta.get("shared_configuration") != configuration
    requests, package_requests = {}, {}
    package_change = package_configuration is not None and previous_meta.get("shared_package_configuration") != package_configuration
    for stream in STREAMS:
        recipe_packages = []
        if components is not None:
            old_components = resolved_components(previous_meta.get('shared_components', {}), stream)
            current_components = resolved_components(components, stream)
            recipe_packages = [name for name in old_components.keys() | current_components.keys()
                               if old_components.get(name) != current_components.get(name)]
        previous_artifacts = stream_artifacts(previous, previous_meta, stream)
        current_artifacts = stream_artifacts(current, current_meta, stream)
        stream_changes = {name for name in previous_artifacts.keys() | current_artifacts.keys()
                          if previous_artifacts.get(name) != current_artifacts.get(name)}
        stream_changes = {'frankenphp' if name in ('go', 'go-xcaddy') else name for name in stream_changes}
        stream_full = (full or any(name.replace('_', '-') in compiled_in for name in stream_changes)
                       or previous_meta.get("php_versions", {}).get(stream) != current_meta.get("php_versions", {}).get(stream)
                       or previous_meta.get("php_source_hashes", {}).get(stream, {}) != current_meta.get("php_source_hashes", {}).get(stream, {}))
        stream_packages = {name for name in stream_changes if component_used(name, stream, artifact=True, active=active)}
        stream_packages.update(name for name in recipe_packages if component_used(name, stream, active=active))
        if stream_full or stream_packages:
            requests[stream] = {"full": bool(stream_full), "packages": [] if stream_full else sorted(stream_packages)}
        if package_change or package_only:
            package_requests[stream] = {"full": bool(package_change), "packages": [] if package_change else sorted(set(package_only))}
    component_inputs = {}
    for stream in STREAMS:
        component_sources = stream_artifacts(current, current_meta, stream)
        recipes = {name: value for name, value in resolved_components(components or {}, stream).items()
                   if component_used(name, stream, active=active)}
        values = dict(recipes)
        values.update({name: digest({"recipe": recipes.get(name), "source": value}) for name, value in component_sources.items()
                       if component_used(name, stream, artifact=True, active=active) and name.replace('_', '-') not in compiled_in})
        component_inputs[stream] = values
    base_inputs = {stream: digest({"configuration": configuration, "php": current_meta.get("php_versions", {}).get(stream),
                                  "php_source": current_meta.get("php_source_hashes", {}).get(stream),
                                  'core_artifacts': {name: value for name, value in stream_artifacts(current, current_meta, stream).items() if name.replace('_', '-') in compiled_in}}) for stream in STREAMS}
    library_package_change = library_package_configuration is not None and previous_meta.get('shared_library_package_configuration') != library_package_configuration
    package_libraries = sorted((build_inputs or {}).get('libraries', {})) if library_package_change else []
    old_variants = previous_meta.get('shared_variant_configuration', {})
    variant_requests = {profile: ['openssl'] for profile in old_variants.keys() | (variants or {}).keys()
                        if old_variants.get(profile) != (variants or {}).get(profile)} if variants is not None else {}
    format_requests = {}
    old_formats = previous_meta.get('shared_format_package_configuration', {})
    if format_package_configuration is not None:
        for kind in old_formats.keys() | format_package_configuration.keys():
            old, new = old_formats.get(kind, {}), format_package_configuration.get(kind, {})
            if old.get('identity') != new.get('identity'):
                packages = sorted(set(old.get('packages', [])) | set(new.get('packages', [])))
                format_requests[kind] = {stream: {'full': False, 'packages': packages} for stream in STREAMS}
    return {"schema": 1, "requests": requests, "package_requests": package_requests,
            'format_package_requests': format_requests,
            'library_package_requests': package_libraries, 'variant_package_requests': variant_requests,
            "libraries": sorted(set(libraries)), "library_build_inputs": library_build_inputs, "configuration": configuration,
            "package_configuration": package_configuration, "payload_core_inputs": base_inputs,
            "payload_component_inputs": component_inputs,
            "payload_inputs": {stream: digest({"core": base_inputs[stream], "components": component_inputs[stream]}) for stream in STREAMS}}


def reconcile(original, sources, metadata, configuration, packaging, components, inventory, library_packaging, variants, compiled_in=None, active=None,
              format_package_configuration=None):
    effective = plan(sources, sources, metadata, metadata, configuration=configuration,
                     package_configuration=packaging, components=components, build_inputs=inventory,
                     library_ids=set(inventory['managed_library_artifacts']),
                     library_package_configuration=library_packaging, variants=variants, compiled_in=compiled_in, active=active,
                     format_package_configuration=format_package_configuration)
    for field in ('requests', 'package_requests'):
        for stream, request in original.get(field, {}).items():
            current = effective[field].setdefault(stream, {'full': False, 'packages': []})
            current['full'] |= bool(request.get('full'))
            current['packages'] = [] if current['full'] else sorted(set(current['packages']) | set(request.get('packages', [])))
    for field in ('libraries', 'library_build_inputs', 'library_package_requests'):
        effective[field] = sorted(set(effective[field]) | set(original.get(field, [])))
    for kind, names in original.get('variant_package_requests', {}).items():
        effective['variant_package_requests'][kind] = sorted(set(effective['variant_package_requests'].get(kind, [])) | set(names))
    for kind, requests in original.get('format_package_requests', {}).items():
        for stream, request in requests.items():
            current = effective['format_package_requests'].setdefault(kind, {}).setdefault(stream, {'full': False, 'packages': []})
            current['full'] |= bool(request.get('full'))
            current['packages'] = [] if current['full'] else sorted(set(current['packages']) | set(request.get('packages', [])))
    effective['build_inputs'] = inventory
    effective['snapshot_configuration'] = original.get('configuration')
    return effective


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument("--force-full", action="store_true")
    parser.add_argument('--reconcile', action='store_true', help='Reconcile current checkout recipes with an immutable pinned source snapshot')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    root = args.root.resolve()
    read = lambda name: json.loads((root / name).read_text())
    metadata = read("downloads/.spc-meta.json")
    metadata["php_source_hashes"] = php_archive_inputs(root, metadata, verify=args.reconcile)
    metadata['extension_sources'] = extension_source_inputs(root)
    configuration = identity(root)
    packaging = package_identity(root)
    components = component_identities(root)
    library_packaging = library_package_identity(root)
    variants = variant_identities(root)
    inventory = json.loads(subprocess.check_output(["php", str(Path(__file__).with_name("inventory.php")), str(root)], cwd=root, universal_newlines=True))
    library_ids = set(inventory["managed_library_artifacts"])
    if args.reconcile:
        if args.output is None:
            parser.error('--reconcile requires --output; pinned snapshot files must remain immutable')
        request = reconcile(read('downloads/build-plan.json'), read('downloads/.cache.json'), metadata,
                            configuration, packaging, components, inventory, library_packaging, variants, core_components(root), active_components(root),
                            format_package_identities(root))
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(request, sort_keys=True, indent=2) + '\n')
        print('effective-plan=' + str(args.output))
        return
    request = plan(read(".download-state/lock.prev.json"), read("downloads/.cache.json"), read(".download-state/meta.prev.json"), metadata, args.force_full, configuration, library_ids, inventory, packaging, components, library_packaging, variants, core_components(root), active_components(root), format_package_identities(root))
    request["build_inputs"] = inventory
    metadata["shared_configuration"] = configuration
    metadata["shared_package_configuration"] = packaging
    metadata["shared_components"] = components
    metadata["shared_library_recipes"] = {name: item.get("recipe_hash") for name, item in inventory.get("libraries", {}).items()}
    metadata['shared_library_package_configuration'] = library_packaging
    metadata['shared_variant_configuration'] = variants
    metadata['shared_format_package_configuration'] = format_package_identities(root)
    (root / "downloads/.spc-meta.json").write_text(json.dumps(metadata, sort_keys=True) + "\n")
    (root / "downloads/build-plan.json").write_text(json.dumps(request, sort_keys=True, indent=2) + "\n")
    print("any_change=" + str(bool(request["requests"] or request["package_requests"] or request["libraries"] or request["library_build_inputs"]
                                   or request['library_package_requests'] or request['variant_package_requests'] or request['format_package_requests'])).lower())
    print("changed_libraries=" + ",".join(request["libraries"]))


if __name__ == "__main__":
    main()
