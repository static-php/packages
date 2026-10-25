"""Preserve explicit phpmod disable choices through native package upgrades."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
import uuid


ROOT = Path(__file__).resolve().parents[1]


def run(command):
    result = subprocess.run([str(item) for item in command], text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    if result.returncode:
        raise RuntimeError('Command failed: ' + ' '.join(map(str, command)) + '\n' + result.stdout)
    return result.stdout


def scripts(root, ini, state):
    code = r'''
require $argv[1] . '/vendor/autoload.php';
define('TEMP_DIR', $argv[4]);
$method = new ReflectionMethod(staticphp\extension::class, 'writeActivationScripts');
echo json_encode($method->invoke(null, $argv[2], $argv[3]), JSON_THROW_ON_ERROR);
'''
    return json.loads(run(['php', '-r', code, ROOT, ini, state, root]))


class ExtensionState(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='spp-extension-state-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.ini = self.root / '20-review.ini'
        self.disabled = Path(str(self.ini) + '.disabled')
        self.state = self.root / 'pending'
        self.scripts = scripts(self.root, self.ini, self.state)

    def before(self):
        run(['sh', self.scripts['before']])

    def after(self):
        run(['sh', self.scripts['after']])

    def test_disabled_module_stays_disabled_and_keeps_user_settings(self):
        self.disabled.write_text('extension=review\nreview.option=edited\n')
        self.before()
        self.ini.write_text('extension=review\nreview.option=new-default\n')
        self.after()
        self.assertFalse(self.ini.exists())
        self.assertEqual(self.disabled.read_text(), 'extension=review\nreview.option=edited\n')
        self.assertFalse(self.state.exists())

    def test_enabled_and_fresh_modules_keep_their_active_dropin(self):
        for existing in (False, True):
            with self.subTest(existing=existing):
                if existing:
                    self.ini.write_text('extension=review\n')
                self.before()
                self.ini.write_text('extension=review\n')
                self.after()
                self.assertTrue(self.ini.exists())
                self.ini.unlink()

    def test_interrupted_upgrade_retains_pending_disable_choice(self):
        self.disabled.write_text('extension=review\n')
        self.before()
        self.ini.write_text('extension=review\n')
        self.before()
        self.after()
        self.assertFalse(self.ini.exists())
        self.assertTrue(self.disabled.exists())

    def test_explicit_reenable_clears_pending_disable_marker(self):
        self.disabled.write_text('extension=review\n')
        self.before()
        self.disabled.rename(self.ini)
        self.before()
        self.after()
        self.assertTrue(self.ini.exists())
        self.assertFalse(self.state.exists())

    def test_static_builtin_configuration_does_not_disable_new_shared_dropin(self):
        (self.root / 'review.ini.disabled').write_text('; old static configuration\n')
        self.before()
        self.ini.write_text('extension=review\n')
        self.after()
        self.assertTrue(self.ini.exists())

    @unittest.skipUnless(os.environ.get('SPP_NATIVE_EXTENSION_STATE') == '1', 'Set SPP_NATIVE_EXTENSION_STATE=1 for native package probes')
    def test_native_upgrades_keep_disabled_modules(self):
        for kind, image in [('rpm', 'almalinux:8'), ('deb', 'debian:11'), ('apk', 'alpine:3.21')]:
            with self.subTest(kind=kind):
                tool = 'nfpm' if kind == 'apk' else 'fpm'
                binary = os.environ.get('SPP_' + tool.upper() + '_BINARY') or shutil.which(tool)
                self.assertTrue(binary and Path(binary).is_file(), tool + ' is required for native state checks')
                name = 'spp-extension-state-' + uuid.uuid4().hex[:12]
                generated = scripts(self.root, '/etc/php-zts/conf.d/20-review.ini', '/run/php-zts-module-state/review')
                packages = []
                for version in ('1', '2'):
                    source = self.root / ('source-' + kind + '-' + version)
                    source.write_text('extension=review\nreview.option=' + ('1' if kind == 'deb' else version) + '\n')
                    output = self.root / ('module-' + version + '.' + kind)
                    if kind in ('rpm', 'deb'):
                        args = [binary, '-s', 'dir', '-t', kind, '--name', 'spp-extension-state', '--version', version,
                                '--iteration', '1', '--package', output,
                                '--config-files', '/etc/php-zts/conf.d/20-review.ini']
                        args += ['--no-rpm-autoreqprov'] if kind == 'rpm' else ['--deb-no-default-config-files']
                        if version == '2':
                            args += ['--before-install', generated['before'], '--before-upgrade', generated['before'],
                                     '--after-install', generated['after'], '--after-upgrade', generated['after']]
                        run([*args, str(source) + '=/etc/php-zts/conf.d/20-review.ini'])
                    else:
                        config = {'name': 'spp-extension-state', 'version': version, 'release': '1',
                                  'arch': 'amd64', 'platform': 'linux', 'version_schema': 'none',
                                  'contents': [{'src': str(source), 'dst': '/etc/php-zts/conf.d/20-review.ini', 'type': 'config|noreplace'}]}
                        if version == '2':
                            config['scripts'] = {'preinstall': generated['before'], 'postinstall': generated['after']}
                            config['apk'] = {'scripts': {'preupgrade': generated['before'], 'postupgrade': generated['after']}}
                        path = self.root / ('nfpm-' + version + '.json')
                        path.write_text(json.dumps(config))
                        run([binary, 'package', '--config', path, '--packager', 'apk', '--target', output])
                    packages.append(output)
                commands = {
                    'rpm': ['rpm -i --nodeps /task/module-1.rpm', 'rpm -U --nodeps /task/module-2.rpm'],
                    'deb': ['dpkg -i /task/module-1.deb', 'dpkg -i /task/module-2.deb'],
                    'apk': ['apk add --allow-untrusted /task/module-1.apk', 'apk add --allow-untrusted --upgrade /task/module-2.apk'],
                }[kind]
                command = commands[0] + '; mv /etc/php-zts/conf.d/20-review.ini /etc/php-zts/conf.d/20-review.ini.disabled; ' + commands[1]
                command += '; test ! -e /etc/php-zts/conf.d/20-review.ini; grep -q review.option=1 /etc/php-zts/conf.d/20-review.ini.disabled; test ! -e /run/php-zts-module-state/review/disabled'
                try:
                    run(['docker', 'run', '--rm', '--name', name, '--mount', 'type=bind,src=' + str(self.root) + ',dst=/task,readonly', image, 'sh', '-ec', command])
                finally:
                    subprocess.run(['docker', 'rm', '-f', name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


if __name__ == '__main__':
    unittest.main()
