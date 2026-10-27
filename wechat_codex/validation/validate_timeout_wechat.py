"""仅供操作员观察真实群聊超时测试，不发送或重放。"""
import asyncio
import json
from pathlib import Path
import sqlite3
import sys
import time
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
sys.path.insert(0, str(ROOT / 'wechat_codex'))
from scripts.wechat_stack import manage
from local_agent.mcp import MCP,allowed
from local_agent.runtime import atomic_json

async def main():
    config,_,token=manage.configuration()
    bridge=MCP(config['mcp_url'],token)
    consumer='timeout-live-validation-20261024'
    output=ROOT/'wechat_codex/state/live/timeout-wechat-validation.json'
    report=json.loads(output.read_text(encoding='utf-8')) if output.exists() else {}
    for _ in range(20):
        batch=await bridge.call('get_messages',consumer_id=consumer,timeout=0,limit=100,start='earliest')
        for event in batch['events']:
            if (event.get('conversation_id')=='48418824345@chatroom' and
                    'TIMEOUT_CHECK_20261024' in event.get('payload',{}).get('text','') and
                    allowed(event,config['account'],config['conversations'])):
                report={'scope':'real_wechat_guarded_model_timeout_one_trigger','event_id':event['event_id'],'peer_confirmed':False}
                atomic_json(output,report)
        if batch.get('ack_token'):
            await bridge.call('ack_messages',consumer_id=consumer,ack_token=batch['ack_token'])
        if len(batch['events'])<100:break
    if not report:
        print(json.dumps({'state':'waiting_for_authorized_marker'}));return
    connection=sqlite3.connect((ROOT/'wechat_codex/state/live/agent.sqlite3').as_uri()+'?mode=ro',uri=True)
    connection.row_factory=sqlite3.Row
    try:
        row=connection.execute('SELECT conversation,state,reply,result,error FROM jobs WHERE event_id=?',(report['event_id'],)).fetchone()
        if row:
            report.update(checked_at=time.time(),state=row['state'],error=row['error'],
                timeout_error_reply=bool(row['reply'] and '超时' in row['reply']),
                result=json.loads(row['result']) if row['result'] else {},
                conversation_paused=connection.execute('SELECT paused FROM conversations WHERE id=?',(row['conversation'],)).fetchone()[0])
    finally:connection.close()
    report['operation']=await bridge.call('get_reply_status',event_id=report['event_id'])
    atomic_json(output,report)
    print(json.dumps(report,ensure_ascii=True))

if __name__=='__main__':asyncio.run(main())
