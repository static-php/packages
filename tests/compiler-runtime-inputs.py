#!/usr/bin/env python3

import copy
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


REPOSITORY = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("compiler_runtime_inputs", REPOSITORY / ".github/actions/shared-libraries/compiler-runtime-inputs.py")
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def inputs(mode="shared"):
    semantic = {"mode": mode, "soname": "libstdc++.so.6"} if mode == "shared" else {"mode": mode, "archive_sha256": "a" * 64}
    semantic["headers_sha256"] = "c" * 64
    return {"schema": 1, "semantic": semantic, "fingerprint": MODULE.digest(semantic)}


class RuntimeInputs(unittest.TestCase):
    def test_real_shared_and_embedded_runtime_selection(self):
        with tempfile.TemporaryDirectory(prefix="spp-cxx-inputs-") as directory:
            root = Path(directory)
            (root / "lib").mkdir()
            c = root / "plain.c"
            c.write_text("int plain(int value) { return value + 1; }\n")
            cxx = root / "runtime.cpp"
            cxx.write_text('#include <string>\nextern "C" int length(const char *value) { std::string s(value); return s.size(); }\n')
            pure = root / "pure.cpp"
            pure.write_text('int cpp_only(int value) { return value + 1; }\n')
            inline = root / "inline.cpp"
            inline.write_text('#include <array>\nextern "C" int inline_cpp(int value) { std::array<int,2> a{{value,value+1}}; return a[0]+a[1]; }\n')
            subprocess.check_call([os.environ.get("CC", "gcc"), "-shared", "-fPIC", str(c), "-o", str(root / "lib/plain.so")])
            compiler = os.environ.get("CXX", "g++")
            subprocess.check_call([compiler, "-shared", "-fPIC", "-Wl,--as-needed", str(pure), "-o", str(root / "lib/pure.so")])
            subprocess.check_call([compiler, "-shared", "-fPIC", "-O3", "-g", "-gdwarf-4", "-Wl,--as-needed", str(inline), "-o", str(root / "lib/inline.so")])
            subprocess.check_call([compiler, "-shared", "-fPIC", str(cxx), "-Wl,--no-as-needed", "-l:libstdc++.so.6", "-Wl,--as-needed", "-o", str(root / "lib/shared.so")])
            subprocess.check_call([compiler, "-shared", "-fPIC", "-static-libstdc++", "-Wl,--exclude-libs,ALL", "-Wl,--build-id=sha1", str(cxx), "-o", str(root / "lib/embedded.so")])
            manifest = {"schema": 1, "libraries": {name: {"files": ["lib/" + name + ".so"]} for name in ["plain", "pure", "inline", "shared", "embedded"]}}
            new = MODULE.scan_sdk(root, inputs(), manifest)
            self.assertEqual(new["must_rebuild"], ["embedded"])
            self.assertEqual(new["affected_static_libraries"], ["embedded"])
            self.assertFalse(new["libraries"]["plain"]["uses_cxx_runtime"])
            self.assertTrue(new["libraries"]["shared"]["uses_cxx_runtime"])
            self.assertFalse(new["libraries"]["shared"]["embedded_cxx_runtime"])
            old = MODULE.scan_sdk(root, inputs("static"), manifest)
            self.assertEqual(old["libraries"]["plain"]["fingerprint"], new["libraries"]["plain"]["fingerprint"])
            self.assertEqual(old["libraries"]["pure"]["fingerprint"], new["libraries"]["pure"]["fingerprint"])
            self.assertEqual(old["libraries"]["inline"]["fingerprint"], new["libraries"]["inline"]["fingerprint"])
            self.assertTrue(new["libraries"]["inline"]["uses_cxx_headers"])
            changed_headers = inputs()
            changed_headers["semantic"]["headers_sha256"] = "d" * 64
            changed_headers["fingerprint"] = MODULE.digest(changed_headers["semantic"])
            changed = MODULE.scan_sdk(root, changed_headers, manifest)
            self.assertNotEqual(new["libraries"]["pure"]["fingerprint"], changed["libraries"]["pure"]["fingerprint"])
            self.assertNotEqual(new["libraries"]["inline"]["fingerprint"], changed["libraries"]["inline"]["fingerprint"])
            self.assertEqual(new["libraries"]["plain"]["fingerprint"], changed["libraries"]["plain"]["fingerprint"])
            for name in ["shared", "embedded"]:
                self.assertNotEqual(old["libraries"][name]["fingerprint"], new["libraries"][name]["fingerprint"])
            (root / "bin").mkdir()
            (root / "modules").mkdir()
            (root / "payload/debug/bin").mkdir(parents=True)
            subprocess.check_call(["objcopy", "--only-keep-debug", str(root / "lib/plain.so"), str(root / "payload/debug/bin/php-zts.debug")])
            subprocess.check_call(["strip", "--strip-all", str(root / "lib/plain.so")])
            subprocess.check_call(["objcopy", "--add-gnu-debuglink=" + str(root / "payload/debug/bin/php-zts.debug"), str(root / "lib/plain.so")])
            subprocess.check_call(["objcopy", "--only-keep-debug", str(root / "lib/embedded.so"), str(root / "payload/debug/bin/frankenphp.debug")])
            subprocess.check_call(["strip", "--strip-all", str(root / "lib/embedded.so")])
            (root / "build-payload.json").write_text(json.dumps({"prepared": {
                "bin/frankenphp": {"debug": {"path": "payload/debug/bin/frankenphp.debug"}},
                "bin/php": {"debug": {"path": "payload/debug/bin/php-zts.debug"}}
            }}))
            shutil.copyfile(root / "lib/plain.so", root / "bin/php")
            shutil.copyfile(root / "lib/embedded.so", root / "bin/frankenphp")
            shutil.copyfile(root / "lib/shared.so", root / "modules/imagick-zts-86.so")
            consumers = MODULE.scan_payload(root, inputs())
            self.assertEqual(set(consumers["consumers"]), {"core", "frankenphp", "imagick"})
            self.assertEqual(consumers["must_rebuild"], ["frankenphp"])
            self.assertFalse(consumers["consumers"]["core"]["uses_cxx_runtime"])
            self.assertTrue(consumers["consumers"]["frankenphp"]["files"][0]["debug_companion"])
            self.assertTrue(consumers["consumers"]["core"]["files"][0]["debug_companion"])
            filtered = MODULE.scan_payload(root, inputs(), {"modules/imagick-zts-86.so"})
            self.assertEqual(set(filtered["consumers"]), {"imagick"})

    def test_shared_debug_payload_changes_do_not_change_link_semantics(self):
        first = inputs()
        first["evidence"] = {"runtime_sha256": "a" * 64}
        second = copy.deepcopy(first)
        second["evidence"]["runtime_sha256"] = "b" * 64
        self.assertEqual(first["fingerprint"], second["fingerprint"])

    def test_manifest_escape_is_rejected(self):
        with tempfile.TemporaryDirectory(prefix="spp-cxx-inputs-") as directory:
            with self.assertRaisesRegex(RuntimeError, "Missing or escaping"):
                MODULE.scan_sdk(Path(directory), inputs(), {"schema": 1, "libraries": {"bad": {"files": ["../outside.so"]}}})

    def test_untrusted_input_fingerprint_is_rejected(self):
        with tempfile.TemporaryDirectory(prefix="spp-cxx-inputs-") as directory:
            value = inputs()
            value["semantic"]["mode"] = "static"
            with self.assertRaisesRegex(RuntimeError, "Invalid compiler runtime input"):
                MODULE.scan_sdk(Path(directory), value, {"schema": 1, "libraries": {}})


if __name__ == "__main__":
    unittest.main()
