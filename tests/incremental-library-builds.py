#!/usr/bin/env python3

import copy
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
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / ".github/actions/shared-libraries"))
spec = importlib.util.spec_from_file_location("incremental_libraries", ROOT / ".github/actions/shared-libraries/incremental-build.py")
incremental = importlib.util.module_from_spec(spec)
spec.loader.exec_module(incremental)


class IncrementalLibraryTests(unittest.TestCase):
    def test_reuse_only_rejects_compilation_before_modifying_sdk(self):
        for plan in [{'full': True, 'selected': []}, {'full': False, 'selected': ['base']}]:
            with mock.patch.dict(os.environ, NO_COMPILE='true'), mock.patch.object(incremental.subprocess, 'run') as run:
                with self.assertRaisesRegex(RuntimeError, 'refusing compilation'):
                    incremental.execute(self.root, self.sdk, plan, 'apk')
            run.assert_not_called()

    def test_rebuild_orders_alias_chain_before_targets_without_touching_outputs(self):
        other = incremental.file_snapshot(self.sdk, self.manifest, ['other'])
        target = self.sdk / 'lib/libbase.so.1'
        soname = self.sdk / 'lib/libbase-1.so.1'
        alias = self.sdk / 'lib/libbase.so'
        soname.symlink_to(target.name)
        alias.symlink_to(soname.name)
        ownership = copy.deepcopy(self.manifest)
        ownership['libraries']['base']['files'] += ['lib/' + soname.name, 'lib/' + alias.name]
        incremental.write(self.sdk / '.shared-library-ownership.json', ownership)
        incremental.order_owned_outputs(self.sdk, ['base'])
        files = incremental.read(self.sdk / '.shared-library-ownership.json')['libraries']['base']['files']
        self.assertLess(files.index('lib/' + alias.name), files.index('lib/' + soname.name))
        self.assertLess(files.index('lib/' + soname.name), files.index('lib/' + target.name))
        self.assertTrue(soname.is_symlink())
        self.assertTrue(alias.is_symlink())
        self.assertEqual(target.read_bytes(), b'compiled base')
        self.assertEqual(incremental.file_snapshot(self.sdk, self.manifest, ['other']), other)

    def test_rebuild_alias_cleanup_rejects_escaping_parent_before_removing_links(self):
        outside = self.root / 'outside'
        outside.mkdir()
        (outside / 'link').symlink_to('missing')
        (self.sdk / 'escape').symlink_to(outside, target_is_directory=True)
        valid = self.sdk / 'lib/valid-link'
        valid.symlink_to('missing')
        incremental.write(self.sdk / '.shared-library-ownership.json', {'libraries': {
            'base': {'files': ['lib/valid-link', 'escape/link']}}})
        with self.assertRaisesRegex(RuntimeError, 'Escaping SDK output parent'):
            incremental.order_owned_outputs(self.sdk, ['base'])
        self.assertTrue(valid.is_symlink())
        self.assertTrue((outside / 'link').is_symlink())

    def test_rebuild_alias_cleanup_rejects_conflicting_ownership(self):
        link = self.sdk / 'lib/shared-link'
        link.symlink_to('missing')
        incremental.write(self.sdk / '.shared-library-ownership.json', {'libraries': {
            'base': {'files': ['lib/shared-link']}, 'other': {'files': ['lib/shared-link']}}})
        with self.assertRaisesRegex(RuntimeError, 'Conflicting SDK ownership'):
            incremental.order_owned_outputs(self.sdk, ['base'])
        self.assertTrue(link.is_symlink())

    def test_rebuild_order_rejects_external_target_without_changing_ownership(self):
        outside = self.root / 'outside-file'
        outside.write_bytes(b'keep')
        link = self.sdk / 'lib/external-link'
        link.symlink_to(outside)
        path = self.sdk / '.shared-library-ownership.json'
        incremental.write(path, {'libraries': {'base': {'files': ['lib/external-link']}}})
        before = path.read_bytes()
        with self.assertRaisesRegex(RuntimeError, 'Escaping SDK symlink target'):
            incremental.order_owned_outputs(self.sdk, ['base'])
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(outside.read_bytes(), b'keep')
        self.assertTrue(link.is_symlink())

    def test_rebuild_order_rejects_cyclic_aliases_before_mutating_metadata(self):
        (self.sdk / 'lib/first-link').symlink_to('second-link')
        (self.sdk / 'lib/second-link').symlink_to('first-link')
        path = self.sdk / '.shared-library-ownership.json'
        incremental.write(path, {'libraries': {'base': {'files': ['lib/first-link', 'lib/second-link']}}})
        before = path.read_bytes()
        with self.assertRaisesRegex(RuntimeError, 'Cyclic SDK ownership links'):
            incremental.order_owned_outputs(self.sdk, ['base'])
        self.assertEqual(path.read_bytes(), before)

    def test_recreated_unchanged_soname_link_preserves_output_snapshot(self):
        link = self.sdk / 'lib/libother.so'
        link.symlink_to('libother.so.1')
        entry = self.manifest['libraries']['other']
        entry['files'].append('lib/libother.so')
        entry['file_hashes']['lib/libother.so'] = incremental.signature(link)
        before = incremental.file_snapshot(self.sdk, self.manifest, ['other'])
        timestamp = link.lstat().st_mtime_ns
        link.unlink()
        link.symlink_to('libother.so.1')
        os.utime(link, ns=(timestamp + 1000000000, timestamp + 1000000000), follow_symlinks=False)
        self.assertEqual(incremental.file_snapshot(self.sdk, self.manifest, ['other']), before)
        link.unlink()
        link.symlink_to('libconsumer.so.1')
        with self.assertRaisesRegex(RuntimeError, 'checksum mismatch'):
            incremental.file_snapshot(self.sdk, self.manifest, ['other'])

    def test_unselected_regular_file_timestamp_change_is_still_rejected(self):
        path = self.sdk / 'lib/libother.so.1'
        timestamp = path.stat().st_mtime_ns
        code = 'import os; os.utime(' + repr(str(path)) + ', ns=(' + str(timestamp + 1000000000) + ',)*2)'
        with self.assertRaisesRegex(RuntimeError, 'changed unselected library outputs: lib/libother.so.1'):
            incremental.execute(self.root, self.sdk, self.plan(sources=self.changed('base'), libraries='base'),
                                'rpm', [sys.executable, '-c', code])

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="selective-library-build-")
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.sdk = self.root / "buildroot"
        (self.sdk / "lib").mkdir(parents=True)
        (self.root / "downloads").mkdir()
        self.graph = {
            "base": {"artifact": "base", "depends": [], "inputs": ["base"], "recipe_hash": "base-recipe"},
            "consumer": {"artifact": "consumer-source", "depends": ["base"], "inputs": ["base", "consumer-source"], "recipe_hash": "consumer-recipe"},
            "other": {"artifact": "other", "depends": [], "inputs": ["other"], "recipe_hash": "other-recipe"},
        }
        self.inventory = {"libraries": self.graph, "required_libraries": list(self.graph)}
        self.sources = {}
        for name, record in self.graph.items():
            artifact = record["artifact"]
            body = ("source for " + artifact + " version1").encode()
            filename = artifact + "-1.tar.gz"
            (self.root / "downloads" / filename).write_bytes(body)
            self.sources[artifact] = {"source": {"cache_type": "archive", "filename": filename,
                "hash": hashlib.sha1(body).hexdigest(), "version": "1", "config": {"type": "url", "url": "https://example.invalid/" + filename}}}
        self.manifest = {"schema": 1, "linkage": "shared", "libraries": {}}
        for name, record in self.graph.items():
            relative = "lib/lib" + name + ".so.1"
            (self.sdk / relative).write_bytes(("compiled " + name).encode())
            self.manifest["libraries"][name] = {"source_hash": self.sources[record["artifact"]]["source"]["hash"],
                "files": [relative], "file_hashes": {relative: incremental.signature(self.sdk / relative)},
                "depends": record["depends"]}
        full = {"full": True, "inventory": self.inventory, "effective_sources": self.sources, "compatibility": "compatible", "reused": []}
        self.state = incremental.record_state(full, self.manifest)
        incremental.write(self.root / "downloads/.cache.json", self.sources)
        incremental.write(self.sdk / "shared-libraries.json", self.manifest)
        incremental.write(self.sdk / ".shared-library-ownership.json", self.manifest)
        incremental.write(self.sdk / incremental.STATE, self.state)

    def plan(self, **overrides):
        values = dict(inventory=self.inventory, sources=self.sources, manifest=self.manifest,
                      state=self.state, compatibility="compatible")
        values.update(overrides)
        return incremental.selection(**values)

    def changed(self, artifact, version="2"):
        current = copy.deepcopy(self.sources)
        body = ("source for " + artifact + " version" + version).encode()
        filename = artifact + "-" + version + ".tar.gz"
        (self.root / "downloads" / filename).write_bytes(body)
        current[artifact]["source"].update(filename=filename, hash=hashlib.sha1(body).hexdigest(), version=version)
        return current

    def test_compatible_update_builds_only_changed_root(self):
        plan = self.plan(sources=self.changed("base"), changed_artifacts=["base"])
        self.assertEqual(plan["selected"], ["base"])
        self.assertEqual(plan["reused"], ["consumer", "other"])

    def test_manual_one_or_set_ignores_unrelated_new_sources(self):
        current = self.changed("other")
        for requested in ["base", "base,consumer", "consumer-source"]:
            with self.subTest(requested=requested):
                plan = self.plan(sources=current, libraries=requested)
                self.assertEqual(plan["selected"], [])
                self.assertEqual(plan["effective_sources"]["other"], self.sources["other"])

    def test_manual_scope_compiles_only_changed_requested_inputs(self):
        current = self.changed("base")
        current["other"] = self.changed("other")["other"]
        plan = self.plan(sources=current, libraries="base,consumer")
        self.assertEqual(plan["selected"], ["base"])
        self.assertEqual(plan["effective_sources"]["other"], self.sources["other"])
        self.assertEqual(self.plan(sources=self.changed("consumer-source"), libraries="consumer-source")["selected"], ["consumer"])
        inventory = copy.deepcopy(self.inventory)
        inventory["libraries"]["base"]["recipe_hash"] = "new-base-recipe"
        inventory["libraries"]["other"]["recipe_hash"] = "new-other-recipe"
        self.assertEqual(self.plan(inventory=inventory, libraries="base")["selected"], [])

    def test_manual_unchanged_scope_never_invokes_builder_or_rewrites_sdk(self):
        plan = self.plan(libraries="base,consumer")
        before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in [self.sdk / incremental.STATE, self.sdk / "shared-libraries.json"]}
        with mock.patch.object(incremental, 'file_snapshot', side_effect=AssertionError('No build to compare')), \
                mock.patch.object(subprocess, 'run', side_effect=AssertionError('No compilation requested')):
            result = incremental.execute(self.root, self.sdk, plan, "rpm", ["must-not-run"])
        self.assertFalse(result["built"])
        self.assertEqual({p: (p.read_bytes(), p.stat().st_mtime_ns) for p in before}, before)

    def test_restored_sdk_discards_php_outputs_before_a_library_build(self):
        leftover = self.sdk / 'bin/php'
        leftover.parent.mkdir(parents=True)
        leftover.write_bytes(b'previous inline PHP build')
        incremental.write(self.sdk / 'build-payload.json', {'kind': 'sdk', 'files': {}})
        code = 'from pathlib import Path; assert not Path("buildroot/bin/php").exists(); raise SystemExit(7)'
        with self.assertRaises(subprocess.CalledProcessError) as error:
            incremental.execute(self.root, self.sdk, self.plan(sources=self.changed('base'), libraries='base'),
                                'rpm', [sys.executable, '-c', code])
        self.assertEqual(error.exception.returncode, 7)
        self.assertFalse(leftover.exists())
        self.assertEqual((self.sdk / 'lib/libbase.so.1').read_bytes(), b'compiled base')

    def test_damaged_unchanged_library_fails_without_scheduling_compilation(self):
        with self.assertRaisesRegex(RuntimeError, "checksum mismatch: base"):
            self.plan(libraries="consumer", invalid=["base"])
        self.assertEqual(self.plan(sources=self.changed("base"), changed_artifacts=["base"], invalid=["base"])["selected"], ["base"])

    def test_pending_consumer_is_retained(self):
        manifest = dict(self.manifest, pending_rebuilds={"consumer": {"libbase.so.1": "base"}})
        self.assertEqual(self.plan(manifest=manifest, libraries="other")["selected"], ["consumer"])

    def test_new_forward_dependency_is_added_without_unrelated_libraries(self):
        inventory = copy.deepcopy(self.inventory)
        inventory["libraries"]["new"] = {"artifact": "new", "depends": [], "inputs": ["new"], "recipe_hash": "new-recipe"}
        inventory["libraries"]["consumer"]["depends"].append("new")
        plan = self.plan(inventory=inventory, libraries="consumer")
        self.assertEqual(plan["selected"], ["new"])

    def test_missing_required_library_catches_up_independently_of_latest_snapshot_delta(self):
        inventory = copy.deepcopy(self.inventory)
        sources = copy.deepcopy(self.sources)
        for name in ["new-required", "new-forward", "optional-unused"]:
            inventory["libraries"][name] = {"artifact": name, "depends": [], "inputs": [name], "recipe_hash": name}
            sources[name] = {"source": {"hash": name, "version": "1"}}
        inventory["libraries"]["new-required"]["depends"] = ["new-forward"]
        inventory["required_libraries"].append("new-required")
        plan = self.plan(inventory=inventory, sources=sources, changed_artifacts=[])
        self.assertEqual(plan["selected"], ["new-forward", "new-required"])
        self.assertEqual(plan["reused"], ["base", "consumer", "other"])
        manual = self.plan(inventory=inventory, sources=sources, libraries="base", changed_artifacts=[])
        self.assertEqual(manual["selected"], [])

    def test_recipe_changes_do_not_compile_unchanged_sources(self):
        inventory = copy.deepcopy(self.inventory)
        inventory["libraries"]["base"]["recipe_hash"] = "updated-recipe"
        self.assertEqual(self.plan(inventory=inventory)["selected"], [])

    def test_build_tool_and_source_metadata_changes_do_not_compile_libraries(self):
        inventory = copy.deepcopy(self.inventory)
        inventory["libraries"]["base"]["inputs"].append("cmake")
        current = copy.deepcopy(self.sources)
        current["cmake"] = {"source": {"hash": "new-build-tool"}}
        current["base"]["source"].update(version="renamed-version", filename="renamed-archive.tar.gz",
                                          config={"type": "url", "url": "https://mirror.invalid/base.tar.gz"})
        self.assertEqual(self.plan(inventory=inventory, sources=current, libraries="base")["selected"], [])

    def test_compiler_image_flags_and_headers_do_not_select_libraries(self):
        for requested in ["", "base"]:
            plan = self.plan(compatibility="different compiler, image, flags and headers", libraries=requested)
            self.assertFalse(plan["full"])
            self.assertEqual(plan["selected"], [])
        self.assertEqual(self.plan(changed_artifacts=["base"], compatibility="new-compiler")["selected"], [])

    def test_replayed_update_does_not_rebuild_an_unchanged_library(self):
        plan = self.plan(changed_artifacts=["base"])
        self.assertEqual(plan["selected"], [])
        self.assertEqual(plan["reused"], ["base", "consumer", "other"])
        plan = self.plan(sources=self.changed("other"), changed_artifacts=["base"])
        self.assertEqual(plan["selected"], [])
        self.assertEqual(plan["effective_sources"]["other"], self.sources["other"])

    def test_timestamps_and_php_consumer_changes_do_not_select_libraries(self):
        current = copy.deepcopy(self.sources)
        current["base"]["source"]["time"] = 123
        current["php-src"] = {"source": {"hash": "new PHP"}}
        current["ext-redis"] = {"source": {"hash": "new extension"}}
        current["go"] = {"source": {"hash": "new Go"}}
        path = self.sdk / incremental.STATE
        before = (path.read_bytes(), path.stat().st_mtime_ns)
        plan = self.plan(sources=current)
        self.assertEqual(plan["selected"], [])
        result = incremental.execute(self.root, self.sdk, plan, "rpm", ["must-not-run"])
        self.assertFalse(result["built"])
        self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), before)

    def test_library_sdk_reuse_preserves_original_compilation_identity(self):
        plan = self.plan(compatibility="different", libraries="base")
        before = (self.sdk / incremental.STATE).read_bytes()
        result = incremental.execute(self.root, self.sdk, plan, "rpm", ["must-not-run"])
        self.assertFalse(result["built"])
        self.assertEqual((self.sdk / incremental.STATE).read_bytes(), before)

    def test_only_a_missing_sdk_needs_a_cold_build(self):
        with self.assertRaisesRegex(RuntimeError, "require a recorded buildroot"):
            self.plan(state=None, libraries="base")
        self.assertTrue(self.plan(state=None)["full"])

    def test_incremental_cache_miss_fails_before_invoking_a_builder(self):
        (self.sdk / incremental.STATE).unlink()
        arguments = ['incremental-build.py', 'plan', '--root', str(self.root), '--compatibility', 'new-builder',
                     '--output', str(self.root / 'plan.json')]
        with mock.patch.object(sys, 'argv', arguments), mock.patch.dict(os.environ, {'MODE': 'incremental'}), \
                mock.patch.object(subprocess, 'check_output', return_value=json.dumps(self.inventory)):
            with self.assertRaisesRegex(RuntimeError, 'refusing a full rebuild'):
                incremental.main()

    def test_php_selection_ignores_unrelated_snapshot_library_updates(self):
        incremental.write(self.root / 'downloads/.cache.json', self.changed('base'))
        incremental.write(self.root / 'downloads/build-plan.json', {'libraries': ['base']})
        output = self.root / 'plan.json'
        arguments = ['incremental-build.py', 'plan', '--root', str(self.root), '--compatibility', 'compatible',
                     '--output', str(output)]
        for packages, selected in [('ssh2', []), ('', ['base'])]:
            with mock.patch.object(sys, 'argv', arguments), mock.patch.dict(os.environ, MODE='incremental', PACKAGES=packages), \
                    mock.patch.object(subprocess, 'check_output', return_value=json.dumps(self.inventory)):
                incremental.main()
            self.assertEqual(incremental.read(output)['selected'], selected)

    def test_unknown_manual_library_is_rejected(self):
        with self.assertRaisesRegex(RuntimeError, "Unknown managed library"):
            self.plan(libraries="typo")

    def test_runtime_abi_rebuild_expands_only_affected_unbuilt_consumers(self):
        current = self.changed("base")
        first = self.plan(sources=current, changed_artifacts=["base"])
        first["rebuilt_libraries"] = ["base"]
        next_state = incremental.record_state(first, self.manifest)
        self.manifest["libraries"]["base"]["source_hash"] = current["base"]["source"]["hash"]
        second = incremental.expansion(first, self.manifest, next_state, "base")
        self.assertEqual(second["selected"], ["consumer"])
        self.assertEqual(second["rebuilt_libraries"], ["base"])
        second["rebuilt_libraries"] += ["consumer"]
        latest = incremental.record_state(second, self.manifest)
        retry = incremental.expansion(dict(second, rebuilt_libraries=[]), self.manifest, latest, "base")
        self.assertEqual(retry["selected"], [])

    def test_pending_marker_forces_consumer_even_when_cached_signatures_match(self):
        manifest = copy.deepcopy(self.manifest)
        manifest["pending_rebuilds"] = {"consumer": {"libbase.so.1": "base"}}
        first = dict(self.plan(), rebuilt_libraries=["consumer"])
        second = incremental.expansion(first, manifest, self.state, "")
        self.assertEqual(second["selected"], ["consumer"])
        with self.assertRaisesRegex(RuntimeError, "still requires ABI rebuilds: consumer"):
            incremental.validate_final(manifest)
        manifest.pop("pending_rebuilds")
        incremental.validate_final(manifest)

    def test_malformed_pending_markers_are_rejected_during_planning_and_expansion(self):
        for pending in [[], {"consumer": {}}, {"missing": {"libbase.so.1": "base"}},
                        {"consumer": {"libbase.so.1": "missing"}}]:
            with self.subTest(pending=pending):
                manifest = dict(self.manifest, pending_rebuilds=pending)
                with self.assertRaisesRegex(RuntimeError, "Invalid pending shared-library"):
                    self.plan(manifest=manifest)
                with self.assertRaisesRegex(RuntimeError, "Invalid pending shared-library"):
                    incremental.expansion(self.plan(), manifest, self.state, "base")

    def test_rebuilt_consumer_must_clear_its_pending_marker_before_state_commit(self):
        manifest = dict(self.manifest, pending_rebuilds={"consumer": {"libbase.so.1": "base"}})
        incremental.write(self.sdk / "shared-libraries.json", manifest)
        before = (self.sdk / incremental.STATE).read_bytes()
        with self.assertRaisesRegex(RuntimeError, "Rebuilt libraries still require ABI rebuilds: consumer"):
            incremental.execute(self.root, self.sdk, self.plan(manifest=manifest, libraries="consumer"),
                                "rpm", [sys.executable, "-c", "pass"])
        self.assertEqual((self.sdk / incremental.STATE).read_bytes(), before)

    def test_failed_builder_restores_source_snapshot_and_leaves_state(self):
        current = self.changed("base")
        current["other"] = self.changed("other")["other"]
        incremental.write(self.root / "downloads/.cache.json", current)
        original = (self.root / "downloads/.cache.json").read_bytes()
        state_before = (self.sdk / incremental.STATE).read_bytes()
        plan = self.plan(sources=current, libraries="base")
        with self.assertRaises(subprocess.CalledProcessError):
            incremental.execute(self.root, self.sdk, plan, "rpm", [sys.executable, "-c", "raise SystemExit(7)"])
        self.assertEqual((self.root / "downloads/.cache.json").read_bytes(), original)
        self.assertEqual((self.sdk / incremental.STATE).read_bytes(), state_before)

    def test_unselected_output_mutation_is_rejected_before_sdk_state_commit(self):
        original = (self.sdk / incremental.STATE).read_bytes()
        code = "from pathlib import Path;Path('buildroot/lib/libother.so.1').write_bytes(b'corrupt')"
        with self.assertRaisesRegex(RuntimeError, "checksum mismatch"):
            incremental.execute(self.root, self.sdk, self.plan(sources=self.changed("base"), libraries="base"), "rpm", [sys.executable, "-c", code])
        self.assertEqual((self.sdk / incremental.STATE).read_bytes(), original)

    @unittest.skipUnless(shutil.which("cc") and shutil.which("readelf"), "native C toolchain unavailable")
    def test_real_shared_objects_compatible_then_abi_break_compile_counters(self):
        builder = self.root / "builder.py"
        builder.write_text('''import hashlib,json,pathlib,subprocess,sys
r=pathlib.Path(sys.argv[1]);p=json.loads(pathlib.Path(sys.argv[2]).read_text());m=json.loads((r/'buildroot/shared-libraries.json').read_text());counts=json.loads((r/'counts.json').read_text()) if (r/'counts.json').exists() else {};pending=m.get('pending_rebuilds',{})
for name in p['selected']:
 counts[name]=counts.get(name,0)+1
 source=p['effective_sources'][p['inventory']['libraries'][name]['artifact']]['source']
 version=source['version']; abi=2 if name=='base' and version=='3' else 1
 c=r/(name+'.c');libs=[]
 if name=='base':
  c.write_text('int '+('base_new' if abi==2 else 'base')+'(void){return '+version+';}')
 elif name=='consumer':
  base=m['libraries']['base']['files'][0];symbol='base_new' if base.endswith('.2') else 'base';c.write_text('int '+symbol+'(void);int consumer(void){return '+symbol+'()+3;}');libs=['-L'+str(r/'buildroot/lib'),'-l:'+pathlib.Path(base).name]
 else:c.write_text('int other(void){return 9;}')
 relative='lib/lib'+name+'.so.'+str(abi);dest=r/'buildroot'/relative
 subprocess.run(['cc','-shared','-fPIC','-Wl,-soname,'+dest.name,'-Wl,--enable-new-dtags,-rpath,/usr/lib/php-zts/lib','-o',str(dest),str(c),*libs],check=True)
 for old in m['libraries'][name]['files']:
  if old!=relative:
   if name=='base':pending['consumer']={pathlib.Path(old).name:'base'}
   (r/'buildroot'/old).unlink()
 m['libraries'][name].update(source_hash=source['hash'],files=[relative],file_hashes={relative:hashlib.sha256(dest.read_bytes()).hexdigest()})
 if name=='consumer':pending.pop(name,None)
if pending:m['pending_rebuilds']=pending
else:m.pop('pending_rebuilds',None)
(r/'counts.json').write_text(json.dumps(counts));(r/'buildroot/shared-libraries.json').write_text(json.dumps(m));(r/'buildroot/.shared-library-ownership.json').write_text(json.dumps(m))
''')
        # Start with three actual DSOs, then assert compile counters and unrelated bytes/mtime.
        seed = dict(self.plan(), selected=sorted(self.graph), reused=[])
        plan_path = self.root / "plan.json"
        incremental.write(plan_path, seed)
        command = [sys.executable, str(builder), str(self.root), str(plan_path)]
        incremental.execute(self.root, self.sdk, seed, "rpm", command)
        (self.root / "counts.json").write_text("{}")
        self.manifest = incremental.read(self.sdk / "shared-libraries.json")
        incremental.write(self.sdk / 'shared-libraries.json', self.manifest)
        self.state = incremental.read(self.sdk / incremental.STATE)
        unchanged = {n: ((self.sdk / self.manifest["libraries"][n]["files"][0]).read_bytes(),
                         (self.sdk / self.manifest["libraries"][n]["files"][0]).stat().st_mtime_ns) for n in ["consumer", "other"]}
        current = self.changed("base", "2")
        first = self.plan(sources=current, changed_artifacts=["base"])
        incremental.write(plan_path, first)
        incremental.execute(self.root, self.sdk, first, "rpm", command)
        self.assertEqual(incremental.read(self.root / "counts.json"), {"base": 1})
        for name, (contents, mtime) in unchanged.items():
            path = self.sdk / self.manifest["libraries"][name]["files"][0]
            self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), (contents, mtime))
        # A SONAME change recompiles the reverse library in a separate pass, not the root twice.
        self.manifest = incremental.read(self.sdk / "shared-libraries.json")
        self.state = incremental.read(self.sdk / incremental.STATE)
        first = self.plan(sources=self.changed("base", "3"), changed_artifacts=["base"])
        incremental.write(plan_path, first)
        incremental.execute(self.root, self.sdk, first, "rpm", command)
        current_manifest = incremental.read(self.sdk / "shared-libraries.json")
        self.assertEqual(current_manifest["libraries"]["base"]["files"], ["lib/libbase.so.2"])
        self.assertEqual(current_manifest["pending_rebuilds"], {"consumer": {"libbase.so.1": "base"}})
        with self.assertRaisesRegex(RuntimeError, "still requires ABI rebuilds"):
            incremental.validate_final(current_manifest)
        second = incremental.expansion(first, current_manifest, incremental.read(self.sdk / incremental.STATE), "base")
        self.assertEqual(second["selected"], ["consumer"])
        incremental.write(plan_path, second)
        incremental.execute(self.root, self.sdk, second, "rpm", command)
        incremental.validate_final(incremental.read(self.sdk / "shared-libraries.json"))
        self.assertEqual(incremental.read(self.root / "counts.json"), {"base": 2, "consumer": 1})
        dynamic = subprocess.check_output(["readelf", "-d", str(self.sdk / "lib/libconsumer.so.1")], text=True)
        self.assertIn("Shared library: [libbase.so.2]", dynamic)
        other = self.sdk / self.manifest["libraries"]["other"]["files"][0]
        self.assertEqual((other.read_bytes(), other.stat().st_mtime_ns), unchanged["other"])


if __name__ == "__main__":
    unittest.main()
