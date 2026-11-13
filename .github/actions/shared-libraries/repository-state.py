#!/usr/bin/env python3
"""Published repository metadata maintained by successful CI publications."""

import copy
import gzip
import hashlib
from functools import lru_cache
import importlib.util
import json
import os
from pathlib import Path
import runpy
import tempfile
from types import SimpleNamespace
import uuid


HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
STREAMS = ('8.2', '8.3', '8.4', '8.5', '8.6')
_backend = None


def enabled():
    return os.environ.get('GITHUB_ACTIONS') == 'true' and bool(os.environ.get('GITHUB_REPOSITORY'))


def backend():
    global _backend
    if _backend is None:
        spec = importlib.util.spec_from_file_location('release_cache', HERE.parent / 'buildroot-cache/cache.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _backend = module.GitHub(os.environ['GITHUB_REPOSITORY'], os.environ.get('GH_TOKEN') or os.environ.get('GITHUB_TOKEN', ''))
    return _backend


def name(kind, arch, stream):
    if kind not in ('rpm', 'deb', 'apk') or arch not in ('x86_64', 'arm64') or stream not in STREAMS:
        raise ValueError('Invalid repository state target')
    return 'repository-' + kind + '-' + arch + ('' if kind == 'rpm' else '-php' + stream.replace('.', '')) + '.json.gz'


def load(kind, arch, stream):
    if not enabled():
        return None
    asset = name(kind, arch, stream)
    directory = Path('stage/repository-state')
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / asset
    if not path.exists() and not backend().download('cache', asset, path):
        if not backend().download('cache', asset + '.previous', path):
            return None
    value = json.loads(gzip.decompress(path.read_bytes()))
    if (value.get('schema') != 1 or value.get('type') != kind or value.get('architecture') != arch
            or kind != 'rpm' and value.get('php_stream') != stream):
        raise RuntimeError('Repository state does not match its target')
    return value


def save(value):
    asset = name(value['type'], value['architecture'], value['php_stream'])
    service = backend()
    release = service.release('cache')
    with tempfile.TemporaryDirectory(prefix='spp-repository-state-') as temporary:
        path = Path(temporary) / asset
        content = path_bytes(value)
        path.write_bytes(content)
        staged = service.upload(release, asset + '.upload-' + uuid.uuid4().hex[:12], path)
        previous = service.find_asset(release, asset + '.previous')
        current = service.find_asset(release, asset)
        if current:
            if previous:
                service.delete(previous['id'])
            service.rename(current['id'], asset + '.previous')
        service.rename(staged['id'], asset)
        previous = service.find_asset(release, asset + '.previous')
        if previous:
            service.delete(previous['id'])
    local = Path('stage/repository-state') / asset
    local.parent.mkdir(parents=True, exist_ok=True)
    local.write_bytes(content)


def path_bytes(value):
    return gzip.compress(json.dumps(value, sort_keys=True).encode(), mtime=0)


def snapshot(kind, arch, stream):
    value = load(kind, arch, stream)
    return copy.deepcopy(value.get('snapshots', {}).get(stream)) if value else None


def invalidate(kind, arch, stream):
    if not enabled():
        return
    asset = name(kind, arch, stream)
    service = backend()
    release = service.release('cache')
    for suffix in ('', '.previous'):
        current = service.find_asset(release, asset + suffix)
        if current:
            service.delete(current['id'])
    (Path('stage/repository-state') / asset).unlink(missing_ok=True)


def restore_baseline(kind, arch, streams, destination, consumers=False, libraries=None):
    states = [load(kind, arch, stream) for stream in (streams if kind != 'rpm' else streams[:1])]
    if not states or any(value is None for value in states):
        return False
    planner = runpy.run_path(str(ROOT / 'bin/shared-library-plan'))
    baselines = []
    for state in states:
        value = state['baseline']
        scope = value.get('metadata_scope')
        if scope is not None and (libraries is None or set(libraries) - set(scope)):
            return False
        if consumers and not value.get('consumer_inventory_complete'):
            return False
    destination.mkdir(parents=True, exist_ok=True)
    for state in states:
        value = copy.deepcopy(state['baseline'])
        if libraries is not None:
            value['libraries'] = {key: entry for key, entry in value['libraries'].items() if key in libraries}
            value['metadata_scope'] = sorted(libraries)
            for owner, entries in value.get('stream_libraries', {}).items():
                value['stream_libraries'][owner] = {key: entry for key, entry in entries.items() if key in libraries}
        directory = destination if kind == 'rpm' else destination / 'owners' / state['php_stream']
        directory.mkdir(parents=True, exist_ok=True)
        (directory / 'shared-libraries.json').write_text(json.dumps(value))
        baselines.append((value, directory))
    merged, divergent = planner['merge_baselines'](baselines, kind, destination, independent_streams=kind != 'rpm')
    merged['divergent_libraries'] = sorted(divergent)
    (destination / 'shared-libraries.json').write_text(json.dumps(merged, sort_keys=True))
    print('Restored published ' + kind + ' repository state; no package scan.', flush=True)
    return True


def materialize(root, entry, archive='published_archive', checksum='published_archive_sha256'):
    relative = Path(entry[archive])
    if relative.is_absolute() or '..' in relative.parts:
        raise RuntimeError('Invalid published archive path')
    path = Path(root) / relative
    if not path.is_file():
        url = entry.get('download_url')
        if not url:
            return path
        freezer = runpy.run_path(str(ROOT / 'bin/freeze-migration-baseline.py'))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(freezer['fetch'](url))
    if entry.get(checksum) and hashlib.sha256(path.read_bytes()).hexdigest() != entry[checksum]:
        raise RuntimeError('Published archive checksum mismatch: ' + str(relative))
    return path


def record_publication(source, kind, target, stream):
    if not enabled():
        return
    arch = os.environ['MATRIX_ARCH']
    library = source / ('shared-libs-' + kind + '-' + target)
    previous = load(kind, arch, stream)
    baseline = library / 'baseline'
    owner = baseline / 'owners' / stream / 'shared-libraries.json'
    baseline_file = owner if kind != 'rpm' and owner.is_file() else baseline / 'shared-libraries.json'
    captured_baseline = json.loads(baseline_file.read_text())
    if previous is None:
        previous = {'schema': 1, 'type': kind, 'architecture': arch, 'php_stream': stream,
                    'baseline': captured_baseline, 'snapshots': {}}
    value = copy.deepcopy(previous)
    value['baseline']['libraries'] = {**captured_baseline['libraries'], **value['baseline']['libraries']}
    if captured_baseline.get('metadata_scope') is None:
        value['baseline'].pop('metadata_scope', None)
    planner = runpy.run_path(str(ROOT / 'bin/shared-library-plan'))
    freezer = runpy.run_path(str(ROOT / 'bin/freeze-migration-baseline.py'))
    compare = lru_cache(maxsize=None)(planner['native_compare'])
    freezer['published_inventory'].__globals__['fetch'] = lru_cache(maxsize=None)(freezer['fetch'])
    for selected in STREAMS if kind == 'rpm' else (stream,):
        args = SimpleNamespace(type=kind, arch=arch, php=selected, el='el8', base_url=None)
        value['snapshots'][selected] = freezer['snapshot_inventory'](args, refresh=True)
    inventory = value['snapshots'][stream]['inventory']
    baseline = value['baseline']
    uploaded_consumers = {}
    version_of = lambda item: (str(item.get('epoch', '0')) + ':' if kind == 'rpm' else '') + item['version']
    indexed = {(item['name'], version_of(item)): item for item in inventory}
    for archive_path in sorted((Path('dist') / kind).glob('*.' + kind)):
        package_name, version, package_arch = planner['archive_identity'](archive_path, kind)
        package = indexed.get((package_name, version))
        if package is None:
            package = next((item for item in inventory if item['name'] == package_name
                            and compare(kind, version_of(item), version) == 0), None)
        if package is None:
            raise RuntimeError('Published package missing from native repository index: ' + package_name)
        checksum = hashlib.sha256(archive_path.read_bytes()).hexdigest()
        published = package.get('published_checksum', {})
        indexed = freezer['apk_control_checksum'](archive_path.read_bytes()) if kind == 'apk' else hashlib.new(published.get('algorithm', 'sha256'), archive_path.read_bytes()).hexdigest()
        if published.get('value') and indexed != published['value']:
            raise RuntimeError('Published index differs from the uploaded archive: ' + package_name)
        reference = {'download_url': package['publication_url'], 'published_archive': 'archives/' + archive_path.name,
                     'published_archive_sha256': checksum, 'published_archive_filename': archive_path.name,
                     'published_version': version}
        if package_name.startswith('php-zts-lib-') and not package_name.endswith('-debuginfo'):
            identifier = package_name[len('php-zts-lib-'):]
            entry = planner['archive_runtime_json'](archive_path, kind, planner['METADATA'] + '/' + identifier + '.json')
            if entry is None:
                raise RuntimeError('Published library metadata is missing: ' + package_name)
            for field in ('id', 'runtime_libdir', 'architecture', 'libc'):
                entry.pop(field, None)
            entry.update(reference)
            debug = next((item for item in inventory if item['name'] == package_name + '-debuginfo' and item['version'] == package['version']), None)
            entry['published_debug_required'] = debug is not None
            if debug:
                entry['published_debug_archive'] = {'type': kind, 'name': debug['name'], 'package_version': version,
                    'architecture': debug['architecture'], 'download_url': debug['publication_url'],
                    'checksum': debug['published_checksum']}
            baseline['libraries'][identifier] = entry
        elif package_name == 'php-zts-runtime-layout':
            layout = planner['archive_runtime_json'](archive_path, kind, 'usr/share/php-zts/runtime-layout.json')
            baseline['runtime_layout'] = dict(layout, archive=reference['published_archive'], archive_sha256=checksum,
                                              package_version=version, complete=True, download_url=package['publication_url'])
        elif not package_name.endswith(('-debuginfo', '-dbgsym')):
            uploaded_consumers[package_name] = package
            if package_name == 'php-zts-cli':
                cutoffs = planner['archive_migration_cutoffs'](archive_path, kind)
                if cutoffs:
                    if cutoffs.get('php_stream') != stream:
                        raise RuntimeError('Published CLI migration metadata has the wrong PHP stream')
                    baseline.setdefault('migration_cutoffs', {})[stream] = cutoffs['formats'].get(kind, {})
            metadata = planner['archive_runtime_json'](archive_path, kind, 'usr/share/php-zts/consumers/' + package_name + '.json')
            if metadata:
                dependencies = planner['consumer_dependencies'](metadata, package_name, stream, arch)
                baseline.setdefault('consumer_dependencies', {}).setdefault(stream, {})[package_name] = dependencies
                if metadata.get('extension') and metadata.get('logical_name') != package_name:
                    baseline.setdefault('consumer_components', {}).setdefault(stream, {})[package_name] = metadata['extension']
    for selected, captured in value['snapshots'].items():
        consumers = [item for item in captured['inventory'] if not item['name'].startswith('php-zts-lib-')
                     and item['name'] != 'php-zts-runtime-layout' and not item['name'].endswith(('-debuginfo', '-dbgsym'))]
        known = baseline.get('consumer_native_metadata', {}).get(selected)
        retained = baseline.get('consumer_inventory_complete') and isinstance(known, dict)
        latest = dict(known) if retained else {}
        if retained:
            consumers = uploaded_consumers.values() if selected == stream else ()
        for item in consumers:
            if item['name'] not in latest or version_of(item) != version_of(latest[item['name']]) and compare(kind, version_of(item), version_of(latest[item['name']])) > 0:
                latest[item['name']] = item
        baseline.setdefault('consumers', {})[selected] = sorted(latest)
        baseline.setdefault('consumer_versions', {})[selected] = {name: version_of(item) for name, item in latest.items()}
        baseline.setdefault('consumer_native_metadata', {})[selected] = latest
        dependencies = baseline.setdefault('consumer_dependencies', {}).setdefault(selected, {})
        for package_name, item in latest.items():
            dependencies[package_name] = sorted(set(dependencies.get(package_name, [])) | {
                name[len('php-zts-lib-'):] for name in item.get('requires', []) if name.startswith('php-zts-lib-')})
    baseline['consumer_inventory_complete'] = True
    baseline['consumer_inventory_streams'] = sorted(value['snapshots'])
    if kind != 'rpm':
        baseline['stream_libraries'] = {stream: copy.deepcopy(baseline['libraries'])}
    for identifier, entry in baseline['libraries'].items():
        if not entry.get('download_url'):
            match = next((item for item in inventory if item['name'] == 'php-zts-lib-' + identifier
                          and compare(kind, item['version'], entry.get('published_version', '')) == 0), None)
            if match:
                entry['download_url'] = match['publication_url']
    layout = baseline.get('runtime_layout')
    if layout and not layout.get('download_url'):
        match = next((item for item in inventory if item['name'] == 'php-zts-runtime-layout'
                      and compare(kind, item['version'], layout['package_version']) == 0), None)
        if match:
            layout['download_url'] = match['publication_url']
    value['publication_run_id'] = os.environ.get('GITHUB_RUN_ID')
    save(value)
