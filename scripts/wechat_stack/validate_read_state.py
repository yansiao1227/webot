"""Observe real read operations via MCP only; never mark/read/send on its own."""
import asyncio
import json
from pathlib import Path
import sys
import time
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'wechat_codex'));sys.path.insert(0,str(ROOT))
from scripts.wechat_stack import manage
from local_agent.mcp import MCP,MCPError
from local_agent.runtime import atomic_json
OUTPUT=ROOT/'wechat_codex/state/live/read-state-validation.json'
CONSUMER='read-state-validation-20261024'
async def main():
    config,_,token=manage.configuration();bridge=MCP(config['mcp_url'],token)
    status=await bridge.call('bridge_status')
    if status.get('simulation') is not False or status.get('account')!=config['account']:
        raise ValueError('真实账号不符')
    report=json.loads(OUTPUT.read_text(encoding='utf-8')) if OUTPUT.exists() else {'events':{}}
    batch=await bridge.call('get_messages',consumer_id=CONSUMER,timeout=0,limit=100,start='latest')
    for event in batch['events']:
        text=event.get('payload',{}).get('text','')
        if event.get('direction')=='incoming' and 'READ_BADGE_' in text:
            mentions=event.get('mentions') or {}
            report['events'][event['event_id']]={k:event.get(k) for k in (
                'event_id','conversation_id','message_id','captured_at')}
            report['events'][event['event_id']]['self_mentioned']=config['account'] in mentions.get('wxids',[])
    if batch.get('ack_token'):await bridge.call('ack_messages',consumer_id=CONSUMER,ack_token=batch['ack_token'])
    for eid,record in report['events'].items():
        try:record['read']=await bridge.call('get_conversation_read_status',event_id=eid)
        except MCPError:record['read']={'state':'no_authorized_operation'}
        try:
            reply=await bridge.call('get_reply_status',event_id=eid)
            record['reply']={k:reply.get(k) for k in ('state','request_id','send_echo')}
        except MCPError:record['reply']={'state':'no_reply_intent'}
    report['checked_at']=time.time();atomic_json(OUTPUT,report)
    print(json.dumps(report,ensure_ascii=True))
if __name__=='__main__':asyncio.run(main())
