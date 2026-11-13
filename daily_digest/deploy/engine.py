"""冻结日报快照并通过固定 SSH 目标进行可核查的两阶段发布。"""
from contextlib import closing, contextmanager
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import tarfile
import tempfile
import time
import urllib.request

from daily_digest.content import content_hash, validate_edition
from tool.settings import path_setting

ROOT = Path(__file__).resolve().parents[2]
MAX_SNAPSHOT_BYTES = 192 * 1024 * 1024
RELEASE = re.compile(r'd[0-9]{8}-[a-f0-9]{32}')
WEB_FILES = ('dist/server/index.js', 'scripts/serve.mjs', 'scripts/sqlite-storage.mjs',
             'migrations/0001_editions.sql', 'migrations/0002_media.sql')
DEPLOY_FILES = ('Dockerfile', 'release.sh', 'verify-release.mjs')


class DeploymentError(RuntimeError):
    """仅返回稳定错误代码，不将 SSH 输出或凭据写入业务日志。"""


@dataclass(frozen=True)
class Release:
    release_id: str
    directory: Path
    bundle: Path
    manifest: dict


def atomic_json(path, value):
    temporary = path.with_suffix(path.suffix + '.tmp')
    with temporary.open('w', encoding='utf-8') as file:
        json.dump(value, file, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)
        file.flush()
        os.fsync(file.fileno())
    os.replace(temporary, path)


def digest_file(path):
    digest = hashlib.sha256()
    with path.open('rb') as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


@contextmanager
def deployment_lock(directory):
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / 'deploy.lock').open('a+b') as file:
        if file.tell() == 0:
            file.write(b'0'); file.flush()
        file.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise DeploymentError('deployment_busy') from None
        yield


def snapshot_manifest(database):
    """读取冻结文件；包括所有历史与媒体，不只校验当天的一行。"""
    if database.stat().st_size > MAX_SNAPSHOT_BYTES:
        raise DeploymentError('snapshot_capacity_exceeded')
    with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)) as db:
        if db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise DeploymentError('snapshot_integrity_failed')
        editions, bound_media = {}, set()
        for date, saved_hash, body in db.execute('SELECT date,content_hash,body FROM editions ORDER BY date'):
            value = validate_edition(json.loads(body))
            if value['date'] != date or value['content_hash'] != saved_hash:
                raise DeploymentError('snapshot_edition_hash_mismatch')
            editions[date] = saved_hash
            bound_media.update(source['image']['asset_id'] for source in value['sources']
                               if source.get('image', {}).get('asset_id'))
        if not editions:
            raise DeploymentError('snapshot_empty')
        media = []
        for digest, body, kind in db.execute('SELECT sha256,body,content_type FROM media ORDER BY sha256'):
            data = bytes(body)
            if (not re.fullmatch(r'[a-f0-9]{64}', digest) or kind != 'image/webp' or
                    not 30 <= len(data) <= 512 * 1024 or data[:4] != b'RIFF' or data[8:12] != b'WEBP' or
                    int.from_bytes(data[4:8], 'little') + 8 != len(data) or
                    hashlib.sha256(data).hexdigest() != digest):
                raise DeploymentError('snapshot_media_invalid')
            media.append({'sha256': digest, 'size': len(data)})
        if bound_media - {item['sha256'] for item in media}:
            raise DeploymentError('snapshot_media_missing')
    return {'editions': editions, 'media': media, 'latest_date': max(editions),
            'latest_hash': editions[max(editions)]}


