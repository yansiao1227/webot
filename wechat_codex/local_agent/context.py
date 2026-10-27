"""生成前选择有界上下文快照，不获取附件字节。"""
import json

def choose(store,event_id,mode=None,limit=None,query=''):
    current=store.db.execute('SELECT seq,conversation,event FROM messages WHERE event_id=?',(event_id,)).fetchone()
    if not current:raise ValueError('触发事件不存在')
    event=json.loads(current['event'])
    if mode is None:
        # 新任务线程无法仅凭触发消息判断缺少哪些历史。
        # 即使消息措辞看似完整，也提供一个小范围快照。
        mode='reference' if event.get('reference') else 'recent'
    if mode not in ('none','recent','reference','search'):raise ValueError('不支持的上下文范围')
    default_limit=12
    limit=0 if mode=='none' else min(12,max(1,limit if isinstance(limit,int) and not isinstance(limit,bool) else default_limit))
    conversation=store.conversation(current['conversation'])
    result={'messages':[],'results':[],'through_seq':current['seq'],'epoch':conversation['epoch'],
            'selection':{'mode':mode,'limit':limit,'max_bytes':16384}}
    if not limit:return result
    mid=(event.get('reference')or{}).get('message_id')
    if mode=='reference' and not mid:
        result['selection']['reason']='native_reference_missing'
        return result
    anchor=None
    if mode=='reference':
        anchor=store.db.execute('''SELECT seq FROM messages WHERE conversation=? AND seq<?
            AND json_extract(event,'$.message_id')=? ORDER BY seq DESC LIMIT 1''',
            (current['conversation'],current['seq'],mid)).fetchone()
        if anchor is None:
            result['selection']['reason']='reference_target_missing'
            return result
        result['selection'].update(before_limit=limit,after_limit=limit)
        rows=[]
        for condition,order,bound in (('m.seq<?','DESC',anchor['seq']),
                                       ('m.seq>=?','ASC',anchor['seq'])):
            rows.extend(store.db.execute(f'''SELECT m.seq,m.event,j.reply,j.result,j.state
                FROM messages m LEFT JOIN jobs j USING(event_id)
                WHERE m.conversation=? AND m.seq<? AND {condition}
                ORDER BY m.seq {order} LIMIT 256''',
                (current['conversation'],current['seq'],bound)).fetchall())
        rows.sort(key=lambda row:row['seq'],reverse=True)
    else:
        rows=store.db.execute('''SELECT m.seq,m.event,j.reply,j.result,j.state FROM messages m LEFT JOIN jobs j USING(event_id)
            WHERE m.conversation=? AND m.seq<? ORDER BY m.seq DESC LIMIT 256''',
            (current['conversation'],current['seq'])).fetchall()
    selected=[];used=0;seen=set();own_sorts=set()
    for prior in rows:
        r=json.loads(prior['result'] or '{}')
        ops=[r.get('operation',{})]+[p.get('operation',{}) for p in r.get('parts',[])]
        for op in ops:
            native=((op.get('result')or{}).get('result')or{})
            if native.get('native_sort_seq_source')=='weixin41930_send_task_record110_v1':
                own_sorts.add(native.get('native_sort_seq'))
    for row in rows:
        e=json.loads(row['event'])
        # 将持久保存的模型输出用作助手上下文，不单独选择
        # 自动回显；人工出站文本仍可作为候选。
        if e.get('direction')=='outgoing':
            key=(e.get('native_sort_seq') or e.get('native_client_id') or e['event_id'])
            if key in seen:continue
            seen.add(key)
            if e.get('sender_id')==e.get('account'):
                if (e.get('native_sort_seq') and e['native_sort_seq'] in own_sorts and
                        not (mode=='reference' and e.get('message_id')==mid)):continue
        if mode=='search' and (not query or query.casefold() not in (json.dumps(e,ensure_ascii=False)+(row['reply']or'')).casefold()):
            continue
        value={k:e[k] for k in ('event_id','sender_id','message_id','direction','message_type','captured_at','reference') if k in e}
        value['payload']=({'text':e['payload']['text'][:3000]} if e['message_type']=='text' else
            {k:e['payload'][k] for k in ('name','mime','size') if k in e['payload']})
        if row['state']=='accepted' and row['reply']:
            r=json.loads(row['result']or'{}')
            # 从已提交的回复段重建多段回答。
            answer='\n'.join(p['text'] for p in r.get('parts',[]) if p.get('state')=='accepted') or row['reply']
            value['assistant_reply']=answer[:4000]
        selected.append((row['seq'],value))
    if anchor is not None:
        center=[item for item in selected if item[0]==anchor['seq']]
        before=[item for item in selected if item[0]<anchor['seq']][:limit]
        after=list(reversed([item for item in selected if item[0]>anchor['seq']]))[:limit]
        # 先为精确目标预留空间，再优先选择距离最近的消息。
        selected=center+[item for pair in zip(before,after) for item in pair]+before[len(after):]+after[len(before):]
    messages=[]
    for seq,value in selected:
        size=len(json.dumps(value,ensure_ascii=False).encode())
        if used+size>16000:continue  # 为选择结果封套预留空间。
        messages.append((seq,value));used+=size
        if anchor is None and len(messages)>=limit:break
    result['messages']=[value for seq,value in sorted(messages,key=lambda item:item[0])]
    result['selection']['bytes']=used
    return result
