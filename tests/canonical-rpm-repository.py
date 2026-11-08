import contextlib
import hashlib
import io
import json
import os
import runpy
import shutil
import subprocess
import sys
import tempfile
import http.server
import threading
import unittest
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import yaml


ROOT = Path(__file__).resolve().parents[1]
REPO = runpy.run_path(str(ROOT / "bin/canonical-rpm-repository"))
MODULES = REPO["MODULES"]


class RepositoryMetadata(unittest.TestCase):
    def test_suffix_free_packages_and_layout(self):
        names = ["php-zts-cli-8.5.12-42.x86_64.rpm", "php-zts-lib-zlib-1.3.1-42.aarch64.rpm",
                 "php-zts-imagick-3.8.1_85.12-42.x86_64.rpm", "frankenphp-1.13.3_85.12-42.aarch64.rpm"]
        for name in names:
            self.assertIsNotNone(MODULES["parse_rpm_info"](name))
        layout = "php-zts-runtime-layout-1-1.noarch.rpm"
        self.assertEqual(MODULES["parse_rpm_info"](layout)[-1], "libraries")
        documents = MODULES["module_documents"]([layout, names[0]], "el8", "x86_64", 100)
        module = documents[0]["data"]
        self.assertEqual(module["version"], 100)
        self.assertEqual(module["dependencies"], [{"buildrequires": {"platform": ["el8"]}}])
        self.assertNotIn(layout, module["artifacts"]["rpms"])

    def test_module_publication_versions_increase_even_with_clock_rollback(self):
        self.assertEqual(MODULES["next_module_version"](20990101000000), 20990101000001)

    def test_nginx_preserves_original_legacy_fallback_before_other_histories(self):
        with contextlib.redirect_stdout(io.StringIO()) as output:
            REPO["nginx"](SimpleNamespace(document_root="/mnt/data/rpm", url_prefix="/"))
        config = output.getvalue()
        self.assertIn("el(?:8|9|10)", config)
        self.assertLess(config.index("/$php_repo_arch/$php_repo_platform/"), config.index("/$php_repo_arch/.php-zts/files/"))


