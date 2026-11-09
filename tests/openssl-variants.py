#!/usr/bin/env python3

import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("openssl_variants", ROOT / ".github/actions/shared-libraries/openssl-variants.py")
variants = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(variants)


class VariantTests(unittest.TestCase):
    def test_openssl4_requires_provider_path_without_removed_engine_command(self):
        root = Path('/sdk')
        runtime = '/usr/lib/php-zts/lib'
        with patch.object(variants.subprocess, 'check_output', return_value='MODULESDIR: "' + runtime + '/ossl-modules"') as probe:
            self.assertEqual(variants.compiled_plugin_directories(root, '4.1.0', runtime), {'module': runtime + '/ossl-modules'})
        self.assertEqual([call.args[0][-1] for call in probe.call_args_list], ['-m'])

    def test_openssl3_still_requires_both_private_plugin_paths(self):
        runtime = '/usr/lib/php-zts/lib'
        with patch.object(variants.subprocess, 'check_output', side_effect=[
            'ENGINESDIR: "' + runtime + '/engines-3"', 'MODULESDIR: "' + runtime + '/ossl-modules"']):
            self.assertEqual(variants.compiled_plugin_directories(Path('/sdk'), '3.6.5', runtime),
                             {'engine': runtime + '/engines-3', 'module': runtime + '/ossl-modules'})
        with patch.object(variants.subprocess, 'check_output', return_value='MODULESDIR: "/unexpected"'):
            with self.assertRaisesRegex(RuntimeError, 'provenance'):
                variants.compiled_plugin_directories(Path('/sdk'), '4.1.0', runtime)

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="openssl-variant-tests-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.sdk = self.root / "buildroot"
        (self.sdk / "lib").mkdir(parents=True)
        (self.sdk / "include/openssl").mkdir(parents=True)
        (self.sdk / "include/openssl/test.h").write_text("int library_function(void);\n")
        recipe = self.root / "vendor/crazywhalecc/static-php-cli/src/Package/Library/openssl.php"
        recipe.parent.mkdir(parents=True)
        recipe.write_text("same pinned OpenSSL recipe\n")
        (self.sdk / "lib/libcrypto.so.3").write_bytes(b"common crypto")
        (self.sdk / "lib/libssl.so.3").write_bytes(b"common ssl")
        (self.sdk / "lib/libcrypto.so").symlink_to("libcrypto.so.3")
        (self.sdk / "lib/libssl.so").symlink_to("libssl.so.3")
        (self.sdk / "lib/libother.so.1").write_bytes(b"unrelated consumer")
        files = ["lib/libcrypto.so.3", "lib/libcrypto.so", "lib/libssl.so.3", "lib/libssl.so"]
        self.entry = {"source_hash": "a" * 40, "version": "3.6.3", "runtime_libdir": "/usr/lib/php-zts/lib", "files": files,
                      "runtime_assets": {}, "elf": {"lib/libcrypto.so.3": {"soname": "libcrypto.so.3"},
                                                   "lib/libssl.so.3": {"soname": "libssl.so.3"}},
                      "file_hashes": {name: variants.file_hash(self.sdk / name) for name in files},
                      "abi_epoch": 2, "package_versions": {"rpm": "3.6.3-12"}}
        self.manifest = {"schema": 1, "linkage": "shared", "libc": "glibc", "architecture": "x86_64",
                         "runtime_libdir": "/usr/lib/php-zts/lib", "libraries": {"openssl": self.entry,
                          "other": {"files": ["lib/libother.so.1"]}}}
        variants.write(self.sdk / variants.MANIFEST, self.manifest)
        variants.write(self.sdk / variants.STATE, {"compatibility": "el8-common", "libraries": {
            "openssl": {"recipe_hash": "recipe1", "dependency_signatures": {"zlib": {"source_hash": "z1"}}}}})
        self.environment = {"architecture": "x86_64", "environment": {"OPENSSLDIR": "/etc/pki/tls", "SPC_LIBC": "glibc"},
                            "options": {"with-shared-libdir": "/usr/lib/php-zts/lib"}}
        self.output = self.sdk / "openssl-variants"
        self.abi = {"sonames": ["libcrypto.so.3", "libssl.so.3"], "symbols_sha256": "symbols", "types_verified": True}
        self.builds = []

    def fake_producer(self, root, sdk, environment, output, profile):
        self.builds.append(profile)
        with tempfile.TemporaryDirectory(prefix="profile-", dir=self.root) as temporary:
            target = Path(temporary)
            shutil.copytree(sdk / "include", target / "include")
            (target / "lib").mkdir()
            for name in self.entry["files"]:
                variants.copy_file(sdk / name, target / name)
            (target / "lib/libcrypto.so.3").write_bytes(b"deb crypto " + variants.PROFILES[profile].encode())
            manifest = variants.read(sdk / variants.MANIFEST)
            entry = manifest["libraries"]["openssl"]
            entry["file_hashes"] = {name: variants.file_hash(target / name) for name in entry["files"]}
            variants.write(target / variants.MANIFEST, manifest)
            return variants.export_overlay(target, entry, output, profile,
                variants.fingerprint(root, sdk, environment, manifest["libraries"]["openssl"], profile), self.abi, variants.common_identity(sdk, manifest["libraries"]["openssl"]))

    def build(self):
        with patch.object(variants, "assert_el8"), patch.object(variants, "probe_directory"), \
                patch.object(variants, "abi_inventory", return_value=(self.abi, {})), \
                patch.object(variants, "produce", side_effect=self.fake_producer):
            return variants.build(self.root, self.sdk, self.environment, self.output)

    def test_cache_avoids_both_rebuilds_and_unrelated_library_invalidation(self):
        self.assertEqual(self.build()["rebuilt_profiles"], ["deb"])
        self.assertEqual(self.build()["rebuilt_profiles"], [])
        state = variants.read(self.sdk / variants.STATE)
        state["libraries"]["other"] = {"recipe_hash": "updated-other"}
        variants.write(self.sdk / variants.STATE, state)
        self.assertEqual(self.build()["rebuilt_profiles"], [])
        self.assertEqual(self.builds, ["deb"])

    def test_exported_profile_is_readable_by_packaging_uid(self):
        self.build()
        for profile in variants.PROFILES:
            self.assertEqual((self.output / profile).stat().st_mode & 0o777, 0o755)

    def test_cached_profiles_do_not_run_abi_tools(self):
        self.build()
        with patch.object(variants, 'assert_el8'), patch.object(variants, 'probe_directory'), \
                patch.object(variants, 'abi_inventory', side_effect=AssertionError('unchanged ABI')), \
                patch.object(variants, 'produce', side_effect=AssertionError('unchanged profile')):
            result = variants.build(self.root, self.sdk, self.environment, self.output)
        self.assertEqual(result['rebuilt_profiles'], [])

    def test_profile_sdk_keeps_all_owned_dependency_outputs_and_rebuilds_openssl(self):
        dependency_files = ["lib/libz.so.1", "lib/libz.a", "include/zlib.h", "include/zconf.h"]
        for name in dependency_files + ["bin/openssl"]:
            path = self.sdk / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(name.encode())
        self.manifest["libraries"]["openssl"]["depends"] = ["zlib"]
        self.manifest["libraries"]["zlib"] = {"files": ["lib/libz.so.1"]}
        ownership = {"schema": 1, "libraries": {
            "openssl": {"files": self.entry["files"] + ["bin/openssl"]},
            "zlib": {"files": dependency_files},
            "other": {"files": ["lib/libother.so.1"]}}}
        cache = {name: {"package": name, "files": record["files"]}
                 for name, record in ownership["libraries"].items()}
        variants.write(self.sdk / ".shared-library-ownership.json", ownership)
        variants.write(self.sdk / ".build.json", cache)
        before = {name: variants.file_record(self.sdk / name) for name in dependency_files}
        target = self.root / "profile-sdk"
        target.mkdir()
        variants.copy_sdk(self.sdk, target, self.manifest)
        self.assertEqual({name: variants.file_record(target / name) for name in dependency_files}, before)
        self.assertEqual(variants.read(target / ".build.json"), {"zlib": cache["zlib"]})
        self.assertEqual(set(variants.read(target / ".shared-library-ownership.json")["libraries"]), {"openssl", "zlib"})
        self.assertFalse((target / "lib/libother.so.1").exists())
        self.assertEqual(variants.read(self.sdk / ".build.json"), cache)
        self.assertEqual({name: variants.file_record(self.sdk / name) for name in dependency_files}, before)

    def test_profile_path_change_only_rebuilds_that_profile(self):
        self.build()
        rpm = (self.output / "rpm/overlay.json").read_bytes()
        with patch.dict(variants.PROFILES, {"deb": "/etc/debian-ssl"}):
            self.assertEqual(self.build()["rebuilt_profiles"], ["deb"])
        self.assertEqual((self.output / "rpm/overlay.json").read_bytes(), rpm)

    def test_source_update_invalidates_both_receipts(self):
        self.build()
        old = variants.read(self.output / "variants.json")
        manifest = variants.read(self.sdk / variants.MANIFEST)
        manifest["libraries"]["openssl"]["source_hash"] = "b" * 40
        variants.write(self.sdk / variants.MANIFEST, manifest)
        self.assertEqual(self.build()["rebuilt_profiles"], ["deb"])
        new = variants.read(self.output / "variants.json")
        for profile in variants.PROFILES:
            self.assertNotEqual(old["profiles"][profile], new["profiles"][profile])

    def test_apply_preserves_other_library_and_source_sdk_and_versions(self):
        self.build()
        with tempfile.TemporaryDirectory(prefix="packaging-", dir=self.root) as temporary:
            target = Path(temporary)
            shutil.copytree(self.sdk, target, dirs_exist_ok=True, symlinks=True)
            before = (self.sdk / "lib/libcrypto.so.3").read_bytes()
            untouched = (target / "lib/libother.so.1").read_bytes()
            versions = variants.read(target / variants.MANIFEST)["libraries"]["openssl"]["package_versions"]
            result = variants.apply(target, self.output, "deb")
            self.assertEqual(result["profile"], "deb")
            self.assertNotEqual((target / "lib/libcrypto.so.3").read_bytes(), before)
            self.assertEqual((target / "lib/libother.so.1").read_bytes(), untouched)
            self.assertEqual((self.sdk / "lib/libcrypto.so.3").read_bytes(), before)
            self.assertEqual(variants.read(target / variants.MANIFEST)["libraries"]["openssl"]["package_versions"], versions)
            self.assertTrue((target / "lib/libcrypto.so").is_symlink())
            marker = variants.read(target / "openssl-variant.json")
            self.assertEqual(marker["files"]["lib/libcrypto.so.3"], variants.file_record(target / "lib/libcrypto.so.3"))

    def test_apply_retains_classified_abi_added_after_profile_export(self):
        self.build()
        manifest = variants.read(self.sdk / variants.MANIFEST)
        relative = "usr/share/php-zts/libraries/abi/openssl/libcrypto.so.3.xml"
        cache = self.sdk / relative
        cache.parent.mkdir(parents=True)
        cache.write_text("classified ABI cache\n")
        manifest["libraries"]["openssl"].update(abi_dump={"libcrypto.so.3": relative},
            abi_dump_hashes={"libcrypto.so.3": variants.file_hash(cache)}, abi_empty=[])
        variants.write(self.sdk / variants.MANIFEST, manifest)
        for profile in variants.PROFILES:
            with self.subTest(profile=profile), tempfile.TemporaryDirectory(dir=self.root) as temporary:
                target = Path(temporary)
                shutil.copytree(self.sdk, target, dirs_exist_ok=True, symlinks=True)
                variants.apply(target, self.output, profile)
                self.assertEqual((target / relative).read_text(), cache.read_text())
                entry = variants.read(target / variants.MANIFEST)["libraries"]["openssl"]
                for field in ('abi_dump', 'abi_dump_hashes', 'abi_empty'):
                    self.assertEqual(entry[field], manifest['libraries']['openssl'][field])
                self.assertEqual(entry["abi_epoch"], manifest["libraries"]["openssl"]["abi_epoch"])
                self.assertEqual((target / "lib/libother.so.1").read_bytes(), b"unrelated consumer")
        self.assertEqual(cache.read_text(), "classified ABI cache\n")

    def test_apply_still_rejects_changed_runtime_ownership(self):
        self.build()
        manifest = variants.read(self.sdk / variants.MANIFEST)
        manifest["libraries"]["openssl"]["files"].append("lib/unexpected.so")
        (self.sdk / "lib/unexpected.so").write_bytes(b"unexpected runtime")
        variants.write(self.sdk / variants.MANIFEST, manifest)
        with self.assertRaisesRegex(RuntimeError, "ownership set"):
            variants.apply(self.sdk, self.output, "rpm")
        self.assertEqual((self.sdk / "lib/libcrypto.so.3").read_bytes(), b"common crypto")

    def test_corrupt_overlay_is_rejected(self):
        self.build()
        (self.output / "deb/files/lib/libcrypto.so.3").write_bytes(b"damaged")
        with self.assertRaisesRegex(RuntimeError, "integrity"):
            variants.apply(self.sdk, self.output, "deb")

    def test_swapped_or_changed_receipt_is_rejected(self):
        self.build()
        receipt = variants.read(self.output / "deb/overlay.json")
        receipt["profile"] = "rpm"
        receipt["openssldir"] = variants.PROFILES["rpm"]
        variants.write(self.output / "deb/overlay.json", receipt)
        with self.assertRaisesRegex(RuntimeError, "integrity"):
            variants.apply(self.sdk, self.output, "deb")

    def test_overlay_cannot_overwrite_php_consumer(self):
        self.build()
        receipt = variants.read(self.output / "deb/overlay.json")
        path = self.output / "deb/files/bin/php"
        path.parent.mkdir()
        path.write_bytes(b"not an OpenSSL-owned file")
        receipt["files"]["bin/php"] = variants.file_record(path)
        variants.write(self.output / "deb/overlay.json", receipt)
        index = variants.read(self.output / "variants.json")
        index["profiles"]["deb"]["receipt_sha256"] = variants.file_hash(self.output / "deb/overlay.json")
        variants.write(self.output / "variants.json", index)
        with self.assertRaisesRegex(RuntimeError, "undeclared"):
            variants.apply(self.sdk, self.output, "deb")

    def test_headers_must_match(self):
        self.build()
        (self.sdk / "include/openssl/test.h").write_text("int incompatible_header(void);\n")
        with self.assertRaisesRegex(RuntimeError, "does not match"):
            variants.apply(self.sdk, self.output, "deb")

    def test_overlay_cannot_mix_compiler_input_generations(self):
        self.build()
        state = variants.read(self.sdk / variants.STATE)
        state["compatibility"] = "different-toolchain-generation"
        variants.write(self.sdk / variants.STATE, state)
        with self.assertRaisesRegex(RuntimeError, "does not match"):
            variants.apply(self.sdk, self.output, "deb")

    def test_relocation_only_accepts_layout_environment_changes(self):
        current = copy.deepcopy(self.environment)
        current["options"]["with-shared-libdir"] = "/usr/lib64/php-zts/lib"
        current["type"] = "glibc"
        variants.relocated_environment(self.environment, current)
        current["environment"]["SPC_DEFAULT_CFLAGS"] = "-march=x86-64-v3"
        with self.assertRaisesRegex(RuntimeError, "compiler, recipe or build flags"):
            variants.relocated_environment(self.environment, current)

    def test_php_empty_arrays_and_structured_runtime_assets(self):
        self.assertEqual(variants.entry_paths({"files": ["lib/libcrypto.so.3"], "runtime_assets": [], "abi_dump": []}), {"lib/libcrypto.so.3"})
        self.assertEqual(variants.entry_paths({"files": [], "runtime_assets": [{"source": "lib/engines-3/afalg.so", "destination": "/usr/lib/php-zts/lib/engines-3/afalg.so"}]}), {"lib/engines-3/afalg.so"})
        with self.assertRaisesRegex(RuntimeError, "structured runtime asset"):
            variants.entry_paths({"runtime_assets": ["lib/engines-3/afalg.so"]})

    def test_paths_cannot_escape(self):
        for name in ["../bin/php", "/usr/bin/php", "", "."]:
            with self.assertRaises(RuntimeError):
                variants.relative(name)

    def test_type_abi_difference_is_fatal(self):
        process = subprocess.CompletedProcess([], 12, "changed function type", "")
        with patch.object(variants.subprocess, "run", return_value=process):
            with self.assertRaisesRegex(RuntimeError, "type ABI"):
                variants.compare_abi(self.abi, self.abi, {"libcrypto.so.3": Path("one")}, {"libcrypto.so.3": Path("two")})

    @unittest.skipUnless(all(shutil.which(name) for name in ["gcc", "readelf", "objcopy", "patchelf"]), "native ELF relocation tools unavailable")
    def test_rpath_relocation_preserves_code_and_symbol_identity(self):
        source = self.root / "relocation.c"
        output = self.root / "relocation.so"
        source.write_text("int shared_api(int value) { return value + 42; }\n")
        subprocess.run(["gcc", "-shared", "-fPIC", "-gdwarf-4", "-Wl,-soname,libcrypto.so.3", "-Wl,-rpath,/usr/lib64/php-zts/lib", "-o", str(output), str(source)], check=True)
        before = variants.elf_identity(output, self.root)
        subprocess.run(["patchelf", "--set-rpath", "/usr/lib/php-zts/lib", str(output)], check=True)
        self.assertEqual(before, variants.elf_identity(output, self.root))
        self.assertEqual(subprocess.check_output(["patchelf", "--print-rpath", str(output)], text=True).strip(), "/usr/lib/php-zts/lib")

    @unittest.skipUnless(shutil.which("gcc") and shutil.which("readelf"), "native ELF tools unavailable")
    def test_native_symbol_inventory_detects_export_changes(self):
        source = self.root / "library.c"
        output = self.root / "library.so"
        source.write_text('const char *configuration_path(void) { return "/etc/pki/tls"; }\n')
        subprocess.run(["gcc", "-shared", "-fPIC", "-o", str(output), str(source)], check=True)
        common = variants.public_symbols(output)
        source.write_text('const char *configuration_path(void) { return "/etc/ssl"; }\n')
        subprocess.run(["gcc", "-shared", "-fPIC", "-o", str(output), str(source)], check=True)
        self.assertEqual(common, variants.public_symbols(output))
        source.write_text('int changed_public_api(void) { return 1; }\n')
        subprocess.run(["gcc", "-shared", "-fPIC", "-o", str(output), str(source)], check=True)
        self.assertNotEqual(common, variants.public_symbols(output))


if __name__ == "__main__":
    unittest.main()
