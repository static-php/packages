#!/usr/bin/env python3
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
HELPERS = ROOT / '.github/actions/shared-libraries'
sys.path.insert(0, str(HELPERS))
spec = importlib.util.spec_from_file_location('formats', HELPERS / 'format-packages.py')
formats = importlib.util.module_from_spec(spec)
spec.loader.exec_module(formats)


class Formats(unittest.TestCase):
    def test_formats_share_successful_payload_ids_without_including_failed_targets(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            artifacts = {}
            for arch in ('arm64', 'x86_64'):
                target = 'alma8-' + arch
                directory = root / target
                directory.mkdir()
                images = {kind: 'ghcr.io/static-php/image@sha256:' + 'a' * 64 for kind in ('rpm', 'deb')}
                (directory / 'target.json').write_text(json.dumps({'target': target, 'arch': arch, 'image': images['rpm'], 'package_images': images}))
                request = {'full': False, 'packages': ['ssh2']}
                (directory / 'shared-library-plan.json').write_text(json.dumps({'php_requests': {'8.6': request},
                    'format_selection': {kind: {'libraries': [], 'php_requests': {'8.6': request}} for kind in images}}))
                artifacts['shared-sdk-glibc-' + target] = len(artifacts) + 1
                artifacts['shared-payload-glibc-' + target + '-php8.6'] = len(artifacts) + 1
            jobs = [{'name': 'Caller / Build libs shared (alma8-' + arch + ')', 'conclusion': 'success'} for arch in ('arm64', 'x86_64')]
            jobs += [{'name': 'Caller / Build payload (alma8-arm64 php8.6)', 'conclusion': 'success'},
                     {'name': 'Build payload (alma8-x86_64 php8.6)', 'conclusion': 'failure'}]
            matrices = [formats.select({'artifacts': artifacts}, root, jobs, kind, ['8.6']) for kind in ('rpm', 'deb')]
            for libraries, packages, publishing in matrices:
                self.assertEqual([target['arch'] for target in libraries], ['arm64'])
                self.assertEqual([target['arch'] for target in publishing], ['arm64'])
                self.assertEqual(len(packages), 1)
                self.assertEqual(packages[0]['arch'], 'arm64')
                self.assertFalse(packages[0]['libraries-required'])
                self.assertIsNone(packages[0]['sdk-artifact'])
            self.assertEqual(matrices[0][1][0]['payload-artifact'], matrices[1][1][0]['payload-artifact'])
            self.assertEqual(formats.select({'artifacts': artifacts}, root, jobs, 'rpm', ['8.6'], ['x86_64']), ([], [], []))
            directory = root / 'alma8-arm64'
            plan = json.loads((directory / 'shared-library-plan.json').read_text())
            plan['format_selection']['rpm']['libraries'] = ['fixture']
            (directory / 'shared-library-plan.json').write_text(json.dumps(plan))
            artifacts['shared-libs-rpm-alma8-arm64'] = 99
            libraries, packages, _ = formats.select({'artifacts': artifacts}, root, jobs, 'rpm', ['8.6'])
            self.assertEqual(libraries[0]['libraries-artifact'], 99)
            self.assertFalse(libraries[0]['libraries-required'])
            self.assertEqual(packages[0]['payload-artifact'], matrices[0][1][0]['payload-artifact'])
            self.assertEqual(formats.select({'artifacts': artifacts}, root, jobs, 'rpm', ['8.6'], reuse_libraries=False), ([], [], []))
            plan['format_selection']['rpm']['library_streams'] = ['8.2', '8.6']
            (directory / 'shared-library-plan.json').write_text(json.dumps(plan))
            original = (directory / 'shared-library-plan.json').read_bytes()
            _, packages, _ = formats.select({'artifacts': artifacts}, root, jobs, 'rpm', ['8.6'])
            self.assertEqual([entry['php-version'] for entry in packages], ['8.6'])
            self.assertEqual((directory / 'shared-library-plan.json').read_bytes(), original)
            plan['format_selection']['rpm'].update(php_requests={}, library_streams=[])
            (directory / 'shared-library-plan.json').write_text(json.dumps(plan))
            _, packages, _ = formats.select({'artifacts': artifacts}, root, jobs, 'rpm', ['8.6'])
            self.assertEqual([entry['php-version'] for entry in packages], ['8.6'])
            self.assertTrue(packages[0]['library-only'])



if __name__ == '__main__':
    unittest.main()
