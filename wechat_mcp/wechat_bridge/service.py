"""桥接后台任务与受会话范围约束的回复操作。"""
from datetime import datetime, timezone
import hmac
import json
import secrets
import threading
import time
from urllib.parse import quote, urlencode, urlsplit

from .httpio import CallbackHTTP, RemoteError, secret_bytes
from .httpio import encode
from .store import Store

EVENT_NAME = 'wechat.message.received'
MENTION_SOURCE = 'weixin41930_record_source_180'
MESSAGE_TYPES = {'text', 'image', 'voice', 'video', 'file', 'sticker', 'card', 'unknown'}
REFERENCE_SOURCE = 'weixin41930_native_reference_v1'


def group_reply_allowed(event, account):
    """Fail closed; neither an agent argument nor display text can grant access."""
    if not event.get('conversation_id', '').endswith('@chatroom'):
        return True
    mentions = event.get('mentions')
    if not isinstance(mentions, dict) or mentions.get('state') != 'known' or mentions.get('source') != MENTION_SOURCE:
        return False
    ids = mentions.get('wxids')
    return (isinstance(ids, list) and len(ids) <= 256 and
            all(isinstance(value, str) and 1 <= len(value) <= 127 for value in ids) and account in ids)


def iso(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat().replace('+00:00', 'Z')


class Bridge:
    def __init__(self, directory, hook, account, conversations, simulation=False, consumer='mcp-bridge', start='latest'):
        if not account or not conversations or len(conversations) > 100 or any(not isinstance(x, str) or not 1 <= len(x) <= 511 for x in conversations):
            raise ValueError('必须指定账号和至少一个允许会话')
        self.account, self.conversations = account, frozenset(conversations)
        self.simulation, self.hook, self.consumer, self.start = simulation, hook, consumer, start
        self.store = Store(directory, account, simulation)
        self.callbacks = CallbackHTTP(simulation)
        self.stop = threading.Event()
        self.threads = []
        self.worker_errors = {}
        self.reply_lock = threading.Lock()
        self.poll_slots = threading.BoundedSemaphore(16)

    def start_workers(self):
        for target in (self.collect_loop, self.delivery_loop):
            thread = threading.Thread(target=target, daemon=True)
            thread.start()
            self.threads.append(thread)

    def checked_event(self, event):
        if not isinstance(event, dict) or event.get('account') != self.account:
            raise ValueError('上游账号不匹配，已暂停推进消费者')
        if not isinstance(event.get('event_id'), str) or not 1 <= len(event['event_id']) <= 512:
            raise ValueError('无效 event_id')
        if event.get('direction') not in ('incoming', 'outgoing') or event.get('message_type') not in MESSAGE_TYPES:
            return False
        if event.get('conversation_id') not in self.conversations:
            return False
        if not isinstance(event.get('payload'), dict):
            raise ValueError('消息载荷不合法')
        if event['message_type'] == 'text':
            text = event['payload'].get('text')
            if not isinstance(text, str) or len(text.encode('utf-8')) > 65536:
                raise ValueError('入站正文不合法')
        if not isinstance(event.get('captured_at'), (float, int)):
            raise ValueError('缺少本地采集时间')
        iso(event['captured_at'])
        if len(encode(event)) > 262144:
            raise ValueError('单条事件超过桥接存储大小限制')
        return True

    def reply_allowed(self, event):
        return (self.checked_event(event) and event['direction'] == 'incoming' and
                event['message_type'] == 'text' and group_reply_allowed(event, self.account))

    def resolve_reference(self, event_id):
        trigger = self.store.event(event_id)
        if not self.reply_allowed(trigger):
            raise ValueError('引用指令不允许触发处理')
        reference = trigger.get('reference')
        if not isinstance(reference, dict) or reference.get('source') != REFERENCE_SOURCE:
            raise ValueError('缺少可信原生引用')
        mid = reference.get('message_id')
        if not isinstance(mid, str) or not mid or len(mid) > 128:
            raise ValueError('引用目标标识无效')
        target = self.store.resolve_reference(trigger, mid)
        if not self.checked_event(target):
            raise ValueError('引用目标不在授权范围')
        return target

    def mark_conversation_read(self, event_id, status_only=False):
        event = self.store.event(event_id)
        if not self.reply_allowed(event):
            raise ValueError('已读操作需要白名单内合法入站指令，群聊须真实@')
        self.verify_account()
        route = 'status' if status_only else 'mark'
        return self.hook.request('POST', '/client_hook/read/' + route, {'event_id': event_id})

    def acquire_reference(self, event_id):
        target = self.resolve_reference(event_id)
        rid = target['payload'].get('resource_id')
        if target['message_type'] not in MESSAGE_TYPES - {'text', 'unknown'} or not isinstance(rid, str):
            raise ValueError('引用目标没有可获取的附件')
        self.verify_account()
        result = self.hook.request('POST', '/client_hook/resources/acquire',
                                   {'event_id': target['event_id'], 'resource_id': rid})
        self.store.bind_resource(event_id, target['event_id'], result['id'])
        return result

    def reference_resource(self, event_id, resource_id, offset=None):
        target = self.resolve_reference(event_id)
        self.store.check_resource(event_id, target['event_id'], resource_id)
        self.verify_account()
        path = '/client_hook/resources/' + quote(resource_id, safe='')
        if offset is not None:
            path += '?' + urlencode({'offset': offset})
        return self.hook.request('GET', path)

    def verify_account(self):
        if self.hook.request('POST', '/GetSelfProfile', {}).get('wxid') != self.account:
            raise ValueError('资源获取时上游账号不匹配')

    def collect_once(self, timeout=30):
        batch = self.hook.request('GET', '/messages?' + urlencode({'consumer_id': self.consumer, 'timeout': timeout, 'limit': 100}))
        events = [e for e in batch['events'] if self.checked_event(e)]
        self.store.prune()
        self.store.ingest(events, publish={e['event_id'] for e in events if self.reply_allowed(e)})
        if batch['events']:
            if not batch.get('ack_token'):
                raise ValueError('上游批次缺少确认 token')
            self.hook.request('POST', '/consumers/' + quote(self.consumer, safe='') + '/ack', {'ack_token': batch['ack_token']})
        capture = batch.get('capture', {})
        self.store.meta('source', {'state': capture.get('state', 'unknown'), 'last_poll': time.time()})
        if not batch['events'] and capture.get('state') != 'running':
            raise ValueError('上游采集未运行')

    def collect_loop(self):
        registered = False
        while not self.stop.is_set():
            try:
                if not registered:
                    registered_consumer = self.hook.request('POST', '/consumers', {'consumer_id': self.consumer, 'start': self.start})
                    if registered_consumer.get('account') != self.account:
                        raise ValueError('上游消费者账号与配置不匹配')
                    registered = True
                self.collect_once()
                self.worker_errors.pop('source', None)
            except Exception as error:
                # 不自动 reset 过期游标，不根据错误切换账号，也不回退模拟。
                failure = {'state': 'fault', 'error': type(error).__name__,
                           'http_status': getattr(error, 'status', None), 'at': time.time()}
                self.worker_errors['source'] = failure
                try:
                    self.store.meta('source', failure)
                except Exception:
                    pass
                self.stop.wait(2)

    def filters(self, arguments):
        if not isinstance(arguments, dict) or set(arguments) != {'conversation_ids'}:
            raise ValueError('订阅需提供 conversation_ids')
        items = arguments['conversation_ids']
        if not isinstance(items, list) or not items or len(items) > 100 or any(not isinstance(x, str) for x in items):
            raise ValueError('conversation_ids 应为非空字符串数组')
        if not set(items) <= self.conversations:
            raise ValueError('订阅会话超出本机允许范围')
        return {'conversation_ids': sorted(set(items))}

    def subscribe(self, params, owner):
        delivery_hint = params.get('delivery')
        self.store.meta('subscription_attempt', {
            'name_matches': params.get('name') == EVENT_NAME,
            'argument_keys': sorted(params.get('arguments', {})) if isinstance(params.get('arguments'), dict) else None,
            'delivery_mode': delivery_hint.get('mode') if isinstance(delivery_hint, dict) else None,
            'secret_present': bool(isinstance(delivery_hint, dict) and delivery_hint.get('secret')),
            'cursor_is_null': params.get('cursor') is None})
        if params.get('name') != EVENT_NAME or params.get('cursor') is not None:
            raise ValueError('仅支持文本事件；本期不提供事件协议历史重放')
        filters = self.filters(params.get('arguments'))
        delivery = params.get('delivery', {})
        if not isinstance(delivery, dict) or delivery.get('mode') != 'webhook':
            raise ValueError('仅支持 webhook 投递')
        url, secret = delivery.get('url'), delivery.get('secret')
        self.store.meta('subscription_stage', {'stage': 'secret_validation'})
        secret_bytes(secret)
        self.store.meta('subscription_stage', {'stage': 'destination_validation'})
        try:
            self.callbacks.destination(url)
        except Exception as error:
            # 只记录边界内定义的固定诊断，不保存回调 URL 或系统异常正文。
            known = {'回调 URL 无效', '回调 URL 不允许凭据或 fragment', '真实回调必须使用 HTTPS',
                     '回调地址无法解析', '模拟模式仅允许回环回调', '回调禁止私网、回环和非公网地址'}
            self.store.meta('subscription_stage', {'stage': 'destination_validation',
                'kind': type(error).__name__, 'reason': str(error) if str(error) in known else 'destination_failed',
                'hostname': urlsplit(url).hostname if isinstance(url, str) else None})
            raise
        ttl = params.get('ttlMs', 86400000)
        self.store.meta('subscription_stage', {'stage': 'ttl_validation',
            'ttlMs': ttl if isinstance(ttl, (int, float)) else None})
        if ttl is None:
            ttl = 86400000  # 不授予永不过期订阅。
        if isinstance(ttl, bool) or not isinstance(ttl, (int, float)) or not 1000 <= ttl <= 604800000:
            raise ValueError('ttlMs 范围为 1 秒至 7 天')
        import hashlib
        from .httpio import encode
        sid = 'sub_' + hashlib.sha256(encode([owner, url, EVENT_NAME, filters])).hexdigest()[:40]
        challenge = secrets.token_urlsafe(32)
        self.store.meta('subscription_stage', {'stage': 'callback_verification'})
        try:
            response = self.callbacks.signed(url, secret, sid, 'verify_' + secrets.token_hex(16),
                                             {'type': 'verification', 'challenge': challenge})
            if not isinstance(response, dict) or not isinstance(response.get('challenge'), str) or not hmac.compare_digest(response['challenge'], challenge):
                raise ValueError('challenge_failed')
        except Exception as error:
            self.store.meta('subscription_verification_error', {'kind': type(error).__name__,
                'http_status': getattr(error, 'status', None), 'at': time.time()})
            raise CallbackError() from None
        sid, expires = self.store.subscribe(owner, filters, url, secret, ttl / 1000)
        self.store.meta('subscription_stage', {'stage': 'active'})
        return {'id': sid, 'refreshBefore': iso(expires), 'cursor': None, 'truncated': False}

    def deliver_once(self):
        job = self.store.next_delivery()
        if not job:
            return False
        try:
            event = self.store.event(job['event_id'])
            if not self.reply_allowed(event):
                raise ValueError('会话已撤销')
            payload = {'eventId': event['event_id'], 'name': EVENT_NAME, 'timestamp': iso(event['captured_at']),
                       'data': {'event_id': event['event_id'], 'account': self.account,
                                'conversation_id': event['conversation_id'], 'text': event['payload']['text'],
                                'message_type': 'text', 'source': event.get('source', 'live'),
                                'mentions': event.get('mentions')}, 'cursor': None}
            self.callbacks.signed(job['url'], job['secret'], job['subscription_id'], event['event_id'], payload, job['old_secret'])
            self.store.finish_delivery(job)
        except Exception as error:
            code = getattr(error, 'status', None)
            self.store.finish_delivery(job, error=f'{type(error).__name__}:{code or "delivery_failed"}',
                                       permanent=code in (400, 401, 403, 404, 410, 413) or isinstance(error, ValueError))
        return True

    def delivery_loop(self):
        while not self.stop.is_set():
            try:
                worked = self.deliver_once()
            except Exception:
                self.worker_errors['delivery'] = {'state': 'fault', 'at': time.time()}
                worked = False
            if not worked:
                self.stop.wait(.2)

    def reply_text(self, event_id, text):
        if not isinstance(text, str) or not text or '\0' in text or len(text.encode('utf-8')) > 65536:
            raise ValueError('回复必须为 1–65536 字节 UTF-8 文本且不包含 NUL')
        event = self.store.event(event_id)
        if not self.reply_allowed(event):
            raise ValueError('不允许回复该事件：会话未授权或群消息未明确@当前账号')
        with self.reply_lock:
            operation, created = self.store.prepare_reply(event_id, self.account, text)
            if not created and operation['state'] in ('accepted', 'failed', 'unknown'):
                return self.reply_result(operation)
            if created or operation['state'] == 'prepared':
                # 发送前再次确认身份，杜绝切换微信账号后向旧事件的会话发送。
                profile = self.hook.request('POST', '/GetSelfProfile', {})
                if profile.get('wxid') != self.account:
                    raise ValueError('发送时上游账号不匹配')
                self.store.reply_state(event_id, 'submitting')
                try:
                    result = self.hook.request('POST', '/SendTextMsg', {'toid': event['conversation_id'], 'msg': text,
                                                                                    'request_id': operation['request_id']})
                except Exception:
                    # 结果未知时只查询原操作；绝不新建 request_id。
                    return self.refresh_reply(event_id)
                self.store.reply_state(event_id, result['state'], result)
            else:
                return self.refresh_reply(event_id)
            return self.reply_result(self.store.reply(event_id))

    def refresh_reply(self, event_id):
        operation = self.store.reply(event_id)
        if not operation:
            with self.store.lock:
                last=self.store.db.execute('SELECT MAX(part_index) FROM reply_parts WHERE event_id=?',(event_id,)).fetchone()[0]
            if last is not None:return self.refresh_part(event_id,last)
            raise ValueError('该事件还没有回复意图')
        if operation['state'] in ('prepared', 'accepted', 'failed', 'unknown'):
            return self.reply_result(operation)
        try:
            result = self.hook.request('GET', '/client_hook/requests/' + quote(operation['request_id'], safe=''))
            if result.get('account') != self.account:
                raise ValueError('操作账号不匹配')
            self.store.reply_state(event_id, result['state'], result)
        except RemoteError as error:
            if error.status == 404:
                self.store.reply_state(event_id, 'unknown', {'reason': 'upstream_operation_missing'})
        except (OSError, TimeoutError):
            pass
        return self.reply_result(self.store.reply(event_id))

    def reply_part(self,event_id,index,text,final):
        if not isinstance(text,str) or not 1<=len(text)<=1200 or '\0' in text:
            raise ValueError('分段文字必须为1至1200字')
        event=self.store.event(event_id)
        if not self.reply_allowed(event):raise ValueError('该事件不允许分段回复')
        with self.reply_lock:
            op,created=self.store.prepare_part(event_id,self.account,index,text,final)
            if created or op['state']=='prepared':
                self.verify_account()
                self.store.part_state(event_id,index,'submitting')
                try:
                    result=self.hook.request('POST','/SendTextMsg',{'toid':event['conversation_id'],
                        'msg':text,'request_id':op['request_id']})
                    self.store.part_state(event_id,index,result['state'],result)
                except Exception:
                    return self.refresh_part(event_id,index)
            return self.refresh_part(event_id,index)

    def refresh_part(self,event_id,index):
        event=self.store.event(event_id)
        if not self.reply_allowed(event):raise ValueError('该事件不允许查询分段回复')
        if isinstance(index,bool) or not isinstance(index,int) or not 0<=index<64:raise ValueError('分段序号无效')
        op=self.store.part(event_id,index)
        if not op:raise ValueError('没有该段发送意图')
        if op['state'] not in ('prepared','accepted','failed','unknown'):
            try:
                result=self.hook.request('GET','/client_hook/requests/'+quote(op['request_id'],safe=''))
                if result.get('account')!=self.account:raise ValueError('操作账号不符')
                self.store.part_state(event_id,index,result['state'],result)
            except RemoteError as error:
                if error.status==404:self.store.part_state(event_id,index,'unknown',{'reason':'upstream_operation_missing'})
            except (OSError,TimeoutError):pass
            op=self.store.part(event_id,index)
        return {'event_id':event_id,'part_index':index,'final':bool(op['is_final']),
            'request_id':op['request_id'],'state':op['state'],'delivery_confirmed':False,
            'result':json.loads(op['result']) if op['result'] else None,
            'send_echo':self.store.send_echo(event_id,op)}

    def reply_result(self, operation):
        return {'event_id': operation['event_id'], 'request_id': operation['request_id'],
                'state': operation['state'], 'delivery_confirmed': False,
                'result': json.loads(operation['result']) if operation['result'] else None,
                'send_echo': self.store.send_echo(operation['event_id'])}

    def close(self):
        self.stop.set()
        for thread in self.threads:
            thread.join(42)
        if any(t.is_alive() for t in self.threads):
            raise RuntimeError('桥接任务尚未退出，状态锁保持占用')
        self.store.close()


class CallbackError(Exception):
    pass