@unittest.skipUnless(all(shutil.which(tool) for tool in ["rpmbuild", "rpm", "rpmdev-vercmp", "createrepo_c", "modifyrepo_c"]), "native RPM repository tools required")
class NativeRepository(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="canonical-rpm-native-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.architecture = subprocess.check_output(["rpm", "--eval", "%{_arch}"], text=True).strip()
        self.assertIn(self.architecture, ("x86_64", "aarch64"))
        self.state = self.root / self.architecture / ".php-zts"
        self.top = self.root / "rpmbuild"
        for directory in ("BUILD", "BUILDROOT", "RPMS", "SOURCES", "SPECS", "SRPMS"):
            (self.top / directory).mkdir(parents=True)

    def make_rpm(self, name, version, release, requires=(), provides=(), content="fixture", consumer=None):
        spec = self.top / "SPECS/fixture.spec"
        architecture = "noarch" if name == "php-zts-runtime-layout" else self.architecture
        metadata_path = '/usr/share/php-zts/consumers/' + name + '.json'
        metadata_commands = ["mkdir -p %{buildroot}/usr/share/php-zts/consumers",
                             "echo '" + json.dumps(consumer) + "' > %{buildroot}" + metadata_path] if consumer else []
        spec.write_text("\n".join([
            "Name: " + name, "Version: " + version, "Release: " + str(release), "Summary: Repository transaction fixture",
            "License: MIT", "BuildArch: " + architecture,
            *["Requires: " + item for item in requires], *["Provides: " + item for item in provides],
            "%description", "Canonical repository fixture.", "%install",
            "mkdir -p %{buildroot}/usr/share/canonical-rpm-tests",
            "echo '" + content + "' > %{buildroot}/usr/share/canonical-rpm-tests/" + name, *metadata_commands,
            "%files", "/usr/share/canonical-rpm-tests/" + name, *([metadata_path] if consumer else []), ""
        ]))
        result = subprocess.run(["rpmbuild", "-bb", "--define", "_topdir " + str(self.top),
                                 "--define", "_build_id_links none", str(spec)], capture_output=True, text=True)
        if result.returncode:
            self.fail(result.stdout + result.stderr)
        return self.top / "RPMS" / architecture / f"{name}-{version}-{release}.{architecture}.rpm"

    def stage(self, *rpms, legacy=()):
        incoming = self.root / "incoming"
        if incoming.exists():
            shutil.rmtree(incoming)
        incoming.mkdir()
        for package in rpms:
            shutil.copy2(package, incoming / package.name)
        receipt = self.root / "cohort.json"
        receipt.write_text(json.dumps({"schema": 1, "plan_fingerprint": "native-fixture",
            "files": [{"path": str(package), "sha256": hashlib.sha256(package.read_bytes()).hexdigest()} for package in incoming.glob("*.rpm")]}))
        with contextlib.redirect_stdout(io.StringIO()) as output:
            REPO["stage"](SimpleNamespace(state_root=self.state, incoming=incoming, architecture=self.architecture,
                                          cohort_receipt=receipt, legacy_root=legacy, output=None))
        return Path(json.loads(output.getvalue())["generation"])

    def promote(self, generation, activate_aliases=False):
        with contextlib.redirect_stdout(io.StringIO()):
            REPO["promote"](SimpleNamespace(state_root=self.state, generation=generation, activate_aliases=activate_aliases))

    def test_consumer_index_uses_native_archives_and_retains_verified_unchanged_closure(self):
        consumer = {'schema': 1, 'name': 'php-zts-cli', 'php_stream': '8.5',
                    'architecture': self.architecture, 'libraries': ['zlib']}
        layout = self.make_rpm('php-zts-runtime-layout', '1', 1)
        library = self.make_rpm('php-zts-lib-zlib', '1.3.1', 1, ['php-zts-runtime-layout'], ['php-zts-lib-zlib-abi-1'])
        cli = self.make_rpm('php-zts-cli', '8.5.12', 1,
                            ['php-zts-runtime-layout', 'php-zts-lib-zlib-abi-1'], consumer=consumer)
        first = self.stage(layout, library, cli)
        index = json.loads((first / 'shared-library-consumers.json').read_text())
        self.assertEqual(index['packages'][cli.name]['consumer_metadata'], consumer)
        self.assertEqual(index['packages'][cli.name]['sha256'], hashlib.sha256(cli.read_bytes()).hexdigest())
        self.assertIn('php-zts-consumers', (first / 'repodata/repomd.xml').read_text())
        self.promote(first)
        updated = self.make_rpm('php-zts-lib-zlib', '1.3.2', 1, ['php-zts-runtime-layout'], ['php-zts-lib-zlib-abi-1'])
        with patch.dict(REPO['SHARED_PLAN'], {'archive_runtime_json': Mock(side_effect=AssertionError('Unchanged archive re-extracted'))}):
            second = self.stage(updated)
        later = json.loads((second / 'shared-library-consumers.json').read_text())
        self.assertEqual(later['packages'][cli.name], index['packages'][cli.name])

    def test_native_atomic_generations_dependency_closure_and_legacy_urls(self):
        layout = self.make_rpm("php-zts-runtime-layout", "1", 1)
        library1 = self.make_rpm("php-zts-lib-zlib", "1.3.1", 1, ["php-zts-runtime-layout"], ["php-zts-lib-zlib-abi-1"])
        cli1 = self.make_rpm("php-zts-cli", "8.5.12", 1,
                             ["php-zts-runtime-layout", "php-zts-lib-zlib >= 1.3.1-1", "php-zts-lib-zlib-abi-1"])
        legacy9 = self.root / self.architecture / "el9"
        legacy9.mkdir(parents=True)
        (legacy9 / "modules.yaml").write_text(yaml.safe_dump({"document": "modulemd", "data": {"version": 20990101000000}}))
        (legacy9 / "historic.cache").write_text("el9 cached package")
        legacy8 = legacy9.with_name("el8")
        legacy8.mkdir()
        (legacy8 / "historic.cache").write_text("different el8 historical bytes")
        old_stream = self.make_rpm("php-zts-cli", "8.4.27", "7.el8")
        shutil.copy2(old_stream, legacy8 / old_stream.name)
        REPO["command"](["createrepo_c", "--checksum", "sha256", legacy8])
        generation1 = self.stage(layout, library1, cli1, legacy=[legacy8, legacy9])
        self.assertFalse((self.state / "current").exists())
        manifest1 = REPO["verify_generation"](generation1)
        self.assertIn(old_stream.name, manifest1["packages"])
        self.assertGreater(manifest1["module_version"], 20990101000000)
        self.promote(generation1)
        first_pointer = os.readlink(self.state / "current")
        first_metadata = next((generation1 / "repodata").glob("*-primary.xml.*"))
        library2 = self.make_rpm("php-zts-lib-zlib", "1.3.1", 2, ["php-zts-runtime-layout"], ["php-zts-lib-zlib-abi-1"])
        generation2 = self.stage(library2)
        self.assertEqual(os.readlink(self.state / "current"), first_pointer)
        self.assertIn(cli1.name, REPO["verify_generation"](generation2)["packages"])
        self.assertEqual((generation1 / cli1.name).stat().st_ino, (generation2 / cli1.name).stat().st_ino)
        self.promote(generation2)
        self.assertNotEqual(os.readlink(self.state / "current"), first_pointer)
        self.assertEqual((self.state / "files" / library1.name).read_bytes(), library1.read_bytes())
        for platform in ("el8", "el9", "el10"):
            prefix = "/" + self.architecture + "/" + platform + "/"
            self.assertEqual(REPO["public_file"](self.root, prefix + "repodata/repomd.xml").read_bytes(), (generation2 / "repodata/repomd.xml").read_bytes())
            self.assertEqual(REPO["public_file"](self.root, prefix + "repodata/" + first_metadata.name).read_bytes(), first_metadata.read_bytes())
            self.assertEqual(REPO["public_file"](self.root, prefix + library1.name).read_bytes(), library1.read_bytes())
        self.assertEqual(REPO["public_file"](self.root, "/" + self.architecture + "/el9/historic.cache").read_text(), "el9 cached package")

        broken = self.make_rpm("php-zts-cli", "8.5.12", 2, ["php-zts-lib-openssl >= 3.6.0"])
        pointer = os.readlink(self.state / "current")
        with self.assertRaisesRegex(RuntimeError, "Incomplete repository"):
            self.stage(broken)
        self.assertEqual(os.readlink(self.state / "current"), pointer)
        with self.assertRaisesRegex(RuntimeError, "downgrade"):
            self.stage(library1)
        rewritten = self.make_rpm("php-zts-lib-zlib", "1.3.1", 2, ["php-zts-runtime-layout"],
                                  ["php-zts-lib-zlib-abi-1"], content="changed without a revision")
        with self.assertRaisesRegex(RuntimeError, "same NEVRA"):
            self.stage(rewritten)
        with self.assertRaisesRegex(RuntimeError, "did not advance"):
            self.promote(generation1)
        generation3 = self.stage(self.make_rpm("php-zts-lib-zlib", "1.3.1", 3, ["php-zts-runtime-layout"], ["php-zts-lib-zlib-abi-1"]))
        with (generation3 / "modules.yaml").open("a") as output:
            output.write("# changed after validation\n")
        with self.assertRaisesRegex(RuntimeError, "changed after validation"):
            self.promote(generation3)
        self.assertEqual(os.readlink(self.state / "current"), pointer)

        self.promote(generation2, activate_aliases=True)
        for platform in ("el8", "el9", "el10"):
            alias = self.root / self.architecture / platform
            self.assertTrue(alias.is_symlink())
            self.assertEqual((alias / "repodata/repomd.xml").read_bytes(), (generation2 / "repodata/repomd.xml").read_bytes())
            self.assertEqual((alias / library1.name).read_bytes(), library1.read_bytes())
            self.assertEqual((alias / library2.name).stat().st_ino, (generation2 / library2.name).stat().st_ino)
        self.assertEqual((self.root / self.architecture / "el9/historic.cache").read_text(), "el9 cached package")
        self.assertEqual((self.root / self.architecture / "el8/historic.cache").read_text(), "different el8 historical bytes")

        directory = str(self.root)
        class QuietHandler(http.server.SimpleHTTPRequestHandler):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, directory=directory, **kwargs)

            def log_message(self, *args):
                pass

        static_server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), QuietHandler)
        thread = threading.Thread(target=static_server.serve_forever)
        thread.start()
        try:
            for platform in ("el8", "el9", "el10"):
                url = f"http://127.0.0.1:{static_server.server_port}/{self.architecture}/{platform}/repodata/repomd.xml"
                with urllib.request.urlopen(url, timeout=5) as response:
                    self.assertEqual(response.read(), (generation2 / "repodata/repomd.xml").read_bytes())
        finally:
            static_server.shutdown()
            static_server.server_close()
            thread.join(timeout=10)

        server = subprocess.Popen([sys.executable, str(ROOT / "bin/canonical-rpm-repository"), "serve",
                                   "--repository-root", str(self.root), "--port", "0"],
                                  stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        try:
            ready = json.loads(server.stdout.readline())
            for platform in ("el8", "el9", "el10"):
                url = f"http://127.0.0.1:{ready['port']}/{self.architecture}/{platform}/repodata/repomd.xml"
                with urllib.request.urlopen(url, timeout=5) as response:
                    self.assertEqual(response.read(), (generation2 / "repodata/repomd.xml").read_bytes())
        finally:
            server.terminate()
            server.wait(timeout=10)
            server.stdout.close()


if __name__ == "__main__":
    unittest.main()
