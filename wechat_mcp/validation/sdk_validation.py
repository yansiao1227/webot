"""可选：用官方 Python MCP SDK 验证 HTTP 2.0、旧握手和 stdio 互操作。"""
import asyncio
from importlib.metadata import version
import json
from pathlib import Path
import secrets
import sys
import tempfile
import threading

MODULE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MODULE))
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client
import httpx2
from wechat_bridge.httpio import HookClient
from wechat_bridge.server import Server
from wechat_bridge.service import Bridge
from wechat_bridge.simulation import SimulatedHook


async def main():
    with tempfile.TemporaryDirectory() as temp:
        source = SimulatedHook()
        bridge = Bridge(Path(temp) / 'bridge', HookClient(source.url), source.account, ['wxid_sim_peer'], True, start='earliest')
        server = Server(('127.0.0.1', 0), bridge, secrets.token_urlsafe(32))
        thread = threading.Thread(target=server.serve_forever); thread.start()
        passed = []
        try:
            bridge.hook.request('POST', '/consumers', {'consumer_id': bridge.consumer, 'start': 'earliest'})
            source.inject('wxid_sim_peer', 'SDK 互操作')
            bridge.collect_once(0)
            url = f'http://127.0.0.1:{server.server_port}/mcp'
            async with httpx2.AsyncClient(headers={'Authorization': 'Bearer ' + server.token}) as http:
                for mode in ('modern', 'legacy'):
                    async with streamable_http_client(url, http_client=http) as streams:
                        async with ClientSession(streams[0], streams[1]) as session:
                            if mode == 'modern':
                                await session.discover()
                            else:
                                await session.initialize()
                            tools = await session.list_tools()
                            assert 'reply_text' in [t.name for t in tools.tools]
                            batch = await session.call_tool('get_messages', {'consumer_id': mode})
                            event = batch.structured_content['events'][0]
                            reply = await session.call_tool('reply_text', {'event_id': event['event_id'], 'text': 'SDK 回复'})
                            assert not reply.is_error
                            assert reply.structured_content['state'] == 'accepted'
                            ack = await session.call_tool('ack_messages', {'consumer_id': mode, 'ack_token': batch.structured_content['ack_token']})
                            assert not ack.is_error
                            passed.append('http-' + mode)
            assert len(source.sent) == 1
            params = StdioServerParameters(command=sys.executable, args=['-B', '-X', 'utf8', str(MODULE/'bridge.py'),
                'serve', '--simulate', '--transport', 'stdio', '--state-dir', str(Path(temp)/'stdio')])
            async with stdio_client(params) as streams:
                async with ClientSession(*streams) as session:
                    await session.initialize()
                    result = await session.call_tool('bridge_status', {})
                    assert result.structured_content['simulation']
                    passed.append('stdio-legacy')
            report = {'sdk_version': version('mcp'), 'passed': passed, 'simulated_sends': len(source.sent), 'native_sends': 0}
            (MODULE/'state/sdk-validation-result.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
            print(json.dumps(report, indent=2))
        finally:
            source.close(); bridge.close(); server.shutdown(); server.server_close(); thread.join()


if __name__ == '__main__':
    asyncio.run(main())
