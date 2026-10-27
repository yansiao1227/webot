"""Owned attachment files; only a trusted backend can materialize bytes."""
import base64
import hashlib
import threading
from pathlib import Path

from .errors import ApiError

MAX_RESOURCE_BYTES = 64 * 1024 * 1024
CHUNK_BYTES = 256 * 1024


class Resources:
    def __init__(self, store, backend):
        self.store, self.backend = store, backend
        self.root = store.path.parent / 'resources'
        self.root.mkdir(exist_ok=True)
        self.lock = threading.RLock()
        self.workers = {}
        with store.transaction():
            store.db.execute('''CREATE TABLE IF NOT EXISTS resources(
                id TEXT PRIMARY KEY,account TEXT NOT NULL,event_id TEXT NOT NULL,
                state TEXT NOT NULL,created REAL NOT NULL,size INTEGER,sha256 TEXT,error TEXT)''')
            store.db.execute("UPDATE resources SET state='failed',error='获取过程被中断' WHERE state='running'")

    def event(self, event_id, account):
        with self.store.condition:
            prefix = self.store.database_id + ':'
            if isinstance(event_id, str) and event_id.startswith(prefix) and event_id[len(prefix):].isdigit():
                row = self.store.db.execute('SELECT body,created FROM events WHERE account=? AND seq=?',
                                            (account, int(event_id[len(prefix):]))).fetchone()
                if row:
                    import json
                    return {**json.loads(row['body']), 'captured_at': row['created'],
                            'account': account, 'event_id': event_id}
        raise ApiError(404, 'event_not_found', '原消息不存在或已过期')

    def acquire(self, event_id, resource_id, account):
        event = self.event(event_id, account)
        if event.get('message_type') not in {'image', 'voice', 'video', 'file', 'sticker', 'card'}:
            raise ApiError(400, 'not_attachment', '目标不是已验证的附件类型')
        if event.get('payload', {}).get('resource_id') != resource_id:
            raise ApiError(403, 'resource_mismatch', '资源不属于原消息')
        key = hashlib.sha256((account + '\0' + event_id + '\0' + resource_id).encode()).hexdigest()
        with self.lock, self.store.transaction():
            row = self.store.db.execute('SELECT * FROM resources WHERE id=?', (key,)).fetchone()
            if not row or row['state'] == 'failed':
                if len(self.workers) >= 2:
                    raise ApiError(429, 'resource_busy', '附件获取并发已满')
                if row:
                    self.store.db.execute("UPDATE resources SET state='running',error=NULL,size=NULL,sha256=NULL WHERE id=?", (key,))
                else:
                    self.store.db.execute('INSERT INTO resources VALUES(?,?,?,?,?,NULL,NULL,NULL)',
                                          (key, account, event_id, 'running', event['captured_at']))
                worker = threading.Thread(target=self._fetch, args=(key, event), daemon=True)
                self.workers[key] = worker
                worker.start()
        return self.status(key, account)

    def _fetch(self, key, event):
        try:
            provider = getattr(self.backend, 'fetch_resource', None)
            if provider is None:
                raise ApiError(503, 'not_implemented', '当前原生版本尚未验证资源获取入口')
            data = provider(event)
            if not isinstance(data, bytes) or not data or len(data) > MAX_RESOURCE_BYTES:
                raise ValueError('资源为空或超过64MiB')
            temp = self.root / (key + '.tmp')
            temp.write_bytes(data)
            temp.replace(self.root / key)
            with self.store.transaction():
                self.store.db.execute("UPDATE resources SET state='ready',size=?,sha256=? WHERE id=?",
                                      (len(data), hashlib.sha256(data).hexdigest(), key))
        except Exception as error:
            with self.store.transaction():
                self.store.db.execute("UPDATE resources SET state='failed',error=? WHERE id=?",
                                      (getattr(error, 'code', None) or type(error).__name__, key))
        finally:
            with self.lock:
                self.workers.pop(key, None)

    def status(self, key, account, offset=None):
        if not isinstance(key, str) or len(key) != 64 or any(c not in '0123456789abcdef' for c in key):
            raise ApiError(400, 'invalid_resource', '资源标识无效')
        with self.lock, self.store.transaction():
            row = self.store.db.execute('SELECT * FROM resources WHERE id=? AND account=?', (key, account)).fetchone()
            if not row:
                raise ApiError(404, 'resource_not_found', '资源不存在')
            value = dict(row)
            if value['created'] < self.store.clock() - self.store.retention and value['state'] != 'running':
                (self.root / key).unlink(missing_ok=True)
                self.store.db.execute("UPDATE resources SET state='expired' WHERE id=?", (key,))
                value['state'] = 'expired'
            if offset is not None:
                if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
                    raise ApiError(400, 'invalid_offset', '资源偏移无效')
                if value['state'] != 'ready':
                    raise ApiError(409, 'resource_not_ready', '资源尚不可读取')
                if offset > value['size']:
                    raise ApiError(400, 'invalid_offset', '偏移超过资源长度')
                with (self.root / key).open('rb') as stream:
                    stream.seek(offset)
                    data = stream.read(CHUNK_BYTES)
                value.update(data=base64.b64encode(data).decode(), offset=offset,
                             next_offset=offset + len(data), eof=offset + len(data) == value['size'])
            return value

    def prune(self):
        with self.lock, self.store.condition:
            keys = [r[0] for r in self.store.db.execute(
                "SELECT id FROM resources WHERE created<? AND state!='running'",
                (self.store.clock() - self.store.retention,))]
        for key in keys:
            with self.store.condition:
                account = self.store.db.execute('SELECT account FROM resources WHERE id=?', (key,)).fetchone()[0]
            self.status(key, account)

        prune = getattr(self.backend, 'prune_resources', None)
        if prune:
            prune(self.store.retention, self.store.clock())

    def close(self):
        for worker in list(self.workers.values()):
            worker.join()
