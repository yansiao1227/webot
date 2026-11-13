"""MCP 朋友圈发布：本机策略、持久业务意图与只查询的恢复路径。"""
import hashlib
import json
import math
from pathlib import Path
import re
import time
from urllib.parse import quote, urlencode


DEFAULT_POLICY = {'allow_direct': True, 'event_conversations': [],
                  'min_interval_seconds': 600, 'window_seconds': 86400, 'max_writes': 5}
INTENT_ROWS = '''SELECT i.*,c.request_id AS closed_request_id,c.params_sha256,c.closed_at
    FROM sns_intents i LEFT JOIN sns_closed_intents c ON c.intent_id=i.intent_id'''


def load_policy(directory, config=None):
    path = Path(config) if config is not None else Path(directory) / 'sns-policy.json'
    return policy(json.loads(path.read_text(encoding='utf-8-sig')) if config is not None or path.exists() else None)


def policy(value=None):
    value = {} if value is None else value
    if not isinstance(value, dict) or set(value) - set(DEFAULT_POLICY):
        raise ValueError('朋友圈策略字段无效')
    result = {**DEFAULT_POLICY, **value}
    for key in ('allow_direct',):
        if not isinstance(result[key], bool):
            raise ValueError(key + ' 必须为布尔值')
    for key in ('min_interval_seconds', 'window_seconds', 'max_writes'):
        number = result[key]
        if isinstance(number, bool) or not isinstance(number, int) or not 1 <= number <= 31536000:
            raise ValueError(key + ' 必须为正整数且不超过31536000')
    conversations = result['event_conversations']
    if (not isinstance(conversations, list) or len(conversations) > 100 or
            any(not isinstance(x, str) or not 1 <= len(x) <= 511 for x in conversations)):
        raise ValueError('event_conversations 无效')
    result['event_conversations'] = list(conversations)
    return result


def initialize(db):
    db.executescript('''
        CREATE TABLE IF NOT EXISTS sns_intents(intent_id TEXT PRIMARY KEY,request_id TEXT UNIQUE NOT NULL,
            params TEXT NOT NULL,evidence TEXT,state TEXT NOT NULL,result TEXT,created REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS sns_policy_state(id INTEGER PRIMARY KEY CHECK(id=1),
            paused INTEGER NOT NULL,reason TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS sns_closed_intents(intent_id TEXT PRIMARY KEY,
            request_id TEXT NOT NULL,params_sha256 TEXT NOT NULL,closed_at REAL NOT NULL);
        INSERT OR IGNORE INTO sns_policy_state VALUES(1,0,'');
        UPDATE sns_intents SET state='unknown' WHERE state='submitting';
    ''')
    if unresolved(db):
        db.execute("UPDATE sns_policy_state SET paused=1,reason='unresolved_publish' WHERE paused=0")


def closed_no_retry(row):
    return (row['closed_request_id'] == row['request_id'] and
            row['params_sha256'] == hashlib.sha256(row['params'].encode('utf-8')).hexdigest())


def unresolved(db):
    rows = db.execute(INTENT_ROWS + " WHERE i.state IN ('submitting','queued','running','accepted','unknown')")
    return any(row['state'] != 'unknown' or not closed_no_retry(row) for row in rows)


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', value):
        raise ValueError('intent_id 需要1至128位字母、数字、下划线或连字符')


def validate_text(text):
    if not isinstance(text, str) or not text.strip() or len(text.encode('utf-8')) > 65536:
        raise ValueError('仅支持非空、至多64KiB的公开纯文字')
    if any(not (c in '\t\n\r' or 0x20 <= ord(c) <= 0xD7FF or
                0xE000 <= ord(c) <= 0xFFFD or 0x10000 <= ord(c) <= 0x10FFFF) for c in text):
        raise ValueError('帖文包含非法XML字符')


def row_for(bridge, intent_id):
    identifier(intent_id)
    with bridge.store.lock:
        return bridge.store.db.execute(INTENT_ROWS + ' WHERE i.intent_id=?', (intent_id,)).fetchone()


def authorize(bridge, evidence):
    if evidence is None:
        if not bridge.sns_policy['allow_direct']:
            raise ValueError('未授权直接发布朋友圈')
    elif (evidence['conversation_id'] not in bridge.sns_policy['event_conversations'] or
          not bridge.reply_allowed(evidence)):
        raise ValueError('朋友圈发布需要专门授权的入站会话，群聊须真实@')


def result(row):
    op = json.loads(row['result']) if row['result'] else {}
    return {'intent_id': row['intent_id'], 'request_id': row['request_id'], 'state': row['state'],
            'closed_no_retry': closed_no_retry(row),
            'local_accepted': op.get('local_accepted') is True,
            'server_confirmed': row['state'] == 'confirmed', 'operation': op or None}


