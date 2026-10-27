"""可选集成测试：实际 client_hook HTTP + FixtureBackend，不接触微信。"""
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest

MODULE = Path(__file__).resolve().parents[1]
HOOK = MODULE.parent / 'client_hook'
sys.path.insert(0, str(MODULE))
from wechat_bridge.httpio import HookClient
from wechat_bridge.service import Bridge


@unittest.skipUnless((HOOK / 'api/server.py').exists(), '独立拷贝未附带 client_hook，跳过可选 HTTP 合同测试')
class ClientHookContract(unittest.TestCase):
    def test_real_http_application_simulated_backend(self):
        sys.path.insert(0, str(HOOK))
        from api.backend import FixtureBackend
        from api.server import Application, Server
        with tempfile.TemporaryDirectory() as temp:
            backend = FixtureBackend()
            app = Application(Path(temp) / 'hook', backend)
            server = Server(('127.0.0.1', 0), app, token='contract-test-token')
            thread = threading.Thread(target=server.serve_forever); thread.start()
            bridge = Bridge(Path(temp) / 'bridge', HookClient(f'http://127.0.0.1:{server.server_port}', 'contract-test-token'),
                            backend.account, ['wxid_sim_peer'], True, start='earliest')
            try:
                bridge.hook.request('POST', '/consumers', {'consumer_id': bridge.consumer, 'start': 'earliest'})
                app.store.append(backend.account, {'message_type': 'text', 'payload': {'text': '真实 HTTP 合同测试 😀'},
                    'conversation_id': 'wxid_sim_peer', 'fromid': 'wxid_sim_peer', 'toid': backend.account,
                    'direction': 'incoming', 'source': 'simulation', 'captured_at': time.time()})
                bridge.collect_once(0)
                batch = bridge.store.messages('generic-agent', 0, 10, 'earliest', bridge.conversations)
                event = batch['events'][0]
                result = bridge.reply_text(event['event_id'], '合同测试回复')
                while result['state'] in ('queued', 'running'):
                    result = bridge.refresh_reply(event['event_id'])
                self.assertEqual(result['state'], 'accepted')
                self.assertFalse(result['delivery_confirmed'])
                bridge.reply_text(event['event_id'], '合同测试回复')
                self.assertEqual(len(backend.sent), 1)
                self.assertEqual(backend.sent[0][1]['toid'], 'wxid_sim_peer')
                bridge.store.ack('generic-agent', batch['ack_token'])
                self.assertEqual(bridge.store.messages('generic-agent', 0, 10, 'earliest', bridge.conversations)['events'], [])
            finally:
                bridge.close(); server.shutdown(); server.server_close(); thread.join(); app.close()
