#!/usr/bin/env python3
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


INSTALLER = Path(__file__).resolve().parents[1] / 'bin' / 'install-repositories'


class RepositoryInstallerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='spp-repositories-')
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.root = self.directory / 'root'
        self.commands = self.directory / 'bin'
        self.commands.mkdir()
        self.environment = dict(os.environ, PATH=f'{self.commands}:{os.environ["PATH"]}',
                                SPP_INSTALL_ROOT=str(self.root),
                                SPP_FORGEJO_HOST='http://fixture.invalid')
        self.command('curl', '''#!/usr/bin/env python3
import os
from pathlib import Path
import sys
args = sys.argv[1:]
url = next(arg for arg in args if arg.startswith(('http://', 'https://')))
if os.environ.get('FAIL_OWNER') and f'/packages/{os.environ["FAIL_OWNER"]}/' in url:
    sys.exit(22)
if url.endswith('/debian/repository.key'):
    Path(args[args.index('-o') + 1]).write_text('-----BEGIN PGP PUBLIC KEY BLOCK-----\\nfixture\\n')
elif url.endswith('/alpine/key'):
    owner = url.split('/packages/')[1].split('/')[0]
    Path(f'forgejo-{owner}.rsa.pub').write_text('-----BEGIN PUBLIC KEY-----\\nfixture\\n')
else:
    Path(args[args.index('-o') + 1]).write_text('static-php-1-2.noarch.rpm static-php-1-10.noarch.rpm')
''')
        self.command('dnf', '#!/bin/sh\nprintf "%s\\n" "$@" > "$SPP_INSTALL_ROOT/dnf-arguments"\n')

    def command(self, name, contents):
        command = self.commands / name
        command.write_text(contents)
        command.chmod(0o755)

    def run_installer(self, package_type, version='8.6', **environment):
        self.root.mkdir(exist_ok=True)
        return subprocess.run(['sh', str(INSTALLER), version],
                              env=dict(self.environment, SPP_INSTALL_TYPE=package_type, **environment),
                              text=True, capture_output=True)

    def test_debian_stream_and_common_repositories_are_signed_and_idempotent(self):
        for _ in range(2):
            result = self.run_installer('deb')
            self.assertEqual(result.returncode, 0, result.stderr)
        sources = self.root / 'etc/apt/sources.list.d'
        self.assertEqual(len(list(sources.glob('*.list'))), 2)
        for owner in ('86', 'libs'):
            expected = (f'deb [signed-by=/etc/apt/keyrings/henderkes{owner}.asc] '
                        f'http://fixture.invalid/api/packages/{owner}/debian php-zts main\n')
            self.assertEqual((sources / f'henderkes{owner}.list').read_text(), expected)
            self.assertTrue((self.root / f'etc/apt/keyrings/henderkes{owner}.asc').is_file())

    def test_alpine_preserves_existing_sources_and_server_key_filenames(self):
        repositories = self.root / 'etc/apk/repositories'
        repositories.parent.mkdir(parents=True)
        repositories.write_text('https://dl-cdn.alpinelinux.org/alpine/v3.21/main')
        for _ in range(2):
            result = self.run_installer('apk', version='82')
            self.assertEqual(result.returncode, 0, result.stderr)
        lines = repositories.read_text().splitlines()
        self.assertIn('https://dl-cdn.alpinelinux.org/alpine/v3.21/main', lines)
        for owner in ('82', 'libs'):
            self.assertEqual(lines.count(f'http://fixture.invalid/api/packages/{owner}/alpine/main/php-zts'), 1)
            self.assertTrue((self.root / f'etc/apk/keys/forgejo-{owner}.rsa.pub').is_file())

    def test_key_download_failure_does_not_change_configuration(self):
        for package_type, relative_file in (('deb', 'etc/apt/sources.list.d/henderkes86.list'),
                                            ('apk', 'etc/apk/repositories')):
            with self.subTest(package_type=package_type):
                original = self.root / relative_file
                original.parent.mkdir(parents=True, exist_ok=True)
                original.write_text('existing configuration\n')
                result = self.run_installer(package_type, FAIL_OWNER='libs')
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(original.read_text(), 'existing configuration\n')
                self.assertFalse((self.root / 'etc/apt/keyrings/henderkes86.asc').exists())
                self.assertFalse((self.root / 'etc/apk/keys/forgejo-86.rsa.pub').exists())

    def test_rpm_uses_existing_repository_package(self):
        result = self.run_installer('rpm', SPP_RPM_REPO_URL='http://rpm.fixture.invalid')
        self.assertEqual(result.returncode, 0, result.stderr)
        arguments = (self.root / 'dnf-arguments').read_text().splitlines()
        self.assertEqual(arguments, [f'--installroot={self.root}', 'install', '-y',
                                     'http://rpm.fixture.invalid/static-php-1-10.noarch.rpm'])
        self.assertIn('php-zts:static-8.6', result.stdout)

    def test_unsupported_php_version_is_rejected_before_configuration(self):
        result = self.run_installer('deb', version='8.7')
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.root / 'etc').exists())


if __name__ == '__main__':
    unittest.main()
