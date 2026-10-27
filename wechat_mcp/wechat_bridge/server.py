"""无状态 Streamable HTTP JSON 响应及标准 newline JSON stdio 传输。"""
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import sys
import threading
from urllib.parse import urlsplit

from .httpio import encode
from .protocol import Protocol
from tool.settings import local_host

MAX_BODY = 300 * 1024


def decode(raw):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('重复 JSON 字段')
            result[key] = value
        return result
    def invalid(value):
        raise ValueError('非有限 JSON 数字')
    return json.loads(raw, object_pairs_hook=unique, parse_constant=invalid)


class Server(ThreadingHTTPServer):
    daemon_threads = False
    block_on_close = True
    def __init__(self, address, bridge, token, simulator=None):
        if not token or len(token) < 24:
            raise ValueError('HTTP 传输必须配置至少 24 字符的 MCP token')
        self.bridge, self.protocol, self.token, self.simulator = bridge, Protocol(bridge), token, simulator
        self.slots = threading.BoundedSemaphore(32)
        super().__init__(address, Handler)

    def process_request(self, request, client_address):
        if not self.slots.acquire(False):
            request.close()
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
    def setup(self):
        super().setup()
        self.connection.settimeout(15)

    def log_message(self, *args):
        pass  # URL、请求正文和密钥不写访问日志。

    def send_json(self, status, payload=None):
        raw = encode(payload) if payload is not None else b''
        try:
            self.send_response(status)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(raw)))
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write(raw)
        except OSError:
            pass

    def authorized(self):
        origin = self.headers.get('Origin')
        host = self.headers.get('Host', '')
        try:
            parsed_host = urlsplit('http://' + host)
            valid_host = parsed_host.hostname in ('localhost', '127.0.0.1', '::1', local_host()) and not parsed_host.username
            if not valid_host or (origin and urlsplit(origin).netloc != host):
                self.send_json(403, {'error': 'invalid_host_or_origin'})
                return False
        except ValueError:
            self.send_json(403, {'error': 'invalid_host'})
            return False
        if not hmac.compare_digest(self.headers.get('Authorization', ''), 'Bearer ' + self.server.token):
            self.send_json(401, {'error': 'unauthorized'})
            return False
        return True

    def do_GET(self):
        if '/.well-known/' in urlsplit(self.path).path:
            # 不提供 OAuth：公开声明元数据不存在，不暴露工具、消息或健康状态。
            self.send_json(404, {'error': 'oauth_metadata_not_supported'})
            return
        if not self.authorized():
            return
        if self.path == '/health':
            self.send_json(200, self.server.protocol.invoke('bridge_status', {}))
        else:
            # OAuth 元数据探测返回不存在；本服务是固定 bearer，不伪造 OAuth。
            self.send_json(405 if self.path == '/mcp' else 404, {'error': 'method_not_allowed' if self.path == '/mcp' else 'not_found'})

    def do_DELETE(self):
        if self.authorized():
            self.send_json(405)

    def do_POST(self):
        if not self.authorized():
            return
        if self.headers.get('Transfer-Encoding'):
            self.send_json(400, {'error': 'chunked_not_supported'})
            return
        try:
            size = int(self.headers.get('Content-Length', '-1'))
            if not 0 <= size <= MAX_BODY:
                self.send_json(413)
                return
            if self.headers.get_content_type() != 'application/json':
                self.send_json(415)
                return
            raw = self.rfile.read(size)
            request = decode(raw)
        except (ValueError, UnicodeError, OSError):
            self.send_json(400, Protocol.error(None, -32700, 'Parse error'))
            return
        if self.path == '/simulation/messages' and self.server.simulator and self.server.bridge.simulation:
            try:
                event = self.server.simulator.inject(request['conversation_id'], request['text'])
                self.send_json(200, event)
            except (ValueError, KeyError, TypeError):
                self.send_json(400, {'error': 'invalid_simulation_message'})
            return
        if self.path != '/mcp':
            self.send_json(404)
            return
        version = self.headers.get('MCP-Protocol-Version')
        if version and version not in ('2026-07-28', '2025-11-25', '2025-06-18', '2024-11-05'):
            self.send_json(400, {'error': 'unsupported_protocol_version'})
            return
        response = self.server.protocol.call(request, version=version)
        if isinstance(request, dict) and isinstance(request.get('method'), str):
            try:
                ok = bool(response and 'error' not in response and not response.get('result', {}).get('isError'))
                self.server.bridge.store.audit(request['method'], ok)
            except Exception:
                self.server.bridge.worker_errors['audit'] = {'state': 'fault'}
        self.send_json(202 if response is None else 200, response)


def stdio(bridge):
    protocol = Protocol(bridge)
    # stdio 由宿主进程控制权限，无 HTTP bearer；stdout 只输出协议。
    while True:
        raw = sys.stdin.buffer.readline(MAX_BODY + 1)
        if not raw:
            return
        try:
            if len(raw) > MAX_BODY:
                raise ValueError('请求过大')
            response = protocol.call(decode(raw))
        except (ValueError, UnicodeError):
            response = Protocol.error(None, -32700, 'Parse error')
        if response is not None:
            sys.stdout.buffer.write(encode(response) + b'\n')
            sys.stdout.buffer.flush()
