import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

spec = importlib.util.spec_from_file_location('relocate_builds', Path(__file__).resolve().parents[1] / 'scripts/relocate_builds.py')
relocation = importlib.util.module_from_spec(spec)
spec.loader.exec_module(relocation)
from hook_runtime import service


class BuildRelocationTests(unittest.TestCase):
    def make_build(self, root, kind):
        directory = root / (kind + '-example')
        directory.mkdir()
        artifact = directory / ('hook-host.exe' if kind == 'runtime' else 'sidecar.dll')
        artifact.write_bytes(b'owned-test-artifact')
        digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
        pins = {'directory': str(root.parent / 'missing-old-root' / directory.name)}
        pins.update({'files': {artifact.name: digest}} if kind == 'runtime' else {'dll_name': artifact.name, 'dll_sha256': digest})
        for path in (directory / 'pins.json', root / (kind + '-current.json')):
            path.write_text(json.dumps(pins), encoding='utf-8')
        return directory, artifact

    def test_dry_run_then_apply_all_build_types_and_repeat(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / 'build'
            root.mkdir()
            directories = [self.make_build(root, kind)[0] for kind in ('runtime', 'resource', 'read')]
            before = (root / 'runtime-current.json').read_bytes()
            self.assertEqual(relocation.relocate(root)['manifest_count'], 6)
            self.assertEqual((root / 'runtime-current.json').read_bytes(), before)
            self.assertEqual(relocation.relocate(root, True)['manifest_count'], 6)
            for directory in directories:
                pins = json.loads((directory / 'pins.json').read_text(encoding='utf-8'))
                self.assertEqual(Path(pins['directory']), directory)
            self.assertEqual(len(list(root.rglob('*.before-relocation-*.json'))), 6)
            self.assertEqual(relocation.relocate(root, True)['manifest_count'], 0)

    def test_tampered_later_artifact_prevents_all_manifest_changes(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / 'build'
            root.mkdir()
            self.make_build(root, 'runtime')
            _, artifact = self.make_build(root, 'resource')
            artifact.write_bytes(b'tampered')
            before = (root / 'runtime-current.json').read_bytes()
            with self.assertRaises(ValueError):
                relocation.relocate(root, True)
            self.assertEqual((root / 'runtime-current.json').read_bytes(), before)
            self.assertEqual(list(root.rglob('*.before-relocation-*.json')), [])

    def test_existing_old_directory_is_not_silently_replaced(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / 'build'
            root.mkdir()
            self.make_build(root, 'runtime')
            (root.parent / 'missing-old-root' / 'runtime-example').mkdir(parents=True)
            try:
                with self.assertRaises(ValueError):
                    relocation.relocation_plan(root)
            finally:
                (root.parent / 'missing-old-root' / 'runtime-example').rmdir()
                (root.parent / 'missing-old-root').rmdir()

    def test_conflicting_manifests_are_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / 'build'
            root.mkdir()
            directory, _ = self.make_build(root, 'runtime')
            (directory / 'pins.json').write_text('{}', encoding='utf-8')
            with self.assertRaises(ValueError):
                relocation.relocation_plan(root)

    def test_loaded_old_location_requires_normal_client_restart(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / 'build'
            root.mkdir()
            old_dll = Path(temp) / 'old-build/runtime-example/hook-live.dll'
            resident = {'pid': 123, 'creation_time': '456', 'protocol': 3, 'dll': str(old_dll)}
            (root / 'resident-123-456.json').write_text(json.dumps(resident), encoding='utf-8')
            api = Mock()
            api.inspect.return_value = {'creation_filetime_100ns': 456}
            api.modules.return_value = [(service.normalized(old_dll), 0, 0)]
            with patch.object(service, 'WORK', root), \
                    patch.object(service, 'current_build', return_value=(root / 'runtime-example', {})), \
                    patch('hook_runtime.target.discover', return_value={'pid': 123, 'exe_path': 'Weixin.exe', 'core_path': 'Weixin.dll'}):
                with self.assertRaisesRegex(RuntimeError, '正常退出微信'):
                    service.resolve_target(api, SimpleNamespace(pid=None, fixture_pid=None))
