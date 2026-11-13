"""朋友圈调用契约与持久意图；未知结果只查询，不重新执行。"""
import json
import re

from .errors import ApiError
from .store import encode


ACTIONS = ('timeline', 'like', 'unlike', 'comment', 'publish')


def identifier(value, label):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,63}', value):
        raise ApiError(400, 'invalid_parameter', f'{label} 必须是 1–63 位字母、数字、下划线或连字符')
    return value


def wxid(value, label):
    if not isinstance(value, str) or not re.fullmatch(r'wxid_[A-Za-z0-9_-]{1,120}', value):
        raise ApiError(400, 'invalid_parameter', f'{label} 必须是 wxid')
    return value


def decimal_id(value, label, allow_zero=False):
    if (not isinstance(value, str) or not re.fullmatch(r'0|[1-9][0-9]{0,19}', value) or
            int(value) > 2**64 - 1 or (not allow_zero and value == '0')):
        raise ApiError(400, 'invalid_parameter', f'{label} 必须是无前导零的 uint64 十进制字符串')
    return value


def text(value):
    if not isinstance(value, str) or not value.strip() or '\0' in value:
        raise ApiError(400, 'invalid_parameter', 'text 必须是无 NUL 的非空正文')
    try:
        size = len(value.encode('utf-8'))
    except UnicodeEncodeError:
        raise ApiError(400, 'invalid_parameter', 'text 包含无效 Unicode') from None
    if size > 65536:
        raise ApiError(413, 'text_too_large', 'text 超过 64 KiB UTF-8')
    return value


def normalize(action, body):
    if action not in ACTIONS:
        raise ApiError(404, 'sns_route_not_found', '朋友圈操作不存在')
    if not isinstance(body, dict):
        raise ApiError(400, 'invalid_parameter', '需要 JSON 对象')
    account = wxid(body.get('account'), 'account')
    if action == 'timeline':
        allowed = {'account', 'author_id', 'max_id', 'limit'}
        limit = body.get('limit', 20)
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ApiError(400, 'invalid_parameter', 'limit 必须为 1–100 的整数')
        params = {'max_id': decimal_id(body.get('max_id', '0'), 'max_id', True), 'limit': limit}
        if 'author_id' in body:
            params['author_id'] = wxid(body['author_id'], 'author_id')
        request_id = None
    else:
        request_id = identifier(body.get('request_id'), 'request_id')
        allowed = {'account', 'request_id'}
        params = {}
        if action in ('like', 'unlike', 'comment'):
            allowed |= {'post_id', 'author_id'}
            params = {'post_id': decimal_id(body.get('post_id'), 'post_id'),
                      'author_id': wxid(body.get('author_id'), 'author_id')}
        if action in ('comment', 'publish'):
            allowed.add('text')
            params['text'] = text(body.get('text'))
        if action == 'comment':
            allowed.add('reply_comment_id')
            if 'reply_comment_id' in body:
                params['reply_comment_id'] = decimal_id(body['reply_comment_id'], 'reply_comment_id')
        if action == 'publish':
            allowed.add('visibility')
            if body.get('visibility') != 'public':
                raise ApiError(400, 'invalid_parameter', '当前仅支持显式 visibility=public 的文字发布')
            params['visibility'] = 'public'
            if any(ord(char) < 32 and char not in '\t\r\n' or ord(char) in (0xfffe, 0xffff)
                   for char in params['text']):
                raise ApiError(400, 'invalid_parameter', '发布正文包含 XML 不支持的字符')
    if set(body) - allowed:
        raise ApiError(400, 'invalid_parameter', '存在不支持的朋友圈参数')
    return account, request_id, params


