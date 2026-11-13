import importlib.util
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('source_state', ROOT / '.github/actions/shared-libraries/source-state.py')
state = importlib.util.module_from_spec(spec)
spec.loader.exec_module(state)


class SourceState(unittest.TestCase):
    def write(self, root, name, value):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value))

    def test_existing_comparison_state_needs_no_github_requests(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            self.write(root, '.download-state/lock.prev.json', {'libaom': 'old'})
            self.write(root, '.download-state/meta.prev.json', {'php_versions': {'8.6': '8.6.0RC7'}})
            github = Mock()
            self.assertEqual(state.restore(root, 'org/repo', '30', github=github), 'cache')
            github.api.assert_not_called()

    def test_restored_sources_can_supply_missing_comparison_cache(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            self.write(root, 'downloads/.cache.json', {'libaom': 'old'})
            self.write(root, 'downloads/.spc-meta.json', {'php_versions': {'8.6': '8.6.0RC7'}})
            github = Mock()
            self.assertEqual(state.restore(root, 'org/repo', '30', allow_sources=True, github=github), 'source-cache')
            self.assertEqual(state.read(root / '.download-state/lock.prev.json'), {'libaom': 'old'})
            github.api.assert_not_called()

    def test_missing_cache_recovers_only_a_preceding_snapshot(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            def api(path):
                if path == '/actions/runs/30':
                    return dict(workflow_id=7, head_branch='master', created_at='2026-11-10T00:00:00Z')
                if path.startswith('/actions/workflows/7/runs?'):
                    return {'workflow_runs': [dict(id=40, created_at='2026-11-11T00:00:00Z'),
                                              dict(id=20, created_at='2026-11-09T00:00:00Z')]}
                self.assertEqual(path, '/actions/runs/20/artifacts?per_page=100')
                return {'artifacts': [dict(name='downloads-inputs', expired=False)]}
            def fetch(destination, repository, run_id):
                self.assertEqual((repository, run_id), ('org/repo', 20))
                self.write(destination, 'downloads/.cache.json', {'libaom': 'actual-old'})
                self.write(destination, 'downloads/.spc-meta.json', {'php_versions': {'8.6': '8.6.0RC7'}})
            self.assertEqual(state.restore(root, 'org/repo', '30', github=Mock(api=api), fetch=fetch), '20')
            self.assertEqual(state.read(root / '.download-state/lock.prev.json'), {'libaom': 'actual-old'})

    def test_no_previous_state_does_not_invent_a_full_rebuild(self):
        with TemporaryDirectory() as directory:
            github = Mock()
            github.api.side_effect = [dict(workflow_id=7, head_branch='master', created_at='2026-11-10'), {'workflow_runs': []}]
            with self.assertRaisesRegex(RuntimeError, 'refusing to classify every input as changed'):
                state.restore(Path(directory), 'org/repo', '30', github=github, fetch=Mock())

    def test_only_explicit_full_requests_can_start_without_previous_state(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            github = Mock()
            self.assertEqual(state.restore(root, 'org/repo', '30', github=github, allow_empty=True), 'explicit-full')
            self.assertEqual(state.read(root / '.download-state/meta.prev.json'), {})
            github.api.assert_not_called()


if __name__ == '__main__':
    unittest.main()
