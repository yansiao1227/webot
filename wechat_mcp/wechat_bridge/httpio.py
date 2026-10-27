"""HTTP 边界：固定上游、受限回调、连接时 DNS 校验和 Standard Webhooks。"""
import base64
import hashlib
import hmac
import http.client
import ipaddress
import json
import socket
import ssl
import time
from urllib.parse import urlsplit


def encode(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode('utf-8')


class RemoteError(Exception):
    def __init__(self, status, body=None):
        self.status, self.body = status, body
        super().__init__(f'HTTP {status}')


def secret_bytes(secret):
    if not isinstance(secret, str) or not secret.startswith('whsec_'):
        raise ValueError('需要 whsec_ 签名密钥')
    try:
        value = base64.b64decode(secret[6:], validate=True)
    except (ValueError, TypeError):
        raise ValueError('签名密钥编码无效') from None
    if not 24 <= len(value) <= 64:
        raise ValueError('签名密钥长度必须为 24–64 字节')
    return value


def signature(secret, event_id, timestamp, body):
    message = event_id.encode() + b'.' + str(timestamp).encode() + b'.' + body
    return 'v1,' + base64.b64encode(hmac.new(secret_bytes(secret), message, hashlib.sha256).digest()).decode()


def verify_signature(secret, headers, body, now=None):
    stamp = int(headers['webhook-timestamp'])
    if abs((time.time() if now is None else now) - stamp) > 300:
        raise ValueError('签名已过期')
    expected = signature(secret, headers['webhook-id'], stamp, body)
    if not any(hmac.compare_digest(expected, part) for part in headers['webhook-signature'].split()):
        raise ValueError('签名不匹配')


class CallbackHTTP:
    def __init__(self, simulation=False, timeout=10):
        self.simulation, self.timeout = simulation, timeout

    def destination(self, url):
        if not isinstance(url, str) or len(url) > 2048:
            raise ValueError('回调 URL 无效')
        parsed = urlsplit(url)
        if parsed.username or parsed.password or parsed.fragment or not parsed.hostname:
            raise ValueError('回调 URL 不允许凭据或 fragment')
        if parsed.scheme != 'https' and not (self.simulation and parsed.scheme == 'http'):
            raise ValueError('真实回调必须使用 HTTPS')
        port = parsed.port or (443 if parsed.scheme == 'https' else 80)
        addresses = socket.getaddrinfo(parsed.hostname, port, type=socket.SOCK_STREAM)
        if not addresses:
            raise ValueError('回调地址无法解析')
        for _, _, _, _, address in addresses:
            ip = ipaddress.ip_address(address[0])
            if self.simulation:
                if not ip.is_loopback:
                    raise ValueError('模拟模式仅允许回环回调')
            elif not ip.is_global or ip.is_multicast:
                raise ValueError('回调禁止私网、回环和非公网地址')
        return parsed, addresses[0][4][0], port

    def post(self, url, body, headers):
        parsed, address, port = self.destination(url)
        # 直接连接已校验 IP；TLS 仍验证原 hostname，避免校验与连接间的 DNS 重绑定。
        conn = http.client.HTTPConnection(parsed.hostname, port, timeout=self.timeout)
        try:
            raw = socket.create_connection((address, port), self.timeout)
            if parsed.scheme == 'https':
                try:
                    raw = ssl.create_default_context().wrap_socket(raw, server_hostname=parsed.hostname)
                except BaseException:
                    raw.close()
                    raise
            conn.sock = raw
            path = parsed.path or '/'
            if parsed.query:
                path += '?' + parsed.query
            conn.request('POST', path, body, headers)
            response = conn.getresponse()
            payload = response.read(262145)
            if len(payload) > 262144:
                raise ValueError('回调响应过大')
            if not 200 <= response.status < 300:
                raise RemoteError(response.status)
            return json.loads(payload) if payload else {}
        finally:
            conn.close()

    def signed(self, url, secret, subscription_id, event_id, payload, previous_secret=None):
        body = encode(payload)
        if len(body) > 262144:
            raise ValueError('事件超过 256 KiB')
        stamp = int(time.time())
        signed = signature(secret, event_id, stamp, body)
        if previous_secret:
            signed += ' ' + signature(previous_secret, event_id, stamp, body)
        return self.post(url, body, {'Content-Type': 'application/json', 'webhook-id': event_id,
                                    'webhook-timestamp': str(stamp), 'webhook-signature': signed,
                                    'X-MCP-Subscription-Id': subscription_id})


class HookClient:
    def __init__(self, base_url, token=None):
        self.base = urlsplit(base_url)
        if self.base.scheme not in ('http', 'https') or not self.base.hostname or self.base.username or self.base.password:
            raise ValueError('client_hook URL 无效')
        if self.base.path not in ('', '/') or self.base.query or self.base.fragment:
            raise ValueError('client_hook URL 应为服务根地址')
        self.token = token

    def request(self, method, path, body=None, timeout=40):
        cls = http.client.HTTPSConnection if self.base.scheme == 'https' else http.client.HTTPConnection
        conn = cls(self.base.hostname, self.base.port, timeout=timeout)
        headers = {'Content-Type': 'application/json'}
        if self.token:
            headers['Authorization'] = 'Bearer ' + self.token
        try:
            conn.request(method, path, encode(body) if body is not None else None, headers)
            response = conn.getresponse()
            raw = response.read(16 * 1024 * 1024 + 1)
            if len(raw) > 16 * 1024 * 1024:
                raise ValueError('上游响应过大')
            result = json.loads(raw)
            if not 200 <= response.status < 300:
                raise RemoteError(response.status, result)
            return result
        finally:
            conn.close()
