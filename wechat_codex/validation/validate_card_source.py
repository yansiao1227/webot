"""对已有授权引用进行真实 MCP/SDK 只读验证，从不发送消息。"""
import argparse
import asyncio
import hashlib
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'wechat_codex'))
from scripts.wechat_stack import manage
from local_agent import attachments, media
from local_agent.codex_backend import CodexBackend
from local_agent.mcp import MCP, allowed
from local_agent.runtime import atomic_json


async def main(event_id):
    config, _, token = manage.configuration()
    bridge = MCP(config['mcp_url'], token)
    status = await bridge.call('bridge_status')
    if status.get('simulation') is not False or status.get('account') != config['account']:
        raise ValueError('真实桥接身份不符')
    event = await bridge.call('get_message', event_id=event_id)
    if not allowed(event, config['account'], config['conversations']):
        raise ValueError('非合法触发事件')
    target = await bridge.call('resolve_reference', event_id=event_id)
    if target['message_type'] != 'card':
        raise ValueError('仅验收引用卡片')
    directory = ROOT / 'wechat_codex/state/card-source-validation'
    event['attachment'] = media.prepare(await attachments.fetch(
        bridge, event_id, target, ROOT / 'wechat_codex/state/live/resources'))
    event['reference_target'] = {k: target[k] for k in ('event_id', 'message_type', 'payload')}
    backend = CodexBackend(directory / 'workspace', timeout=240)
    try:
        await backend.start()
        thread = await backend.ensure_thread(event['conversation_id'])
        reply, result = await backend.generate(thread, event)
        report = {'checked_at': time.time(), 'scope': 'real_reference_real_sdk_source_tools_no_send',
                  'event_id': event_id, 'source_urls': event['attachment']['prepared']['source_urls'],
                  'generation': result, 'reply_chars': len(reply),
                  'reply_sha256': hashlib.sha256(reply.encode()).hexdigest(), 'wechat_reply_sent': False}
        atomic_json(directory / 'result.json', report)
        print(json.dumps({'report': report, 'reply_for_operator_review': reply}, ensure_ascii=False), flush=True)
    finally:
        await backend.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('event_id')
    asyncio.run(main(parser.parse_args().event_id))
