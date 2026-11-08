import json
from pathlib import Path
import shutil
import subprocess
from tempfile import TemporaryDirectory
import unittest


ROOT = Path(__file__).resolve().parents[1]
TOOLS = {'rpm': 'rpmdev-vercmp', 'deb': 'dpkg', 'apk': 'apk'}


class ConsumerVersionPreflight(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory(prefix='consumer-version-preflight-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def run_preflight(self, kind, candidates, packages=None, flags=(), requires_native=True, **baseline):
        if requires_native and not shutil.which(TOOLS[kind]):
            self.skipTest('Native ' + kind + ' comparator required')
        captured = {'schema': 1, 'type': kind, 'php_stream': '8.6',
                    'architecture': 'x86_64', 'packages': packages or [], **baseline}
        (self.root / 'baseline.json').write_text(json.dumps(captured))
        (self.root / 'candidates.json').write_text(json.dumps({'schema': 1, 'candidates': candidates}))
        result = subprocess.run(['php', str(ROOT / 'bin/preflight-consumer-versions'), '--type=' + kind,
                                 '--architecture=x86_64', '--candidates=' + str(self.root / 'candidates.json'),
                                 '--baseline-manifest=' + str(self.root / 'baseline.json'),
                                 '--output=' + str(self.root / 'receipt.json'), *flags], capture_output=True, text=True)
        receipt = self.root / 'receipt.json'
        return result, json.loads(receipt.read_text()) if receipt.is_file() else None

    def candidate(self, name='php-zts-grpc', upstream='1.83.0', php='8.6.0RC4', **values):
        return dict(name=name, upstream_version=upstream, php_version=php, php_stream=php[:3], **values)

    def package(self, name, version, **values):
        return dict(name=name, version=version, architecture='x86_64', **values)

    def check_numeric_rollback(self, kind, version):
        result, receipt = self.run_preflight(kind, [self.candidate(php='8.6.1')],
                                            [self.package('php-zts-grpc', version)])
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertFalse(receipt['passed'])
        self.assertEqual(receipt['rejected'][0]['reason'], 'upstream-version-rollback')

    def test_rpm_numeric_rollback_is_not_hidden_by_a_newer_php_tag(self):
        self.check_numeric_rollback('rpm', '1.84.1_86.0~rc4-1.el10')

    def test_deb_numeric_rollback_is_not_hidden_by_a_newer_php_tag(self):
        self.check_numeric_rollback('deb', '1.84.1+php86.0~rc4-1')

    def test_apk_numeric_rollback_is_not_hidden_by_a_newer_php_tag(self):
        self.check_numeric_rollback('apk', '1.84.1p86.0_rc4-r0')

    def test_rpm_old_boundary_and_highest_el_release_migrate_using_production_rules(self):
        version = '1.13.3_86.0~rc4+ext-47.el10'
        result, receipt = self.run_preflight('rpm', [self.candidate(name='frankenphp', upstream='1.13.3')],
                                            [self.package('frankenphp', version)])
        self.assertEqual(result.returncode, 0, result.stderr)
        candidate = receipt['candidates'][0]
        self.assertEqual(candidate['production_base_version'], '1.13.3_86.0~rc4+ext')
        self.assertEqual(candidate['version'], '1.13.3_86.0~rc4+ext-48')

    def test_deb_old_boundary_migrates_using_production_rules(self):
        result, receipt = self.run_preflight('deb', [self.candidate(name='frankenphp', upstream='1.13.3')],
                                            [self.package('frankenphp', '1.13.3+php86.0~rc4+ext-7')])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(receipt['candidates'][0]['version'], '1.13.3+php86.0~rc4+ext-8')

    def test_apk_old_boundary_migrates_using_production_rules(self):
        result, receipt = self.run_preflight('apk', [self.candidate(name='frankenphp', upstream='1.13.3')],
                                            [self.package('frankenphp', '1.13.3p86.0_rc4_p0-r7')])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(receipt['candidates'][0]['version'], '1.13.3p86.0_rc4_p0-r8')

    def test_rpm_snapshot_keeps_all_actual_streams(self):
        result, receipt = self.run_preflight('rpm', [self.candidate(upstream='1.84.1', php='8.5.12')], [],
                                            published_packages=[self.package('php-zts-grpc', '1.84.1_85.12-23.el10'),
                                                                self.package('php-zts-grpc', '1.99.0_86.0~rc4-1.el10')])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(receipt['candidates'][0]['version'], '1.84.1_85.12-24')
        self.assertEqual(receipt['candidates'][0]['published_versions'], ['0:1.84.1_85.12-23.el10'])

    def test_foreign_architecture_does_not_block_a_new_package(self):
        package = self.package('php-zts-grpc', '1.99.0_86.0~rc4-5.el10')
        package['architecture'] = 'aarch64'
        result, receipt = self.run_preflight('rpm', [self.candidate()], [package])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(receipt['candidates'][0]['published_versions'], [])

    def test_published_epoch_is_compared_natively(self):
        result, receipt = self.run_preflight('rpm', [self.candidate(upstream='1.84.1')],
                                            [self.package('php-zts-grpc', '1.83.0_86.0~rc4-1.el10', epoch='1')])
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertEqual(receipt['rejected'][0]['reason'], 'native-version-downgrade')

    def test_prerelease_rollback_is_compared_natively(self):
        result, receipt = self.run_preflight('rpm', [self.candidate(upstream='1.84.0beta1')],
                                            [self.package('php-zts-grpc', '1.84.0~rc1_86.0~rc4-1.el10')])
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertEqual(receipt['rejected'][0]['reason'], 'native-version-downgrade')

    def test_shared_consumer_versions_are_also_a_native_floor(self):
        result, receipt = self.run_preflight('rpm', [self.candidate()], [], linkage='shared', libraries={},
                                            consumer_versions={'8.6': {'php-zts-grpc': '0:1.84.1_86.0~rc4-1'}})
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertEqual(receipt['rejected'][0]['reason'], 'upstream-version-rollback')

    def test_duplicate_candidates_fail_before_comparison(self):
        result, receipt = self.run_preflight('rpm', [self.candidate(), self.candidate()])
        self.assertEqual(result.returncode, 1)
        self.assertIsNone(receipt)
        self.assertIn('Duplicate candidate', result.stderr)

    def test_native_candidate_override_cannot_claim_a_different_source_version(self):
        result, receipt = self.run_preflight('rpm', [self.candidate(version='1.99.0_86.0~rc4-100')])
        self.assertEqual(result.returncode, 1)
        self.assertIsNone(receipt)
        self.assertIn('production source version', result.stderr)

    def check_shared_bump(self, kind, base):
        separator = '-r' if kind == 'apk' else '-'
        packages = [self.package('php-zts-grpc', base + separator + '5')]
        for flags, expected_revision, expected_status in [([], 0 if kind == 'apk' else 1, 3), (['--bump'], 6, 0),
                                                          (['--iteration=7'], 7, 0), (['--bump', '--iteration=4'], 4, 3)]:
            with self.subTest(flags=flags):
                result, receipt = self.run_preflight(kind, [self.candidate()], packages, flags=flags,
                                                    migration_cutoffs={'8.6': {}})
                self.assertEqual(result.returncode, expected_status, result.stderr)
                self.assertEqual(receipt['candidates'][0]['version'], base + separator + str(expected_revision))

    def test_rpm_shared_bump_models_the_captured_native_release(self):
        self.check_shared_bump('rpm', '1.83.0_86.0~rc4')

    def test_deb_shared_bump_models_the_captured_native_release(self):
        self.check_shared_bump('deb', '1.83.0+php86.0~rc4')

    def test_apk_shared_bump_models_the_captured_native_release(self):
        self.check_shared_bump('apk', '1.83.0p86.0_rc4')

    def test_bump_does_not_mask_a_genuine_upstream_rollback(self):
        result, receipt = self.run_preflight('rpm', [self.candidate()],
                                            [self.package('php-zts-grpc', '1.84.1_86.0~rc4-5')], flags=['--bump'])
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertEqual(receipt['rejected'][0]['reason'], 'upstream-version-rollback')

    def test_shared_abi_plan_advances_only_the_affected_consumer_with_bump_false(self):
        plan = self.root / 'plan.json'
        plan.write_text(json.dumps({'schema': 1, 'type': 'rpm', 'abi_break': True,
                                    'published_consumers': {'8.6': {'php-zts-grpc': '0:1.83.0_86.0~rc4-5'}}}))
        import os
        from unittest.mock import patch
        with patch.dict(os.environ, {'SPP_SHARED_LIBRARY_PLAN': str(plan)}):
            result, receipt = self.run_preflight('rpm', [self.candidate()],
                                                [self.package('php-zts-grpc', '1.83.0_86.0~rc4-5')],
                                                migration_cutoffs={'8.6': {}})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(receipt['candidates'][0]['version'], '1.83.0_86.0~rc4-6')

    def test_apk_package_names_remain_unchanged(self):
        result, receipt = self.run_preflight('apk', [self.candidate(name='php-zts-amqp', upstream='2.2.0')],
                                            requires_native=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(receipt['candidates'][0]['name'], 'php-zts-amqp')
        self.assertNotIn('logical_name', receipt['candidates'][0])
        self.assertEqual(receipt['candidates'][0]['published_versions'], [])

    def test_php_prerelease_phase_advances_in_retained_versions(self):
        for kind, tag, marker, separator in [('rpm', '_86.0', '~', '-'),
                                             ('deb', '+php86.0', '~', '-'),
                                             ('apk', 'p86.0', '_', '-r')]:
            if not shutil.which(TOOLS[kind]):
                continue
            for php, suffix in [('8.6.0RC5', marker + 'rc5'), ('8.6.0', ''), ('8.6.1RC1', '')]:
                with self.subTest(kind=kind, php=php):
                    old = '1.13.5' + tag + marker + 'rc2' + separator + '7'
                    candidate = self.candidate(name='frankenphp', upstream='1.13.5', php=php)
                    result, receipt = self.run_preflight(kind, [candidate], [self.package('frankenphp', old)])
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(receipt['candidates'][0]['production_base_version'], '1.13.5' + tag + suffix)

    def test_php_prerelease_rollback_is_not_hidden_in_a_retained_version(self):
        for kind, old in [('rpm', '1.13.5_86.0~rc5-1'), ('deb', '1.13.5+php86.0~rc5-1'), ('apk', '1.13.5p86.0_rc5-r0')]:
            if not shutil.which(TOOLS[kind]):
                continue
            with self.subTest(kind=kind):
                result, receipt = self.run_preflight(kind, [self.candidate(name='frankenphp', upstream='1.13.5', php='8.6.0RC2')],
                                                    [self.package('frankenphp', old)], flags=['--bump'])
                self.assertEqual(result.returncode, 3, result.stderr)
                self.assertEqual(receipt['rejected'][0]['reason'], 'native-version-downgrade')

    def test_pie_always_has_a_stream_only_php_tag(self):
        for kind, tag, separator in [('rpm', '_86', '-'), ('deb', '+php86', '-'), ('apk', 'p86', '-r')]:
            if not shutil.which(TOOLS[kind]):
                continue
            for php in ['8.6.0alpha1', '8.6.0beta3', '8.6.0RC5', '8.6.0', '8.6.1RC1']:
                with self.subTest(kind=kind, php=php):
                    base = '1.5.5' + tag
                    result, receipt = self.run_preflight(kind, [self.candidate(name='pie-zts', upstream='1.5.5', php=php)],
                                                        [self.package('pie-zts', base + separator + '7')], flags=['--bump'])
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(receipt['candidates'][0]['version'], base + separator + '8')

    def test_patch_rc_keeps_compiled_consumer_tag_and_advances_core_version(self):
        for kind, tag, rc, separator in [('rpm', '_86', '~rc1', '-'), ('deb', '+php86', '~rc1', '-'), ('apk', 'p86', '_rc1', '-r')]:
            if not shutil.which(TOOLS[kind]):
                continue
            with self.subTest(kind=kind):
                candidates = [self.candidate(name='frankenphp', upstream='1.13.5', php='8.6.1RC1'),
                              self.candidate(name='php-zts-cli', upstream='8.6.1RC1', php='8.6.1RC1')]
                packages = [self.package('frankenphp', '1.13.5' + tag + separator + '7'),
                            self.package('php-zts-cli', '8.6.0' + separator + '7')]
                result, receipt = self.run_preflight(kind, candidates, packages, flags=['--bump'])
                self.assertEqual(result.returncode, 0, result.stderr)
                versions = {item['name']: item['version'] for item in receipt['candidates']}
                self.assertEqual(versions['frankenphp'], '1.13.5' + tag + separator + '8')
                self.assertEqual(versions['php-zts-cli'], '8.6.1' + rc + separator + ('0' if kind == 'apk' else '1'))

    def test_apk_package_renames_are_rejected(self):
        candidate = self.candidate(name='php-zts-amqp-shared', logical_name='php-zts-amqp',
                                   component='igbinary', upstream='2.2.0')
        result, receipt = self.run_preflight('apk', [candidate], requires_native=False)
        self.assertEqual(result.returncode, 1)
        self.assertIsNone(receipt)
        self.assertIn('package renames are not supported', result.stderr)

    def test_apk_same_upstream_rebuild_is_blocked_without_inventing_a_version(self):
        candidate = self.candidate(name='php-zts-igbinary', component='igbinary', upstream='3.2.17RC1', php='8.2.35')
        result, receipt = self.run_preflight('apk', [candidate],
                                            [self.package('php-zts-igbinary', '3.2.17~rc1_82-r1')], php_stream='8.2')
        self.assertEqual(result.returncode, 3, result.stderr)
        record = receipt['candidates'][0]
        self.assertEqual(record['version'], '3.2.17_rc1_p82-r0')
        self.assertEqual(receipt['rejected'][0]['reason'], 'native-version-downgrade')
        self.assertEqual(record['name'], 'php-zts-igbinary')
        self.assertEqual(record['upstream_version'], '3.2.17RC1')
        self.assertEqual(record['published_versions'], ['3.2.17~rc1_82-r1'])

    def test_apk_source_rollback_remains_rejected(self):
        old = [self.package('php-zts-igbinary', '3.2.17~rc1_82-r1')]
        for upstream, reason in [('3.2.16', 'upstream-version-rollback'), ('3.2.17alpha1', 'upstream-phase-rollback')]:
            with self.subTest(upstream=upstream):
                result, receipt = self.run_preflight('apk', [self.candidate(name='php-zts-igbinary', upstream=upstream, php='8.2.35')],
                                                    old, php_stream='8.2')
                self.assertEqual(result.returncode, 3, result.stderr)
                self.assertEqual(receipt['rejected'][0]['reason'], reason)

    def test_apk_later_upstream_release_uses_normal_version_and_revisions(self):
        packages = [self.package('php-zts-igbinary', '3.2.17~rc1_82-r100'),
                    self.package('php-zts-igbinary', '3.2.18p82-r5')]
        result, receipt = self.run_preflight('apk', [self.candidate(name='php-zts-igbinary', upstream='3.2.18', php='8.2.35')],
                                            packages, flags=['--bump'], php_stream='8.2', migration_cutoffs={'8.2': {}})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(receipt['candidates'][0]['version'], '3.2.18p82-r6')

    def test_apk_checks_each_legacy_source_phase_when_native_versions_compare_equal(self):
        packages = [self.package('php-zts-igbinary', '3.2.17~rc1_82-r1'),
                    self.package('php-zts-igbinary', '3.2.17~rc2_82-r1')]
        result, receipt = self.run_preflight('apk', [self.candidate(name='php-zts-igbinary',
                                                                  upstream='3.2.17RC1', php='8.2.35')],
                                            packages, php_stream='8.2')
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertEqual({item['published']: item['reason'] for item in receipt['rejected']}, {
            '3.2.17~rc1_82-r1': 'native-version-downgrade',
            '3.2.17~rc2_82-r1': 'upstream-phase-rollback',
        })

    def test_apk_valid_legacy_source_phase_cannot_roll_back(self):
        for old, upstream in [('3.2.17_rc1_p82-r0', '3.2.17alpha1'),
                              ('3.2.17_rc2_p82-r0', '3.2.17RC1')]:
            with self.subTest(old=old, upstream=upstream):
                result, receipt = self.run_preflight('apk', [self.candidate(name='php-zts-igbinary',
                                                                          upstream=upstream, php='8.2.35')],
                                                    [self.package('php-zts-igbinary', old)], php_stream='8.2')
                self.assertEqual(result.returncode, 3, result.stderr)
                self.assertEqual(receipt['rejected'][0]['reason'], 'upstream-phase-rollback')
                self.assertEqual(receipt['rejected'][0]['published'], old)

    def test_apk_valid_legacy_source_phase_can_stay_or_advance(self):
        for upstream in ['3.2.17RC1', '3.2.17RC2', '3.2.17']:
            with self.subTest(upstream=upstream):
                result, receipt = self.run_preflight('apk', [self.candidate(name='php-zts-igbinary',
                                                                          upstream=upstream, php='8.2.35')],
                                                    [self.package('php-zts-igbinary', '3.2.17_rc1_p82-r0')],
                                                    php_stream='8.2')
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertTrue(receipt['passed'])


if __name__ == '__main__':
    unittest.main()