def unknown(bridge, row, reason):
    return {'account': bridge.account, 'request_id': row['request_id'], 'action': 'publish',
            'state': 'unknown', 'error': reason}


def source_fault(bridge, reason):
    """复用上游观察暂停后续发布；不推断风控原因或改写原帖结果。"""
    bridge.worker_errors['sns_protection'] = {'state': 'fault', 'reason': reason}
    with bridge.store.tx() as db:
        db.execute('UPDATE sns_policy_state SET paused=1,reason=? WHERE id=1 AND paused=0 '
                   'AND EXISTS(SELECT 1 FROM sns_intents)', (reason,))
    # 写盘失败时保留进程内门禁；不能因下一轮采集恢复而掩盖保护故障。
    bridge.worker_errors.pop('sns_protection', None)


def require_available(bridge):
    """只读能力预检；明确不可用时不预占意图，更不提交 POST。"""
    status = bridge.hook.request('GET', '/client_hook/status')
    capabilities = status.get('capabilities', {}) if isinstance(status, dict) else {}
    native = capabilities.get('sns', {}) if isinstance(capabilities, dict) else {}
    if not isinstance(native, dict) or native.get('publish') is not True:
        raise ValueError('Hook 朋友圈原生发布适配不可用，不能提交')


def submission_blocked(bridge, db):
    return (bridge.stop.is_set() or bridge.worker_errors.get('source') or
            bridge.worker_errors.get('sns_protection') or
            db.execute('SELECT paused FROM sns_policy_state WHERE id=1').fetchone()[0])


def save(bridge, row, operation):
    state = operation.get('state') if isinstance(operation, dict) else None
    valid = (isinstance(operation, dict) and operation.get('account') == bridge.account and
             operation.get('request_id') == row['request_id'] and operation.get('action') == 'publish' and
             state in ('queued', 'running', 'accepted', 'confirmed', 'failed', 'unknown'))
    if valid and state == 'confirmed':
        post_id = (operation.get('result') or {}).get('post_id') if isinstance(operation.get('result'), dict) else None
        valid = (operation.get('server_confirmed') is True and isinstance(post_id, str) and
                 re.fullmatch(r'[1-9][0-9]{0,19}', post_id) is not None and int(post_id) < 2**64)
    if valid and state == 'accepted':
        valid = operation.get('local_accepted') is True
    if not valid:
        operation, state = {'error': 'invalid_hook_result'}, 'unknown'
    try:
        serialized = json.dumps(operation, ensure_ascii=False, allow_nan=False)
    except (ValueError, TypeError):
        serialized, state, valid = json.dumps({'error': 'invalid_hook_result'}), 'unknown', False
    with bridge.store.tx() as db:
        db.execute('UPDATE sns_intents SET state=?,result=? WHERE intent_id=?',
                   (state, serialized, row['intent_id']))
        closed = closed_no_retry(row)
        if (state in ('unknown', 'failed') and (not closed or not valid) or
                closed and state in ('queued', 'running', 'accepted')):
            db.execute('UPDATE sns_policy_state SET paused=1,reason=? WHERE id=1 AND paused=0',
                       ('closed_intent_active' if closed and state in ('queued', 'running', 'accepted')
                        else 'publish_' + state,))
    return result(row_for(bridge, row['intent_id']))


def refresh(bridge, row):
    if row['state'] in ('confirmed', 'failed'):
        return result(row)
    bridge.verify_account()
    path = '/client_hook/sns/requests/' + quote(row['request_id'], safe='') + '?' + urlencode({'account': bridge.account})
    try:
        operation = bridge.hook.request('GET', path)
    except Exception:
        # 包括404：不能由“未找到”推断此前没有执行，更不能补发。
        operation = unknown(bridge, row, 'status_unavailable')
    return save(bridge, row, operation)


