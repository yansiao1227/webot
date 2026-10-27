"""通用微信 MCP 桥接 CLI：serve / demo / doctor。"""
import argparse
import base64
import json
import os
from pathlib import Path
import secrets
import threading
import time
import uuid

import wechat_bridge
from tool.settings import hook_url, mcp_port, local_host
from wechat_bridge.httpio import HookClient
from wechat_bridge.server import Server, stdio
from wechat_bridge.service import Bridge, EVENT_NAME
from wechat_bridge.simulation import SimulatedAgent, SimulatedHook

ROOT = Path(__file__).resolve().parents[1]


def demo(directory):
    source = SimulatedHook()
    bridge = Bridge(directory, HookClient(source.url), source.account, ['wxid_sim_peer', '12345@chatroom'], True, start='earliest')
    server = Server(('127.0.0.1', 0), bridge, secrets.token_urlsafe(32))
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    secret = 'whsec_' + base64.b64encode(secrets.token_bytes(32)).decode()
    agent = SimulatedAgent(f'http://127.0.0.1:{server.server_port}', server.token, secret)
    try:
        subscription = agent.rpc('events/subscribe', {'name': EVENT_NAME,
            'arguments': {'conversation_ids': ['wxid_sim_peer', '12345@chatroom']},
            'delivery': {'mode': 'webhook', 'url': agent.url, 'secret': secret}})
        if 'error' in subscription:
            raise RuntimeError('模拟订阅失败：' + json.dumps(subscription))
        bridge.start_workers()
        first = source.inject('wxid_sim_peer', '你好，中文与 emoji 😀')
        source.inject('12345@chatroom', '群聊测试', mentions={
            'state': 'known', 'wxids': [source.account], 'all': False,
            'source': 'weixin41930_record_source_180'})
        source.inject('wxid_not_allowed', '这条不应触发回复')
        deadline = time.monotonic() + 12
        while len(agent.results) < 2 and time.monotonic() < deadline:
            time.sleep(.05)
        duplicate = agent.rpc('tools/call', {'name': 'reply_text', 'arguments': {
            'event_id': first['event_id'], 'text': '模拟自动回复：' + first['payload']['text']}})
        if len(source.sent) != 2 or any(x['result'].get('isError') for x in agent.results) or duplicate.get('result', {}).get('isError'):
            raise RuntimeError('模拟自动回复、过滤或去重验证失败')
        report = {'mode': 'simulation_rule_agent_not_dot', 'passed': True, 'injected': 3,
                  'webhook_events': len(agent.events), 'native_sends': 0, 'simulated_sends': len(source.sent),
                  'duplicate_reply_did_not_resend': True, 'status': bridge.store.status(),
                  'sent': source.sent, 'dot_verified': False}
        Path(directory).mkdir(parents=True, exist_ok=True)
        (Path(directory) / 'demo-result.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps(report, ensure_ascii=False, indent=2))
    finally:
        agent.close(); server.shutdown(); server.server_close(); thread.join()
        source.close(); bridge.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    serve = sub.add_parser('serve')
    serve.add_argument('--transport', choices=['http', 'stdio'], default='http')
    serve.add_argument('--host', choices=tuple(dict.fromkeys(('127.0.0.1', 'localhost', local_host()))), default=local_host())
    serve.add_argument('--port', type=int, default=mcp_port())
    serve.add_argument('--stop-file', type=Path, help='本机管理脚本使用的正常退出标记')
    serve.add_argument('--state-dir', type=Path, default=ROOT / 'state/live')
    serve.add_argument('--hook-url', default=hook_url())
    serve.add_argument('--account')
    serve.add_argument('--allow-conversation', action='append', default=[])
    serve.add_argument('--consumer-id', default='mcp-bridge')
    serve.add_argument('--start', choices=['earliest', 'latest'], default='latest')
    serve.add_argument('--simulate', action='store_true', help='模拟上游，仅回环事件回调')
    scenario = sub.add_parser('demo')
    scenario.add_argument('--state-dir', type=Path, default=ROOT / ('state/demo-' + uuid.uuid4().hex[:10]))
    sub.add_parser('doctor')
    args = parser.parse_args()
    if args.command == 'doctor':
        print(json.dumps({'mcp_token_configured': bool(os.getenv('MCP_BRIDGE_TOKEN')),
                          'hook_token_configured': bool(os.getenv('CLIENT_HOOK_TOKEN'))}, indent=2))
        return
    if args.command == 'demo':
        demo(args.state_dir)
        return
    if args.transport == 'http' and len(os.getenv('MCP_BRIDGE_TOKEN', '')) < 24:
        parser.error('请先设置至少 24 字符的 MCP_BRIDGE_TOKEN 环境变量')
    simulator = SimulatedHook() if args.simulate else None
    account = simulator.account if simulator else args.account
    conversations = args.allow_conversation or (['wxid_sim_peer'] if simulator else [])
    if not account or not conversations:
        parser.error('真实模式需要 --account 和 --allow-conversation')
    if simulator and args.state_dir == ROOT / 'state/live':
        args.state_dir = ROOT / 'state/simulation'
    hook = HookClient(simulator.url if simulator else args.hook_url, None if simulator else os.getenv('CLIENT_HOOK_TOKEN'))
    try:
        bridge = Bridge(args.state_dir, hook, account, conversations, bool(simulator), args.consumer_id, args.start)
    except BaseException:
        if simulator:
            simulator.close()
        raise
    server = None
    done = threading.Event()
    try:
        if args.transport == 'http':
            server = Server((args.host, args.port), bridge, os.getenv('MCP_BRIDGE_TOKEN'), simulator)
        bridge.start_workers()
        if server:
            import sys
            print(json.dumps({'mcp_url': f'http://{args.host}:{server.server_port}/mcp', 'simulation': bool(simulator)}), file=sys.stderr)
            if args.stop_file:
                def watch_stop():
                    while not done.wait(.25):
                        if args.stop_file.exists():
                            server.shutdown()
                            return
                threading.Thread(target=watch_stop, daemon=True).start()
            server.serve_forever()
        else:
            stdio(bridge)
    except KeyboardInterrupt:
        pass
    finally:
        if args.stop_file and server:
            done.set()
        if server:
            server.server_close()
        if simulator:
            simulator.close()
        bridge.close()


