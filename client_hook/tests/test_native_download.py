import sys
import json
from pathlib import Path
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from api.native_download import request_spec
from api.errors import ApiError
from hook_runtime.resource_rpc import ResourceRpc
import ctypes
import tempfile
import hashlib
import os
from unittest.mock import patch
from api import native_download


class DownloadTests(unittest.TestCase):
    def test_exact_native_request_types_and_fields(self):
        key = 'a' * 32
        digest = 'b' * 32
        examples = [
            ('image', f'<msg><img cdnbigimgurl="native-file-id" aeskey="{key}" hdlength="123" md5="{digest}"/></msg>', 1),
            ('video', f'<msg><videomsg cdnvideourl="native-file-id" aeskey="{key}" length="123" md5="{digest}"/></msg>', 4),
            ('file', f'<msg><appmsg><md5>{digest}</md5><appattach><cdnattachurl>native-file-id</cdnattachurl><aeskey>{key}</aeskey><totallen>123</totallen></appattach></appmsg></msg>', 5)]
        for kind, raw, file_type in examples:
            result = request_spec({'message_type': kind}, raw)
            self.assertEqual(result, dict(file_id='native-file-id', aes_key=key, size=123, md5=digest, file_type=file_type))

    def test_missing_identity_does_not_become_a_download(self):
        with self.assertRaises(ApiError):
            request_spec({'message_type': 'image'}, '<msg><img cdnbigimgurl="guess"/></msg>')
        with self.assertRaises(ValueError):
            request_spec({'message_type': 'file'}, '<!DOCTYPE x><msg/>')

    def test_rpc_abi_offsets(self):
        self.assertEqual(ResourceRpc.expected_size.offset, 6496)
        self.assertEqual(ctypes.sizeof(ResourceRpc), 6608)

    def test_retention_only_removes_stale_owned_inactive_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            owned = root / 'account/msg/temp/client_hook_downloads'
            for name in ('a' * 64, 'b' * 64, 'c' * 64, 'user-file'):
                folder = owned / name
                folder.mkdir(parents=True)
                (folder / 'content').write_bytes(b'data')
                if name == 'c' * 64:
                    (folder / 'unknown').write_bytes(b'keep')
                os.utime(folder / 'content', (1, 1))
                os.utime(folder, (1, 1))
            active = str(owned / ('b' * 64) / 'content')
            with patch('api.native_assets.account_directory', return_value=root / 'account'), \
                    patch.object(native_download, 'ROOT', root), patch.object(native_download, '_ACTIVE', {active}):
                native_download.prune('wxid_self', retention=100, now=1000)
            self.assertFalse((owned / ('a' * 64)).exists())
            self.assertTrue((owned / ('b' * 64) / 'content').is_file())
            self.assertTrue((owned / ('c' * 64) / 'unknown').is_file())
            self.assertTrue((owned / 'user-file' / 'content').is_file())

    def test_completion_requires_native_success_and_all_bytes(self):
        data = b'actual native download format variant'
        raw = ('<msg><videomsg cdnvideourl="exact-native-id" aeskey="' + 'a' * 32 +
               '" length="12" md5="' + 'b' * 32 + '"/></msg>')
        event = dict(account='wxid_self', conversation_id='wxid_peer', event_id='db:9', message_type='video')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'state').mkdir()
            calls = []
            result = dict(done=1, error=0, thread_error=0, cleanup_ok=1, stage=5, native_done=1,
                          native_status_0=0, native_status_1=0, transferred=len(data), total=len(data), callback_tid=42)
            def invoke(*arguments, mode):
                calls.append(mode)
                if mode == 1:
                    output = arguments[4]
                    output.parent.mkdir(parents=True, exist_ok=True)
                    output.write_bytes(data)
                return {**result, 'stage': 6 if mode==3 else result['stage']}
            with patch('api.native_assets.account_directory', return_value=root / 'account'), \
                    patch('hook_runtime.resource_rpc.invoke', side_effect=invoke), patch.object(native_download, 'ROOT', root):
                self.assertEqual(native_download.download(event, raw, attempt_id='complete'), data)
                evidence = json.loads(next((root / 'state/native-download-validation').glob('*.json')).read_text(encoding='utf-8'))
                self.assertFalse(evidence['source_md5_matches'])
                self.assertEqual(evidence['sha256'], hashlib.sha256(data).hexdigest())
                self.assertEqual(calls.count(1), 1)
                self.assertIn(3, calls)
                result['transferred'] -= 1
                with self.assertRaises(ApiError):
                    native_download.download(event, raw, timeout=.01, attempt_id='partial')
                result['native_status_0'] = 1234
                with self.assertRaises(ApiError):
                    native_download.download(event, raw, attempt_id='native-error')
