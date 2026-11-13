"""SNS RPC 构建绑定、查询与未知状态边界；不附加微信、不加载 DLL。"""
import ctypes as C
import hashlib
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from hook_runtime import sns_rpc


class FakePE:
    def __init__(self, path):
        self.data = Path(path).read_bytes()

    def rva_to_offset(self, rva):
        return rva


class FakeWin:
    def __init__(self, dll, image, *, loaded=True, timeout_once=False):
        self.dll, self.image, self.loaded = str(dll), image, loaded
        self.timeout_once = timeout_once
        self.remote = {}
        self.allocations, self.threads, self.closed, self.freed = [], [], [], []
        self.modes = []
        self.api = SimpleNamespace(OpenProcess=lambda *args: 11, CloseHandle=self.close,
                                   VirtualFreeEx=self.free, WaitForSingleObject=self.wait,
                                   GetExitCodeThread=self.exit_code)

    def creation(self, process):
        return (123,)

    def modules(self, pid):
        return [(self.dll, 0x100000, 0x1000)] if self.loaded else []

    def allocate(self, process, data):
        address = 0x200000 + len(self.allocations) * 0x100000
        self.remote[address] = data
        self.allocations.append(data)
        return address

    def thread(self, process, entry, address):
        self.threads.append((entry, address))
        request = sns_rpc.SnsRpc.from_buffer_copy(self.remote[address])
        self.modes.append(request.mode)
        request.state = 2
        request.submitted = request.completed = request.cleaned = request.calls = 1
        request.status0 = 0
        request.status1 = -110
        self.remote[address] = bytes(request)
        return 22 + len(self.threads)

    def loader(self, *args):
        raise AssertionError('查询路径不能取加载器或加载新 DLL')

    def read(self, process, address, size):
        if address == 0x100010:
            return self.image[16:32]
        return self.remote[address][:size]

    def wait(self, thread, timeout):
        if self.timeout_once:
            self.timeout_once = False
            return 258
        return 0

    def exit_code(self, thread, output):
        output._obj.value = 0
        return 1

    def close(self, handle):
        self.closed.append(handle)
        return 1

    def free(self, process, address, size, mode):
        self.freed.append(address)
        return 1


class SnsRpcTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.build = self.root / 'build' / 'sns-test'
        self.build.mkdir(parents=True)
        self.image = b'SNS-FAKE-DLL-ONLY' * 4
        self.dll = self.build / 'client-hook-sns.dll'
        self.dll.write_bytes(self.image)
        self.core = self.root / 'core.dll'
        self.core.write_bytes(b'fixed-test-core')
        self.pins = dict(directory=str(self.build), dll_name=self.dll.name,
                         dll_sha256=hashlib.sha256(self.image).hexdigest(),
                         core_sha256=hashlib.sha256(self.core.read_bytes()).hexdigest())
        self.manifest = self.root / 'build' / 'sns-current.json'
        self.manifest.write_text(json.dumps(self.pins))
        self.builds = {}
        self.base_patches = [patch.object(sns_rpc, 'ROOT', self.root),
                             patch.object(sns_rpc, '_BUILDS', self.builds),
                             patch.object(sns_rpc, 'PE', FakePE),
                             patch.object(sns_rpc, 'export_rva', return_value=16),
                             patch.object(sns_rpc, 'normalized', side_effect=str),
                             patch.object(sns_rpc, 'same_user'),
                             patch.object(sns_rpc, 'resolve_target', return_value={
                                 'core': self.core, 'pid': 99, 'created': 123})]
        for item in self.base_patches:
            item.start()

    def tearDown(self):
        for item in reversed(self.base_patches):
            item.stop()
        self.temp.cleanup()

    def test_rpc_signed_status_and_x64_layout(self):
        self.assertEqual(sns_rpc.SnsRpc.status0.offset, 52)
        self.assertEqual(sns_rpc.SnsRpc.status1.offset, 56)
        self.assertEqual(sns_rpc.SnsRpc.post_id.offset, 64)
        self.assertEqual(sns_rpc.SnsRpc.server_post_id.offset, 72)
        self.assertEqual(sns_rpc.SnsRpc.account.offset, 80)
        self.assertEqual(sns_rpc.SnsRpc.request_id.offset, 208)
        self.assertEqual(sns_rpc.SnsRpc.author.offset, 272)
        self.assertEqual(sns_rpc.SnsRpc.body.offset, 400)
        self.assertEqual(C.sizeof(sns_rpc.SnsRpc), 65944)
        rpc = sns_rpc.SnsRpc(status0=-110, status1=-1)
        copied = sns_rpc.SnsRpc.from_buffer_copy(bytes(rpc))
        self.assertEqual((copied.status0, copied.status1), (-110, -1))

    def test_query_missing_original_module_never_loads_or_submits(self):
        api = FakeWin(self.dll, self.image, loaded=False)
        with patch.object(sns_rpc, 'Win', return_value=api), self.assertRaisesRegex(RuntimeError, '状态未知'):
            sns_rpc.invoke('wxid_self', 'original_request', 123, mode=1)
        self.assertEqual(api.allocations, [])
        self.assertEqual(api.threads, [])
        self.assertEqual(api.closed, [11])

    def test_publish_uses_verified_fixture_build_and_single_submission(self):
        api = FakeWin(self.dll, self.image)
        with patch.object(sns_rpc, 'Win', return_value=api):
            result = sns_rpc.invoke('wxid_self', 'fixture_publish', mode=0, action=4, body='测试')
        self.assertEqual(api.modes, [0])
        self.assertEqual(len(api.threads), 1)
        self.assertEqual(result['state'], 2)
        self.assertEqual(self.builds['fixture_publish'], self.pins)
        payload = sns_rpc.SnsRpc.from_buffer_copy(api.allocations[0])
        self.assertEqual(payload.action, 4)
        self.assertEqual(payload.body, '测试'.encode('utf-8'))

    def test_old_publish_query_remains_available_without_submission(self):
        api = FakeWin(self.dll, self.image)
        with patch.object(sns_rpc, 'Win', return_value=api):
            result = sns_rpc.invoke('wxid_self', 'original_publish', mode=1, action=4)
        self.assertEqual(api.modes, [1])
        self.assertEqual(result['state'], 2)


    def test_query_only_uses_original_rpc_and_signed_result(self):
        api = FakeWin(self.dll, self.image)
        with patch.object(sns_rpc, 'Win', return_value=api):
            result = sns_rpc.invoke('wxid_self', 'original_request', 123, mode=1)
        self.assertEqual(api.modes, [1])
        self.assertEqual(len(api.allocations), 1)
        self.assertEqual(api.threads[0][0], 0x100010)
        self.assertEqual(result['status1'], -110)
        self.assertEqual(result['calls'], 1)
        self.assertEqual(len(api.freed), 1)

    def test_timeout_preserves_remote_input_and_never_retries_submission(self):
        api = FakeWin(self.dll, self.image, timeout_once=True)
        with patch.object(sns_rpc, 'Win', return_value=api):
            with self.assertRaisesRegex(RuntimeError, '提交状态未知'):
                sns_rpc.invoke('wxid_self', 'stable_request', 123, mode=0)
            self.assertEqual(api.modes, [0])
            self.assertEqual(api.freed, [])
            sns_rpc.invoke('wxid_self', 'stable_request', 123, mode=1)
        self.assertEqual(api.modes, [0, 1])
        self.assertEqual(len(api.threads), 2)
        self.assertEqual(len(api.freed), 1)

    def test_existing_request_remains_bound_when_current_build_changes(self):
        api = FakeWin(self.dll, self.image)
        with patch.object(sns_rpc, 'Win', return_value=api):
            sns_rpc.invoke('wxid_self', 'bound_request', 123, mode=1)
            replacement = self.root / 'build' / 'sns-replacement'
            replacement.mkdir()
            (replacement / self.dll.name).write_bytes(b'wrong-build')
            self.manifest.write_text(json.dumps({**self.pins, 'directory': str(replacement)}))
            sns_rpc.invoke('wxid_self', 'bound_request', 123, mode=1)
        self.assertEqual(api.modes, [1, 1])
        self.assertEqual(self.builds['bound_request']['directory'], str(self.build))

    def test_hash_mismatch_rejected_before_process_access(self):
        self.dll.write_bytes(b'tampered')
        with patch.object(sns_rpc, 'Win') as win, self.assertRaisesRegex(ValueError, '摘要'):
            sns_rpc.invoke('wxid_self', 'tampered_request', 123, mode=1)
        win.assert_not_called()


if __name__ == '__main__':
    unittest.main()
