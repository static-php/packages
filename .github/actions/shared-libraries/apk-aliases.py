import gzip
import hashlib
import json
import re
import tarfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]


def aliases():
    value = json.loads((ROOT / 'config/apk-runtime-aliases.json').read_text())
    if value.get('schema') != 1 or not isinstance(value.get('extensions'), dict):
        raise RuntimeError('Invalid APK runtime alias configuration')
    result = {}
    for extension, config in value['extensions'].items():
        if not re.fullmatch(r'[a-z0-9][a-z0-9_]*', extension) or config != {'suffix': '-shared'}:
            raise RuntimeError('Unsupported APK runtime alias configuration')
        result[extension] = config['suffix']
    return result


def logical_name(name):
    debug = '-debuginfo' if name.endswith('-debuginfo') else ''
    base = name.removesuffix(debug) if debug else name
    for extension, suffix in aliases().items():
        if base.endswith('-' + extension + suffix) and base.startswith('php-'):
            return base.removesuffix(suffix) + debug
    return name


def physical_name(name):
    debug = '-debuginfo' if name.endswith('-debuginfo') else ''
    base = name.removesuffix(debug) if debug else name
    for extension, suffix in aliases().items():
        if base.endswith('-' + extension) and base.startswith('php-'):
            return base + suffix + debug
    return name


def extension_name(name):
    logical = logical_name(name).removesuffix('-debuginfo')
    return next((extension for extension in aliases() if logical.endswith('-' + extension)), None)


def archive_evidence(path):
    result = None
    module = None
    with gzip.GzipFile(filename=str(path)) as stream, tarfile.open(fileobj=stream, mode='r|', ignore_zeros=True) as archive:
        for member in archive:
            name = member.name.removeprefix('./').lstrip('/')
            if name == '.PKGINFO':
                if result is not None or not member.isfile() or member.size > 1024 * 1024:
                    raise RuntimeError('Invalid APK control metadata')
                fields = {}
                for line in archive.extractfile(member).read().decode().splitlines():
                    if ' = ' in line:
                        key, value = line.split(' = ', 1)
                        fields.setdefault(key, []).append(value)
                result = {'name': fields.get('pkgname', [None])[0], 'version': fields.get('pkgver', [None])[0],
                          'arch': fields.get('arch', [None])[0], 'depends': fields.get('depend', []),
                          'provides': fields.get('provides', []), 'replaces': fields.get('replaces', [])}
                extension = extension_name(result['name']) if isinstance(result['name'], str) else None
                if extension is None or logical_name(result['name']) == result['name'] or result['name'].endswith('-debuginfo'):
                    return result
                module = re.compile(r'usr/lib/php-zts/modules/' + re.escape(extension) + r'(?:-[A-Za-z0-9_-]+)?\.so')
            elif module is not None and module.fullmatch(name):
                if not member.isfile() or result.get('module_sha256'):
                    raise RuntimeError('Invalid APK shared module payload')
                digest = hashlib.sha256()
                with archive.extractfile(member) as data:
                    for block in iter(lambda: data.read(1024 * 1024), b''):
                        digest.update(block)
                result['module_path'] = name
                result['module_sha256'] = digest.hexdigest()
    if result is None:
        raise RuntimeError('APK archive has no control metadata')
    return result


def verify_alias(evidence, metadata=None, core=None):
    physical = evidence.get('name', '')
    logical = logical_name(physical)
    if logical == physical or physical_name(logical) != physical or physical.endswith('-debuginfo'):
        raise RuntimeError('APK package is not a configured runtime alias')
    if (logical not in evidence.get('provides', [])
            or any(value.startswith(logical + '=') for value in evidence.get('provides', []))
            or '!' + logical + '>0' not in evidence.get('depends', [])
            or logical not in evidence.get('replaces', [])
            or not re.fullmatch('[a-f0-9]{64}', evidence.get('module_sha256', ''))):
        raise RuntimeError('APK runtime alias lacks native replacement relationships or module bytes')
    capabilities = [value for value in evidence.get('depends', []) if re.fullmatch(r'php-[a-z0-9-]+-shared-runtime-\d+-abi\d+-\d+', value)]
    if len(capabilities) != 1:
        raise RuntimeError('APK runtime alias must depend on one shared core generation')
    if metadata is not None and (metadata.get('name') != physical or metadata.get('logical_name') != logical
                                 or metadata.get('extension') != extension_name(physical)
                                 or metadata.get('module_sha256') != evidence['module_sha256']):
        raise RuntimeError('APK runtime alias metadata differs from its compiled module')
    if core is not None and ('!' + logical + '>0' not in core.get('depends', []) or capabilities[0] not in core.get('provides', [])):
        raise RuntimeError('APK shared CLI does not enforce the runtime alias replacement')
    return {'logical_name': logical, 'name': physical, 'core_capability': capabilities[0],
            'module_sha256': evidence['module_sha256']}


def effective_records(records, core):
    result = list(records)
    replacements = {}
    for record in records:
        evidence = record.get('native_metadata', {})
        if logical_name(record['name']) == record['name'] or record['name'].endswith('-debuginfo'):
            continue
        proof = verify_alias(evidence, record.get('consumer_metadata'), core)
        replacements[proof['logical_name']] = proof['name']
    return [record for record in result if record['name'] not in replacements], replacements
