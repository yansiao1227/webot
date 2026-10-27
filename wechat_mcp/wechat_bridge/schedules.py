"""长期原会话授权与每次定时投递的持久幂等操作。"""
import hashlib
import json
import re
import time
from urllib.parse import quote
from .httpio import RemoteError


def initialize(db):
    db.executescript('''
        CREATE TABLE IF NOT EXISTS schedule_grants(id TEXT PRIMARY KEY,event_id TEXT NOT NULL,
            evidence TEXT NOT NULL,active INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS scheduled_parts(schedule_id TEXT NOT NULL,run_id TEXT NOT NULL,
            part_index INTEGER NOT NULL,request_id TEXT UNIQUE NOT NULL,text TEXT NOT NULL,
            final INTEGER NOT NULL,state TEXT NOT NULL,result TEXT,created REAL NOT NULL,
            PRIMARY KEY(schedule_id,run_id,part_index));
    ''')


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', value):
        raise ValueError('定时任务或执行标识无效')


def authorize(bridge, event_id, schedule_id):
    identifier(schedule_id)
    event = bridge.store.event(event_id)
    if not bridge.reply_allowed(event) or not event.get('sender_id'):
        raise ValueError('定时任务缺少有效入站授权或创建者')
    evidence = {k: event[k] for k in ('account', 'conversation_id', 'sender_id', 'direction',
        'message_type', 'event_id', 'captured_at', 'mentions') if k in event}
    evidence['payload'] = {'text': ''}
    with bridge.reply_lock:
        bridge.verify_account()
        with bridge.store.tx() as db:
            row = db.execute('SELECT * FROM schedule_grants WHERE id=?', (schedule_id,)).fetchone()
            if row and (row['event_id'] != event_id or not row['active']):
                raise ValueError('任务授权已存在或已撤销，禁止覆盖')
            db.execute('INSERT OR IGNORE INTO schedule_grants VALUES(?,?,?,1)',
                       (schedule_id, event_id, json.dumps(evidence)))
    return {'schedule_id': schedule_id, 'active': True,
            'conversation_id': event['conversation_id'], 'sender_id': event['sender_id']}


def revoke(bridge, schedule_id):
    identifier(schedule_id)
    with bridge.reply_lock, bridge.store.tx() as db:
        db.execute('UPDATE schedule_grants SET active=0 WHERE id=?', (schedule_id,))
    return {'schedule_id': schedule_id, 'active': False}


def grant(bridge, schedule_id):
    identifier(schedule_id)
    with bridge.store.lock:
        row = bridge.store.db.execute('SELECT * FROM schedule_grants WHERE id=?', (schedule_id,)).fetchone()
    if not row or not row['active'] or not bridge.reply_allowed(json.loads(row['evidence'])):
        raise ValueError('定时授权已撤销或账号、会话门禁失效')
    return json.loads(row['evidence'])


def operation(bridge, key):
    with bridge.store.lock:
        row = bridge.store.db.execute('SELECT * FROM scheduled_parts WHERE schedule_id=? AND run_id=? AND part_index=?', key).fetchone()
    if not row:
        raise ValueError('没有该定时发送意图')
    return dict(row)


def check(bridge, schedule_id):
    event = grant(bridge, schedule_id)
    bridge.verify_account()
    return {'schedule_id': schedule_id, 'active': True, 'conversation_id': event['conversation_id'],
            'sender_id': event['sender_id']}


def update(bridge, key, state, result=None):
    with bridge.store.tx() as db:
        db.execute('UPDATE scheduled_parts SET state=?,result=COALESCE(?,result) WHERE schedule_id=? AND run_id=? AND part_index=?',
                   (state, json.dumps(result) if result is not None else None, *key))


def validate_key(schedule_id, run_id, part_index):
    identifier(schedule_id); identifier(run_id)
    if isinstance(part_index, bool) or not isinstance(part_index, int) or not 0 <= part_index < 64:
        raise ValueError('分段序号必须为0至63')
    return schedule_id, run_id, part_index


def status(bridge, schedule_id, run_id, part_index):
    key = validate_key(schedule_id, run_id, part_index)
    # 撤销后仍允许核查已提交的原操作，但不能产生新的发送。
    op = operation(bridge, key)
    if op['state'] not in ('prepared', 'accepted', 'failed', 'unknown'):
        try:
            result = bridge.hook.request('GET', '/client_hook/requests/' + quote(op['request_id'], safe=''))
            if result.get('account') != bridge.account:
                raise ValueError('操作账号不匹配')
            update(bridge, key, result['state'], result)
        except RemoteError as error:
            if error.status == 404:
                update(bridge, key, 'unknown')
        except (OSError, TimeoutError):
            pass
        op = operation(bridge, key)
    return {'schedule_id': schedule_id, 'run_id': run_id, 'part_index': part_index,
            'request_id': op['request_id'], 'state': op['state'], 'delivery_confirmed': False,
            'result': json.loads(op['result']) if op['result'] else None}


def send(bridge, schedule_id, run_id, part_index, text, final):
    key = validate_key(schedule_id, run_id, part_index)
    if not isinstance(text, str) or not 1 <= len(text) <= 1200 or '\0' in text or not isinstance(final, bool):
        raise ValueError('定时正文或结束标记无效')
    with bridge.reply_lock:
        event = grant(bridge, schedule_id)
        with bridge.store.tx() as db:
            old = db.execute('SELECT * FROM scheduled_parts WHERE schedule_id=? AND run_id=? AND part_index=?', key).fetchone()
            if old:
                if old['text'] != text or bool(old['final']) != final:
                    raise ValueError('禁止覆盖定时发送意图')
            else:
                previous = db.execute('SELECT * FROM scheduled_parts WHERE schedule_id=? AND run_id=? ORDER BY part_index DESC LIMIT 1', key[:2]).fetchone()
                if ((previous is None and part_index != 0) or (previous is not None and
                        (previous['part_index'] + 1 != part_index or previous['final'] or previous['state'] != 'accepted'))):
                    raise ValueError('禁止跳段、越过未知状态或在结束后追加')
                rid = 'schedule_' + hashlib.sha256(json.dumps([bridge.account, *key]).encode()).hexdigest()[:48]
                db.execute('INSERT INTO scheduled_parts VALUES(?,?,?,?,?,?,?,NULL,?)',
                           (*key, rid, text, int(final), 'prepared', time.time()))
        op = operation(bridge, key)
        if op['state'] == 'prepared':
            bridge.verify_account()
            update(bridge, key, 'submitting')
            try:
                result = bridge.hook.request('POST', '/SendTextMsg',
                    {'toid': event['conversation_id'], 'msg': text, 'request_id': op['request_id']})
                update(bridge, key, result['state'], result)
            except Exception:
                return status(bridge, *key)
        return status(bridge, *key)
