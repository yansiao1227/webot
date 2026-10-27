"""三层均使用真实回环 HTTP，仅使用自有字节和脚本化模型。"""
import asyncio
from pathlib import Path
import secrets
import sys
import tempfile
import threading
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'client_hook'))
sys.path.insert(0, str(ROOT / 'wechat_codex'))
sys.path.insert(0, str(ROOT / 'wechat_mcp'))
from api.backend import FixtureBackend
from api.server import Application, Server as HookServer
from wechat_bridge.httpio import HookClient
from wechat_bridge.service import Bridge, REFERENCE_SOURCE
from wechat_bridge.server import Server as MCPServer
from local_agent.mcp import MCP
from local_agent.storage import Store
from local_agent.service import Agent
from local_agent.runtime import identity


class FullContextContract(unittest.TestCase):
    def test_file_is_silent_then_native_reference_fetches_exact_bytes(self):
        data = '合同测试附件内容'.encode()
        class Provider(FixtureBackend):
            fetched = 0
            def fetch_resource(self, event):
                self.fetched += 1
                return data
        class Model:
            async def ensure_thread(self, cid, thread_id=None):
                return thread_id or 'scripted-test-thread'
            async def generate(self, thread_id, event):
                if 'attachment' in event:
                    assert Path(event['attachment']['path']).read_bytes() == data
                    return '已读取指定附件', {}
                return '内部计划', {'read_attachment': True}
        with tempfile.TemporaryDirectory() as temp:
            provider = Provider()
            app = Application(Path(temp) / 'hook', provider)
            token = secrets.token_urlsafe(32)
            hook_server = HookServer(('127.0.0.1', 0), app, token)
            hook_thread = threading.Thread(target=hook_server.serve_forever)
            hook_thread.start()
            bridge = Bridge(Path(temp) / 'bridge', HookClient(f'http://127.0.0.1:{hook_server.server_port}', token),
                            provider.account, ['wxid_peer'], True, start='earliest')
            mcp_server = MCPServer(('127.0.0.1', 0), bridge, token)
            mcp_thread = threading.Thread(target=mcp_server.serve_forever)
            mcp_thread.start()
            config = {'account': provider.account, 'conversations': ['wxid_peer'], 'simulation': True,
                      'consumer_id': 'contract-agent', 'mcp_url': f'http://127.0.0.1:{mcp_server.server_port}/mcp'}
            store = Store(Path(temp) / 'agent', identity(config))
            agent = Agent(store, MCP(config['mcp_url'], token), Model(), config)
            async def scenario():
                await agent.verify()
                await agent.collect_once(0)
                bridge.hook.request('POST', '/consumers', {'consumer_id': bridge.consumer, 'start': 'earliest'})
                common = {'conversation_id': 'wxid_peer', 'direction': 'incoming', 'source': 'simulation',
                          'sender_id': 'wxid_peer'}
                app.store.append(provider.account, {**common, 'message_type': 'file', 'message_id': '123',
                    'payload': {'resource_id': 'fixture-file', 'name': 'contract.txt'}})
                bridge.collect_once(0)
                await agent.collect_once(0)
                self.assertEqual(store.ready(), [])
                self.assertEqual(provider.fetched, 0)
                self.assertEqual(provider.sent, [])
                app.store.append(provider.account, {**common, 'message_type': 'text',
                    'payload': {'text': '读取这个引用的文件'},
                    'reference': {'source': REFERENCE_SOURCE, 'message_id': '123'}})
                bridge.collect_once(0)
                await agent.collect_once(0)
                await agent.process(store.ready()[0])
                self.assertEqual(provider.fetched, 1)
                self.assertEqual(len(provider.sent), 1)
                self.assertEqual(provider.sent[0][1]['msg'], '已读取指定附件')
                self.assertEqual(store.status()['jobs'], {'accepted': 1})
            try:
                asyncio.run(scenario())
            finally:
                store.close()
                mcp_server.shutdown(); mcp_server.server_close(); mcp_thread.join()
                bridge.close()
                hook_server.shutdown(); hook_server.server_close(); hook_thread.join()
                app.close()
