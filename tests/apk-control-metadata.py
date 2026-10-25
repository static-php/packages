#!/usr/bin/env python3

import argparse
import gzip
import hashlib
import importlib.machinery
import importlib.util
import io
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
LOADER = importlib.machinery.SourceFileLoader("apk_control_metadata", str(ROOT / "bin/apk-control-metadata"))
SPEC = importlib.util.spec_from_loader(LOADER.name, LOADER)
APK = importlib.util.module_from_spec(SPEC)
LOADER.exec_module(APK)
PREDICATES = "php-zts-cli php-zts-shared-runtime=1"
NATIVE_IMAGE = None


def tar(entries, cut=False):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.USTAR_FORMAT) as archive:
        for name, content in entries:
            header = tarfile.TarInfo(name)
            header.size = len(content)
            header.mode = 0o755 if name.startswith(".post-") else 0o644
            header.uid = 123
            header.gid = 456
            header.mtime = 1234567890
            archive.addfile(header, io.BytesIO(content))
        end = buffer.tell()
    return buffer.getvalue()[:end] if cut else buffer.getvalue()


def fixture(path, extra="", data=b"unchanged ELF placeholder", datahash=None):
    payload = gzip.compress(tar([("usr/lib/php-zts/modules/curl.so", data)]), mtime=0)
    digest = datahash or hashlib.sha256(payload).hexdigest()
    metadata = "pkgname = php-zts-curl\npkgver = 8.6.0_rc4-r1\narch = x86_64\ndatahash = " + digest + "\n" + extra
    control = gzip.compress(tar([(".PKGINFO", metadata.encode()), (".post-upgrade", b"#!/bin/sh\nexit 0\n")], cut=True), mtime=0)
    path.write_bytes(control + payload)
    path.chmod(0o640)
    return payload


class MetadataTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="spp-apk-metadata-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.package = self.root / "curl.apk"

    def fields(self, path=None):
        with (path or self.package).open("rb") as source:
            content, offset = APK.control_member(source)
            entries, metadata, fields = APK.read_control(content)
            return entries, metadata, fields, source.read()

    def test_preserves_payload_scripts_and_control_attributes(self):
        payload = fixture(self.package)
        before, _, fields, _ = self.fields()
        result = APK.rewrite(self.package, PREDICATES)
        after, metadata, new_fields, new_payload = self.fields()
        self.assertTrue(result["changed"])
        self.assertEqual(new_payload, payload)
        self.assertEqual(new_fields["datahash"], fields["datahash"])
        self.assertEqual(new_fields["install_if"], [PREDICATES])
        self.assertEqual(len([e for e, _ in after if e.name == ".PKGINFO"]), 1)
        self.assertEqual(after[1][1], before[1][1])
        self.assertEqual(after[1][0].mode, 0o755)
        for (old, _), (new, _) in zip(before, after):
            self.assertEqual((old.uid, old.gid, old.mtime, old.mode), (new.uid, new.gid, new.mtime, new.mode))
        self.assertEqual(self.package.stat().st_mode & 0o777, 0o640)
        self.assertNotIn(b"\0" * 1024, gzip.decompress(self.package.read_bytes()[:len(self.package.read_bytes()) - len(payload)])[-1024:])

    def test_idempotent(self):
        fixture(self.package)
        APK.rewrite(self.package, PREDICATES)
        first = self.package.read_bytes()
        self.assertFalse(APK.rewrite(self.package, PREDICATES)["changed"])
        self.assertEqual(self.package.read_bytes(), first)

    def test_provider_priority_preserves_payload_without_install_if(self):
        payload = fixture(self.package)
        result = APK.rewrite(self.package, provider_priority=100)
        self.assertTrue(result["changed"])
        fields = self.fields()
        self.assertEqual(fields[2]["provider_priority"], ["100"])
        self.assertNotIn("install_if", fields[2])
        self.assertEqual(fields[3], payload)
        first = self.package.read_bytes()
        self.assertFalse(APK.rewrite(self.package, provider_priority=100)["changed"])
        self.assertEqual(self.package.read_bytes(), first)
        with self.assertRaisesRegex(ValueError, "different provider_priority"):
            APK.rewrite(self.package, provider_priority=99)
        for priority in [-1, 65536, "100", True]:
            with self.assertRaisesRegex(ValueError, "provider_priority"):
                APK.rewrite(self.package, provider_priority=priority)

    def test_separate_output_does_not_modify_input(self):
        fixture(self.package)
        before = self.package.read_bytes()
        output = self.root / "selected.apk"
        APK.rewrite(self.package, PREDICATES, output)
        self.assertEqual(self.package.read_bytes(), before)
        self.assertEqual(self.fields(output)[2]["install_if"], [PREDICATES])
        copy_path = self.root / "copy.apk"
        self.assertFalse(APK.rewrite(output, PREDICATES, copy_path)["changed"])
        self.assertEqual(output.read_bytes(), copy_path.read_bytes())

    def test_rejects_signed_input_without_changing_it(self):
        fixture(self.package)
        signed = gzip.compress(tar([(".SIGN.RSA.test.rsa.pub", b"signature")], cut=True), mtime=0) + self.package.read_bytes()
        self.package.write_bytes(signed)
        with self.assertRaisesRegex(ValueError, "Signed APK"):
            APK.rewrite(self.package, PREDICATES)
        self.assertEqual(self.package.read_bytes(), signed)

    def test_rejects_datahash_mismatch_without_output(self):
        fixture(self.package, datahash="0" * 64)
        original = self.package.read_bytes()
        with self.assertRaisesRegex(ValueError, "datahash does not match"):
            APK.rewrite(self.package, PREDICATES)
        self.assertEqual(self.package.read_bytes(), original)
        self.assertEqual(list(self.root.iterdir()), [self.package])

    def test_rejects_existing_different_or_duplicate_predicates(self):
        for extra in ["install_if = another-package\n", "install_if = " + PREDICATES + "\ninstall_if = " + PREDICATES + "\n"]:
            with self.subTest(extra=extra):
                fixture(self.package, extra)
                before = self.package.read_bytes()
                with self.assertRaisesRegex(ValueError, "install_if"):
                    APK.rewrite(self.package, PREDICATES)
                self.assertEqual(self.package.read_bytes(), before)

    def test_rejects_invalid_predicates(self):
        for predicate in ["", "php-zts-cli = 1", "php-zts-cli php-zts-cli", "php-zts-cli;false"]:
            with self.subTest(predicate=predicate), self.assertRaisesRegex(ValueError, "install_if"):
                APK.install_if_value(predicate)

    def test_rejects_truncated_or_extra_members(self):
        fixture(self.package)
        complete = self.package.read_bytes()
        for broken in [complete[:-3], complete + gzip.compress(b"unexpected", mtime=0)]:
            self.package.write_bytes(broken)
            with self.assertRaises(ValueError):
                APK.rewrite(self.package, PREDICATES)
            self.assertEqual(self.package.read_bytes(), broken)

    def test_streams_highly_compressible_large_payload(self):
        payload = fixture(self.package, data=b"x" * (8 * 1024 * 1024))
        APK.rewrite(self.package, PREDICATES)
        self.assertEqual(self.fields()[3], payload)

    @unittest.skipUnless(shutil.which("nfpm") or Path("/home/m/go/bin/nfpm").exists(), "nFPM is unavailable")
    def test_real_nfpm_archive(self):
        nfpm = shutil.which("nfpm") or "/home/m/go/bin/nfpm"
        content = self.root / "module"
        content.write_bytes(b"module payload")
        config = self.root / "nfpm.json"
        config.write_text(json.dumps({"name":"php-zts-curl", "arch":"amd64", "version":"1.0.0", "release":"1", "maintainer":"Test <test@example.invalid>", "description":"fixture", "license":"MIT", "contents":[{"src":str(content), "dst":"/usr/lib/php-zts/modules/curl.so"}]}))
        subprocess.run([nfpm, "package", "--config", str(config), "--packager", "apk", "--target", str(self.package)], check=True, capture_output=True)
        payload = self.fields()[3]
        result = subprocess.run([sys.executable, str(ROOT / "bin/apk-control-metadata"), str(self.package), "--install-if", PREDICATES], check=True, capture_output=True, text=True)
        self.assertTrue(json.loads(result.stdout)["changed"])
        self.assertEqual(self.fields()[3], payload)
        self.assertEqual(self.fields()[2]["install_if"], [PREDICATES])

    def test_native_upgrade_auto_selection_and_opt_out(self):
        if NATIVE_IMAGE is None:
            self.skipTest("pass --native-image to exercise the native APK solver")
        nfpm = shutil.which("nfpm") or "/home/m/go/bin/nfpm"
        if not Path(nfpm).is_file() and shutil.which(nfpm) is None:
            self.skipTest("nFPM is unavailable")
        architecture = "arm64" if platform.machine() in {"aarch64", "arm64"} else "amd64"
        apk_architecture = "aarch64" if architecture == "arm64" else "x86_64"
        repo = self.root / "repo" / apk_architecture
        repo.mkdir(parents=True)
        config = self.root / "package.json"
        payload = self.root / "content"
        payload.write_text("fixture\n")

        def package(name, release, provides=(), depends=(), automatic=False):
            target = repo / f"{name}-1.0.0-r{release}.apk"
            config.write_text(json.dumps({"name":name, "arch":architecture, "version":"1.0.0", "release":str(release), "maintainer":"Test <test@example.invalid>", "description":"fixture", "license":"MIT", "provides":list(provides), "depends":list(depends), "contents":[{"src":str(payload), "dst":"/usr/share/fixture/" + name}]}))
            subprocess.run([nfpm, "package", "--config", str(config), "--packager", "apk", "--target", str(target)], check=True, capture_output=True)
            if automatic:
                APK.rewrite(target, PREDICATES)

        package("php-zts-cli", 1, ["php-zts-curl=1.0.0", "php-zts-fileinfo=1.0.0"])
        for release in [2, 3]:
            package("php-zts-cli", release, ["php-zts-shared-runtime=1"])
        for extension in ["curl", "fileinfo"]:
            package("php-zts-" + extension, 2, depends=["php-zts-cli>=1.0.0", "php-zts-shared-runtime=1"], automatic=True)
        (self.root / "repositories").write_text("/work/repo\n")
        script = """set -eu
cd /work/repo/ARCH
client() { apk --root /client --arch ARCH --allow-untrusted --no-network --repositories-file /work/repositories "$@"; }
indexed() { apk index --allow-untrusted -o APKINDEX.tar.gz "$@"; client update; }
installed() { client info | grep -qx "$1"; }
mkdir -p /client
apk index --allow-untrusted -o APKINDEX.tar.gz php-zts-cli-1.0.0-r1.apk
client add --initdb php-zts-cli
indexed php-zts-cli-1.0.0-r1.apk php-zts-curl-1.0.0-r2.apk php-zts-fileinfo-1.0.0-r2.apk
client upgrade
! installed php-zts-curl
! installed php-zts-fileinfo
indexed php-zts-cli-1.0.0-r2.apk php-zts-curl-1.0.0-r2.apk php-zts-fileinfo-1.0.0-r2.apk
client upgrade
installed php-zts-curl
installed php-zts-fileinfo
indexed php-zts-cli-1.0.0-r3.apk php-zts-curl-1.0.0-r2.apk php-zts-fileinfo-1.0.0-r2.apk
client upgrade
client info -v | grep -qx php-zts-cli-1.0.0-r3
installed php-zts-curl
installed php-zts-fileinfo
client add '!php-zts-curl'
client upgrade
! installed php-zts-curl
installed php-zts-fileinfo
echo native-install-if-upgrade-passed
""".replace("ARCH", apk_architecture)
        container = "spp-apk-control-" + self.root.name
        try:
            result = subprocess.run(["docker", "run", "--rm", "--name", container, "--network", "none", "-v", str(self.root) + ":/work", NATIVE_IMAGE, "sh", "-c", script], capture_output=True, text=True, timeout=60)
        finally:
            subprocess.run(["docker", "rm", "-f", container], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("native-install-if-upgrade-passed", result.stdout)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--native-image")
    arguments, remaining = parser.parse_known_args()
    NATIVE_IMAGE = arguments.native_image
    unittest.main(argv=[sys.argv[0], *remaining])
