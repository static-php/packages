import json
from pathlib import Path
import runpy
import subprocess
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch


scope = runpy.run_path(str(Path(__file__).with_name('runtime-service-containers.py')))


class ServiceContainerSecurity(unittest.TestCase):
    def test_apk_service_upgrades_refresh_indexes_without_changing_solver(self):
        harness = self.harness('docker', 'apk')
        harness.execute = Mock()
        harness.upgrade('fixture')
        self.assertEqual([call.args for call in harness.execute.call_args_list], [
            ('fixture', 'apk', 'update', '--cache-max-age', '0'),
            ('fixture', 'apk', 'upgrade')])

    def setUp(self):
        environment = patch.dict(scope['os'].environ, {'GITHUB_ACTIONS': 'false'})
        environment.start()
        self.addCleanup(environment.stop)

    def harness(self, engine='podman', kind='deb'):
        image = {'rpm': 'almalinux:8', 'deb': 'debian:11', 'apk': 'alpine:3.21'}[kind]
        harness = scope['Harness'](SimpleNamespace(container_engine=engine, type=kind, image=image,
                                                  php_stream='8.6', probe_only=True, arch='x86_64'))
        self.addCleanup(harness.temporary.cleanup)
        return harness

    def test_host_service_commands_are_rejected_before_execution(self):
        harness = self.harness('docker')
        with patch.object(scope['subprocess'], 'run') as command:
            for executable in ('systemctl', '/usr/sbin/shutdown', 'reboot', 'poweroff', 'rc-service'):
                with self.subTest(executable=executable), self.assertRaisesRegex(RuntimeError, 'Host service'):
                    harness.command([executable, 'stop'])
        command.assert_not_called()

    def test_exec_requires_a_tracked_test_container(self):
        harness = self.harness('docker')
        harness.command = Mock()
        with self.assertRaisesRegex(RuntimeError, 'tracked test container'):
            harness.execute('unrelated', 'systemctl', 'stop', 'php-fpm')
        harness.command.assert_not_called()
        harness.containers.append('fixture')
        harness.execute('fixture', 'systemctl', 'stop', 'php-fpm')
        self.assertEqual(harness.command.call_args.args[0], ['docker', 'exec', 'fixture',
            'env', '-u', 'LD_LIBRARY_PATH', '-u', 'PHPRC', '-u', 'PHP_INI_SCAN_DIR',
            'systemctl', 'stop', 'php-fpm'])

    def test_host_namespaces_and_control_mounts_never_reach_the_engine(self):
        harness = self.harness('docker')
        harness.command = Mock()
        options = [('--pid=host',), ('--ipc', 'host'), ('--cgroupns=host',),
                   ('--network=host',), ('--pid=container:unrelated',),
                   ('-v', '/:/host'), ('--volume=/sys/fs/cgroup:/sys/fs/cgroup',),
                   ('--mount', 'type=bind,source=/run/systemd,target=/run/systemd'),
                   ('-v', '/var/run/docker.sock:/var/run/docker.sock')]
        for arguments in options:
            with self.subTest(arguments=arguments), self.assertRaisesRegex(RuntimeError, 'forbidden'):
                harness.docker('run', *arguments, harness.args.image)
        harness.command.assert_not_called()

    def test_cli_defaults_to_docker_and_accepts_a_configured_executable(self):
        for flags, expected in [([], 'docker'), (['--container-engine', 'podman'], 'podman'),
                                (['--container-engine', '/tmp/engine-wrapper'], '/tmp/engine-wrapper')]:
            selected = Mock(side_effect=RuntimeError('engine selected'))
            with self.subTest(flags=flags), \
                    patch.object(sys, 'argv', ['runtime-service-containers.py', '--type', 'deb', '--probe-only',
                                              '--output', 'unused.json', *flags]), \
                    patch.dict(scope['main'].__globals__, {'Harness': selected}):
                with self.assertRaisesRegex(RuntimeError, 'engine selected'):
                    scope['main']()
            self.assertEqual(selected.call_args.args[0].container_engine, expected)

    def test_commands_use_the_configured_engine_without_identity_policing(self):
        harness = self.harness('/tmp/engine-wrapper')
        with patch.object(scope['subprocess'], 'run', return_value=subprocess.CompletedProcess([], 0, '', '')) as command:
            harness.command(['docker', 'info'])
        self.assertEqual(command.call_args.args[0], ['/tmp/engine-wrapper', 'info'])

    def test_init_boots_with_private_namespaces_locally_and_in_ci(self):
        for ci in ('true', 'false'):
            for kind in ('rpm', 'deb', 'apk'):
                with self.subTest(ci=ci, kind=kind), patch.dict(scope['os'].environ, {'GITHUB_ACTIONS': ci}):
                    harness = self.harness('docker', kind)
                    harness.docker = Mock()
                    harness.execute = Mock(return_value=subprocess.CompletedProcess([], 0,
                        'default\n' if kind == 'apk' else 'running\n', ''))
                    harness.boot(harness.args.image, 'running')
                    arguments = harness.docker.call_args.args
                    self.assertEqual(arguments[-1], '/sbin/init')
                    self.assertFalse(any(option.startswith('--pid') for option in arguments))
                    for option in ('--ipc=private', '--cgroupns=private'):
                        self.assertIn(option, arguments)
                    self.assertNotIn('--privileged', arguments)
                    if kind == 'apk':
                        self.assertIn('SYS_PTRACE', arguments)
                    else:
                        self.assertIn('SIGRTMIN+3', arguments)

    def test_privileged_launch_is_rejected_before_execution(self):
        for engine in ('docker', 'podman', '/tmp/engine-wrapper'):
            harness = self.harness(engine)
            harness.command = Mock()
            for flag in ('--privileged', '--privileged=true'):
                with self.subTest(engine=engine, flag=flag), self.assertRaisesRegex(RuntimeError, 'Privileged containers are forbidden'):
                    harness.docker('run', flag, harness.args.image)
            harness.command.assert_not_called()

    def test_actual_privilege_or_host_sharing_is_rejected(self):
        for actual in ({'Privileged': True, 'CgroupnsMode': 'private'},
                       {'Privileged': False, 'PidMode': 'host'}):
            with self.subTest(actual=actual):
                harness = self.harness('docker')
                harness.command = Mock(side_effect=[subprocess.CompletedProcess([], 0, 'fixture\n', ''),
                    subprocess.CompletedProcess([], 0, json.dumps([{'HostConfig': actual}]), '')])
                identity = {'native': True, 'emulated': False, 'target_architecture': 'x86_64'}
                with patch.dict(scope['ARCHITECTURE'], {'engine_identity': Mock(return_value=identity),
                                                       'container_identity': Mock(return_value={})}):
                    with self.assertRaisesRegex(RuntimeError, 'Privileged containers are forbidden|shares a host'):
                        harness.docker('run', harness.args.image)

    def test_optional_podman_boot_retains_its_init_options(self):
        harness = self.harness()
        harness.docker = Mock()
        harness.execute = Mock(return_value=subprocess.CompletedProcess([], 0, 'running\n', ''))
        harness.boot(harness.args.image, 'running')
        arguments = harness.docker.call_args.args
        self.assertIn('--pid=private', arguments)
        self.assertIn('--systemd=always', arguments)
        self.assertIn('--cgroupns=private', arguments)
        self.assertIn('no-new-privileges', arguments)
        self.assertNotIn('--privileged', arguments)
        self.assertFalse(any('=host' in value or '/sys/fs/cgroup' in value for value in arguments))

    def test_podman_resource_evidence_uses_actual_quota_and_memory(self):
        harness = self.harness()
        config = {'Memory': 805306368, 'MemorySwap': 805306368, 'CpuQuota': 100000, 'CpuPeriod': 100000}
        result = subprocess.CompletedProcess([], 0, json.dumps([{'HostConfig': config}]), '')
        with patch.object(scope['subprocess'], 'run', return_value=result) as command:
            checked = harness.command(['docker', 'inspect', '--format', '{{.HostConfig.Memory}} {{.HostConfig.MemorySwap}} {{.HostConfig.NanoCpus}}', 'fixture'])
        self.assertEqual(checked.stdout, '805306368 805306368 1000000000')
        self.assertEqual(command.call_args.args[0], ['podman', 'inspect', 'fixture'])

    def test_served_process_proof_must_match_managed_pid_and_installed_inode(self):
        harness = self.harness()
        proof = {'pid': 42, 'exe': '/usr/bin/frankenphp', 'exe_device': 7, 'exe_inode': 99,
                 'runtime_mappings': [{'path': '/usr/lib/php-zts/lib/libssl.so.3', 'inode': 55}]}
        def execute(container, *arguments):
            return subprocess.CompletedProcess([], 0, '42\n' if arguments[0] == 'systemctl' else '7:99\n', '')
        harness.execute = execute
        response = lambda value: 'SPP_SERVICE_OK\n' + json.dumps(value)
        self.assertEqual(harness.inspect('fixture', 'frankenphp', response(proof)), proof)
        for key, value in [('pid', 43), ('exe_inode', 100), ('exe', '/usr/bin/another-server')]:
            with self.subTest(key=key), self.assertRaisesRegex(RuntimeError, 'managed executable'):
                harness.inspect('fixture', 'frankenphp', response(dict(proof, **{key: value})))


if __name__ == '__main__':
    unittest.main()
