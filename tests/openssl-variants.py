#!/usr/bin/env python3

import copy
import importlib.util
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
                      "package_versions": {"rpm": "3.6.3-12"}}
        self.manifest = {"schema": 1, "linkage": "shared", "libc": "glibc", "architecture": "x86_64",
                         "runtime_libdir": "/usr/lib/php-zts/lib", "libraries": {"openssl": self.entry,
                          "other": {"files": ["lib/libother.so.1"]}}}
        variants.write(self.sdk / variants.MANIFEST, self.manifest)
        variants.write(self.sdk / variants.STATE, {"compatibility": "el8-common", "libraries": {
            "openssl": {"recipe_hash": "recipe1", "dependency_signatures": {"zlib": {"source_hash": "z1"}}}}})
        self.environment = {"architecture": "x86_64", "environment": {"OPENSSLDIR": "/etc/pki/tls", "SPC_LIBC": "glibc"},
                            "options": {"with-shared-libdir": "/usr/lib/php-zts/lib"}}
        self.output = self.sdk / "openssl-variants"
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
                variants.fingerprint(root, sdk, environment, manifest["libraries"]["openssl"], profile), variants.common_identity(sdk, manifest["libraries"]["openssl"]))

    def build(self):
        with patch.object(variants, "assert_el8"), patch.object(variants, "probe_directory"), \
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
                patch.object(variants.subprocess, 'run', side_effect=AssertionError('cached profile invoked a tool')), \
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

    def test_legacy_abi_dumps_are_not_required_or_exported(self):
        manifest = variants.read(self.sdk / variants.MANIFEST)
        manifest["libraries"]["openssl"].update(abi_dump={"libcrypto.so.3": "missing.xml"},
            abi_dump_hashes={"libcrypto.so.3": "unused"}, abi_empty=[],
            abi_reference_version="3.0", abi_comparison={"result": "old"}, abi_epoch=2,
            abi_capability="php-zts-lib-openssl-abi-2", abi_capabilities=["php-zts-lib-openssl-abi-2"])
        variants.write(self.sdk / variants.MANIFEST, manifest)
        self.build()
        for profile in variants.PROFILES:
            receipt = variants.read(self.output / profile / "overlay.json")
            self.assertNotIn("missing.xml", receipt["files"])
            for field in ("abi_dump", "abi_dump_hashes", "abi_empty", "abi_reference_version", "abi_comparison", "abi_epoch", "abi_capability", "abi_capabilities"):
                self.assertNotIn(field, receipt["entry"])
        variants.apply(self.sdk, self.output, "deb")
        entry = variants.read(self.sdk / variants.MANIFEST)["libraries"]["openssl"]
        self.assertNotIn("abi_epoch", entry)
        self.assertNotIn("abi_dump", entry)

    def test_legacy_profiles_reuse_runtime_files_without_dumps_or_recompiling(self):
        self.build()
        index = variants.read(self.output / "variants.json")
        for profile in variants.PROFILES:
            directory = self.output / profile
            receipt = variants.read(directory / "overlay.json")
            receipt.pop("sonames")
            receipt["abi"] = {"types_verified": True}
            receipt["entry"]["abi_dump"] = {"libcrypto.so.3": "missing.xml"}
            receipt["files"]["missing.xml"] = {"sha256": "unused"}
            variants.write(directory / "overlay.json", receipt)
            index["profiles"][profile]["receipt_sha256"] = variants.file_hash(directory / "overlay.json")
        variants.write(self.output / "variants.json", index)
        self.assertEqual(self.build()["rebuilt_profiles"], [])
        variants.apply(self.sdk, self.output, "deb")
        self.assertNotIn("abi_dump", variants.read(self.sdk / variants.MANIFEST)["libraries"]["openssl"])

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
        self.assertEqual(variants.runtime_paths({"files": ["lib/libcrypto.so.3"], "runtime_assets": [], "abi_dump": []}), {"lib/libcrypto.so.3"})
        self.assertEqual(variants.runtime_paths({"files": [], "runtime_assets": [{"source": "lib/engines-3/afalg.so", "destination": "/usr/lib/php-zts/lib/engines-3/afalg.so"}]}), {"lib/engines-3/afalg.so"})
        with self.assertRaisesRegex(RuntimeError, "structured runtime asset"):
            variants.runtime_paths({"runtime_assets": ["lib/engines-3/afalg.so"]})

    def test_paths_cannot_escape(self):
        for name in ["../bin/php", "/usr/bin/php", "", "."]:
            with self.assertRaises(RuntimeError):
                variants.relative(name)

    def test_only_library_names_and_sonames_define_compatibility(self):
        changed = copy.deepcopy(self.entry)
        changed["version"] = "4.0.0"
        changed["elf"]["lib/libcrypto.so.3"]["sha256"] = "different implementation"
        changed["elf"]["lib/libcrypto.so.3.2"] = changed["elf"].pop("lib/libcrypto.so.3")
        variants.compare_names(self.entry, changed)
        changed["elf"]["lib/libcrypto.so.3.2"]["soname"] = "libcrypto.so.4"
        with self.assertRaisesRegex(RuntimeError, "SONAMEs"):
            variants.compare_names(self.entry, changed)

    def test_added_removed_or_renamed_library_is_rejected(self):
        for operation in ("add", "remove", "rename"):
            with self.subTest(operation=operation):
                changed = copy.deepcopy(self.entry)
                if operation == "add":
                    changed["elf"]["lib/libextra.so.1"] = {"soname": "libextra.so.1"}
                elif operation == "remove":
                    changed["elf"].pop("lib/libcrypto.so.3")
                else:
                    changed["elf"]["lib/libcrypto.so.3"]["soname"] = "librenamed.so.3"
                with self.assertRaisesRegex(RuntimeError, "SONAMEs"):
                    variants.compare_names(self.entry, changed)

    def test_names_without_soname_use_the_shared_library_filename(self):
        entry = {"elf": {"lib/plugin.so": {}, "bin/helper": {}}}
        self.assertEqual(variants.library_names(entry), ["plugin.so"])
        with self.assertRaisesRegex(RuntimeError, "SONAMEs"):
            variants.compare_names(entry, {"elf": {"lib/renamed.so": {}}})

    def test_library_names_have_no_openssl_specific_rules(self):
        entry = {"elf": {"lib/libother.so.7": {"soname": "libother.so.7"}}}
        variants.compare_names(entry, copy.deepcopy(entry))

    @unittest.skipUnless(all(shutil.which(name) for name in ["gcc", "readelf", "objcopy", "patchelf"]), "native ELF relocation tools unavailable")
    def test_rpath_relocation_preserves_code_and_dependencies(self):
        source = self.root / "relocation.c"
        output = self.root / "relocation.so"
        source.write_text("int shared_api(int value) { return value + 42; }\n")
        subprocess.run(["gcc", "-shared", "-fPIC", "-gdwarf-4", "-Wl,-soname,libcrypto.so.3", "-Wl,-rpath,/usr/lib64/php-zts/lib", "-o", str(output), str(source)], check=True)
        before = variants.elf_identity(output, self.root)
        subprocess.run(["patchelf", "--set-rpath", "/usr/lib/php-zts/lib", str(output)], check=True)
        self.assertEqual(before, variants.elf_identity(output, self.root))
        self.assertEqual(subprocess.check_output(["patchelf", "--print-rpath", str(output)], text=True).strip(), "/usr/lib/php-zts/lib")


if __name__ == "__main__":
    unittest.main()
