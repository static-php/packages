"""Execution-mode and runtime identity checks without Docker mutations."""

import os
from pathlib import Path
import runpy
import subprocess
import unittest
from unittest.mock import patch


scope = runpy.run_path(str(Path(__file__).with_name('container-architecture.py')))
IMAGE = 'sha256:' + '1' * 64


class ContainerArchitecture(unittest.TestCase):
    def command(self, arguments):
        values = {'{{.Architecture}}': 'x86_64', '{{.ID}}': 'engine-fixture'}
        return subprocess.CompletedProcess(arguments, 0, values[arguments[-1]], '')

    def test_cross_arch_requires_explicit_local_opt_in(self):
        with self.assertRaisesRegex(RuntimeError, 'native arm64 Docker engine'):
            scope['engine_identity'](self.command, 'arm64')
        with patch.dict(os.environ, {'GITHUB_ACTIONS': 'false'}):
            execution = scope['engine_identity'](self.command, 'arm64', True)
        self.assertFalse(execution['native'])
        self.assertTrue(execution['emulated'])
        self.assertEqual(execution['docker_engine_architecture'], 'x86_64')
        self.assertEqual(execution['target_architecture'], 'arm64')

    def test_explicit_opt_in_cannot_relax_native_ci_enforcement(self):
        with patch.dict(os.environ, {'GITHUB_ACTIONS': 'true'}):
            with self.assertRaisesRegex(RuntimeError, 'CI requires a native'):
                scope['engine_identity'](self.command, 'arm64', True)

    def test_native_mode_records_engine_identity_without_binfmt_mount(self):
        execution = scope['engine_identity'](self.command, 'x86_64')
        self.assertTrue(execution['native'])
        self.assertFalse(execution['emulated'])
        self.assertEqual(scope['container_options'](execution), ['--platform', 'linux/amd64'])

    def runtime(self, *, guest='aarch64', image_arch='arm64', registration=None, registration_name='qemu-aarch64',
                resources='805306368 805306368 1000000000'):
        registration = registration if registration is not None else 'enabled\ninterpreter /usr/bin/qemu-aarch64\nflags: POCF\noffset 0\nmagic abc'
        def command(arguments, **kwargs):
            if arguments[1] == 'exec':
                value = guest if arguments[3:] == ['uname', '-m'] else 'enabled' if arguments[-1] == '/spp-binfmt/status' else registration
                if arguments[3] == 'cat' and arguments[-1] != '/spp-binfmt/status' and arguments[-1] != '/spp-binfmt/' + registration_name:
                    return subprocess.CompletedProcess(arguments, 1, '', 'No such file')
            elif arguments[1] == 'image':
                value = image_arch
            elif any('HostConfig.Memory' in value for value in arguments):
                value = resources
            else:
                value = IMAGE
            return subprocess.CompletedProcess(arguments, 0, value, '')
        return command

    def test_emulated_receipt_binds_actual_guest_image_and_daemon_registration(self):
        execution = {'native': False, 'emulated': True, 'target_architecture': 'arm64'}
        options = scope['container_options'](execution)
        self.assertEqual(options[:2], ['--platform', 'linux/arm64'])
        self.assertIn('type=bind,src=/proc/sys/fs/binfmt_misc,dst=/spp-binfmt,readonly', options)
        evidence = scope['container_identity'](self.runtime(), 'container', execution)
        self.assertEqual(evidence['image_id'], IMAGE)
        self.assertEqual(evidence['guest_architecture'], 'arm64')
        self.assertEqual(evidence['daemon_binfmt']['flags'], 'POCF')
        self.assertTrue(evidence['daemon_binfmt']['read_only'])
        desktop = scope['container_identity'](self.runtime(registration_name='aarch64'), 'container', execution)
        self.assertEqual(desktop['daemon_binfmt']['registration'], 'aarch64')

    def test_guest_image_or_disabled_registration_cannot_pass_as_emulation(self):
        execution = {'native': False, 'emulated': True, 'target_architecture': 'arm64'}
        for command in (self.runtime(guest='x86_64'), self.runtime(image_arch='amd64')):
            with self.assertRaisesRegex(RuntimeError, 'architecture differs'):
                scope['container_identity'](command, 'container', execution)
        with self.assertRaisesRegex(RuntimeError, 'no enabled aarch64'):
            scope['container_identity'](self.runtime(registration='disabled\ninterpreter /usr/bin/qemu-aarch64\nflags: POCF'), 'container', execution)

    def test_resource_bounds_are_enforced_without_host_swap(self):
        for role, memory in [('repo', 384), ('client', 768)]:
            limits = scope['resource_limits'](role)
            self.assertEqual(limits, {'memory_mib': memory, 'swap_mib': memory, 'cpus': 1})
            self.assertEqual(scope['resource_options'](limits),
                             ['--memory', str(memory) + 'm', '--memory-swap', str(memory) + 'm', '--cpus', '1'])
        identity = {'native': True, 'emulated': False, 'target_architecture': 'arm64'}
        limits = scope['resource_limits']('client')
        receipt = scope['container_identity'](self.runtime(), 'container', identity, limits)
        self.assertFalse(receipt['resource_limits']['host_swap_allowed'])
        with self.assertRaisesRegex(RuntimeError, 'resource limits differ'):
            scope['container_identity'](self.runtime(resources='0 0 0'), 'container', identity, limits)


if __name__ == '__main__':
    unittest.main()
