"""有界本地 HTTP 服务；发送、消费和回调共享持久存储。"""
from __future__ import annotations

import hmac
import ipaddress
import json
import os
from pathlib import Path
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit
from urllib.request import Request, urlopen
import uuid

from .errors import ApiError
from tool.settings import hook_port
from .store import Store, encode
from .resources import Resources


def identifier(value, label='id'):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,63}', value):
        raise ApiError(400, 'invalid_parameter', f'{label} 必须是 1–63 位字母、数字、下划线或连字符')
    return value


def start_mode(value):
    if value not in ('earliest', 'latest'):
        raise ApiError(400, 'invalid_parameter', 'start 必须为 earliest 或 latest')
    return value


def alias(body, names):
    values = [body[n] for n in names if n in body]
    if not values or not isinstance(values[0], str) or not values[0] or any(v != values[0] for v in values):
        raise ApiError(400, 'invalid_parameter', f'{names[0]} 缺失、为空或别名冲突')
    return values[0]


class StateLock:
    def __init__(self, directory):
        directory.mkdir(parents=True, exist_ok=True)
        self.file = (directory / 'service.lock').open('a+b')
        try:
            if os.fstat(self.file.fileno()).st_size == 0:
                self.file.write(b'0')
                self.file.flush()
            self.file.seek(0)
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.file.close()
            raise RuntimeError('该状态目录已有服务运行') from None

    def close(self):
        self.file.close()


