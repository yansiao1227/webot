"""操作员验证：自有业务台账、SDK 计时及 MCP 精确发送状态。"""
import asyncio
from datetime import datetime
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import time
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'wechat_codex'));sys.path.insert(0,str(ROOT))
from scripts.wechat_stack import manage
from local_agent.mcp import MCP,MCPError
from local_agent.runtime import atomic_json

def sdk_evidence(thread_id):
    files=list((Path.home()/'.codex/sessions').glob('**/*'+thread_id+'*.jsonl'))
    result={'turns':[],'contexts':[]}
    if len(files)!=1:return result
    active={}
    for line in files[0].open(encoding='utf-8'):
        row=json.loads(line);p=row.get('payload',{})
        if row.get('type')=='event_msg':
            kind=p.get('type');tid=p.get('turn_id')
            if kind=='task_started':active[tid]={'turn_id':tid,'started_at':row['timestamp']}
            if kind=='task_complete' and tid in active:
                value=active.pop(tid);value['completed_at']=row['timestamp']
                value['elapsed_seconds']=(datetime.fromisoformat(value['completed_at'].replace('Z','+00:00'))-
                    datetime.fromisoformat(value['started_at'].replace('Z','+00:00'))).total_seconds()
                result['turns'].append(value)
        if row.get('type')=='response_item' and p.get('type')=='function_call_output' and isinstance(p.get('output'),str):
            try:value=json.loads(p['output'])
            except ValueError:continue
            if isinstance(value,dict) and 'conversation_id' in value:
                context=value.get('context')or{}
                result['contexts'].append({'selection':context.get('selection',{'mode':'not_attached'}),
                    'event_ids':[e.get('event_id') for e in context.get('messages',[])]})
    return result

async def main():
    config,_,token=manage.configuration();bridge=MCP(config['mcp_url'],token)
    connection=sqlite3.connect('file:'+str(ROOT/'wechat_codex/state/live/agent.sqlite3')+'?mode=ro',uri=True)
    report={'checked_at':time.time(),'events':{}}
    try:
        rows=connection.execute('SELECT event_id,state,event,result FROM jobs ORDER BY seq DESC LIMIT 30').fetchall()
        for eid,state,raw,raw_result in rows:
            event=json.loads(raw);text=event.get('payload',{}).get('text','');details=json.loads(raw_result or '{}')
            if not details.get('adaptive'):continue
            if not ('ADAPTIVE_' in text or '刚才第三阶段' in text):continue
            value={'state':state,'conversation_id':event['conversation_id'],'message_id':event.get('message_id'),
                'task_thread_id':details['task_thread_id'],'text_sha256':hashlib.sha256(text.encode()).hexdigest(),
                'generation_error':details.get('generation',{}).get('error'),'parts':[],
                'sdk':sdk_evidence(details['task_thread_id'])}
            for part in details.get('parts',[]):
                index=part['part_index']
                try:op=await bridge.call('get_reply_part_status',event_id=eid,part_index=index)
                except MCPError:op={'state':'query_failed'}
                value['parts'].append({'part_index':index,'text_sha256':hashlib.sha256(part['text'].encode()).hexdigest(),
                    **{k:op.get(k) for k in ('state','final','request_id','send_echo')}})
            report['events'][eid]=value
    finally:connection.close()
    output=ROOT/'wechat_codex/state/live/adaptive-replies-validation.json'
    if output.exists():
        old=json.loads(output.read_text(encoding='utf-8'))
        for key in ('peer_confirmed','peer_confirmation'):
            if key in old:report[key]=old[key]
    atomic_json(output,report)
    print(json.dumps(report,ensure_ascii=True))

if __name__=='__main__':asyncio.run(main())
