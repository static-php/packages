#!/usr/bin/env python3
"""Keep a filtered PHP build from impersonating a complete reusable payload."""

import hashlib
import json
from pathlib import Path
import re
import shlex


def names(value):
    if value is None:
        return []
    if isinstance(value, str):
        return [item.strip() for item in value.split(',') if item.strip()]
    if isinstance(value, list) and all(isinstance(item, str) and item for item in value):
        return list(dict.fromkeys(value))
    raise RuntimeError('Invalid PHP payload profile list')


def expected_profile(root, stream):
    text = (Path(root) / 'config/templates/craft.yml.twig').read_text()
    result = {}
    for variable, key in [('base_sapi_str', 'sapis'), ('base_shared_str', 'shared'), ('base_extensions_str', 'extensions')]:
        match = re.search(r'\bset\s+' + variable + r'''\s*=\s*(['"])(.*?)\1''', text, re.S)
        if match is None:
            if key == 'extensions' and variable not in text:
                result[key] = []
                continue
            raise RuntimeError('Cannot resolve the complete PHP payload profile: ' + variable)
        result[key] = names(match[2])
    version = tuple(int(value) for value in stream.split('.'))
    if version >= (8, 5):
        result['shared'] = [name for name in result['shared'] if name != 'memcache']
    if version >= (8, 6):
        result['shared'] = [name for name in result['shared'] if name != 'swoole']
    return result


def extension_metadata(root, include_suggests=False):
    result = {}
    paths = []
    for directory in ['vendor/crazywhalecc/static-php-cli/config/pkg/ext', 'config/pkg/ext']:
        paths.extend(sorted((Path(root) / directory).glob('*.yml')))
        paths.extend(sorted((Path(root) / directory).glob('*.yaml')))
    for path in paths:
        text = path.read_text()
        blocks = list(re.finditer(r'^ext-([A-Za-z0-9_-]+):[^\n]*\n', text, re.M))
        for index, match in enumerate(blocks):
            end = blocks[index + 1].start() if index + 1 < len(blocks) else len(text)
            block = text[match.end():end]
            addon = re.search(r'^\s+arg-type:\s*[\'\"]?none[\'\"]?\s*$', block, re.M)
            display = re.search(r'^\s+display-name:\s*[\'\"]?([A-Za-z0-9_-]+)', block, re.M)
            fields = {}
            field = None
            for line in block.splitlines():
                direct = re.match(r'^  ([A-Za-z0-9_@-]+):\s*(.*)$', line)
                if direct:
                    field = direct[1]
                    fields[field] = [direct[2]]
                elif field is not None and line.startswith('    '):
                    fields[field].append(line)
                elif line.strip():
                    field = None
            dependencies = []
            keys = ['depends', 'depends@unix', 'depends@linux']
            if include_suggests:
                keys += ['suggests', 'suggests@unix', 'suggests@linux']
            for key in keys:
                value = '\n'.join(fields.get(key, []))
                dependencies += re.findall(r'^\s+-\s+ext-([A-Za-z0-9_-]+)\s*$', value, re.M)
                if value.strip().startswith('['):
                    dependencies += re.findall(r'\bext-([A-Za-z0-9_-]+)', value)
            result[match[1]] = {'addon': bool(addon and display and display[1] != match[1]),
                                'depends': sorted(set(dependencies))}
    return result


def coverage(record, root=None):
    configuration = record.get('configuration')
    if not isinstance(configuration, dict) or record.get('kind', 'consumer') != 'consumer':
        raise RuntimeError('A PHP consumer cache needs recorded consumer configuration')
    sapis = names(configuration.get('sapi'))
    if set(sapis) - {'cli', 'cgi', 'fpm', 'embed', 'frankenphp', 'phpdbg'}:
        raise RuntimeError('Unsupported SAPI in PHP cache profile')
    shared = names(configuration.get('shared-extensions'))
    skipped_manifest = record.get('skipped_shared_extensions')
    skipped = set()
    if skipped_manifest is not None:
        if (not isinstance(skipped_manifest, dict) or skipped_manifest.get('schema') != 1
                or not isinstance(skipped_manifest.get('skipped', []), list)):
            raise RuntimeError('Invalid recorded PHP shared-extension skip profile')
        version = re.match(r'^(\d+)\.(\d+)\.(\d+)', record.get('php_version', ''))
        expected_id = int(version[1]) * 10000 + int(version[2]) * 100 + int(version[3]) if version else None
        if expected_id is None or skipped_manifest.get('php_version_id') != expected_id:
            raise RuntimeError('Recorded skip profile belongs to a different PHP source version')
        for item in skipped_manifest.get('skipped', []):
            if not isinstance(item, dict) or not isinstance(item.get('extension'), str):
                raise RuntimeError('Invalid recorded skipped PHP extension')
            skipped.add(item['extension'])
        if skipped and (skipped_manifest.get('allow_shared_ext_failure') is not True
                        or configuration.get('build-options', {}).get('allow-shared-ext-failure') is not True):
            raise RuntimeError('Missing modules cannot use an unauthorized skip profile')
    if skipped - set(shared):
        raise RuntimeError('Skipped extension does not belong to the recorded PHP profile')
    metadata = extension_metadata(root) if root is not None else {}
    while True:
        dependent = {name for name in shared if set(metadata.get(name, {}).get('depends', [])) & skipped}
        if dependent <= skipped:
            break
        skipped |= dependent
    addons = {name for name in shared if metadata.get(name, {}).get('addon')}
    return {'sapis': sorted(set(sapis)), 'shared': sorted(set(shared)),
            'extensions': sorted(set(names(configuration.get('extensions')))),
            'modules': sorted(set(shared) - skipped - addons), 'skipped': sorted(skipped),
            'addons': sorted(addons)}


