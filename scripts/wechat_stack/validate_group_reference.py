"""Operator-only group live evidence via MCP. Does not generate or send replies."""
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
from local_agent.mcp import MCP, MCPError
from local_agent.runtime import atomic_json

GROUP = '48418824345@chatroom'
CONSUMER = 'group-live-validation-20261024'
OUTPUT = ROOT / 'wechat_mcp/state/group-reference-validation/events.json'


async def main():
    config, _, token = manage.configuration()
    bridge = MCP(config['mcp_url'], token)
    status = await bridge.call('bridge_status')
    if status.get('simulation') is not False or status.get('account') != config['account'] or GROUP not in status.get('allowed_conversations', []):
        raise ValueError('真机账号或群白名单不符')
    report = json.loads(OUTPUT.read_text(encoding='utf-8')) if OUTPUT.exists() else {'events': {}}
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    for _ in range(10):
        batch = await bridge.call('get_messages', consumer_id=CONSUMER, timeout=0, limit=100, start='earliest')
        for event in batch['events']:
            if event.get('conversation_id') != GROUP or int(event['event_id'].rsplit(':', 1)[1]) <= 85:
                continue
            value = {k: event[k] for k in ('event_id', 'account', 'conversation_id', 'direction', 'sender_id',
                'message_type', 'message_id', 'native_client_id', 'native_client_id_source', 'mentions', 'reference', 'captured_at',
                'client_instance','native_sort_seq','native_created_at','native_record_metadata_source',
                'native_local_id','native_message_id_source','native_message_id_state') if k in event}
            if event.get('message_type') == 'text':
                text = event['payload']['text']
                value['text_sha256'] = hashlib.sha256(text.encode()).hexdigest()
                value['text_chars'] = len(text)
            report['events'][event['event_id']] = value
        report['checked_at'] = time.time()
        atomic_json(OUTPUT, report)  # Commit local evidence before acknowledging our own consumer.
        if batch.get('ack_token'):
            await bridge.call('ack_messages', consumer_id=CONSUMER, ack_token=batch['ack_token'])
        if len(batch['events']) < 100:
            break
    for eid, value in report['events'].items():
        if value.get('direction') != 'incoming' or value.get('message_type') != 'text':
            continue
        try:
            value['reply_status'] = await bridge.call('get_reply_status', event_id=eid)
        except MCPError:
            value['reply_status'] = {'state': 'no_reply_intent'}
        if value.get('reference'):
            try:
                target = await bridge.call('resolve_reference', event_id=eid)
                value['resolved_target'] = {k:target[k] for k in ('event_id','message_id','sender_id','message_type') if k in target}
            except MCPError:
                value['reference_access'] = 'denied_or_target_unavailable'
    atomic_json(OUTPUT, report)
    print(json.dumps({'checked_at': report['checked_at'], 'events': [
        {'event_id': eid, **{k:value[k] for k in ('direction','message_type','message_id','native_client_id','mentions','reference') if k in value},
         'reply': value.get('reply_status',{}).get('state'),
         'send_echo': value.get('reply_status',{}).get('send_echo')}
        for eid,value in report['events'].items()]}, ensure_ascii=True))


if __name__ == '__main__':
    asyncio.run(main())
