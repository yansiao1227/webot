"""在自有 host 上运行同一驻留 DLL 的 HTTP 收发；无真实微信业务调用。"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import json
import subprocess
import tempfile
import threading
import unittest
import time
from types import SimpleNamespace

from api.backend import NativeBackend
from api.server import Application, Server
from client import Client
from hook_runtime.loader import Win, PE, export_rva, normalized
from hook_runtime.service import WORK, inspect_status


class NativeApiTests(unittest.TestCase):
    def test_api_process_death_releases_child_receiver_and_restores_entry(self):
        import socket
        pins = json.loads((WORK / 'runtime-current.json').read_text(encoding='utf-8-sig'))
        directory = Path(pins['directory'])
        host = subprocess.Popen([str(directory / 'hook-host.exe')], stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, text=True, encoding='utf-8')
        controller = None
        temp = tempfile.TemporaryDirectory()
        try:
            self.assertEqual(json.loads(host.stdout.readline())['type'], 'host_ready')
            with socket.socket() as sock:
                sock.bind(('127.0.0.1', 0))
                port = sock.getsockname()[1]
            controller = subprocess.Popen([sys.executable, '-B', '-X', 'utf8', str(Path(__file__).resolve().parents[1] / 'client_hook.py'),
                'serve', '--fixture-pid', str(host.pid), '--port', str(port), '--state-dir', temp.name],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding='utf-8')
            self.assertEqual(json.loads(controller.stdout.readline())['type'], 'http_ready')
            controller.kill()  # 这是自有 API 测试桩进程，绝不终止用户的客户端。
            controller.wait(timeout=5)
            until = time.monotonic() + 5
            while inspect_status(SimpleNamespace(fixture_pid=host.pid))['state'] != 'idle':
                self.assertLess(time.monotonic(), until)
                time.sleep(.05)
        finally:
            if controller:
                if controller.poll() is None:
                    controller.kill(); controller.wait(timeout=5)
                controller.stdout.close(); controller.stderr.close()
            host.stdin.write('quit\n'); host.stdin.flush(); host.wait(timeout=10)
            host.stdin.close(); host.stdout.close()
            temp.cleanup()

    def test_real_rpc_and_receive_pipeline_on_owned_host(self):
        pins = json.loads((WORK / 'runtime-current.json').read_text(encoding='utf-8-sig'))
        directory = Path(pins['directory'])
        host = subprocess.Popen([str(directory / 'hook-host.exe')], stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, text=True, encoding='utf-8')
        app = server = None
        temp = tempfile.TemporaryDirectory()
        try:
            self.assertEqual(json.loads(host.stdout.readline())['type'], 'host_ready')
            state=temp.name
            app = Application(state, NativeBackend(fixture_pid=host.pid))
            server = Server(('127.0.0.1', 0), app)
            server_thread = threading.Thread(target=server.serve_forever)
            server_thread.start()
            client = Client(f'http://127.0.0.1:{server.server_port}')
            client.consumer('native')
            for command in ('emit', 'emit-group', 'emit-group-at', 'emit-group-other', 'emit-group-all',
                            'emit-group-invalid', 'emit-long', 'emit-outbound', 'emit-image', 'emit-reference'):
                host.stdin.write(command + '\n')
                host.stdin.flush()
                self.assertEqual(json.loads(host.stdout.readline())['type'], 'emitted')
                if command == 'emit-outbound':
                    batch = client.messages('native', timeout=2)
                    self.assertEqual(len(batch['events']), 1)
                    self.assertEqual(batch['events'][0]['direction'], 'outgoing')
                    self.assertEqual(batch['events'][0]['conversation_id'], 'wxid_fixture_peer')
                    client.ack('native', batch['ack_token'])
                else:
                    batch = client.messages('native', timeout=2)
                    self.assertEqual(len(batch['events']), 1)
                    item = batch['events'][0]
                    if command == 'emit-image':
                        self.assertEqual(item['message_type'], 'image')
                        self.assertEqual(item['native_message_id_candidate'], '123456789')
                        self.assertEqual(item['message_id'], '123456789')
                        self.assertNotIn('_native_resource', item)
                    if command == 'emit-reference':
                        self.assertEqual(item['message_type'], 'text')
                        self.assertEqual(item['reference']['message_id'], '123456789')
                        self.assertEqual(item['payload']['text'], 'analyze image')
                    if command == 'emit-long':
                        self.assertEqual(len(item['payload']['text']), 60000)
                    if command == 'emit-group':
                        self.assertEqual(item['conversation_id'], '123456@chatroom')
                        self.assertEqual(item['mentions']['wxids'], [])
                    if command == 'emit-group-at':
                        self.assertEqual(item['mentions']['state'], 'known')
                        self.assertEqual(item['mentions']['wxids'], ['wxid_fixture_self', 'wxid_fixture_peer'])
                    if command == 'emit-group-other':
                        self.assertEqual(item['mentions']['wxids'], ['wxid_fixture_peer'])
                    if command == 'emit-group-all':
                        self.assertTrue(item['mentions']['all'])
                        self.assertEqual(item['mentions']['wxids'], [])
                    if command == 'emit-group-invalid':
                        self.assertEqual(item['mentions']['state'], 'unknown')
                        self.assertTrue(item['payload']['text'])
                    client.ack('native', batch['ack_token'])
            # 测试桩按 256 KiB 轮转，真实服务按 8 MiB 轮转；控制器使用
            # 同一协议，不累积无界的原始文件。
            for _ in range(6):
                host.stdin.write('emit-long\n'); host.stdin.flush()
                self.assertEqual(json.loads(host.stdout.readline())['type'], 'emitted')
                batch = client.messages('native', timeout=2)
                self.assertEqual(len(batch['events'][0]['payload']['text']), 60000)
                client.ack('native', batch['ack_token'])
            api = Win()
            module = next(m for m in api.modules(host.pid) if Path(m[0]).name == 'weixin.dll')
            pe = PE(str(directory / 'Weixin.dll'))
            process = api.api.OpenProcess(0x1000 | 0x10, False, host.pid)
            try:
                for index, text in enumerate(('short', '中文 😀\n"quote"', 'a' * 60000)):
                    if index in (1, 2):
                        for command in (('cache-nonempty', 'result-heap') if index == 1 else ('result-large',)):
                            host.stdin.write(command + '\n')
                            host.stdin.flush()
                            self.assertEqual(json.loads(host.stdout.readline())['type'], 'configured')
                    result = client.send_text('filehelper', text, 'native' + str(index))
                    if result['state'] in ('queued', 'running'):
                        import time
                        for _ in range(20):
                            time.sleep(.1)
                            result = client.operation('native' + str(index))
                            if result['state'] not in ('queued', 'running'):
                                break
                    self.assertEqual(result['state'], 'accepted', result)
                    diag = result['result']
                    self.assertEqual(diag['native_submit_invocations'], 1)
                    self.assertTrue(diag['cleanup_ok'])
                    actual = api.read(process, module[1] + export_rva(pe, 'FixtureLastBody', code=False), len(text.encode('utf-8'))+1)
                    self.assertEqual(actual, text.encode('utf-8') + b'\0')
                    repeated = client.send_text('filehelper', text, 'native' + str(index))
                    self.assertEqual(repeated['state'], 'accepted')
                for command in ('bad-thread',):
                    host.stdin.write(command + '\n'); host.stdin.flush()
                    self.assertEqual(json.loads(host.stdout.readline())['type'], 'configured')
                from client import ClientError
                with self.assertRaises(ClientError) as caught:
                    client.send_text('filehelper', 'reject wrong thread', 'bad-thread')
                self.assertTrue(caught.exception.body['result']['callback_done'], caught.exception.body)
                host.stdin.write('good-thread\n'); host.stdin.flush()
                self.assertEqual(json.loads(host.stdout.readline())['type'], 'configured')
                host.stdin.write('delay\n'); host.stdin.flush()
                self.assertEqual(json.loads(host.stdout.readline())['type'], 'configured')
                pending = client.send_text('filehelper', 'delayed once', 'pending')
                self.assertIn(pending['state'], ('queued', 'running'))
                import time
                for _ in range(40):
                    time.sleep(.1)
                    pending = client.operation('pending')
                    if pending['state'] not in ('queued', 'running'):
                        break
                self.assertEqual(pending['state'], 'accepted', pending)
                host.stdin.write('stats\n'); host.stdin.flush()
                stats = json.loads(host.stdout.readline())
                self.assertEqual(stats['submitted'], 4)
                self.assertEqual(stats['accepted'], 4)
                self.assertEqual(stats['text_allocs'], stats['text_destroy'])
                self.assertEqual(stats['ctrl_allocs'], stats['ctrl_frees'])
                self.assertEqual(stats['heap_allocs'], stats['heap_frees'])
                self.assertEqual(stats['large_allocs'], stats['large_frees'])
                self.assertEqual(stats['result_bad_free'], 0)
                self.assertEqual(stats['input_bad_refs'], 0)
                host.stdin.write('logout\n'); host.stdin.flush()
                self.assertEqual(json.loads(host.stdout.readline())['type'], 'configured')
                until = time.monotonic() + 3
                while app.store.get_meta('capture')['state'] == 'running':
                    self.assertLess(time.monotonic(), until)
                    time.sleep(.01)
                self.assertEqual(app.store.get_meta('capture')['native']['reason'], 'identity_changed')
                with self.assertRaises(ClientError) as caught:
                    client.send_text('filehelper', 'must not submit', 'after-logout')
                self.assertEqual(caught.exception.status, 503)
            finally:
                api.api.CloseHandle(process)
            server.shutdown()
            app.store.capture('stopped', account=app.backend.account)
            server.server_close()
            server = None
            app.close()
            app = None
        finally:
            if server:
                server.shutdown()
                server.app.store.capture('stopped', account=server.app.backend.account)
                server.server_close()
            if app:
                app.close()
            host.stdin.write('quit\n')
            host.stdin.flush()
            host.wait(timeout=10)
            host.stdin.close()
            host.stdout.close()
            temp.cleanup()


if __name__ == '__main__':
    unittest.main()