def suffix(record):
    configuration = record['configuration']
    flags = configuration.get('extra-env', {}).get('SPC_CMD_VAR_PHP_MAKE_EXTRA_LDFLAGS', '')
    values = shlex.split(flags) if isinstance(flags, str) else []
    if '-release' in values:
        index = values.index('-release') + 1
        if index >= len(values) or not re.fullmatch(r'[A-Za-z0-9_.+-]+', values[index]):
            raise RuntimeError('Invalid recorded PHP shared-library release suffix')
        return '-' + values[index]
    return None


def php_file_allowed(relative, record, root=None, profile=None):
    profile = profile if profile is not None else coverage(record, root)
    sapis = set(profile['sapis'])
    if relative == 'bin/php':
        return 'cli' in sapis
    for sapi, filename in [('cgi', 'bin/php-cgi'), ('fpm', 'bin/php-fpm'), ('phpdbg', 'bin/phpdbg'), ('frankenphp', 'bin/frankenphp')]:
        if relative == filename:
            return sapi in sapis
    if relative.startswith('packaging/'):
        return (relative in {'packaging/pie/pie.phar', 'packaging/pie/metadata.json'}
                or relative.startswith('packaging/php-module-build/')
                or relative.startswith('packaging/spx/') and 'spx' in profile['modules']
                or relative.startswith('packaging/frankenphp/') and 'frankenphp' in sapis)
    if relative in ('bin/phpize', 'bin/php-config') or relative.startswith(('include/php/', 'lib/php/build/')):
        return 'cli' in sapis
    release = suffix(record)
    if relative.startswith('modules/'):
        if '/' in relative[len('modules/'):]:
            return False
        allowed = {'modules/' + name + (release or '') + '.so' for name in profile['modules']}
        return relative in allowed
    if relative.startswith('lib/libphp') and ('embed' in sapis or 'frankenphp' in sapis):
        embed_type = record['configuration'].get('extra-env', {}).get('SPC_CMD_VAR_PHP_EMBED_TYPE', 'shared')
        if embed_type == 'static':
            return relative == 'lib/libphp.a'
        return relative in {'lib/libphp.so', 'lib/libphp' + (release or '') + '.so'}
    return False


def verify_expected_profile(record, expected, root=None):
    actual = coverage(record, root)
    for key in ['sapis', 'shared', 'extensions']:
        missing = set(expected[key]) - set(actual[key])
        extra = set(actual[key]) - set(expected[key])
        if missing or extra:
            raise RuntimeError('Filtered PHP payload cannot use the complete cache key: {} missing={} extra={}'.format(key, sorted(missing), sorted(extra)))
    files = record.get('files')
    if not isinstance(files, dict):
        raise RuntimeError('Recorded PHP profile has no ELF inventory')
    if 'cli' in actual['sapis']:
        assets = record.get('packaging_assets', {})
        for path in ['bin/phpize', 'bin/php-config', 'include/php/Zend/zend_modules.h']:
            if path not in assets:
                raise RuntimeError('Complete PHP cache is missing its development asset: ' + path)
    for sapi, path in [('cli', 'bin/php'), ('cgi', 'bin/php-cgi'), ('fpm', 'bin/php-fpm'), ('phpdbg', 'bin/phpdbg'), ('frankenphp', 'bin/frankenphp')]:
        if sapi in actual['sapis'] and path not in files:
            raise RuntimeError('Recorded PHP profile is missing its built SAPI: ' + sapi)
    if {'embed', 'frankenphp'} & set(actual['sapis']) and not any(name.startswith('lib/libphp') and php_file_allowed(name, record, root) for name in files):
        raise RuntimeError('Recorded PHP profile is missing its built embed library')
    release = suffix(record)
    for module in actual['modules']:
        if 'modules/' + module + (release or '') + '.so' not in files:
            raise RuntimeError('Recorded PHP profile is missing its built module: ' + module)
    return actual


def cache_export_coverage(record, seed, root=None, stream=None):
    if record.get('build_context', {}).get('coverage') == 'incomplete':
        raise RuntimeError('An incomplete PHP build checkpoint cannot be exported as a complete consumer cache')
    stream = stream or '.'.join(record.get('php_version', '').split('.')[:2])
    if not re.fullmatch(r'\d+\.\d+', stream):
        raise RuntimeError('Cannot determine the PHP cache profile stream')
    if '.'.join(record.get('php_version', '').split('.')[:2]) != stream:
        raise RuntimeError('Recorded PHP payload belongs to a different profile stream')
    expected = expected_profile(root, stream) if root is not None else {
        key: coverage(record)[key] for key in ['sapis', 'shared', 'extensions']}
    actual = verify_expected_profile(record, expected, root)
    if (record.get('build_context', {}).get('kind') == 'partial-consumer-build'
            and (seed.get('state') != 'verified' or seed.get('coverage', {}).get('full') is not True)):
        raise RuntimeError('Partial PHP build needs verified complete seed coverage before cache export')
    profile = {key: sorted(expected[key]) for key in ['sapis', 'shared', 'extensions']}
    return dict(actual, schema=1, full=True,
                profile_fingerprint=hashlib.sha256(json.dumps(profile, sort_keys=True, separators=(',', ':')).encode()).hexdigest())
