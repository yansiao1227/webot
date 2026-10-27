"""仅供操作员观察真实发送与回显，不生成、不发送，也不访问数据库。"""
import asyncio
import hashlib
import json
from pathlib import Path
import sys
import time

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'wechat_codex'))
sys.path.insert(0,str(ROOT))
from scripts.wechat_stack import manage
from local_agent.mcp import MCP,MCPError
from local_agent.runtime import atomic_json

GROUP='48418824345@chatroom'
OUTPUT=ROOT/'wechat_codex/state/live/send-echo-chain-validation.json'


async def main():
    config,hook_token,token=manage.configuration()
    bridge=MCP(config['mcp_url'],token)
    status=await bridge.call('bridge_status')
    if (status.get('simulation') is not False or status.get('account')!=config['account']
            or GROUP not in status.get('allowed_conversations',[])):
        raise ValueError('real account/group mismatch')
    capture=manage.request(manage.hook_port(),'/client_hook/status',hook_token)['capture']
    instance=capture['instance'];minimum=capture['outgoing_min_created_at']
    report=json.loads(OUTPUT.read_text(encoding='utf-8')) if OUTPUT.exists() else {}
    if report.get('instance')!=instance:
        report={'instance':instance,'minimum_created_at':minimum,'events':{},'peer_confirmed':False}
    for _ in range(20):
        batch=await bridge.call('get_messages',consumer_id='send-echo-validation-20261024',timeout=0,limit=100,start='earliest')
        for event in batch['events']:
            if event.get('client_instance')!=instance or event.get('conversation_id')!=GROUP:
                continue
            value={k:event[k] for k in ('event_id','account','conversation_id','direction','sender_id','message_id',
                'native_sort_seq','native_created_at','native_record_metadata_source','native_local_id',
                'native_message_id_source','native_message_id_state','message_type') if k in event}
            text=event.get('payload',{}).get('text','')
            value['text_sha256']=hashlib.sha256(text.encode()).hexdigest()
            if text.startswith('OWNER_ECHO_BG_20261024:') or text.startswith('OWNER_ECHO_BG_20261024：'):
                value['probe']='manual_background'
            elif event['direction']=='incoming' and '请复述新代号' in text:
                value['probe']='first_request'
            elif event['direction']=='incoming' and '只复述你上一条回复' in text:
                value['probe']='followup'
            report['events'][event['event_id']]=value
        atomic_json(OUTPUT,report)
        if batch.get('ack_token'):
            await bridge.call('ack_messages',consumer_id='send-echo-validation-20261024',ack_token=batch['ack_token'])
        if len(batch['events'])<100:
            break
    replies={}
    for event in report['events'].values():
        if event.get('probe') not in ('first_request','followup'):
            continue
        try:
            replies[event['probe']]=await bridge.call('get_reply_status',event_id=event['event_id'])
        except MCPError:
            replies[event['probe']]={'state':'no_reply_intent'}
    # 提供方仅统计由精确原生数据库查询得到的 create_time；原始 +134
    # 不是创建时间，不能作为历史消息过滤的依据。
    counters=capture.get('history_filter',{})
    old_persisted=[v['event_id'] for v in report['events'].values() if v.get('direction')=='outgoing'
                   and v.get('native_created_at',minimum)<minimum]
    report.update(checked_at=time.time(),replies=replies,history_filter_counters=counters,
        old_outgoing_persisted=old_persisted,
        manual_background_verified=any(v.get('probe')=='manual_background' and v.get('direction')=='outgoing'
            and v.get('sender_id')==config['account'] and v.get('native_message_id_source')==
            'weixin41930_message_db_exact_sort_seq_v1' for v in report['events'].values()),
        history_filter_verified=counters.get('source')=='exact_native_db_record_metadata' and
            counters.get('filtered_old',0)>0 and counters.get('accepted_current',0)>0 and
            counters.get('unresolved',0)==0 and not old_persisted,
        both_send_echoes_matched=len(replies)==2 and all(v.get('state')=='accepted' and
            v.get('send_echo',{}).get('state')=='matched' and v['send_echo'].get('message_id')
            for v in replies.values()))
    atomic_json(OUTPUT,report)
    print(json.dumps({k:report[k] for k in ('instance','manual_background_verified','history_filter_verified','history_filter_counters',
        'old_outgoing_persisted','both_send_echoes_matched','peer_confirmed','replies')},ensure_ascii=True))


if __name__=='__main__':
    asyncio.run(main())
