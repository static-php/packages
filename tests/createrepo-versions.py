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
    def test_tagged_packages(self):
        for name in ["php-zts-xdebug", "php-zts-xdebug-debuginfo", "pie-zts", "pie-zts-debuginfo", "frankenphp", "frankenphp-debuginfo"]:
            for version in ["3.6.0~dev_86~beta2", "3.6.0_86.0~rc3+ext~alpha1", "3.6.0_86.0~rc3+ext~~dev", "3.6.0_86.0+ext~alpha1", "3.6.0_86.0+ext"]:
                with self.subTest(name=name, version=version):
                    filename = f"{name}-{version}-1.el10.x86_64.rpm"
                    info = repo["parse_rpm_info"](filename)
                    self.assertEqual(info, (name, version, "1.el10", "x86_64", "8.6"))
                    module = repo["build_module_structure"]({"8.6": [filename]}, "el10", "x86_64")[0]
                    self.assertEqual(module["data"]["artifacts"]["rpms"], [f"{name}-0:{version}-1.el10.x86_64"])

    def test_dev_runtime(self):
        filename = "php-zts-cli-8.6.0~~dev-1.el10.x86_64.rpm"
        self.assertEqual(repo["parse_rpm_info"](filename), ("php-zts-cli", "8.6.0~~dev", "1.el10", "x86_64", "8.6"))

    def test_stream_tags(self):
        for tag, stream in [("85", "8.5"), ("86", "8.6"), ("90", "9.0"), ("99", "9.9"), ("100", "10.0")]:
            with self.subTest(tag=tag):
                self.assertEqual(repo["php_stream"](f"3.6.0_{tag}.10~rc3+ext~alpha1"), stream)


if __name__ == "__main__":
    unittest.main()