class Sns:
    def __init__(self, store, backend, stop_event, capacity=64):
        self.store, self.backend, self.stop_event = store, backend, stop_event
        self.capacity = capacity
        with store.transaction():
            store.db.execute('''CREATE TABLE IF NOT EXISTS sns_operations(
                request_id TEXT PRIMARY KEY,account TEXT NOT NULL,action TEXT NOT NULL,
                params TEXT NOT NULL,state TEXT NOT NULL,result TEXT,created REAL NOT NULL)''')
            # 未完成意图保留为 unknown，不因重启自动执行。
            store.db.execute("UPDATE sns_operations SET state='unknown' WHERE state IN ('queued','running')")

    def capabilities(self):
        function = getattr(self.backend, 'sns_capabilities', None)
        values = function() if function else {}
        return {action: values.get(action) is True for action in ACTIONS}

    def require(self, action, account=None):
        if not self.capabilities()[action]:
            raise ApiError(503, 'sns_native_unavailable', '当前后端尚无已验证的朋友圈原生适配',
                           action=action, retryable=False)

    def check_account(self, account):
        if self.backend.account != account or self.backend.profile().get('wxid') != account:
            raise ApiError(409, 'account_changed', '朋友圈操作账号与当前登录账号不符')

    def status(self, request_id, account):
        identifier(request_id, 'request_id')
        with self.store.condition:
            row = self.store.db.execute('SELECT * FROM sns_operations WHERE request_id=?',
                                        (request_id,)).fetchone()
            if not row or row['account'] != account:
                raise ApiError(404, 'sns_request_not_found', '该账号无此朋友圈请求')
            result = json.loads(row['result']) if row['result'] else None
            return {'request_id': request_id, 'account': account, 'action': row['action'],
                    'state': row['state'], 'local_accepted': bool(result and result.get('local_accepted') is True),
                    'server_confirmed': row['state'] == 'confirmed', 'result': result}

    def request(self, action, body):
        account, request_id, params = normalize(action, body)
        if action == 'timeline':
            self.require(action, account)
            self.check_account(account)
            result = self.backend.sns_read(account, params)
            self.check_account(account)
            return 200, result
        canonical = json.dumps(params, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
        with self.store.condition:
            previous = self.store.db.execute('SELECT 1 FROM sns_operations WHERE request_id=?',
                                             (request_id,)).fetchone()
        if not previous:
            self.require(action, account)
            self.check_account(account)
            validate = getattr(self.backend, 'sns_validate', None)
            if validate:
                validate(account, action, params)
        with self.store.transaction():
            row = self.store.db.execute('SELECT * FROM sns_operations WHERE request_id=?',
                                        (request_id,)).fetchone()
            if row:
                if (row['account'], row['action'], row['params']) != (account, action, canonical):
                    raise ApiError(409, 'request_id_conflict', '请求 ID 已用于不同账号、操作或参数')
            else:
                count = self.store.db.execute(
                    "SELECT count(*) FROM sns_operations WHERE state IN ('queued','running')").fetchone()[0]
                if count >= self.capacity:
                    raise ApiError(429, 'sns_queue_full', '朋友圈操作队列已满')
                self.store.db.execute('INSERT INTO sns_operations VALUES(?,?,?,?,?,?,?)',
                    (request_id, account, action, canonical, 'queued', None, self.store.clock()))
                self.store.condition.notify_all()
        result = self.status(request_id, account)
        return (200 if result['state'] in ('confirmed', 'failed') else 202), result

    def run(self):
        while not self.stop_event.is_set():
            with self.store.transaction():
                row = self.store.db.execute(
                    "SELECT * FROM sns_operations WHERE state='queued' ORDER BY created,request_id LIMIT 1").fetchone()
                if row:
                    self.store.db.execute("UPDATE sns_operations SET state='running' WHERE request_id=?",
                                          (row['request_id'],))
            if row is None:
                self.stop_event.wait(.1)
                continue
            state, result = 'unknown', {'error': {'code': 'sns_execution_unknown', 'retryable': False}}
            invoked = False
            try:
                self.require(row['action'], row['account'])
                self.check_account(row['account'])
                if self.stop_event.is_set():
                    raise ApiError(503, 'sns_execution_stopped', '朋友圈执行已停止，不会自动恢复', retryable=False)
                # running 已落盘；从此之后，异常不能推断为无副作用失败。
                invoked = True
                returned = self.backend.sns_execute(row['account'], row['action'],
                                                    json.loads(row['params']), row['request_id'])
                if isinstance(returned, dict):
                    # 先校验结果可持久化，避免错误载荷让 worker 在 running 状态退出。
                    result = json.loads(encode(returned))
                    state = returned.get('state', 'unknown')
                    if state not in ('accepted', 'confirmed', 'failed', 'unknown'):
                        state = 'unknown'
                    if state == 'confirmed' and returned.get('server_confirmed') is not True:
                        state = 'unknown'
                    if state == 'accepted' and returned.get('local_accepted') is not True:
                        state = 'unknown'
            except ApiError as error:
                # 只有尚未调用提供者时的确定拒绝可标成 failed。
                if not invoked:
                    state = 'failed'
                result = error.body()
                result['error']['retryable'] = False
            except Exception:
                pass  # 不在错误响应中泄漏原生异常、正文或账号内部数据。
            with self.store.transaction():
                self.store.db.execute('UPDATE sns_operations SET state=?,result=? WHERE request_id=?',
                                      (state, encode(result), row['request_id']))
                self.store.condition.notify_all()
