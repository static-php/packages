import contextlib
import io
import os
import runpy
import tempfile
import unittest
from pathlib import Path


script = Path(__file__).resolve().parents[1] / "bin/createrepo_static"
with tempfile.TemporaryDirectory(prefix="createrepo-versions-") as temporary:
    previous = os.getcwd()
    try:
        os.chdir(temporary)
        with contextlib.redirect_stdout(io.StringIO()):
            repo = runpy.run_path(str(script))
    finally:
        os.chdir(previous)


class PackageVersions(unittest.TestCase):
    def test_libraries_are_non_modular(self):
        for name, version in [('php-zts-lib-openssl', '3.5.1'), ('php-zts-lib-gcc-runtime', '16.1.0'), ('php-zts-lib-libxml2-debuginfo', '2.14.0~rc1')]:
            filename = f'{name}-{version}-1.el10.x86_64.rpm'
            self.assertEqual(repo['parse_rpm_info'](filename), (name, version, '1.el10', 'x86_64', 'libraries'))
            self.assertEqual(repo['build_module_structure']({'libraries': [filename]}, 'el10', 'x86_64'), [])
        module = repo['build_module_structure']({'libraries': ['php-zts-lib-zlib-1.3.1-1.el10.x86_64.rpm'], '8.4': ['php-zts-cli-8.4.1-1.el10.x86_64.rpm']}, 'el10', 'x86_64')[0]
        self.assertEqual(module['data']['artifacts']['rpms'], ['php-zts-cli-0:8.4.1-1.el10.x86_64'])

    def test_tagged_packages(self):
        for name in ["php-zts-xdebug", "php-zts-xdebug-debuginfo", "pie-zts", "pie-zts-debuginfo", "frankenphp", "frankenphp-debuginfo"]:
            for version in ["3.6.0~dev_86~beta2", "3.6.0_86.0~rc3+ext~alpha1", "3.6.0_86.0~rc3+ext~~dev", "3.6.0_86.0+ext~alpha1", "3.6.0_86.0+ext", "3.6.0_86.0", "3.6.0~alpha1_86.0", "3.6.0~~dev_86.0", "3.6.0_86.0~rc3", "3.6.0~alpha1_86.0~rc3", "3.6.0~~dev_86.0~rc3", "3.6.0~~dev_86.0~~dev", "3.6.0~alpha2~~dev0_86.0~rc4", "3.6.0~beta2~~dev10_86.0~rc4~~dev0", "3.6.0~rc2~~dev9_86.0", "3.6.0_86.0~rc4~~dev0+ext~alpha2~~dev10"]:
                with self.subTest(name=name, version=version):
                    filename = f"{name}-{version}-1.el10.x86_64.rpm"
                    info = repo["parse_rpm_info"](filename)
                    self.assertEqual(info, (name, version, "1.el10", "x86_64", "8.6"))
                    module = repo["build_module_structure"]({"8.6": [filename]}, "el10", "x86_64")[0]
                    self.assertEqual(module["data"]["artifacts"]["rpms"], [f"{name}-0:{version}-1.el10.x86_64"])

    def test_dev_runtime(self):
        for version in ["8.6.0~~dev", "8.6.0~alpha2~~dev0", "8.6.0~beta3~~dev10", "8.6.0~rc4~~dev9"]:
            with self.subTest(version=version):
                filename = f"php-zts-cli-{version}-1.el10.x86_64.rpm"
                self.assertEqual(repo["parse_rpm_info"](filename), ("php-zts-cli", version, "1.el10", "x86_64", "8.6"))

    def test_stream_only_versions(self):
        for name in ["frankenphp", "pie-zts", "php-zts-swoole", "php-zts-xdebug-debuginfo"]:
            for upstream in ["3.6.0", "3.6.0~alpha1", "3.6.0~~dev0", "3.6.0~rc2~~dev9"]:
                for tag, stream in [("82", "8.2"), ("86", "8.6"), ("100", "10.0")]:
                    for phase in [""] if name == "pie-zts" else ["", "~rc5", "~beta3", "~~dev0"]:
                        with self.subTest(name=name, upstream=upstream, tag=tag, phase=phase):
                            version = f"{upstream}_{tag}{phase}"
                            filename = f"{name}-{version}-2.x86_64.rpm"
                            self.assertEqual(repo["parse_rpm_info"](filename), (name, version, "2", "x86_64", stream))
                            module = repo["build_module_structure"]({stream: [filename]}, "el8", "x86_64")[0]
                            self.assertEqual(module["data"]["artifacts"]["rpms"], [f"{name}-0:{version}-2.x86_64"])

    def test_stable_packages(self):
        for name, version in [("frankenphp", "1.13.1_82.35"), ("pie-zts", "1.3.4_82.35"), ("php-zts-clickhouse", "0.13.0_82.35")]:
            with self.subTest(name=name):
                filename = f"{name}-{version}-1.el10.aarch64.rpm"
                self.assertEqual(repo["parse_rpm_info"](filename), (name, version, "1.el10", "aarch64", "8.2"))

    def test_stream_tags(self):
        for tag, stream in [("85", "8.5"), ("86", "8.6"), ("90", "9.0"), ("99", "9.9"), ("100", "10.0")]:
            with self.subTest(tag=tag):
                for suffix in ["~rc3+ext~alpha1", "~rc3~~dev0+ext~alpha2~~dev10", "~rc3~~dev0", "~rc3", ""]:
                    self.assertEqual(repo["php_stream"](f"3.6.0_{tag}.10{suffix}"), stream)


if __name__ == "__main__":
    unittest.main()
