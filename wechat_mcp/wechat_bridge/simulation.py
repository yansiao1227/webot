"""显式模拟上游和规则回复 agent；全部走真实 HTTP，不调用模型或微信。"""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from queue import Queue
import secrets
import threading
import time
from urllib.parse import parse_qs, urlsplit
import uuid

from .httpio import HookClient, encode, verify_signature


class SimulatedHook:
    def __init__(self, account='wxid_sim_self'):
        self.account = account
        self.events, self.consumers, self.operations, self.sent = [], {}, {}, []
        self.condition = threading.Condition()
        self.instance = uuid.uuid4().hex
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), HookHandler)
        self.server.app = self
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.start()
        self.url = f'http://127.0.0.1:{self.server.server_port}'

    def inject(self, conversation, text, mentions=None):
        if not isinstance(conversation, str) or not conversation or not isinstance(text, str) or not text or len(text.encode()) > 65536:
            raise ValueError('模拟消息参数无效')
        with self.condition:
            event = {'event_id': self.instance + ':' + str(len(self.events) + 1), 'account': self.account,
                     'conversation_id': conversation, 'fromid': conversation, 'toid': self.account,
                     'message_type': 'text', 'payload': {'text': text}, 'direction': 'incoming',
                     'source': 'simulation', 'captured_at': time.time()}
            if mentions is not None:
                event['mentions'] = mentions
            self.events.append(event)
            self.condition.notify_all()
            return event

    def dispatch(self, method, path, body):
        parsed = urlsplit(path)
        with self.condition:
            if method == 'POST' and path == '/GetSelfProfile':
                return {'wxid': self.account, 'simulation': True}
            if method == 'POST' and path == '/consumers':
                name = body['consumer_id']
                if name not in self.consumers:
                    self.consumers[name] = {'pos': len(self.events) if body.get('start') == 'latest' else 0, 'pending': None}
                return {'consumer_id': name, 'account': self.account}
            if method == 'GET' and parsed.path == '/messages':
                query = parse_qs(parsed.query)
                consumer = self.consumers[query['consumer_id'][0]]
                deadline = time.monotonic() + min(30, float(query.get('timeout', ['0'])[0]))
                while not consumer['pending'] and consumer['pos'] == len(self.events):
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        return {'events': [], 'ack_token': None, 'capture': {'state': 'running'}}
                    self.condition.wait(remaining)
                    if getattr(self, 'closing', False):
                        return {'events': [], 'capture': {'state': 'stopped'}}
                if not consumer['pending']:
                    consumer['pending'] = {'events': self.events[consumer['pos']:consumer['pos']+100],
                                           'ack_token': secrets.token_urlsafe(20), 'capture': {'state': 'running'}}
                return consumer['pending']
            if method == 'POST' and path.startswith('/consumers/') and path.endswith('/ack'):
                consumer = self.consumers[path.split('/')[2]]
                pending = consumer['pending']
                if pending and pending['ack_token'] == body['ack_token']:
                    consumer['pos'] += len(pending['events']); consumer['pending'] = None
                return {'acknowledged': True}
            if method == 'POST' and path == '/SendTextMsg':
                rid = body['request_id']
                existing = self.operations.get(rid)
                if existing:
                    if existing['params'] != body:
                        raise ValueError('idempotency conflict')
                    return existing['operation']
                op = {'request_id': rid, 'account': self.account, 'state': 'accepted',
                      'delivery_confirmed': False, 'result': {'simulation': True}}
                self.operations[rid] = {'params': body, 'operation': op}
                self.sent.append(body)
                return op
            if method == 'GET' and path.startswith('/client_hook/requests/'):
                return self.operations[path.rsplit('/', 1)[1]]['operation']
            raise KeyError('route not found')

    def close(self):
        with self.condition:
            self.closing = True
            self.condition.notify_all()
        self.server.shutdown(); self.server.server_close(); self.thread.join()


class HookHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass
    def do_GET(self):
        self.run_request()
    def do_POST(self):
        self.run_request()
    def run_request(self):
        try:
            body = json.loads(self.rfile.read(int(self.headers.get('Content-Length', '0')))) if self.command == 'POST' else None
            result = self.server.app.dispatch(self.command, self.path, body)
            status = 200
        except (ValueError, KeyError):
            result, status = {'error': 'simulation_request_failed'}, 404
        raw = encode(result)
        try:
            self.send_response(status); self.send_header('Content-Length', str(len(raw))); self.end_headers(); self.wfile.write(raw)
        except OSError:
            pass


class SimulatedAgent:
    def __init__(self, mcp_url, token, secret):
        self.client = HookClient(mcp_url, token)
        self.secret, self.events, self.results, self.errors = secret, [], [], []
        self.queue = Queue()
        self.seen = set()
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), AgentHandler)
        self.server.app = self
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.start()
        self.worker = threading.Thread(target=self.run)
        self.worker.start()
        self.url = f'http://127.0.0.1:{self.server.server_port}/events'
        self.failures = 0

    def run(self):
        while True:
            event = self.queue.get()
            if event is None:
                return
            try:
                self.results.append(self.rpc('tools/call', {'name': 'reply_text', 'arguments': {
                    'event_id': event['data']['event_id'], 'text': '模拟自动回复：' + event['data']['text']}}))
            except Exception as error:
                self.errors.append(type(error).__name__)

    def rpc(self, method, params=None):
        return self.client.request('POST', '/mcp', {'jsonrpc': '2.0', 'id': uuid.uuid4().hex,
                                                  'method': method, 'params': params or {}})

    def close(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join()
        self.queue.put(None); self.worker.join(15)


class AgentHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass
    def do_POST(self):
        app = self.server.app
        try:
            raw = self.rfile.read(int(self.headers['Content-Length']))
            verify_signature(app.secret, self.headers, raw)
            payload = json.loads(raw)
            if payload.get('type') == 'verification':
                result = {'challenge': payload['challenge']}
            else:
                if app.failures:
                    app.failures -= 1
                    self.send_response(503); self.end_headers(); return
                if self.headers['webhook-id'] != payload['eventId']:
                    raise ValueError('event id mismatch')
                app.events.append(payload)
                if payload['eventId'] not in app.seen:
                    app.seen.add(payload['eventId']); app.queue.put(payload)
                result = {}
            response = encode(result)
            self.send_response(200); self.send_header('Content-Length', str(len(response))); self.end_headers(); self.wfile.write(response)
        except (ValueError, KeyError):
            self.send_response(401); self.end_headers()