class Application:
    def __init__(self, directory, backend, retention=7*86400, max_bytes=1024**3, callback=None, port=None):
        self.lock = StateLock(Path(directory))
        self.backend = backend
        self.stop_event = threading.Event()
        self.callback_url, self.port = callback, hook_port() if port is None else port
        self.threads = []
        self.background_error = None
        try:
            self.store = Store(Path(directory) / 'messages.sqlite3', retention, max_bytes)
            self.resources = Resources(self.store, backend)
            from .read_state import ReadState
            self.read_state = ReadState(self.store, self.resources, backend)
            backend.start(self.store)
            self._thread(self._send_loop)
            self._thread(self._maintenance)
            if callback:
                try:
                    self.store.consumer('callback', self.account(), 'earliest')
                except ApiError as error:
                    self.store.set_meta('callback', {'paused': True, 'error': str(error)})
                self._thread(self._callback_loop)
        except BaseException:
            try:
                backend.stop()
            finally:
                if hasattr(self, 'store'):
                    self.store.close()
                self.lock.close()
            raise

    def _thread(self, function):
        def run():
            try:
                function()
            except Exception as error:
                self.background_error = str(error)
                self.stop_event.set()
                try:
                    self.backend.stop()
                    self.store.capture('fault', account=self.backend.account,
                                       error=self.background_error, coverage_gap=True)
                except Exception:
                    pass
        thread = threading.Thread(target=run, daemon=True)
        self.threads.append(thread)
        thread.start()

    def account(self):
        account = self.backend.account
        if not account:
            raise ApiError(503, 'account_unknown', '尚未确认当前账号')
        return account

    def _send_loop(self):
        while not self.stop_event.is_set():
            job = self.store.next_operation()
            if not job:
                with self.store.condition:
                    self.store.condition.wait(.25)
                continue
            request_id, account, params = job
            try:
                result = self.backend.send(request_id, account, params)
                state = result['state']
                if state not in ('accepted', 'failed', 'unknown'):
                    state = 'unknown'
            except ApiError as error:
                state, result = 'failed', error.body()
            except Exception as error:
                state, result = 'unknown', {'error': str(error)}
            self.store.finish(request_id, state, result)

    def send(self, body):
        toid = alias(body, ('toid', 'wxidorgid'))
        text = alias(body, ('msg', 'content'))
        if not re.fullmatch(r'(?:wxid_[A-Za-z0-9_-]+|[0-9]+@chatroom|filehelper)', toid) or len(toid) > 127:
            raise ApiError(400, 'invalid_recipient', '目标必须是 wxid、群 ID 或 filehelper')
        if '\0' in text:
            raise ApiError(400, 'invalid_text', '正文不能包含 NUL')
        try:
            size = len(text.encode('utf-8'))
        except UnicodeEncodeError:
            raise ApiError(400, 'invalid_text', '正文包含无效 Unicode') from None
        if size > 65536:
            raise ApiError(413, 'text_too_large', '正文超过 64 KiB UTF-8')
        request_id = identifier(body.get('request_id', uuid.uuid4().hex), 'request_id')
        account = self.account()
        # A repeat must remain queryable even after the client becomes unavailable.
        try:
            previous = self.store.operation(request_id)
        except ApiError as error:
            if error.status != 404:
                raise
            previous = None
        if previous is None:
            self.backend.profile()
        self.store.enqueue(request_id, account, {'toid': toid, 'msg': text})
        until = time.monotonic() + 2
        with self.store.condition:
            while True:
                operation = self.store.operation(request_id)
                if operation['state'] not in ('queued', 'running') or time.monotonic() >= until:
                    break
                self.store.condition.wait(max(0, until - time.monotonic()))
        state = operation['state']
        response = {
            'ret': 0 if state == 'accepted' else 1, 'retmsg': 'success' if state == 'accepted' else state,
            **operation}
        if state == 'failed':
            response['error'] = {'code': 'native_business_failure', 'message': '原生发送未成功，详情见 result', 'retryable': False}
        return (200 if state == 'accepted' else 502 if state == 'failed' else 202), response

    def _maintenance(self):
        while not self.stop_event.wait(30):
            try:
                self.store.prune()
                self.resources.prune()
                self.read_state.prune()
            except Exception as error:
                self.backend.stop()
                try:
                    self.store.capture('fault', account=self.account(), error=str(error), coverage_gap=True)
                except Exception:
                    pass
                return

    def _callback_loop(self):
        while not self.stop_event.is_set():
            if self.store.get_meta('callback', {}).get('paused'):
                self.stop_event.wait(.25)
                continue
            try:
                account = self.account()
                batch = self.store.read('callback', account, timeout=1, limit=1)
                if not batch['events']:
                    self.stop_event.wait(.1)
                    continue
                event = batch['events'][0]
                payload = {'ServerPort': self.port, 'selfwxid': account, 'sendorrecv': 2, 'msgnumber': 1,
                           'msglist': [{'event_id': event['event_id'], 'cmdId': 5, 'fromid': event['fromid'],
                                        'toid': event['toid'], 'conversation_id': event['conversation_id'],
                                        'msgtype': 1, 'msg': event['payload']['text'],
                                        'mentions': event.get('mentions')}]}
                request = Request(self.callback_url, encode(payload).encode('utf-8'),
                                  {'Content-Type': 'application/json; charset=utf-8'}, method='POST')
                with urlopen(request, timeout=5) as response:
                    if not 200 <= response.status < 300:
                        raise RuntimeError(f'callback HTTP {response.status}')
                self.store.ack('callback', account, batch['ack_token'])
                self.store.set_meta('callback', {'paused': False, 'last_event_id': event['event_id']})
            except Exception as error:
                if not self.stop_event.is_set():
                    self.store.set_meta('callback', {'paused': True, 'error': str(error)})

    def status(self):
        simulation = self.backend.__class__.__name__ == 'FixtureBackend'
        return {**self.store.status(), 'backend': 'simulation' if simulation else 'native',
                'background_error': self.background_error,
                'native': self.backend.status() if hasattr(self.backend, 'status') else None,
                'profile_version': '4.1.9.30', 'callback': self.store.get_meta('callback', {}),
                'capabilities': {'text_send': 'simulation' if simulation else 'fixture_verified',
                                 'text_receive': 'simulation' if simulation else 'fixture_verified',
                                 'text_outgoing_context': 'fixture_verified',
                                 'reference_xml_parser': 'fixture_verified',
                                 'native_message_identity': 'live_text_verified',
                                 'native_reference_receive': 'live_text_reference_verified',
                                 'conversation_read': ('not_available_in_simulation' if simulation else
                                     'live_verified' if self.read_state.verified() else
                                     'abi_fixture_verified_pending_live'),
                                 'resource_protocol': 'fixture_verified',
                                 'attachments': 'all_six_types_resource_read_live_verified',
                                 'native_attachment_download': 'image_video_file_native_cdn_uncached_live_verified_and_sticker_cdn_live_verified',
                                 'history': 'not_implemented'}}

    def close(self):
        self.stop_event.set()
        try:
            self.backend.stop()
            self.store.capture('stopped', account=self.backend.account, reason='service_shutdown')
            with self.store.condition:
                self.store.condition.notify_all()
            for thread in self.threads:
                thread.join(15)
            if any(t.is_alive() for t in self.threads):
                raise RuntimeError('业务线程未结束，保留数据库连接等待退出')
            self.resources.close()
            self.read_state.close()
            self.store.close()
        finally:
            self.lock.close()


