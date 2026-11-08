#!/usr/bin/env python3
"""Integrity and job selection checks for the single EL8 producer pipeline."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
HELPERS = ROOT / '.github/actions/shared-libraries'


def module(name):
    spec = importlib.util.spec_from_file_location(name, HELPERS / (name + '.py'))
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


workflow = module('workflow')
glibc = module('glibc')
archives = module('payload-archives')
runtime = module('runtime-checks')


class Pipeline(unittest.TestCase):
    def test_sdk_classification_needs_no_temporary_craft_and_scans_consumers_only_after_rebuild(self):
        for rebuilt, cached_variants in [(False, False), (False, True), (True, False)]:
            with self.subTest(rebuilt=rebuilt, cached_variants=cached_variants), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                sdk, stage = root / 'buildroot', root / 'stage'
                value = {'libraries': {'fixture': {'abi_epoch': 1}}}
                glibc.write(sdk / 'shared-libraries.json', value)
                glibc.write(stage / 'library-build-plan.json', {'full': False, 'rebuilt_libraries': ['fixture'] if rebuilt else []})
                glibc.write(stage / 'effective-source-plan.json', {})
                if cached_variants:
                    glibc.write(sdk / 'openssl-variants/variants.json', {})
                calls, scans = [], []
                def baseline(kind, destination, consumers=False, abi_libraries=()):
                    scans.append((kind, consumers, set(abi_libraries)))
                    glibc.write(destination / 'shared-libraries.json', dict(value, consumer_inventory_complete=consumers))
                def execute(arguments, **kwargs):
                    args = [str(arg) for arg in arguments]
                    calls.append(args)
                    option = lambda name: Path(args[args.index(name) + 1])
                    if args[0] == 'cp':
                        subprocess.run(args, check=True)
                    elif 'prepare' in args:
                        glibc.write(option('--manifest-output'), value)
                        glibc.write(option('--output'), {'libraries': [], 'php_requests': {}})
                    elif 'stage-publication-retries' in args:
                        glibc.write(option('--output'), {'remaining_libraries': []})
                    elif 'manifest' in args:
                        shutil.copy2(option('--root') / 'shared-libraries.json', option('--output'))
                with patch.object(glibc, 'ROOT', root), patch.object(glibc, 'STAGE', stage), \
                        patch.object(glibc, 'baseline', baseline), patch.object(glibc, 'run', execute):
                    glibc.classify()
                expected = {'fixture'} if rebuilt else set()
                self.assertEqual(scans, [('rpm', False, expected), ('deb', rebuilt, expected)])
                self.assertFalse((root / 'craft.yml').exists())
                self.assertFalse(any('record' in call for call in calls))
                self.assertTrue(any('seal' in call for call in calls))
                prepares = [call for call in calls if 'prepare' in call]
                self.assertEqual(len(prepares), 2 if cached_variants else 4)
                self.assertTrue(all(call[call.index('--rebuilt-libraries') + 1] == ('fixture' if rebuilt else '') for call in prepares))

    def test_shared_consumer_graph_preserves_native_rpm_versions_and_libraries(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            deb, rpm = root / 'deb', root / 'rpm'
            glibc.write(deb / 'shared-libraries.json', {'consumers': {'8.5': ['php-zts-cli']},
                'consumer_dependencies': {'8.5': {'php-zts-cli': ['openssl']}},
                'consumer_inventory_complete': True, 'consumer_inventory_stream': '8.5',
                'consumer_versions': {'8.5': {'php-zts-cli': 'deb-version'}}})
            glibc.write(rpm / 'shared-libraries.json', {'libraries': {'openssl': {'published_version': 'rpm-version'}},
                'consumer_versions': {'8.5': {'php-zts-cli': 'rpm-version'}}})
            glibc.share_consumers(deb, rpm)
            result = glibc.read(rpm / 'shared-libraries.json')
            self.assertEqual(result['consumer_dependencies']['8.5']['php-zts-cli'], ['openssl'])
            self.assertEqual(result['consumer_versions']['8.5']['php-zts-cli'], 'rpm-version')
            self.assertEqual(result['libraries']['openssl']['published_version'], 'rpm-version')

    def test_format_plans_merge_compile_and_packaging_requests_without_extra_compilation(self):
        value = glibc.combine_plans({
            'rpm': {'php_requests': {'8.6': {'full': True, 'packages': [], 'compile': False, 'compile_full': False, 'compile_packages': []}}},
            'deb': {'php_requests': {'8.6': {'full': False, 'packages': ['imagick'], 'compile': True, 'compile_full': False, 'compile_packages': ['imagick']}}},
        })['php_requests']['8.6']
        self.assertTrue(value['full'])
        self.assertEqual(value['packages'], [])
        self.assertTrue(value['compile'])
        self.assertFalse(value['compile_full'])
        self.assertEqual(value['compile_packages'], ['imagick'])
        self.assertFalse(value['reuse_payload'])
        metadata = glibc.combine_plans({'rpm': {'php_requests': {'8.5': {'full': True, 'compile': False}}}})
        self.assertTrue(metadata['php_requests']['8.5']['reuse_payload'])

    def test_common_abi_merge_keeps_epoch_capability_and_dump_consistent(self):
        common = {'libraries': {'fixture': {'source_hash': 'actual', 'abi_epoch': 1,
                    'abi_capability': 'php-zts-lib-fixture-abi-1', 'abi_capabilities': ['php-zts-lib-fixture-abi-1']}}}
        formats = {'rpm': {'libraries': {'fixture': {'abi_epoch': 2, 'abi_dump': {'libfixture.so.2': 'new.xml'}, 'abi_dump_hashes': {'libfixture.so.2': 'new'}}}},
                   'deb': {'libraries': {'fixture': {'abi_epoch': 1}}}}
        selected = list(glibc.merge_library_abi(common, formats))
        self.assertEqual(selected[0][0], 'rpm')
        value = common['libraries']['fixture']
        self.assertEqual(value['abi_epoch'], 2)
        self.assertEqual(value['abi_capability'], 'php-zts-lib-fixture-abi-2')
        self.assertEqual(value['abi_capabilities'], ['php-zts-lib-fixture-abi-2'])
        self.assertEqual(value['abi_dump'], {'libfixture.so.2': 'new.xml'})
        self.assertEqual(value['source_hash'], 'actual')

    def test_one_successful_payload_fans_out_two_formats_and_failed_target_does_not_cascade(self):
        with tempfile.TemporaryDirectory(prefix='spp-glibc-matrix-') as temporary:
            root = Path(temporary)
            for arch in ['x86_64', 'arm64']:
                target = root / ('shared-plans-glibc-alma8-' + arch)
                target.mkdir()
                (target / 'target.json').write_text(json.dumps({'target': 'alma8-' + arch, 'arch': arch,
                    'image': 'producer', 'package_images': {'rpm': 'rpm-image', 'deb': 'deb-image'}}))
                (target / 'shared-library-plan.json').write_text(json.dumps({'php_requests': {'8.6': {'full': True, 'packages': []}}}))
            result = subprocess.check_output([sys.executable, HELPERS / 'workflow.py', 'packages', '--plans', root,
                '--succeeded', 'alma8-x86_64 8.6'], text=True)
            matrix = json.loads(next(line.split('=', 1)[1] for line in result.splitlines() if line.startswith('matrix=')))['include']
            self.assertEqual(len(matrix), 2)
            self.assertEqual({entry['type'] for entry in matrix}, {'rpm', 'deb'})
            self.assertEqual({entry['arch'] for entry in matrix}, {'x86_64'})
            self.assertTrue(all(entry['image'] == 'producer' for entry in matrix))

    def test_library_only_update_gets_validation_lanes_without_php_payload_jobs(self):
        with tempfile.TemporaryDirectory(prefix='spp-glibc-library-only-') as temporary:
            root = Path(temporary)
            target = root / 'target'
            target.mkdir()
            (target / 'target.json').write_text(json.dumps({'target': 'alma8-x86_64', 'arch': 'x86_64',
                'image': 'producer', 'package_images': {'rpm': 'rpm-image@sha256:' + 'a' * 64, 'deb': 'deb-image@sha256:' + 'b' * 64}}))
            (target / 'shared-library-plan.json').write_text(json.dumps({'php_requests': {}, 'libraries': ['zlib']}))
            self.assertEqual(workflow.matrix(root, workflow.STREAMS)[0], [])
            result = subprocess.check_output([sys.executable, HELPERS / 'workflow.py', 'packages', '--plans', root,
                '--succeeded', ''], text=True)
            values = json.loads(next(line.split('=', 1)[1] for line in result.splitlines() if line.startswith('matrix=')))['include']
            self.assertEqual(len(values), 2)
            self.assertTrue(all(item['library-only'] for item in values))
            self.assertTrue(all(item['tls-checks'] and not item['runtime-checks'] for item in values))

    def test_format_only_library_update_never_creates_an_empty_other_format_lane(self):
        with tempfile.TemporaryDirectory(prefix='spp-glibc-format-') as temporary:
            root = Path(temporary)
            target = root / 'target'
            target.mkdir()
            (target / 'target.json').write_text(json.dumps({'target': 'alma8-x86_64', 'arch': 'x86_64',
                'image': 'producer', 'package_images': {'rpm': 'rpm-image@sha256:' + 'a' * 64, 'deb': 'deb-image@sha256:' + 'b' * 64}}))
            plan = glibc.combine_plans({'rpm': {'libraries': [], 'php_requests': {}},
                                       'deb': {'libraries': ['openssl'], 'php_requests': {}}})
            (target / 'shared-library-plan.json').write_text(json.dumps(plan))
            result = subprocess.check_output([sys.executable, HELPERS / 'workflow.py', 'packages', '--plans', root,
                '--succeeded', ''], text=True)
            values = json.loads(next(line.split('=', 1)[1] for line in result.splitlines() if line.startswith('matrix=')))['include']
            self.assertEqual([value['type'] for value in values], ['deb'])
            result = subprocess.check_output([sys.executable, HELPERS / 'workflow.py', 'matrix', '--plans', root], text=True)
            publish = json.loads(next(line.split('=', 1)[1] for line in result.splitlines() if line.startswith('publish-matrix=')))['include']
            self.assertEqual([value['type'] for value in publish], ['deb'])
            self.assertEqual(publish[0]['package-image-digest'], 'b' * 64)

    def test_native_services_and_tls_are_deduplicated_per_format_and_architecture(self):
        values = [{'target': 'alma8-' + arch, 'type': kind, 'php-version': stream, 'packages': ''}
                  for arch in ['x86_64', 'arm64'] for kind in ['rpm', 'deb'] for stream in workflow.STREAMS]
        workflow.runtime_checks(values)
        self.assertEqual(len([item for item in values if item['runtime-checks']]), 4)
        self.assertEqual(len([item for item in values if item['tls-checks']]), 4)
        self.assertTrue(all(item['php-version'] == '8.6' for item in values if item['runtime-checks'] or item['tls-checks']))
        partial = [{'target': 'x86_64', 'type': 'apk', 'php-version': '8.6', 'packages': 'imagick'}]
        workflow.runtime_checks(partial)
        self.assertTrue(partial[0]['tls-checks'])
        self.assertFalse(partial[0]['runtime-checks'])

    def test_common_abi_cache_transports_receipts_and_keeps_openssl_profiles_separate(self):
        with tempfile.TemporaryDirectory(prefix='spp-format-abi-cache-') as temporary:
            root = Path(temporary)
            source = root / 'rpm'
            abi = source / 'usr/share/php-zts/libraries/abi'
            for name, value in [('grpc/libgrpc.so.50.xml', 'typed-common'), ('grpc/libgrpc.so.50.xml.inputs.json', 'bound-input-proof'),
                                ('openssl/libssl.so.3.xml', 'rpm-openssl'), ('openssl/libssl.so.3.xml.inputs.json', 'rpm-proof')]:
                path = abi / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(value)
            with patch.object(glibc, 'STAGE', root / 'stage'):
                glibc.save_abi_cache(source, 'rpm')
                glibc.seed_abi_cache(root / 'deb', 'deb')
                deb = root / 'deb/usr/share/php-zts/libraries/abi'
                self.assertEqual((deb / 'grpc/libgrpc.so.50.xml.inputs.json').read_text(), 'bound-input-proof')
                self.assertFalse((deb / 'openssl').exists())
                (deb / 'openssl').mkdir()
                (deb / 'openssl/libssl.so.3.xml').write_text('deb-openssl')
                glibc.save_abi_cache(root / 'deb', 'deb')
                glibc.seed_abi_cache(root / 'rpm-next-phase', 'rpm')
                self.assertEqual((root / 'rpm-next-phase/usr/share/php-zts/libraries/abi/openssl/libssl.so.3.xml').read_text(), 'rpm-openssl')

    def test_service_fixture_stages_only_the_real_selected_library_and_its_provenance(self):
        with tempfile.TemporaryDirectory(prefix='spp-runtime-sdk-') as temporary:
            root = Path(temporary)
            source, output = root / 'source', root / 'fixture'
            for relative, contents in {'lib/libz.so.1.3.1': b'actual-zlib', 'licenses/zlib/LICENSE': b'license',
                                       'debug/lib/libz.so.1.3.1.debug': b'actual-debug',
                                       'usr/share/php-zts/libraries/abi/zlib/libz.so.1.xml': b'actual-abi',
                                       'lib/unrelated.so': b'unchanged-other-library'}.items():
                path = source / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(contents)
            (source / 'lib/libz.so.1').symlink_to('libz.so.1.3.1')
            entry = {'files': ['lib/libz.so.1', 'lib/libz.so.1.3.1'], 'license_files': ['licenses/zlib/LICENSE'],
                     'abi_dump': {'libz.so.1': 'usr/share/php-zts/libraries/abi/zlib/libz.so.1.xml'}}
            (source / 'shared-libraries.json').write_text(json.dumps({'libraries': {'zlib': entry, 'other': {}}}))
            record = {'state': 'sealed', 'files': {'lib/libz.so.1.3.1': {'sha256': 'recorded-production-input'}}}
            (source / 'build-payload.json').write_text(json.dumps(record))
            self.assertEqual(runtime.stage_library_sdk(source, output), record)
            self.assertEqual(json.loads((output / 'shared-libraries.json').read_text())['libraries'], {'zlib': entry})
            self.assertEqual(json.loads((output / 'provenance/source-sealed-payload.json').read_text()), record)
            self.assertEqual((output / 'lib/libz.so.1').read_bytes(), b'actual-zlib')
            self.assertEqual((output / 'debug/lib/libz.so.1.3.1.debug').read_bytes(), b'actual-debug')
            self.assertFalse((output / 'lib/unrelated.so').exists())

    def test_partial_source_request_without_cached_core_builds_canonical_payload_once(self):
        with tempfile.TemporaryDirectory(prefix='spp-payload-cache-miss-') as temporary:
            root = Path(temporary)
            stage = root / 'stage'
            for kind in ['rpm', 'deb']:
                (stage / kind).mkdir(parents=True)
                (stage / kind / 'shared-library-plan.json').write_text('{}')
                (stage / kind / 'baseline').mkdir()
                (stage / kind / 'baseline/shared-libraries.json').write_text('{}')
            calls = []
            def record(arguments, **kwargs):
                calls.append([str(value) for value in arguments])
                if arguments[:3] == ['php', 'bin/spp', 'build']:
                    (root / 'build/8.6').mkdir(parents=True)
            environment = {'PHPV': '8.6', 'COMPILE': 'true', 'COMPILE_PACKAGES': 'imagick', 'PACKAGES': 'imagick',
                           'SOURCE_RUN_ID': '42', 'TARGET': 'alma8-x86_64', 'IMAGE': 'producer', 'TYPE': 'rpm', 'MATRIX_ARCH': 'x86_64'}
            with patch.object(glibc, 'ROOT', root), patch.object(glibc, 'STAGE', stage), patch.object(glibc, 'run', record), patch.dict(os.environ, environment):
                previous = Path.cwd()
                try:
                    os.chdir(root)
                    glibc.payload()
                    build = next(call for call in calls if call[:3] == ['php', 'bin/spp', 'build'])
                    self.assertFalse(any(value.startswith('--packages=') for value in build))
                    os.environ['COMPILE'] = 'false'
                    with self.assertRaisesRegex(RuntimeError, 'requires its sealed PHP cache'):
                        glibc.payload()
                finally:
                    os.chdir(previous)

    def test_signing_preserves_exact_publication_retry_archives(self):
        with tempfile.TemporaryDirectory(prefix='spp-retry-signing-') as temporary:
            root = Path(temporary)
            (root / 'dist/rpm').mkdir(parents=True)
            receipt = root / 'cohort/shared-libs-rpm-alma8-x86_64/publication-retries.json'
            receipt.parent.mkdir(parents=True)
            existing, fresh = root / 'dist/rpm/existing.rpm', root / 'dist/rpm/new.rpm'
            existing.write_bytes(b'exact previously signed archive')
            fresh.write_bytes(b'genuinely new archive')
            receipt.write_text(json.dumps({'copied_archives': [{'path': str(existing),
                'sha256': hashlib.sha256(existing.read_bytes()).hexdigest()}]}))
            binaries = root / 'tools'
            binaries.mkdir()
            for name, script in {
                'gpg': 'import sys\nif "--with-colons" in sys.argv: print("fpr:::::::::TESTFINGERPRINT:")\n',
                'gpgconf': 'pass\n',
                'rpmsign': 'import pathlib,sys\np=pathlib.Path(sys.argv[-1]);p.write_bytes(p.read_bytes()+b" signed")\n',
            }.items():
                path = binaries / name
                path.write_text('#!/usr/bin/python3\n' + script)
                path.chmod(0o755)
            environment = dict(os.environ, PATH=str(binaries) + os.pathsep + os.environ['PATH'],
                               RPM_SIGNING_KEY='test fixture key', RPM_SIGNING_PASSWORD='test fixture password', TMPDIR=str(root))
            command = ['bash', str(HELPERS / 'sign-rpm.sh')]
            subprocess.run(command, cwd=root, env=environment, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            self.assertEqual(existing.read_bytes(), b'exact previously signed archive')
            self.assertEqual(fresh.read_bytes(), b'genuinely new archive signed')
            existing.write_bytes(b'same filename but changed bytes')
            rejected = subprocess.run(command, cwd=root, env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            self.assertNotEqual(rejected.returncode, 0)
            self.assertIn(b'changed before signing', rejected.stderr)
            self.assertEqual(fresh.read_bytes(), b'genuinely new archive signed')

    def test_native_receipt_binds_archive_bytes_and_producer_plan(self):
        with tempfile.TemporaryDirectory(prefix='spp-native-receipt-') as temporary:
            root = Path(temporary)
            library = root / 'cohort/shared-libs-rpm-alma8-x86_64'
            consumer = root / 'cohort/shared-consumers-rpm-alma8-x86_64-php8.6'
            library.mkdir(parents=True)
            consumer.mkdir()
            plan = library / 'shared-library-plan.json'
            plan.write_text(json.dumps({'php_requests': {'8.6': {'full': True, 'packages': []}}}))
            image = 'ghcr.io/example/producer@sha256:' + 'a' * 64
            (library / 'target.json').write_text(json.dumps({'arch': 'x86_64', 'source_run_id': '42', 'image': image}))
            report = consumer / 'native-upgrade-receipt.json'
            report.write_text(json.dumps({'ok': True, 'type': 'rpm', 'php_stream': '8.6', 'architecture': 'x86_64'}))
            archive = consumer / 'package.rpm'
            archive.write_bytes(b'validated native archive')
            receipt = workflow.upgrade_receipt(report, plan, consumer, 'rpm', '42', image, '8.6', 'x86_64')
            (consumer / 'shared-library-upgrade-receipt.json').write_text(json.dumps(receipt))
            workflow.verify_upgrades(root / 'cohort', library, 'rpm')
            archive.write_bytes(b'changed after native validation')
            with self.assertRaisesRegex(RuntimeError, 'does not match'):
                workflow.verify_upgrades(root / 'cohort', library, 'rpm')

    def test_extracted_elf_must_match_prepared_producer_bytes_and_modes(self):
        with tempfile.TemporaryDirectory(prefix='spp-archive-identity-') as temporary:
            root = Path(temporary)
            elf = root / 'usr/lib64/php-zts/lib/libfixture.so.1'
            elf.parent.mkdir(parents=True)
            content = b'\x7fELFprepared producer identity fixture'
            elf.write_bytes(content)
            elf.chmod(0o755)
            identity = (hashlib.sha256(content).hexdigest(), 0o755)
            result = archives.verify_extracted(root, {identity: [{'source': 'lib/libfixture.so.1', 'role': 'runtime'}]})
            self.assertIn('/usr/lib/php-zts/lib/libfixture.so.1', result)
            elf.write_bytes(content + b'RPM postprocessing mutation')
            with self.assertRaisesRegex(RuntimeError, 'differs from sealed'):
                archives.verify_extracted(root, {identity: []})


if __name__ == '__main__':
    unittest.main()
