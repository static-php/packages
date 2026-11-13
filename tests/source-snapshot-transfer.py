#!/usr/bin/env python3
import importlib.util
import io
import json
import os
from pathlib import Path
import tarfile
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch
import zipfile


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('snapshot', ROOT / '.github/actions/shared-libraries/source-snapshot.py')
snapshot = importlib.util.module_from_spec(spec)
spec.loader.exec_module(snapshot)


class SourceTransfer(unittest.TestCase):
    def test_recorded_source_metadata_uses_the_action_token_without_a_gh_binary(self):
        spec = importlib.util.spec_from_file_location('source_artifact', ROOT / '.github/actions/shared-libraries/artifact-json.py')
        artifact = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(artifact)
        records = {'.cache.json': {'ext-fixture': {'source': {'hash': 'source-hash'}}},
                   '.spc-meta.json': {'php_versions': {'8.6': '8.6.0RC7'}}}
        compressed = io.BytesIO()
        with tarfile.open(fileobj=compressed, mode='w:gz') as bundle:
            for name, value in records.items():
                data = json.dumps(value).encode()
                entry = tarfile.TarInfo(name)
                entry.size = len(data)
                bundle.addfile(entry, io.BytesIO(data))
        content = io.BytesIO()
        with zipfile.ZipFile(content, mode='w') as archive:
            archive.writestr('downloads-inputs.tar.gz', compressed.getvalue())
        responses = [SimpleNamespace(status_code=200, json=lambda: {'artifacts': [
            {'id': 123, 'name': 'downloads-inputs', 'expired': False}]}),
            SimpleNamespace(status_code=302, headers={'Location': 'https://artifact.invalid/inputs'})]
        with patch.dict(os.environ, {'GITHUB_TOKEN': 'fixture-token', 'GH_TOKEN': ''}), \
                patch.object(artifact, 'request', side_effect=responses), \
                patch.object(artifact, 'RemoteZip', return_value=io.BytesIO(content.getvalue())), \
                patch.object(artifact.subprocess, 'check_output', side_effect=AssertionError('gh is unavailable')):
            self.assertEqual(artifact.source_metadata('42'), records)

    def test_inputs_keep_verified_php_and_package_assets_without_library_sources(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            downloads = root / 'downloads'
            downloads.mkdir()
            values = {'.spc-meta.json': {'php_versions': {'8.5': '8.5.12'}},
                      '.php-sources.json': {'sources': {'8.5.12': {'source': {'filename': 'php.tar.gz'}}}},
                      '.cache.json': {'ext-spx': {'source': {'filename': 'spx.tar.gz'}},
                                      'ext-fixture': {'source': {'cache_type': 'git', 'dirname': 'fixture'}}},
                      'build-plan.json': {'requests': {}}, 'pie.json': {'version': '1.0.0'}}
            for name, value in values.items():
                (downloads / name).write_text(json.dumps(value))
            for name in ['php.tar.gz', 'pie.phar', 'spx.tar.gz', 'grpc.tar.gz']:
                (downloads / name).write_bytes(name.encode())
            (downloads / 'fixture').mkdir()
            (downloads / 'fixture/source.c').write_text('frozen extension source')
            archive = root / 'inputs.tar.gz'
            snapshot.inputs(downloads, archive)
            with tarfile.open(archive) as bundle:
                self.assertEqual(set(bundle.getnames()), set(values) | {'php.tar.gz', 'pie.phar', 'spx.tar.gz'})
                for member in bundle:
                    self.assertEqual(bundle.extractfile(member).read(), (downloads / member.name).read_bytes())
            snapshot.inputs(downloads, archive, php=True)
            with tarfile.open(archive) as bundle:
                self.assertIn('fixture/source.c', bundle.getnames())
                self.assertNotIn('grpc.tar.gz', bundle.getnames())
                self.assertEqual(bundle.extractfile('fixture/source.c').read(), b'frozen extension source')
            (downloads / 'php.tar.gz').unlink()
            with self.assertRaisesRegex(RuntimeError, 'Missing frozen source input'):
                snapshot.inputs(downloads, archive)

    def test_compilation_materializes_only_an_incomplete_pinned_snapshot(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(snapshot, 'fetch') as fetch:
            root = Path(temporary)
            (root / 'downloads').mkdir()
            snapshot.materialize(root)
            fetch.assert_not_called()
            marker = root / 'downloads' / snapshot.MARKER
            for complete in [True, False]:
                marker.write_text(json.dumps({'repository': 'static-php/packages', 'run_id': '1234', 'complete': complete}))
                snapshot.materialize(root)
                if complete:
                    fetch.assert_not_called()
                else:
                    fetch.assert_called_once_with(root, 'static-php/packages', '1234', full=True, php=False)
            fetch.reset_mock()
            snapshot.materialize(root, php=True)
            fetch.assert_called_once_with(root, 'static-php/packages', '1234', full=False, php=True)
            marker.write_text(json.dumps({'repository': 'static-php/packages', 'run_id': '1234', 'complete': False, 'php': True}))
            fetch.reset_mock()
            snapshot.materialize(root, php=True)
            fetch.assert_not_called()
            snapshot.materialize(root)
            fetch.assert_called_once_with(root, 'static-php/packages', '1234', full=True, php=False)


if __name__ == '__main__':
    unittest.main()