class SnapshotDeployer:
    def __init__(self, host, user='root', identity_file=None, database=None, state_dir=None,
                 replace_service='daily-digest', run_command=None, read_url=None, root=ROOT,
                 build_command=None):
        if not isinstance(host, str) or not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9.-]*', host):
            raise ValueError('invalid_deploy_host')
        if not re.fullmatch(r'[a-zA-Z_][a-zA-Z0-9_-]*', user):
            raise ValueError('invalid_deploy_user')
        if not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_.-]*', replace_service):
            raise ValueError('invalid_replace_service')
        self.root = Path(root).resolve()
        self.host, self.user, self.site_url = host, user, 'http://' + host
        self.identity_file = Path(identity_file or Path.home() / '.ssh/webot_daily_digest_ed25519').expanduser().resolve()
        self.database = Path(database or path_setting('DAILY_DIGEST_PREVIEW_DB', 'daily_digest/web/state/preview.sqlite')).resolve()
        self.state_dir = Path(state_dir or self.root / 'daily_digest/state/deploy').resolve()
        self.replace_service = replace_service
        self.run_command = run_command or subprocess.run
        self.build_command = build_command or subprocess.run
        self.read_url = read_url or self._read_url

    @classmethod
    def from_environment(cls, **overrides):
        # 导入 tool.settings 时已加载根.env；只处理发布相关字段。
        values = {'host': os.getenv('DAILY_DIGEST_DEPLOY_HOST', ''),
                  'user': os.getenv('DAILY_DIGEST_DEPLOY_USER', 'root'),
                  'identity_file': path_setting('DAILY_DIGEST_DEPLOY_IDENTITY_FILE',
                                               str(Path.home() / '.ssh/webot_daily_digest_ed25519'))}
        return cls(**{**values, **{k: v for k, v in overrides.items() if v is not None}})

    @staticmethod
    def _read_url(url):
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                return None
        with urllib.request.build_opener(NoRedirect()).open(
                urllib.request.Request(url, headers={'User-Agent': 'DailyObserver/1.0'}), timeout=20) as response:
            raw = response.read(1048577)
            if response.status != 200 or len(raw) > 1048576:
                raise DeploymentError('public_response_invalid')
            return raw

    def _json(self, route):
        return json.loads(self.read_url(self.site_url + route))

    def _record(self, release, state, error=None):
        path = release.directory / 'state.json'
        previous = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
        if previous.get('target') not in (None, [self.host, self.user]):
            raise DeploymentError('release_target_changed')
        value = {**previous, 'release_id': release.release_id, 'target': [self.host, self.user],
                 'state': state, 'updated_at': time.time(), 'error': error}
        if state == 'prepared':
            value['bundle_sha256'] = digest_file(release.bundle)
        atomic_json(path, value)
        return value

    def load_release(self, release_id):
        if not isinstance(release_id, str) or not RELEASE.fullmatch(release_id):
            raise ValueError('invalid_release_id')
        directory = self.state_dir / release_id
        manifest = json.loads((directory / 'manifest.json').read_text(encoding='utf-8'))
        if manifest.get('release_id') != release_id:
            raise DeploymentError('release_manifest_mismatch')
        state_file = directory / 'state.json'
        if state_file.exists() and json.loads(state_file.read_text(encoding='utf-8')).get('target') != [self.host, self.user]:
            raise DeploymentError('release_target_changed')
        return Release(release_id, directory, self.state_dir / (release_id + '.tar.gz'), manifest)

    def prepare(self):
        with deployment_lock(self.state_dir):
            self._ensure_web_build()
            if not self.database.is_file():
                raise DeploymentError('local_database_missing')
            with tempfile.TemporaryDirectory(prefix='prepare-', dir=self.state_dir) as temporary:
                staging = Path(temporary)
                snapshot = staging / 'editions.sqlite'
                with closing(sqlite3.connect(self.database.as_uri() + '?mode=ro', uri=True)) as source:
                    with closing(sqlite3.connect(snapshot)) as target:
                        source.backup(target)
                        target.execute('PRAGMA journal_mode=DELETE')
                logical = snapshot_manifest(snapshot)
                files = {}
                for relative in WEB_FILES:
                    source = self.root / 'daily_digest/web' / relative
                    target = staging / 'web' / relative
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source, target)
                    files['web/' + relative] = digest_file(target)
                for relative in DEPLOY_FILES:
                    source = self.root / 'daily_digest/deploy' / relative
                    # Windows checkout line endings must not become Linux shell tokens.
                    (staging / relative).write_bytes(source.read_bytes().replace(b'\r\n', b'\n'))
                    files[relative] = digest_file(staging / relative)
                artifact_hash = hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()
                core = {**logical, 'artifact_hash': artifact_hash}
                identifier = hashlib.sha256(json.dumps(core, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
                release_id = 'd' + logical['latest_date'].replace('-', '') + '-' + identifier[:32]
                destination = self.state_dir / release_id
                if destination.exists():
                    release = self.load_release(release_id)
                    self._check_frozen(release)
                    return release
                manifest = {**core, 'schema_version': 1, 'release_id': release_id, 'files': files,
                            'snapshot_sha256': digest_file(snapshot), 'snapshot_size': snapshot.stat().st_size,
                            'tmpfs_mib': 256, 'max_snapshot_bytes': MAX_SNAPSHOT_BYTES}
                atomic_json(staging / 'manifest.json', manifest)
                os.replace(staging, destination)
                bundle = self.state_dir / (release_id + '.tar.gz')
                with tarfile.open(bundle, 'w:gz') as archive:
                    for entry in sorted(destination.iterdir()):
                        archive.add(entry, arcname=release_id + '/' + entry.name)
                release = Release(release_id, destination, bundle, manifest)
                self._record(release, 'prepared')
                return release

    def _web_source_hash(self):
        web = self.root / 'daily_digest/web'
        if not (web / 'package.json').is_file():
            raise DeploymentError('web_source_missing')
        ignored = {'node_modules', 'dist', 'build', 'state', '.git', '.openai', '.sites-runtime',
                   '.wrangler', '.cache', '__pycache__', '.vite', '.idea', '.vscode'}
        values = {}
        for current, directories, files in os.walk(web):
            directories[:] = sorted(name for name in directories if name not in ignored
                                     and not (Path(current) / name).is_symlink())
            for name in sorted(files):
                file = Path(current) / name
                lower = name.lower()
                if (file.is_symlink() or lower.startswith('.env') or lower in ('credentials.json', 'secrets.json') or
                        file.suffix.lower() in ('.pem', '.key', '.p12', '.pfx', '.log', '.md', '.sqlite', '.sqlite3', '.db')):
                    continue
                values[file.relative_to(web).as_posix()] = digest_file(file)
        return hashlib.sha256(json.dumps(values, sort_keys=True, separators=(',', ':')).encode()).hexdigest()

    def _ensure_web_build(self):
        """复用持久验证证据；只有源码或Worker产物变化时才重新检查/构建。"""
        build_state = self.root / 'daily_digest/state'
        with deployment_lock(build_state / 'web-build-lock'):
            self._ensure_web_build_locked(build_state / 'web-build.json')

    def _ensure_web_build_locked(self, evidence_path):
        web = self.root / 'daily_digest/web'
        output = web / 'dist/server/index.js'
        source_hash = self._web_source_hash()
        try:
            evidence = json.loads(evidence_path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            evidence = {}
        if (evidence.get('schema_version') == 1 and evidence.get('source_hash') == source_hash and
                output.is_file() and evidence.get('artifact_hash') == digest_file(output)):
            return
        npm = shutil.which('npm')
        if not npm:
            raise DeploymentError('npm_not_available')
        # No account, SSH, model, site or MCP credentials are forwarded to build tools.
        allowed = {'PATH', 'PATHEXT', 'COMSPEC', 'SYSTEMROOT', 'WINDIR', 'TEMP', 'TMP', 'TMPDIR',
                   'USERPROFILE', 'HOME', 'APPDATA', 'LOCALAPPDATA', 'LANG', 'LC_ALL', 'NUMBER_OF_PROCESSORS'}
        environment = {key: value for key, value in os.environ.items() if key.upper() in allowed}
        environment['CI'] = '1'
        try:
            for command, timeout in [('check', 120), ('test', 180), ('build', 120)]:
                self.build_command([npm, 'run', command], cwd=web, env=environment, timeout=timeout,
                                   check=True, capture_output=True, text=True)
        except (subprocess.SubprocessError, OSError):
            raise DeploymentError('web_check_or_build_failed') from None
        if not output.is_file() or self._web_source_hash() != source_hash:
            raise DeploymentError('web_source_changed_during_build')
        atomic_json(evidence_path, {'schema_version': 1, 'source_hash': source_hash,
                                   'artifact_hash': digest_file(output), 'verified_at': time.time(),
                                   'commands': ['npm run check', 'npm test', 'npm run build']})

    def _check_frozen(self, release):
        if (digest_file(release.directory / 'editions.sqlite') != release.manifest['snapshot_sha256'] or
                any(digest_file(release.directory / name) != expected for name, expected in release.manifest['files'].items())):
            raise DeploymentError('frozen_release_changed')
        if not release.bundle.is_file():
            raise DeploymentError('release_bundle_missing')
        state = json.loads((release.directory / 'state.json').read_text(encoding='utf-8'))
        if state.get('bundle_sha256') != digest_file(release.bundle):
            raise DeploymentError('release_bundle_changed')

    def _options(self):
        return ['-o', 'BatchMode=yes', '-o', 'IdentitiesOnly=yes', '-o', 'StrictHostKeyChecking=yes',
                '-o', 'ConnectTimeout=10', '-o', 'ServerAliveInterval=10', '-o', 'ServerAliveCountMax=2',
                '-i', str(self.identity_file)]

    def _run(self, command, timeout):
        try:
            result = self.run_command(command, check=True, capture_output=True, text=True, timeout=timeout)
            return result.stdout or ''
        except (subprocess.SubprocessError, OSError):
            raise DeploymentError('ssh_operation_unconfirmed') from None

    def _remote(self, release, action):
        if not RELEASE.fullmatch(release.release_id):
            raise ValueError('invalid_release_id')
        if action not in ('status', 'activate', 'commit', 'rollback'):
            raise ValueError('invalid_remote_action')
        path = '/opt/daily-digest/releases/' + release.release_id
        operation = self._activation_command(release) if action == 'activate' else f'sh {path}/release.sh {action} {self.replace_service}'
        command = f'if [ -f {path}/release.sh ]; then {operation}; else printf \'{{"phase":"absent"}}\\n\'; fi'
        output = self._run(['ssh', *self._options(), self.user + '@' + self.host, command], 300 if action == 'activate' else 90)
        try:
            return json.loads(output.strip().splitlines()[-1])
        except (IndexError, ValueError):
            raise DeploymentError('remote_status_invalid') from None

    def _activation_command(self, release):
        path = '/opt/daily-digest/releases/' + release.release_id
        # The controller survives a disconnected SSH channel; the foreground caller
        # still waits for the same release and returns only its final status.
        return (f'(nohup sh {path}/release.sh activate {self.replace_service} '
                f'>{path}/activate.log 2>&1 </dev/null & activate_pid=$!; '
                f'wait "$activate_pid" && sh {path}/release.sh status {self.replace_service})')

    def _check_history(self, release):
        if self.replace_service != 'daily-digest':
            return
        cursor, seen = None, set()
        while True:
            page = self._json('/api/editions?limit=50' + ('&cursor=' + cursor if cursor else ''))
            for item in page['items']:
                if release.manifest['editions'].get(item['date']) != item['content_hash']:
                    raise DeploymentError('public_history_missing_or_conflicting')
            cursor = page['next_cursor']
            if not cursor:
                break
            if cursor in seen or len(seen) > 200 or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', cursor):
                raise DeploymentError('public_history_cursor_invalid')
            seen.add(cursor)

    def _public_verify(self, release):
        deadline = time.monotonic() + 120
        def read(route):
            if time.monotonic() > deadline:
                raise DeploymentError('public_verification_deadline')
            return self.read_url(self.site_url + route)
        for date, expected in release.manifest['editions'].items():
            value = json.loads(read('/api/editions/' + date))
            if value.get('date') != date or value.get('content_hash') != expected or content_hash(value) != expected:
                raise DeploymentError('public_edition_mismatch')
        latest = json.loads(read('/api/editions/latest'))
        if (latest.get('date', '') < release.manifest['latest_date'] or
                content_hash(latest) != latest.get('content_hash') or
                (latest.get('date') in release.manifest['editions'] and
                 latest.get('content_hash') != release.manifest['editions'][latest['date']])):
            raise DeploymentError('public_latest_stale')
        for media in release.manifest['media']:
            raw = read('/media/' + media['sha256'])
            if len(raw) != media['size'] or hashlib.sha256(raw).hexdigest() != media['sha256']:
                raise DeploymentError('public_media_mismatch')
        for route in ('/', '/archive', '/daily/' + release.manifest['latest_date']):
            if not read(route):
                raise DeploymentError('public_page_missing')

    def _result(self, release):
        return {'state': 'confirmed', 'release_id': release.release_id, 'date': release.manifest['latest_date'],
                'content_hash': release.manifest['latest_hash'], 'site_url': self.site_url,
                'url': self.site_url + '/daily/' + release.manifest['latest_date']}

    def _verify(self, release):
        remote = self._remote(release, 'status')
        phase = remote.get('phase')
        if phase not in ('activating', 'activated', 'confirmed'):
            self._record(release, 'unknown', 'remote_release_not_activated')
            raise DeploymentError('remote_release_not_activated')
        if phase in ('activating', 'activated') and remote.get('current_release') != release.release_id:
            self._record(release, 'unknown', 'remote_release_not_current')
            raise DeploymentError('remote_release_not_current')
        self._record(release, 'awaiting_public')
        try:
            self._public_verify(release)
        except Exception:
            if phase in ('activating', 'activated'):
                try:
                    self._remote(release, 'rollback')
                    self._record(release, 'rolled_back', 'public_verification_failed')
                except DeploymentError:
                    self._record(release, 'unknown', 'rollback_unconfirmed')
            else:
                self._record(release, 'unknown', 'public_verification_failed')
            raise DeploymentError('public_verification_failed') from None
        if phase in ('activating', 'activated'):
            committed = self._remote(release, 'commit')
            if committed.get('phase') != 'confirmed':
                raise DeploymentError('remote_commit_unconfirmed')
        self._record(release, 'confirmed')
        return self._result(release)

    def verify(self, release):
        if isinstance(release, str):
            release = self.load_release(release)
        with deployment_lock(self.state_dir):
            return self._verify(release)

    def deploy(self, release=None):
        release = release or self.prepare()
        if isinstance(release, str):
            release = self.load_release(release)
        with deployment_lock(self.state_dir):
            self._check_frozen(release)
            remote = self._remote(release, 'status')
            if remote.get('phase') in ('activated', 'confirmed') or (remote.get('phase') == 'activating' and remote.get('current_release') == release.release_id):
                return self._verify(release)
            if remote.get('phase') not in ('absent', 'prepared'):
                self._record(release, 'unknown', 'remote_release_needs_review')
                raise DeploymentError('remote_release_needs_review')
            self._check_history(release)
            self._record(release, 'uploading')
            remote_bundle = '/tmp/daily-digest-' + release.release_id + '.tar.gz'
            try:
                self._run(['scp', *self._options(), str(release.bundle), self.user + '@' + self.host + ':' + remote_bundle], 120)
                self._record(release, 'uploaded')
                archive_hash = digest_file(release.bundle)
                command = (f'printf \'{archive_hash}  {remote_bundle}\\n\' | sha256sum -c - >/dev/null && '
                           f'mkdir -p /opt/daily-digest/releases && tar -xzf {remote_bundle} -C /opt/daily-digest/releases && '
                           + self._activation_command(release))
                self._record(release, 'switching')
                self._run(['ssh', *self._options(), self.user + '@' + self.host, command], 300)
            except DeploymentError:
                self._record(release, 'unknown', 'ssh_operation_unconfirmed')
                # 丢失响应只查原release，绝不生成新release重切。
                try:
                    return self._verify(release)
                except DeploymentError:
                    raise DeploymentError('deployment_unconfirmed') from None
            return self._verify(release)

    def status(self, release_id=None):
        if release_id is None:
            states = []
            for path in sorted(self.state_dir.glob('d*/state.json')):
                states.append(json.loads(path.read_text(encoding='utf-8')))
            return {'releases': states}
        release = self.load_release(release_id)
        path = release.directory / 'state.json'
        return {'local': json.loads(path.read_text(encoding='utf-8')), 'remote': self._remote(release, 'status')}

    def rollback(self, release_id):
        release = self.load_release(release_id)
        with deployment_lock(self.state_dir):
            response = self._remote(release, 'rollback')
            if response.get('phase') != 'rolled_back':
                raise DeploymentError('rollback_unconfirmed')
            self._record(release, 'rolled_back')
            return {'state': 'rolled_back', 'release_id': release_id, 'site_url': self.site_url}
