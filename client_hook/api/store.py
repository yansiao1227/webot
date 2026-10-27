"""事务化消息、消费批次和发送意图；锁不跨越 HTTP 或原生调用。"""
from __future__ import annotations

import json
import hashlib
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from .errors import ApiError


def encode(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


class Store:
    def __init__(self, path, retention=7 * 86400, max_bytes=1024 ** 3, clock=time.time):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.clock, self.retention, self.max_bytes = clock, retention, max_bytes
        self.condition = threading.Condition(threading.RLock())
        self.readers = set()
        self.closed = False
        self.db = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS events(
                seq INTEGER PRIMARY KEY AUTOINCREMENT, account TEXT NOT NULL,
                created REAL NOT NULL, body TEXT NOT NULL, bytes INTEGER NOT NULL);
            CREATE INDEX IF NOT EXISTS events_account ON events(account,seq);
            CREATE TABLE IF NOT EXISTS consumers(
                id TEXT PRIMARY KEY, account TEXT NOT NULL, position INTEGER NOT NULL,
                token TEXT, batch TEXT, batch_end INTEGER, last_ack TEXT,
                epoch INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS gaps(
                id INTEGER PRIMARY KEY, account TEXT NOT NULL, through_seq INTEGER NOT NULL,
                reason TEXT NOT NULL, created REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS operations(
                id TEXT PRIMARY KEY, account TEXT NOT NULL, params TEXT NOT NULL,
                state TEXT NOT NULL, result TEXT, created REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS native_outgoing_facts(
                digest TEXT PRIMARY KEY,seq INTEGER NOT NULL);
        """)
        with self.transaction():
            self.db.execute("INSERT OR IGNORE INTO meta VALUES('database_id',?)", (uuid.uuid4().hex,))
            self.db.execute("INSERT OR IGNORE INTO meta SELECT 'event_bytes',coalesce(sum(bytes),0) FROM events")
            self.db.execute("INSERT OR IGNORE INTO meta VALUES('capture',?)", (encode({"state": "not_started"}),))
            previous = self.get_meta("capture")
            self.previous_capture = previous
            self.db.execute("UPDATE operations SET state='unknown' WHERE state='running'")
        self.database_id = self.db.execute("SELECT value FROM meta WHERE key='database_id'").fetchone()[0]
        if previous.get('state') not in ('not_started', 'stopped'):
            gaps = self.get_meta('coverage_gaps', [])
            self.set_meta('coverage_gaps', (gaps + [{'reason': 'previous_capture_not_cleanly_stopped',
                          'previous_capture': previous, 'detected_at': self.clock(),
                          'lost_message_count': None}])[-20:])

    @contextmanager
    def transaction(self):
        with self.condition:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                yield
                self.db.execute("COMMIT")
            except BaseException:
                self.db.execute("ROLLBACK")
                raise

    def get_meta(self, key, default=None):
        with self.condition:
            row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
            return json.loads(row[0]) if row else default

    def set_meta(self, key, value):
        with self.transaction():
            self.db.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", (key, encode(value)))
        with self.condition:
            self.condition.notify_all()

    def capture(self, state, **details):
        self.set_meta("capture", {"state": state, "updated_at": self.clock(), **details})

    def _cursor(self, consumer, position):
        return f"{self.database_id}:{consumer['id']}:{consumer['epoch']}:{position}"

    def _consumer(self, name, account):
        row = self.db.execute("SELECT * FROM consumers WHERE id=?", (name,)).fetchone()
        if row is None:
            raise ApiError(404, "consumer_not_found", "消费者不存在")
        if row["account"] != account:
            raise ApiError(409, "account_changed", "消费者与当前账号不符，请新建或显式重置")
        return row

    def _latest(self):
        return self.db.execute("SELECT coalesce((SELECT seq FROM sqlite_sequence WHERE name='events'),0)").fetchone()[0]

    def _initial(self, account, start):
        if start == "latest":
            return self._latest()
        first = self.db.execute("SELECT min(seq) FROM events WHERE account=?", (account,)).fetchone()[0]
        return first - 1 if first is not None else self._latest()

    def consumer(self, name, account, start=None):
        with self.transaction():
            if start is not None:
                self.db.execute("INSERT OR IGNORE INTO consumers(id,account,position) VALUES(?,?,?)",
                                (name, account, self._initial(account, start)))
            row = self._consumer(name, account)
            backlog = self.db.execute("SELECT count(*) FROM events WHERE account=? AND seq>?",
                                      (account, row["position"])).fetchone()[0]
            gap = self.db.execute("SELECT max(through_seq) FROM gaps WHERE account=?", (account,)).fetchone()[0]
            return {"consumer_id": name, "account": account, "cursor": self._cursor(row, row["position"]),
                    "pending": bool(row["token"]), "backlog": backlog,
                    "cursor_expired": gap is not None and row["position"] < gap}

    def append(self, account, event):
        # 完整事件是自有 JSON，不是指向客户端对象的指针。
        body = encode(event)
        with self.transaction():
            fact = None
            native_identity = None
            if (event.get('direction') == 'outgoing' and event.get('native_client_id_source') ==
                    'weixin41930_record_client278_v1' and event.get('native_client_id')):
                native_identity = event['native_client_id']  # 保留已有的去重摘要。
            elif (event.get('direction') == 'outgoing' and event.get('native_record_metadata_source') ==
                    'weixin41930_record_identity_v2' and event.get('native_sort_seq')):
                native_identity = ['record_sort_seq',event['native_sort_seq'],event['native_created_at']]
            if native_identity:
                fact = hashlib.sha256(encode([account, event.get('conversation_id'), native_identity,
                    event.get('message_id'), event.get('message_type'), event.get('payload')]).encode()).hexdigest()
                old = self.db.execute('SELECT seq FROM native_outgoing_facts WHERE digest=?', (fact,)).fetchone()
                if old:
                    return f'{self.database_id}:{old[0]}'
            cur = self.db.execute("INSERT INTO events(account,created,body,bytes) VALUES(?,?,?,?)",
                                  (account, self.clock(), body, len(body.encode("utf-8"))))
            seq = cur.lastrowid
            if fact:
                self.db.execute('INSERT INTO native_outgoing_facts VALUES(?,?)', (fact, seq))
            self.db.execute("UPDATE meta SET value=CAST(value AS INTEGER)+? WHERE key='event_bytes'", (len(body.encode('utf-8')),))
            self._prune()
        with self.condition:
            self.condition.notify_all()
        return f"{self.database_id}:{seq}"

    def _prune(self):
        total = int(self.db.execute("SELECT value FROM meta WHERE key='event_bytes'").fetchone()[0])
        through = {}
        # 仅遍历实际删除的前缀，使正常追加保持 O(1)，
        # 避免将全部保留消息日志复制到 Python 中。
        while True:
            row = self.db.execute("SELECT seq,account,created,bytes FROM events ORDER BY seq LIMIT 1").fetchone()
            if row is None:
                break
            if row["created"] >= self.clock() - self.retention and total <= self.max_bytes:
                break
            if self.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='resources'").fetchone():
                if self.db.execute("SELECT 1 FROM resources WHERE event_id=? AND state='running'",
                                   (f'{self.database_id}:{row["seq"]}',)).fetchone():
                    break
            total -= row["bytes"]
            through[row["account"]] = row["seq"]
            self.db.execute("DELETE FROM events WHERE seq=?", (row["seq"],))
            self.db.execute('DELETE FROM native_outgoing_facts WHERE seq=?', (row['seq'],))
        self.db.execute("UPDATE meta SET value=? WHERE key='event_bytes'", (str(total),))
        for account, seq in through.items():
            self.db.execute("INSERT INTO gaps(account,through_seq,reason,created) VALUES(?,?,?,?)",
                            (account, seq, "retention_or_capacity", self.clock()))
        self.db.execute("DELETE FROM operations WHERE created<? AND state IN ('accepted','failed')",
                        (self.clock() - self.retention,))
        if (self.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='native_resources'").fetchone() and
                self.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='resources'").fetchone()):
            self.db.execute('''DELETE FROM native_resources WHERE created<? AND NOT EXISTS
                (SELECT 1 FROM resources r JOIN events e ON r.event_id=? || ':' || CAST(e.seq AS TEXT)
                 WHERE r.state='running' AND json_extract(e.body,'$.payload.resource_id')=native_resources.id)''',
                 (self.clock() - self.retention, self.database_id))
        # 每个账号仅保留最近的删除边界。
        self.db.execute("DELETE FROM gaps WHERE id NOT IN (SELECT max(id) FROM gaps GROUP BY account)")

    def prune(self):
        with self.transaction():
            self._prune()
        with self.condition:
            self.db.execute("PRAGMA wal_checkpoint(PASSIVE)")
            self.condition.notify_all()

    def read(self, name, account, timeout=30, limit=100, cursor=None):
        deadline = time.monotonic() + timeout
        with self.condition:
            if name in self.readers:
                raise ApiError(409, "concurrent_read", "同一消费者已有等待请求")
            self.readers.add(name)
            epoch = None
            try:
                while True:
                    if self.closed:
                        raise ApiError(503, "service_stopping", "服务正在退出")
                    with self.transaction():
                        row = self._consumer(name, account)
                        if epoch is None:
                            epoch = row['epoch']
                        elif epoch != row['epoch']:
                            raise ApiError(409, 'consumer_reset', '等待期间消费者已被重置，请重新获取')
                        capture = self.get_meta("capture", {})
                        if capture.get("account") and capture["account"] != account:
                            raise ApiError(409, "account_changed", "当前采集账号已变化")
                        gaps = self.db.execute("SELECT * FROM gaps WHERE account=? AND through_seq>?",
                                               (account, row["position"])).fetchall()
                        if gaps:
                            raise ApiError(410, "cursor_expired", "未消费消息已过期，需显式重置",
                                           gaps=[dict(g) for g in gaps],
                                           earliest=self._initial(account, "earliest"), latest=self._latest())
                        if cursor and cursor != self._cursor(row, row["position"]):
                            raise ApiError(409, "stale_cursor", "游标不是当前已确认位置")
                        if row["token"]:
                            batch = json.loads(row["batch"])
                            batch["capture"] = capture
                            return batch
                        events = self.db.execute("SELECT * FROM events WHERE account=? AND seq>? ORDER BY seq LIMIT ?",
                                                 (account, row["position"], limit + 1)).fetchall()
                        if events:
                            selected = events[:limit]
                            payload = []
                            for item in selected:
                                value = json.loads(item["body"])
                                value.update(event_id=f"{self.database_id}:{item['seq']}", account=account,
                                             captured_at=item["created"])
                                payload.append(value)
                            end = selected[-1]["seq"]
                            batch = {"events": payload, "next_cursor": self._cursor(row, end),
                                     "ack_token": uuid.uuid4().hex, "has_more": len(events) > limit}
                            self.db.execute("UPDATE consumers SET token=?,batch=?,batch_end=? WHERE id=?",
                                            (batch["ack_token"], encode(batch), end, name))
                            return {**batch, "capture": capture}
                    remaining = deadline - time.monotonic()
                    if remaining <= 0 or capture.get("state") != "running":
                        return {"events": [], "next_cursor": self._cursor(row, row["position"]),
                                "ack_token": None, "has_more": False, "capture": capture}
                    self.condition.wait(remaining)
            finally:
                self.readers.remove(name)

    def ack(self, name, account, token):
        with self.transaction():
            row = self._consumer(name, account)
            gap = self.db.execute("SELECT max(through_seq) FROM gaps WHERE account=?", (account,)).fetchone()[0]
            if gap is not None and row["position"] < gap:
                raise ApiError(410, "cursor_expired", "批次已过期，需显式重置")
            if token != row["last_ack"]:
                if not token or token != row["token"]:
                    raise ApiError(409, "invalid_ack", "确认 token 不属于当前批次")
                self.db.execute("UPDATE consumers SET position=batch_end,last_ack=token,token=NULL,batch=NULL,batch_end=NULL WHERE id=?", (name,))
        return self.consumer(name, account)

    def reset(self, name, account, start):
        with self.transaction():
            row = self.db.execute("SELECT * FROM consumers WHERE id=?", (name,)).fetchone()
            if row is None:
                raise ApiError(404, "consumer_not_found", "消费者不存在")
            position = self._initial(account, start)
            self.db.execute("UPDATE consumers SET account=?,position=?,token=NULL,batch=NULL,batch_end=NULL,last_ack=NULL,epoch=epoch+1 WHERE id=?",
                            (account, position, name))
            self.db.execute("INSERT OR REPLACE INTO meta VALUES(?,?)",
                            ("reset:" + name, encode({"from": row["position"], "to": position,
                                                     "previous_account": row["account"], "time": self.clock()})))
            self.condition.notify_all()
        return self.consumer(name, account)

    def operation(self, request_id):
        with self.condition:
            row = self.db.execute("SELECT * FROM operations WHERE id=?", (request_id,)).fetchone()
            if not row:
                raise ApiError(404, "request_not_found", "发送请求不存在")
            return {"request_id": row["id"], "account": row["account"], "state": row["state"],
                    "delivery_confirmed": False, "result": json.loads(row["result"]) if row["result"] else None}

    def enqueue(self, request_id, account, params, capacity=64):
        body = encode(params)
        with self.transaction():
            old = self.db.execute("SELECT * FROM operations WHERE id=?", (request_id,)).fetchone()
            if old:
                if old["account"] != account or old["params"] != body:
                    raise ApiError(409, "request_id_conflict", "请求 ID 已用于不同账号或内容")
                return False
            count = self.db.execute("SELECT count(*) FROM operations WHERE state IN ('queued','running')").fetchone()[0]
            if count >= capacity:
                raise ApiError(429, "send_queue_full", "发送队列已满")
            self.db.execute("INSERT INTO operations VALUES(?,?,?,'queued',NULL,?)",
                            (request_id, account, body, self.clock()))
            self.condition.notify_all()
            return True

    def next_operation(self):
        with self.transaction():
            row = self.db.execute("SELECT * FROM operations WHERE state='queued' ORDER BY created,id LIMIT 1").fetchone()
            if not row:
                return None
            self.db.execute("UPDATE operations SET state='running' WHERE id=?", (row["id"],))
            return row["id"], row["account"], json.loads(row["params"])

    def finish(self, request_id, state, result):
        with self.transaction():
            self.db.execute("UPDATE operations SET state=?,result=? WHERE id=?", (state, encode(result), request_id))
            self.condition.notify_all()

    def status(self):
        with self.condition:
            return {"database_id": self.database_id, "capture": self.get_meta("capture"),
                    "events": self.db.execute("SELECT count(*) FROM events").fetchone()[0],
                    "event_bytes": self.db.execute("SELECT coalesce(sum(bytes),0) FROM events").fetchone()[0],
                    "max_bytes": self.max_bytes, "retention_seconds": self.retention,
                    "coverage_gaps": self.get_meta('coverage_gaps', []),
                    "capture_before_start": self.previous_capture}

    def close(self):
        with self.condition:
            self.closed = True
            self.condition.notify_all()
            self.db.close()
