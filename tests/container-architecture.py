#!/usr/bin/env python3

import os
import re


def architecture(value):
    return {'amd64': 'x86_64', 'aarch64': 'arm64'}.get(value, value)


def engine_identity(command, target=None, allow_emulation=False):
    host = architecture(command(['docker', 'info', '--format', '{{.Architecture}}']).stdout.strip())
    target = architecture(target or host)
    if target not in ('x86_64', 'arm64') or host not in ('x86_64', 'arm64'):
        raise RuntimeError('Unsupported Docker engine/target architecture: {}/{}'.format(host, target))
    if allow_emulation and os.environ.get('GITHUB_ACTIONS') == 'true':
        raise RuntimeError('--allow-emulation is restricted to local validation; CI requires a native Docker engine')
    if host != target and not allow_emulation:
        raise RuntimeError('Native {} validation requires a native {} Docker engine; found {}'.format(target, target, host))
    engine = command(['docker', 'info', '--format', '{{.ID}}']).stdout.strip()
    if not engine:
        raise RuntimeError('Cannot identify the Docker engine')
    return {'docker_engine_id': engine, 'docker_engine_architecture': host,
            'target_architecture': target, 'allow_emulation': bool(allow_emulation),
            'native': host == target, 'emulated': host != target}


def container_options(identity):
    options = ['--platform', 'linux/amd64' if identity['target_architecture'] == 'x86_64' else 'linux/arm64']
    if identity['emulated']:
        options += ['--mount', 'type=bind,src=/proc/sys/fs/binfmt_misc,dst=/spp-binfmt,readonly']
    return options


def resource_limits(role, runtime=None):
    memory = 384 if role == 'repo' else 768
    if runtime in ('alma8', 'almalinux:8'):
        memory = 1024
    return {'memory_mib': memory, 'swap_mib': memory, 'cpus': 1}


def resource_options(limits):
    return ['--memory', str(limits['memory_mib']) + 'm', '--memory-swap', str(limits['swap_mib']) + 'm',
            '--cpus', str(limits['cpus'])]


def container_identity(command, container, identity, limits=None):
    guest = architecture(command(['docker', 'exec', container, 'uname', '-m']).stdout.strip())
    image = command(['docker', 'inspect', '--format', '{{.Image}}', container]).stdout.strip()
    if not re.fullmatch('sha256:[0-9a-f]{64}', image):
        raise RuntimeError('Cannot pin the running runtime image ID')
    image_arch = architecture(command(['docker', 'image', 'inspect', '--format', '{{.Architecture}}', image]).stdout.strip())
    if guest != identity['target_architecture'] or image_arch != guest:
        raise RuntimeError('Running guest/image architecture differs from the requested target')
    result = {'guest_architecture': guest, 'image_architecture': image_arch, 'image_id': image}
    if limits is not None:
        actual = command(['docker', 'inspect', '--format', '{{.HostConfig.Memory}} {{.HostConfig.MemorySwap}} {{.HostConfig.NanoCpus}}', container]).stdout.strip()
        expected = [limits['memory_mib'] * 1024 * 1024, limits['swap_mib'] * 1024 * 1024, limits['cpus'] * 1000000000]
        if actual.split() != list(map(str, expected)):
            raise RuntimeError('Docker runtime resource limits differ from the prepared bounds')
        result['resource_limits'] = dict(limits, host_swap_allowed=False)
    if identity['emulated']:
        target = 'aarch64' if guest == 'arm64' else 'x86_64'
        status = command(['docker', 'exec', container, 'cat', '/spp-binfmt/status']).stdout.strip()
        for registration in ('qemu-' + target, target):
            checked = command(['docker', 'exec', container, 'cat', '/spp-binfmt/' + registration], check=False)
            contents = checked.stdout.strip()
            interpreter = re.search(r'^interpreter (/.+)$', contents, re.M)
            flags = re.search(r'^flags:\s*(\S+)', contents, re.M)
            if checked.returncode or status != 'enabled' or contents.splitlines()[:1] != ['enabled'] or interpreter is None or flags is None:
                continue
            result['daemon_binfmt'] = {'registration': registration, 'status': status,
                                      'interpreter': interpreter.group(1), 'flags': flags.group(1),
                                      'contents': contents, 'source': '/proc/sys/fs/binfmt_misc', 'read_only': True}
            break
        else:
            raise RuntimeError('The Docker daemon has no enabled {} binfmt registration'.format(target))
    return result
