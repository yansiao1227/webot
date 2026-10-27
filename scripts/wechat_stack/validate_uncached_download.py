"""使用真实 MCP 引用授权及原生下载，精确隔离缓存，不发送消息。"""
import asyncio
import hashlib
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'client_hook'))
sys.path.insert(0, str(ROOT / 'wechat_codex'))
from scripts.wechat_stack import manage
from validation.cache_validation import isolated_native_cache
from local_agent.mcp import MCP, allowed
from local_agent.attachments import fetch
from local_agent.runtime import atomic_json


async def main():
    config, _, token = manage.configuration()
    bridge = MCP(config['mcp_url'], token)
    triggers = ['95944951383340939adc594c694b5da7:' + str(seq) for seq in (81, 82, 83)]
    pairs = []
    for eid in triggers:
        event = await bridge.call('get_message', event_id=eid)
        if not allowed(event, config['account'], config['conversations']):
            raise ValueError('不符合合法触发门禁')
        target = await bridge.call('resolve_reference', event_id=eid)
        pairs.append((event, target))
    report = {'scope': 'real_native_uncached_mcp_reference_acquisition', 'started_at': time.time(),
              'manual_open_or_download': False, 'results': [], 'original_caches_restored': False}
    output = ROOT / 'client_hook/state/native-download-validation/uncached-reference-chain.json'
    try:
        with isolated_native_cache([target for _, target in pairs]) as setup:
            report['cache_setup'] = setup
            atomic_json(output, report)
            for event, target in pairs:
                try:
                    item = await fetch(bridge, event['event_id'], target,
                                       ROOT / 'wechat_codex/state/live/resources', timeout=65)
                    report['results'].append({'trigger_event_id': event['event_id'],
                        'target_event_id': target['event_id'], 'kind': target['message_type'],
                        'size': item['size'], 'sha256': item['sha256'], 'resource_ready': True})
                except Exception as error:
                    report['results'].append({'trigger_event_id': event['event_id'],
                        'target_event_id': target['event_id'], 'kind': target['message_type'],
                        'resource_ready': False, 'error': str(error)})
                atomic_json(output, report)
                print(json.dumps(report['results'][-1], ensure_ascii=False), flush=True)
        report['original_caches_restored'] = True
    finally:
        report['completed_at'] = time.time()
        atomic_json(output, report)
    if len(report['results']) != 3 or not all(r['resource_ready'] for r in report['results']):
        raise SystemExit(1)


if __name__ == '__main__':
    asyncio.run(main())
