"""固定HTTPS站点的幂等更新及匿名读回；不跟随重定向。"""
import asyncio
import http.client
import ipaddress
import json
from urllib.parse import urlsplit


def create_publisher(config, state_dir, *, validate_only=False):
    """按明确配置选择发布路径，不把HTTP公网读地址降级为明文写接口。"""
    mode = config.get('publisher', 'http_api')
    if mode == 'ssh_snapshot':
        from .ssh_publisher import SSHSnapshotPublisher
        return SSHSnapshotPublisher(config, state_dir, validate_only=validate_only)
    if mode != 'http_api':
        raise ValueError('日报publisher仅支持ssh_snapshot或http_api')
    if validate_only:
        token = 'validate-only-not-a-publish-token-' * 2
    else:
        from pathlib import Path
        from wechat_codex.local_agent.vault import unseal
        credentials = json.loads((Path(state_dir) / 'credentials.json').read_text(encoding='utf-8'))
        token = unseal(credentials['publish_token'])
    return SitePublisher(config.get('site_url', ''), token)


class SitePublisher:
    def __init__(self, site_url, token, timeout=25):
        parsed = urlsplit(site_url)
        if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or
                parsed.path not in ('', '/') or parsed.query or parsed.fragment or parsed.port not in (None, 443)):
            raise ValueError('日报站点须为固定HTTPS公网origin')
        try:
            if not ipaddress.ip_address(parsed.hostname).is_global:
                raise ValueError('日报站点不能使用内网地址')
        except ValueError:
            if ':' in parsed.hostname or parsed.hostname.replace('.', '').isdigit():
                raise ValueError('日报站点不能使用内网地址') from None
        if parsed.hostname.lower() in ('localhost',) or parsed.hostname.lower().endswith(('.localhost', '.local')):
            raise ValueError('日报站点不能使用本机地址')
        if not isinstance(token, str) or len(token) < 32:
            raise ValueError('日报发布密钥至少32字符')
        self.origin, self.host, self.token, self.timeout = site_url.rstrip('/'), parsed.hostname, token, timeout

    def _request(self, method, path, data=None, *, authenticated=False):
        connection = http.client.HTTPSConnection(self.host, timeout=self.timeout)
        try:
            headers = {'Accept': 'application/json', 'User-Agent': 'DailyObserver/1.0'}
            if authenticated:
                headers['Authorization'] = 'Bearer ' + self.token
            if data is not None:
                headers['Content-Type'] = 'image/webp' if isinstance(data, bytes) else 'application/json'
            raw = data if isinstance(data, bytes) else json.dumps(data, ensure_ascii=False, allow_nan=False).encode() if data is not None else None
            connection.request(method, path, raw, headers)
            response = connection.getresponse()
            body = response.read(1048577)
            if len(body) > 1048576 or response.status not in (200, 201):
                raise RuntimeError('日报站点请求失败，状态=' + str(response.status))
            return json.loads(body) if 'application/json' in response.getheader('Content-Type', '') else body
        finally:
            connection.close()

    async def upload_media(self, edition, directory):
        from pathlib import Path
        from hashlib import sha256
        for digest in sorted({s['image']['asset_id'] for s in edition['sources'] if s.get('image', {}).get('asset_id')}):
            data = (Path(directory) / (digest + '.webp')).read_bytes()
            if sha256(data).hexdigest() != digest or len(data) > 512 * 1024:
                raise ValueError('invalid_cached_image')
            await asyncio.to_thread(self._request, 'PUT', '/api/media/' + digest, data, authenticated=True)

    async def upload(self, edition):
        return await asyncio.to_thread(self._request, 'PUT', '/api/editions/' + edition['date'], edition, authenticated=True)

    async def verify(self, edition):
        value = await asyncio.to_thread(self._request, 'GET', '/api/editions/' + edition['date'])
        if not isinstance(value, dict) or value.get('date') != edition['date'] or value.get('content_hash') != edition['content_hash']:
            raise ValueError('日报网站读回的日期或摘要不匹配')
        await asyncio.to_thread(self._request, 'GET', '/daily/' + edition['date'])
        from hashlib import sha256
        for digest in {s['image']['asset_id'] for s in edition['sources'] if s.get('image', {}).get('asset_id')}:
            raw = await asyncio.to_thread(self._request, 'GET', '/media/' + digest)
            if not isinstance(raw, bytes) or sha256(raw).hexdigest() != digest:
                raise ValueError('公开媒体摘要核验失败')
        return self.origin + '/daily/' + edition['date']

    async def history(self):
        from datetime import date
        from .content import validate_edition
        result, cursor, seen = [], None, set()
        for _ in range(200):
            batch = await asyncio.to_thread(self._request, 'GET', '/api/editions?limit=50' +
                                            ('&cursor=' + cursor if cursor else ''))
            if not isinstance(batch, dict) or not isinstance(batch.get('items'), list):
                raise ValueError('网站历史列表格式无效')
            for item in batch['items']:
                day = item['date']
                if date.fromisoformat(day).isoformat() != day or day in seen:
                    raise ValueError('网站历史日期无效或重复')
                seen.add(day)
                value = await asyncio.to_thread(self._request, 'GET', '/api/editions/' + day)
                edition = validate_edition(value, allow_simulation=False)
                if edition['date'] != day or edition['content_hash'] != item['content_hash']:
                    raise ValueError('网站历史内容摘要冲突')
                result.append(edition)
            cursor = batch.get('next_cursor')
            if not cursor:
                return result
            if date.fromisoformat(cursor).isoformat() != cursor:
                raise ValueError('网站历史游标无效')
        raise ValueError('网站历史超过页数上限')

    def status(self):
        return {'publisher': 'http_api', 'site_url': self.origin}
