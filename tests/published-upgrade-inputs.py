#!/usr/bin/env python3
"""Freeze a retained pre-update inventory and the live replacement for native upgrades."""

import argparse
import json
from pathlib import Path
import runpy
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
BASELINE = runpy.run_path(str(ROOT / 'bin/freeze-migration-baseline.py'))


def prepare(kind, stream, arch, metadata, output):
    directory = metadata if kind == 'apk' else metadata / kind
    inventory = directory / 'native-inventories' / (stream + '.json')
    plan = json.loads((directory / 'shared-library-plan.json').read_text())
    old = json.loads(inventory.read_text())
    names = set(plan['expected_consumers'][stream]) | {'php-zts-imagick'}
    args = SimpleNamespace(type=kind, php=stream, arch=arch, el='el8', base_url=None,
        inventory=inventory, profile='all', packages=','.join(names), library_plan=None,
        include_debug=False, output=output / 'old')
    current = BASELINE['snapshot_inventory'](args)
    old_latest = BASELINE['latest_packages'](kind, old['inventory'])
    latest = BASELINE['latest_packages'](kind, current['inventory'])
    capabilities = {capability for library in plan['libraries'] for capability in
        ['php-zts-lib-' + library, *old_latest.get('php-zts-lib-' + library, {}).get('provides', [])]}
    names.update(name for name, package in old_latest.items()
        if not name.startswith('php-zts-lib-') and not name.endswith('-debuginfo')
        and capabilities.intersection(package.get('requires', []))
        and name in latest and package['version'] != latest[name]['version'])
    names = sorted(names)
    args.packages = ','.join(names)
    selected = set(names) | {'php-zts-lib-' + name for name in plan['libraries']}
    changed_unrelated = {}
    for name, before in old_latest.items():
        base = name.removesuffix('-debuginfo')
        if base in selected or not (name.startswith(('php-zts-', 'libphp-zts-')) or name in ('frankenphp', 'pie-zts')):
            continue
        after = latest.get(name)
        if after is None or before['version'] != after['version']:
            changed_unrelated[name] = [before['version'], after['version'] if after else None]
    if changed_unrelated:
        raise RuntimeError('Unrelated published package versions changed: ' + json.dumps(changed_unrelated, sort_keys=True))
    if old_latest['php-zts-imagick']['version'] != latest['php-zts-imagick']['version']:
        raise RuntimeError('Imagick changed during the library update')
    if old_latest['php-zts-lib-openssl']['version'].startswith('4.'):
        raise RuntimeError('Retained baseline already contains OpenSSL 4')
    if not latest['php-zts-lib-openssl']['version'].startswith('4.'):
        raise RuntimeError('Live replacement does not contain OpenSSL 4')
    print('Published inventory audit passed; unrelated versions, including Imagick, are unchanged.', flush=True)
    output.mkdir(parents=True, exist_ok=True)
    current_path = output / 'live-inventory.json'
    current_path.write_text(json.dumps(current) + '\n')
    BASELINE['freeze'](args)
    args.inventory, args.output = current_path, output / 'new'
    BASELINE['freeze'](args)
    for label in ('old', 'new'):
        frozen = json.loads((output / label / 'baseline.json').read_text())
        if frozen['defects']:
            raise RuntimeError(label + ' inventory cannot form a complete upgrade fixture: ' + json.dumps(frozen['defects']))
    evidence = {'type': kind, 'php_stream': stream, 'architecture': arch,
        'old_openssl': old_latest['php-zts-lib-openssl']['version'],
        'new_openssl': latest['php-zts-lib-openssl']['version'],
        'imagick_before': old_latest['php-zts-imagick']['version'],
        'imagick_after': latest['php-zts-imagick']['version'],
        'unrelated_versions_unchanged': True, 'selected_consumers': names}
    (output / 'inventory-audit.json').write_text(json.dumps(evidence, indent=2) + '\n')
    print(json.dumps(evidence, sort_keys=True), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--type', choices=('rpm', 'deb', 'apk'), required=True)
    parser.add_argument('--php', required=True)
    parser.add_argument('--arch', choices=('x86_64', 'arm64'), required=True)
    parser.add_argument('--metadata', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    prepare(args.type, args.php, args.arch, args.metadata, args.output)


if __name__ == '__main__':
    main()
