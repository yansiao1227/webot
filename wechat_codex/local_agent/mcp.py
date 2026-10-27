"""Loopback-only MCP client. Credentials never enter model input."""
import asyncio
import http.client
import ipaddress
import json
from urllib.parse import urlsplit


class MCPError(Exception):
    pass


class MCP:
    def __init__(self, url, token):
        self.url = urlsplit(url)
        if self.url.scheme != 'http' or self.url.username or self.url.password or self.url.query or self.url.fragment:
            raise ValueError('MCP必须为无用户信息的本机HTTP地址')
        if not ipaddress.ip_address(self.url.hostname).is_loopback:
            raise ValueError('只允许回环MCP地址，避免误传凭据')
        if not token or len(token) < 24:
            raise ValueError('需要配置MCP bearer')
        self.token = token

    def _call(self, name, arguments):
        connection = http.client.HTTPConnection(self.url.hostname, self.url.port or 80, timeout=35)
        try:
            payload = json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                                  'params': {'name': name, 'arguments': arguments}}, ensure_ascii=False).encode()
            connection.request('POST', self.url.path or '/mcp', payload,
                               {'Authorization': 'Bearer ' + self.token, 'Content-Type': 'application/json'})
            response = connection.getresponse()
            raw = response.read(1048577)
            if response.status != 200 or len(raw) > 1048576:
                raise MCPError('MCP HTTP状态错误或响应超限')
            result = json.loads(raw)
            if result.get('error'):
                raise MCPError('MCP RPC错误')
            value = result['result']
            if value.get('isError'):
                # No error text, event body or secret is written to operator logs.
                raise MCPError('MCP工具拒绝或执行失败')
            return value['structuredContent']
        finally:
            connection.close()

    async def call(self, name, **arguments):
        return await asyncio.to_thread(self._call, name, arguments)


def context_allowed(event, account, conversations):
    return (event.get('account') == account and event.get('conversation_id') in conversations and
            event.get('direction') in ('incoming', 'outgoing') and
            event.get('message_type') in {'text', 'image', 'voice', 'video', 'file', 'sticker', 'card', 'unknown'} and
            isinstance(event.get('payload'), dict))


def allowed(event, account, conversations):
    if (event.get('account') != account or event.get('direction') != 'incoming' or
            event.get('message_type') != 'text' or event.get('conversation_id') not in conversations):
        return False
    if not event['conversation_id'].endswith('@chatroom'):
        return True
    mentions = event.get('mentions') or {}
    ids = mentions.get('wxids')
    return (mentions.get('state') == 'known' and mentions.get('source') == 'weixin41930_record_source_180' and
            isinstance(ids, list) and account in ids)