def publish(bridge, intent_id, text, event_id=None):
    identifier(intent_id)
    validate_text(text)
    params = json.dumps({'account': bridge.account, 'action': 'publish', 'text': text,
                         'visibility': 'public', 'event_id': event_id}, ensure_ascii=False, sort_keys=True)
    with bridge.sns_lock:
        previous = row_for(bridge, intent_id)
        if previous:
            if previous['params'] != params:
                raise ValueError('intent_id 已绑定不同帖文或触发事件')
            authorize(bridge, json.loads(previous['evidence']) if previous['evidence'] else None)
            return refresh(bridge, previous)
        evidence = bridge.store.event(event_id) if event_id is not None else None
        authorize(bridge, evidence)
        bridge.verify_account()
        require_available(bridge)
        now = time.time()
        if not math.isfinite(now):
            raise ValueError('本机时钟无效')
        request_id = 'mcp-sns-' + hashlib.sha256((bridge.account + '\0' + intent_id).encode()).hexdigest()[:48]
        with bridge.store.tx() as db:
            if submission_blocked(bridge, db):
                raise ValueError('朋友圈已暂停或上游不可确认，需要本机操作者核查后显式恢复')
            if unresolved(db):
                raise ValueError('同账号已有未完成或未知的朋友圈写入，请查询原意图')
            last = db.execute('SELECT MAX(created) FROM sns_intents').fetchone()[0]
            count = db.execute('SELECT COUNT(*) FROM sns_intents WHERE created>?',
                               (now - bridge.sns_policy['window_seconds'],)).fetchone()[0]
            if last is not None and now - last < bridge.sns_policy['min_interval_seconds']:
                raise ValueError('朋友圈调用间隔不足；不会排队补发')
            if count >= bridge.sns_policy['max_writes']:
                raise ValueError('朋友圈滚动次数额度已用完；不会排队补发')
            for prior in db.execute(INTENT_ROWS + ' WHERE i.created>? OR c.intent_id IS NOT NULL',
                                    (now - bridge.sns_policy['window_seconds'],)):
                bound = json.loads(prior['params'])
                if bound.get('text') == text and bound.get('visibility') == 'public':
                    raise ValueError('已有相同帖文处于额度窗口或禁止重发记录，请查询原意图：' + prior['intent_id'])
            db.execute('INSERT INTO sns_intents VALUES(?,?,?,?,?,?,?)',
                       (intent_id, request_id, params, json.dumps(evidence) if evidence else None,
                        'submitting', None, now))
        row = row_for(bridge, intent_id)
        with bridge.store.tx() as db:
            blocked = submission_blocked(bridge, db)
        if blocked:
            # 预占后发生暂停/停止时，此分支尚未进入 HTTP 提交，明确保留未发送事实。
            return save(bridge, row, {'account': bridge.account, 'request_id': request_id,
                'action': 'publish', 'state': 'failed', 'local_accepted': False,
                'server_confirmed': False, 'result': {'submitted': False,
                'reason': 'protection_before_submission'}})
        try:
            operation = bridge.hook.request('POST', '/client_hook/sns/publish',
                {'account': bridge.account, 'request_id': request_id, 'text': text, 'visibility': 'public'})
        except Exception:
            # 先持久化未知并暂停，再允许显式查询；绝不重发POST。
            operation = unknown(bridge, row, 'submission_unavailable')
        return save(bridge, row, operation)


def status(bridge, intent_id):
    with bridge.sns_lock:
        row = row_for(bridge, intent_id)
        if not row:
            raise ValueError('朋友圈业务意图不存在')
        # 撤销发布授权仍允许可信MCP调用方核查原状态。
        return refresh(bridge, row)


def policy_status(store, configuration=None):
    with store.lock:
        row = store.db.execute('SELECT paused,reason FROM sns_policy_state WHERE id=1').fetchone()
        closed = sum(closed_no_retry(item) for item in store.db.execute(INTENT_ROWS + " WHERE i.state='unknown'"))
    return {**(configuration or {}), 'paused': bool(row['paused']), 'pause_reason': row['reason'],
            'closed_unknown_count': closed}


def set_pause(store, paused, *, close_unknown=()):
    """本机显式结案仅豁免指定旧unknown的全局阻塞，不改变发送事实或重放它。"""
    if paused and close_unknown:
        raise ValueError('仅 resume 可显式结案未知意图')
    if len(set(close_unknown)) != len(close_unknown):
        raise ValueError('结案意图不能重复')
    for intent_id in close_unknown:
        identifier(intent_id)
    with store.tx() as db:
        for intent_id in close_unknown:
            row = db.execute(INTENT_ROWS + ' WHERE i.intent_id=?', (intent_id,)).fetchone()
            if row is None or row['state'] != 'unknown':
                raise ValueError('只能人工结案指定的 unknown 意图；进行中或已确定的操作不能结案')
            if row['closed_request_id'] is not None and not closed_no_retry(row):
                raise ValueError('结案记录与原意图不匹配，需核查账本')
            db.execute('INSERT OR IGNORE INTO sns_closed_intents VALUES(?,?,?,?)',
                       (intent_id, row['request_id'], hashlib.sha256(row['params'].encode('utf-8')).hexdigest(), time.time()))
        if not paused and unresolved(db):
            raise ValueError('仍有未完成或未知的朋友圈意图；先查询原意图，不能恢复新发布')
        db.execute('UPDATE sns_policy_state SET paused=?,reason=? WHERE id=1',
                   (int(paused), 'manual_pause' if paused else 'manual_resume'))
    return policy_status(store)
