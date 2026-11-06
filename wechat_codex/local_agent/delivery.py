"""发送延迟只挂起原任务；后台只查原意图，绝不重放模型或原段。"""
import asyncio
import json
import time

PENDING = ('queued', 'running', 'submitting', 'prepared')
RECHECK_SECONDS = 10
WAIT_SECONDS = 120
NOTICE = '此前一轮因回复发送确认延迟已停止，最终回答未完成；已发出的内容保留，不会自动重跑。'


def confirm_interruption(store, eid):
    row = store.db.execute('SELECT state,result,error FROM jobs WHERE event_id=?', (eid,)).fetchone()
    details = json.loads(row['result'] or '{}')
    store.update(eid, row['state'], error=row['error'],
                 result={'delivery': {**details.get('delivery', {}), 'interruption_confirmed': True}})


def defer(store, eid, operation=None, *, progress=None):
    row = store.db.execute('SELECT state,result FROM jobs WHERE event_id=?', (eid,)).fetchone()
    details = json.loads(row['result'] or '{}')
    delivery = details.get('delivery', {})
    now = time.time()
    delivery.setdefault('started_at', now)
    delivery.update(next_check=now + RECHECK_SECONDS)
    result = {'delivery': delivery}
    if progress is not None:
        result['progress_pending'] = progress
        delivery.setdefault('interruption_confirmed', False)
    if operation is not None:
        result['operation'] = operation
    state = (operation or {}).get('state')
    target = 'send_failed' if state == 'failed' else (
        'send_held' if state == 'unknown' or row['state']=='send_held' or
        now - delivery['started_at'] >= WAIT_SECONDS else 'send_wait')
    store.update(eid, target, result=result,
                 error='原段发送明确失败，本任务结束，禁止跳段或自动重跑' if target == 'send_failed' else
                 '本任务发送未确认，禁止重发或重跑；后续消息可继续处理')


def due(store, now=None):
    now = time.time() if now is None else now
    return [dict(r) for r in store.db.execute('''SELECT j.* FROM jobs j
        JOIN conversations c ON c.id=j.conversation AND c.epoch=j.epoch
        WHERE c.paused=0 AND j.state IN ('send_wait','send_held')
        AND COALESCE(json_extract(j.result,'$.delivery.next_check'),0)<=?
        ORDER BY j.updated LIMIT 32''', (now,))]


async def reconcile(agent, job):
    store = agent.store
    eid = job['event_id']
    details = json.loads(job['result'] or '{}')
    pending = details.get('progress_pending')
    index = pending['part_index'] if pending else details.get('part_index', 0)
    try:
        await asyncio.wait_for(agent.verify(), 5)
        name = 'get_reply_part_status' if details.get('adaptive') else 'get_reply_status'
        args = {'event_id': eid}
        if details.get('adaptive'):
            args['part_index'] = index
        operation = await asyncio.wait_for(agent.mcp.call(name, **args), 5)
    except Exception:
        # 查询异常不能推断原发送失败，也不能重新 POST。
        defer(store, eid)
        return
    if operation.get('state') != 'accepted':
        defer(store, eid, operation)
        return
    if not details.get('adaptive'):
        store.update(eid, 'accepted', result={'operation': operation})
        return
    parts = details.get('parts', [])
    if not any(p['part_index'] == index for p in parts):
        part = pending or {'part_index': index, 'text': job['reply']}
        parts.append({**part, 'state': 'accepted', 'operation': operation})
    details.update(parts=parts, operation=operation, progress_pending=None)
    if pending or not details.get('part_final', True):
        # 当前轮次已经退出；原段接受后只补固定中止说明，不恢复模型。
        details.update(part_index=index + 1, part_final=True, delivery_notice=True)
        store.update(eid, 'generated', reply=NOTICE, result=details)
    else:
        store.update(eid, 'interrupted' if details.get('delivery_notice') else 'accepted', result=details)