class Server(ThreadingHTTPServer):
    daemon_threads = False
    block_on_close = True
    allow_reuse_address = True

    def __init__(self, address, app, token=None):
        host = address[0]
        try:
            local = ipaddress.ip_address(host).is_loopback
        except ValueError:
            local = host == 'localhost'
        if not local and not token:
            raise ValueError('非回环监听必须配置 token')
        self.app, self.token = app, token
        self.slots = threading.BoundedSemaphore(48)
        self.poll_slots = threading.BoundedSemaphore(16)
        super().__init__(address, Handler)

    def process_request(self, request, client_address):
        if not self.slots.acquire(False):
            request.sendall(b'HTTP/1.1 429 Too Many Requests\r\nContent-Length: 0\r\nConnection: close\r\n\r\n')
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()


class Handler(BaseHTTPRequestHandler):
    server_version = 'ClientHook/3'

    def setup(self):
        super().setup()
        self.connection.settimeout(10)

    def log_message(self, *args):
        pass  # No message text, credentials or unbounded access logs.

    def do_GET(self):
        self.handle_api()

    def do_POST(self):
        self.handle_api()

    def handle_api(self):
        try:
            if self.server.token and not hmac.compare_digest(self.headers.get('Authorization', ''), 'Bearer ' + self.server.token):
                raise ApiError(401, 'unauthorized', '缺少有效 Bearer token')
            url = urlsplit(self.path)
            path = unquote(url.path)
            query = parse_qs(url.query, keep_blank_values=True)
            if any(len(v) != 1 for v in query.values()):
                raise ApiError(400, 'invalid_parameter', '查询参数不能重复')
            body = {}
            if self.command == 'POST':
                if self.headers.get('Transfer-Encoding'):
                    raise ApiError(400, 'invalid_body', '不接受分块请求体')
                if self.headers.get_content_type() != 'application/json':
                    raise ApiError(400, 'invalid_body', '需要 application/json')
                try:
                    length = int(self.headers.get('Content-Length', '0'))
                except ValueError:
                    raise ApiError(400, 'invalid_body', 'Content-Length 无效') from None
                if not 0 <= length <= 34 * 1024 ** 2:
                    raise ApiError(413, 'body_too_large', '请求体超过限制')
                raw = self.rfile.read(length)
                if len(raw) != length:
                    raise ApiError(400, 'invalid_body', '请求体不完整')
                def unique(pairs):
                    result = {}
                    for key, value in pairs:
                        if key in result:
                            raise ValueError('duplicate JSON key')
                        result[key] = value
                    return result
                try:
                    body = json.loads(raw.decode('utf-8'), object_pairs_hook=unique,
                                      parse_constant=lambda s: (_ for _ in ()).throw(ValueError(s)))
                    if not isinstance(body, dict):
                        raise ValueError('JSON object required')
                except (ValueError, UnicodeError):
                    raise ApiError(400, 'invalid_json', '请求体必须是有效 JSON 对象') from None
            code, result = self.dispatch(path, query, body)
        except ApiError as error:
            code, result = error.status, error.body()
        except (ValueError, TypeError):
            code, result = 400, ApiError(400, 'invalid_parameter', '参数格式无效').body()
        except Exception:
            code, result = 503, ApiError(503, 'service_error', '服务错误，请检查状态与持久化存储').body()
        try:
            data = encode(result).encode('utf-8')
            self.send_response(code)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write(data)
        except (OSError, TimeoutError):
            pass  # Disconnect never acknowledges a message batch.

    def dispatch(self, path, query, body):
        app = self.server.app
        store = app.store
        if self.command == 'GET':
            if path == '/openapi.json':
                from .spec import specification
                return 200, specification()
            if path == '/client_hook/status':
                return 200, app.status()
            if path.startswith('/client_hook/resources/'):
                offset = query.get('offset', [None])[0]
                return 200, app.resources.status(path.rsplit('/', 1)[1], app.account(),
                                                 int(offset) if offset is not None else None)
            if path.startswith('/client_hook/requests/'):
                return 200, store.operation(identifier(path.rsplit('/', 1)[1]))
            if path.startswith('/consumers/') and path.count('/') == 2:
                return 200, store.consumer(identifier(path.rsplit('/', 1)[1]), app.account())
            if path == '/messages':
                name = identifier(query.get('consumer_id', [''])[0], 'consumer_id')
                timeout, limit = float(query.get('timeout', ['30'])[0]), int(query.get('limit', ['100'])[0])
                if not 0 <= timeout <= 30 or not 1 <= limit <= 100:
                    raise ApiError(400, 'invalid_parameter', 'timeout 范围 0–30，limit 范围 1–100')
                if name == 'callback':
                    raise ApiError(409, 'reserved_consumer', 'callback 消费者供内部投递使用')
                if not self.server.poll_slots.acquire(False):
                    raise ApiError(429, 'poll_limit', '长轮询连接已满')
                try:
                    return 200, store.read(name, app.account(), timeout, limit, query.get('cursor', [None])[0])
                finally:
                    self.server.poll_slots.release()
        else:
            if path in ('/client_hook/read/mark', '/client_hook/read/status'):
                if set(body) != {'event_id'} or not isinstance(body['event_id'], str):
                    raise ApiError(400, 'invalid_parameter', '需要原入站event_id')
                function = app.read_state.request if path.endswith('/mark') else app.read_state.status
                return 200, function(body['event_id'], app.account())
            if path == '/client_hook/resources/acquire':
                if set(body) != {'event_id', 'resource_id'} or not all(isinstance(v, str) for v in body.values()):
                    raise ApiError(400, 'invalid_parameter', '需要原消息event_id和resource_id')
                return 202, app.resources.acquire(body['event_id'], body['resource_id'], app.account())
            if path == '/SendTextMsg':
                return app.send(body)
            if path == '/GetSelfProfile':
                return 200, app.backend.profile()
            if path == '/IsLoginStatus':
                app.backend.profile()
                return 200, {'onlinestatus': '3', 'msg': '登陆完成！'}
            if path == '/consumers':
                name = identifier(body.get('consumer_id'), 'consumer_id')
                if name == 'callback':
                    raise ApiError(409, 'reserved_consumer', 'callback 消费者供内部投递使用')
                return 200, store.consumer(name, app.account(), start_mode(body.get('start', 'earliest')))
            match = re.fullmatch(r'/consumers/([^/]+)/(ack|reset)', path)
            if match:
                name, operation = identifier(match[1]), match[2]
                if name == 'callback':
                    raise ApiError(409, 'reserved_consumer', 'callback 消费者供内部投递使用')
                if operation == 'ack':
                    return 200, store.ack(name, app.account(), identifier(body.get('ack_token'), 'ack_token'))
                return 200, store.reset(name, app.account(), start_mode(body.get('start')))
            if path == '/client_hook/callback/resume':
                if not app.callback_url:
                    raise ApiError(409, 'callback_not_configured', '未配置回调')
                store.set_meta('callback', {'paused': False})
                return 200, {'resumed': True}
            if path == '/client_hook/callback/reset':
                if not app.callback_url or not store.get_meta('callback', {}).get('paused'):
                    raise ApiError(409, 'callback_not_paused', '仅允许显式重置已暂停的回调消费者')
                return 200, store.reset('callback', app.account(), start_mode(body.get('start')))
            if path in {'/SendTextMsg_NoSrc', '/SendAtMsg_NoSrc', '/SendFileMsg', '/SendImgMsg',
                        '/SendImgMsg_NoSrc', '/SendFileMsg_NoSrc', '/ForwardXMLMsg', '/SendVoiceMsg',
                        '/ForwardVoiceMsg', '/SendPatMsg', '/SendFinderMsg_NoSrc', '/SendCardMsg_NoSrc', '/upload'}:
                raise ApiError(503, 'not_implemented', '该能力属于后续交付阶段')
        raise ApiError(404, 'route_not_found', '接口不存在或请求方法不匹配')
