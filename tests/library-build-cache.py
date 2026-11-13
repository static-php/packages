#!/usr/bin/env python3
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
class WorkflowCacheOrderTests(unittest.TestCase):
    def test_apk_saves_only_completed_exports_even_after_packaging_failure(self):
        document = yaml.safe_load((ROOT / '.github/actions/apk-packages/action.yml').read_text())
        save = next(step for step in document['runs']['steps'] if step.get('name') == 'Save sealed PHP payload cache')
        self.assertIn('!cancelled()', save['if'])
        self.assertIn('hashFiles', save['if'])
        self.assertEqual(save['with']['path'], 'php-payload-cache-ready.tar.zst')
        for export_requested, failed_export in [(False, False), (True, False), (True, True)]:
            with self.subTest(export_requested=export_requested, failed_export=failed_export), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                for directory in ['shims', 'stage', 'buildroot', 'bin']:
                    (root / directory).mkdir()
                (root / 'stage/shared-libraries.json').write_text('{}')
                (root / 'stage/payload-cache-key').write_text('current-key')
                (root / 'stage/payload-cache-seed-key').write_text('current-seed')
                (root / 'stage/library-compatibility').write_text('compiler')
                (root / 'stage/payload-provenance.json').write_text('{"compiled_source_run_id":"42"}')
                (root / 'php-payload-cache.tar.zst').write_text('old restored archive')
                for name, body in {
                    'git': 'exit 0', 'composer': 'exit 0',
                    'php': 'if [[ "$1 $2" == "bin/spp package" ]]; then exit 9; fi',
                    'python3': f'''exec {sys.executable} - "$@" <<'PY'
import pathlib,subprocess,sys
args=sys.argv[1:]
if args[0]=='-c': sys.exit(subprocess.run([sys.executable,*args]).returncode)
if 'export' in args:
 pathlib.Path(args[args.index('--output')+1]).write_text('fresh exported archive')
 sys.exit({5 if failed_export else 0})
PY''',
                }.items():
                    shim = root / 'shims' / name
                    shim.write_text('#!/usr/bin/env bash\n' + body + '\n')
                    shim.chmod(0o755)
                environment = dict(os.environ, PATH=str(root / 'shims') + ':' + os.environ['PATH'],
                    TYPE='apk', IN_BUILDER='true', MODE='full', LIBRARIES='', LIBRARY_ONLY='false',
                    PHPV='8.4', PACKAGES='', ITERATION='', BUMP='false',
                    EXPORT_PAYLOAD_CACHE=str(export_requested).lower())
                result = subprocess.run(['bash', str(ROOT / '.github/actions/shared-libraries/run.sh'), 'php'],
                                        cwd=root, env=environment, capture_output=True, text=True)
                self.assertEqual(result.returncode, 5 if failed_export else 9, result.stdout + result.stderr)
                ready = root / save['with']['path']
                self.assertEqual(ready.exists(), export_requested and not failed_export)
                if ready.exists():
                    self.assertEqual(ready.read_text(), 'fresh exported archive')
                self.assertEqual((root / 'php-payload-cache.tar.zst').read_text(), 'old restored archive')

    def test_all_formats_restore_before_compile_and_publish_final_sdk(self):
        for filename in ['build-rpm-modular-packages.yml', 'build-apk-forgejo.yml']:
            document = yaml.safe_load((ROOT / '.github/workflows' / filename).read_text())
            steps = document['jobs']['build-libs']['steps']
            names = [step.get('name', '') for step in steps]
            with self.subTest(workflow=filename):
                self.assertLess(names.index('Extract restored buildroot'), names.index('Build changed shared libraries'))
                self.assertLess(names.index('Classify ABI and package shared libraries'), names.index('Pack buildroot'))
                self.assertLess(names.index('Pack buildroot'), names.index('Save buildroot cache'))
                build = steps[names.index('Build changed shared libraries')]
                self.assertEqual(build.get('if', ''), "inputs.repackage_run_id == ''" if 'rpm' in filename else '')
                restore = steps[names.index('Restore buildroot cache')]
                self.assertIn('seed-key', restore['with'])
                save = steps[names.index('Save buildroot cache')]
                self.assertIn('cache-current', save['if'])
                self.assertIn('sdk-rebuilt', save['if'])
                producer = document['jobs']['build-payload' if 'rpm' in filename else 'build']['steps']
                consumers = [step for step in producer if step.get('name') == 'Download exact common SDK']
                self.assertEqual(len(consumers), 1)
                self.assertTrue(consumers[0]['with']['name'].startswith('shared-sdk-buildroot-'))
                action = 'php-payload' if 'rpm' in filename else 'apk-packages'
                payload_steps = yaml.safe_load((ROOT / '.github/actions' / action / 'action.yml').read_text())['runs']['steps']
                php_cache = next(step for step in payload_steps if step.get('name') == 'Restore sealed PHP payload')
                self.assertEqual(php_cache['with']['path'], 'php-payload-cache.tar.zst')
                self.assertIn('seed-key', php_cache['with'])
                packing = steps[names.index('Pack buildroot')]['run']
                self.assertEqual('--zstd' in packing, filename != 'build-rpm-modular-packages.yml')


if __name__ == '__main__':
    unittest.main()
