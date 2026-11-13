"""先在本机校验导入，再通过已核验SSH发布只读快照；HTTP只用于公开读回。"""
import asyncio
from hashlib import sha256
from datetime import date as Date
import ipaddress
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
from urllib.parse import urlsplit

from tool.settings import path_setting

ROOT = Path(__file__).resolve().parents[1]


def validate_origin(site_url, host):
    parsed = urlsplit(site_url)
    if (parsed.scheme not in ('http', 'https') or parsed.hostname != host or
            parsed.username or parsed.password or parsed.path not in ('', '/') or
            parsed.query or parsed.fragment or
            parsed.port not in (None, 80 if parsed.scheme == 'http' else 443)):
        raise ValueError('SSH公开地址须与已配置部署主机一致，且为HTTP80或HTTPS443根地址')
    if not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9.-]*', host):
        raise ValueError('SSH部署主机无效')
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        if host.lower() == 'localhost' or host.lower().endswith(('.local', '.localhost')):
            raise ValueError('SSH公开地址不能为本机或内网地址') from None
    else:
        if not address.is_global:
            raise ValueError('SSH公开地址不能为内网地址')
    return site_url.rstrip('/')


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2), encoding='utf-8')
    temporary.replace(path)


class SSHSnapshotPublisher:
    def __init__(self, config, state_dir, *, validate_only=False, deployer=None, command=None, reader=None):
        self.directory = Path(state_dir) / 'ssh-publisher'
        self.host = config.get('ssh_host') or os.getenv('DAILY_DIGEST_DEPLOY_HOST', '')
        self.user = config.get('ssh_user') or os.getenv('DAILY_DIGEST_DEPLOY_USER', 'root')
        if not re.fullmatch(r'[A-Za-z0-9_-]+', self.user):
            raise ValueError('SSH部署用户名无效')
        self.origin = validate_origin(config.get('site_url', ''), self.host)
        selected = config.get('ssh_identity_file') or os.getenv('DAILY_DIGEST_DEPLOY_IDENTITY_FILE')
        self.identity = Path(selected).expanduser() if selected else Path.home() / '.ssh/webot_daily_digest_ed25519'
        if not self.identity.is_absolute():
            self.identity = ROOT / self.identity
        self.database = path_setting('DAILY_DIGEST_PREVIEW_DB', 'daily_digest/web/state/preview.sqlite')
        self.command = command or subprocess.run
        self.reader = reader or self._read
        self._deployer = deployer
        self.media_directory = None
        if not validate_only and deployer is None and not self.identity.is_file():
            raise ValueError('日报专用SSH密钥尚未安装或本机私钥不存在')

    @property
    def deployer(self):
        if self._deployer is None:
            from .deploy.engine import SnapshotDeployer
            self._deployer = SnapshotDeployer(self.host, self.user, identity_file=self.identity,
                database=self.database, state_dir=self.directory / 'releases')
        return self._deployer

    def _read(self, path):
        # 不跟随重定向，避免将其它站点当成本次服务器的读回结果。
        import http.client
        parsed = urlsplit(self.origin)
        cls = http.client.HTTPSConnection if parsed.scheme == 'https' else http.client.HTTPConnection
        connection = cls(self.host, timeout=25)
        try:
            connection.request('GET', path, headers={'User-Agent': 'DailyObserver/1.0', 'Accept': 'application/json'})
            response = connection.getresponse()
            data = response.read(1048577)
            if response.status != 200 or len(data) > 1048576:
                raise RuntimeError('公开日报读回未确认：HTTP ' + str(response.status))
            return json.loads(data) if 'application/json' in response.getheader('Content-Type', '') else data
        finally:
            connection.close()

    def _mapping(self, edition):
        if Date.fromisoformat(edition['date']).isoformat() != edition['date'] or not re.fullmatch(r'[a-f0-9]{64}', edition['content_hash']):
            raise ValueError('日报日期或摘要无效')
        return self.directory / 'intents' / (edition['date'] + '-' + edition['content_hash'] + '.json')

    async def upload_media(self, edition, directory):
        from .content import validate_edition
        validate_edition(edition, allow_simulation=False)
        self.media_directory = Path(directory).resolve()
        for digest in {s['image']['asset_id'] for s in edition['sources'] if s.get('image', {}).get('asset_id')}:
            if not re.fullmatch(r'[a-f0-9]{64}', digest):
                raise ValueError('媒体标识无效')
            data = (self.media_directory / (digest + '.webp')).read_bytes()
            if len(data) > 512 * 1024 or sha256(data).hexdigest() != digest:
                raise ValueError('本地绑定媒体缺失或摘要错误')

    def _upload(self, edition):
        from .content import validate_edition
        validated = validate_edition(edition, allow_simulation=False)
        mapping = self._mapping(validated)
        if mapping.exists():
            saved = json.loads(mapping.read_text(encoding='utf-8'))
            release = self.deployer.load_release(saved['release_id'])
        else:
            payload = mapping.with_suffix('.edition.json')
            atomic_json(payload, validated)
            args = [shutil.which('node') or 'node', str(ROOT / 'daily_digest/web/scripts/import-edition.mjs'),
                    '--db', str(self.database), '--edition', str(payload)]
            if self.media_directory is not None:
                args += ['--media', str(self.media_directory)]
            completed = self.command(args, cwd=ROOT, capture_output=True, text=True, timeout=90)
            if completed.returncode:
                raise RuntimeError('正式日报本地导入失败，原历史保留；退出码=' + str(completed.returncode))
            release = self.deployer.prepare()
            atomic_json(mapping, {'date': validated['date'], 'content_hash': validated['content_hash'],
                                  'release_id': release.release_id})
        # 发布ID先落盘；异常后runner只调用verify，不新建快照或重切服务。
        return self.deployer.deploy(release)

    async def upload(self, edition):
        return await asyncio.to_thread(self._upload, edition)

    def _verify(self, edition):
        mapping = self._mapping(edition)
        if not mapping.exists():
            raise RuntimeError('没有对应的SSH发布意图，不能从未知状态自动创建发布')
        saved = json.loads(mapping.read_text(encoding='utf-8'))
        confirmed = self.deployer.verify(self.deployer.load_release(saved['release_id']))
        if not isinstance(confirmed, dict) or confirmed.get('state') != 'confirmed':
            raise RuntimeError('原SSH发布尚未取得完整确认')
        self._verify_public(edition)
        return self.origin + '/daily/' + edition['date']

    def _verify_public(self, edition):
        value = self.reader('/api/editions/' + edition['date'])
        if not isinstance(value, dict) or value.get('date') != edition['date'] or value.get('content_hash') != edition['content_hash']:
            raise ValueError('公开日报日期或摘要与原版不一致')
        self.reader('/daily/' + edition['date'])
        for digest in {s['image']['asset_id'] for s in edition['sources'] if s.get('image', {}).get('asset_id')}:
            data = self.reader('/media/' + digest)
            if not isinstance(data, bytes) or sha256(data).hexdigest() != digest:
                raise ValueError('公开日报绑定媒体未通过摘要核验')
        return value

    async def verify(self, edition):
        # 首次接入的同日手工版没有自动部署记录，仍可精确核验后复用。
        if not self._mapping(edition).exists():
            await asyncio.to_thread(self._verify_public, edition)
            return self.origin + '/daily/' + edition['date']
        return await asyncio.to_thread(self._verify, edition)

    def _history(self):
        from .content import validate_edition
        result, cursor, seen = [], None, set()
        for _ in range(200):
            batch = self.reader('/api/editions?limit=50' + ('&cursor=' + cursor if cursor else ''))
            if not isinstance(batch, dict) or not isinstance(batch.get('items'), list):
                raise ValueError('服务器历史索引无效')
            for item in batch['items']:
                edition_date = item['date']
                if Date.fromisoformat(edition_date).isoformat() != edition_date or edition_date in seen:
                    raise ValueError('服务器历史日期重复')
                seen.add(edition_date)
                edition = validate_edition(self.reader('/api/editions/' + edition_date), allow_simulation=False)
                if edition['date'] != edition_date or edition['content_hash'] != item['content_hash']:
                    raise ValueError('服务器历史摘要不一致')
                self._verify_public(edition)
                result.append(edition)
            cursor = batch.get('next_cursor')
            if not cursor:
                return result
            if Date.fromisoformat(cursor).isoformat() != cursor:
                raise ValueError('服务器历史游标无效')
        raise ValueError('服务器历史页数超过上限')

    async def history(self):
        return await asyncio.to_thread(self._history)

    def status(self):
        # 心跳只读本机状态，不做SSH或HTTP请求。
        value = {'publisher': 'ssh_snapshot', 'site_url': self.origin, 'latest_release': None}
        try:
            records = sorted((self.directory / 'intents').glob('*.json')) if self.directory.exists() else []
            records = [p for p in records if not p.name.endswith('.edition.json')]
            if not records:
                return value
            mapping = json.loads(records[-1].read_text(encoding='utf-8'))
            release_id = mapping.get('release_id')
            if not isinstance(release_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', release_id):
                raise ValueError('invalid_release_id')
            edition_date, digest = mapping.get('date'), mapping.get('content_hash')
            if Date.fromisoformat(edition_date).isoformat() != edition_date or not isinstance(digest, str) or not re.fullmatch(r'[a-f0-9]{64}', digest):
                raise ValueError('invalid_publication_mapping')
            latest = {'date': edition_date, 'content_hash': digest, 'release_id': release_id,
                      'state': 'unknown', 'error': 'local_release_state_missing', 'server_confirmed': False}
            value['latest_release'] = latest
            path = self.directory / 'releases' / release_id / 'state.json'
            if path.exists():
                saved = json.loads(path.read_text(encoding='utf-8'))
                if saved.get('release_id') != release_id or saved.get('target') != [self.host, self.user]:
                    raise ValueError('release_state_mismatch')
                state = saved.get('state')
                if not isinstance(state, str) or not re.fullmatch(r'[a-z_]{1,40}', state):
                    raise ValueError('invalid_release_state')
                error = saved.get('error')
                if error is not None and (not isinstance(error, str) or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_.:-]{0,127}', error)):
                    error = 'deployment_error'
                latest.update(state=state, error=error, server_confirmed=state == 'confirmed')
                if isinstance(saved.get('updated_at'), (int, float)):
                    latest['updated_at'] = saved['updated_at']
            return value
        except (ValueError, TypeError, OSError):
            value.update(state='unavailable', error='invalid_local_publication_status')
            return value
