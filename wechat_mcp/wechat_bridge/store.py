"""事务 inbox、订阅 outbox 与每个入站事件唯一的回复意图。"""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import threading
import time
import secrets

from .httpio import encode
from .vault import seal, unseal

MAX_EVENTS = 100000
MAX_SUBSCRIPTIONS = 32


class StateLock:
    def __init__(self, directory):
        directory.mkdir(parents=True, exist_ok=True)
        self.file = (directory / 'bridge.lock').open('a+b')
        try:
            if os.fstat(self.file.fileno()).st_size == 0:
                self.file.write(b'0'); self.file.flush()
            self.file.seek(0)
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException:
            self.file.close()
            raise RuntimeError('该桥接状态目录已被占用') from None

    def close(self):
        self.file.close()


class Store:
    def __init__(self, directory, account, simulation):
        directory = Path(directory)
        self.owner_lock = StateLock(directory)
        self.lock = threading.RLock()
        self.changed = threading.Condition(self.lock)
        try:
            if os.name != 'nt':
                directory.chmod(0o700)
            self.db = sqlite3.connect(directory / 'bridge.sqlite3', check_same_thread=False, isolation_level=None)
            self.db.row_factory = sqlite3.Row
            self.db.executescript('''
                PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL; PRAGMA foreign_keys=ON;
                CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS inbox(seq INTEGER PRIMARY KEY AUTOINCREMENT,event_id TEXT UNIQUE NOT NULL,
                    account TEXT NOT NULL,conversation TEXT NOT NULL,event TEXT NOT NULL,created REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS subscriptions(id TEXT PRIMARY KEY,owner TEXT NOT NULL,filters TEXT NOT NULL,
                    url TEXT NOT NULL,secret TEXT NOT NULL,old_secret TEXT,old_until REAL NOT NULL DEFAULT 0,
                    expires REAL NOT NULL,active INTEGER NOT NULL,since_seq INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS deliveries(subscription_id TEXT NOT NULL,event_id TEXT NOT NULL,
                    state TEXT NOT NULL,attempts INTEGER NOT NULL DEFAULT 0,due REAL NOT NULL,error TEXT,
                    PRIMARY KEY(subscription_id,event_id),FOREIGN KEY(subscription_id) REFERENCES subscriptions(id));
                CREATE TABLE IF NOT EXISTS replies(event_id TEXT PRIMARY KEY,request_id TEXT UNIQUE NOT NULL,
                    text TEXT NOT NULL,state TEXT NOT NULL,result TEXT,created REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS reply_parts(event_id TEXT NOT NULL,part_index INTEGER NOT NULL,
                    request_id TEXT UNIQUE NOT NULL,text TEXT NOT NULL,is_final INTEGER NOT NULL,
                    state TEXT NOT NULL,result TEXT,created REAL NOT NULL,PRIMARY KEY(event_id,part_index));
                CREATE TABLE IF NOT EXISTS consumers(id TEXT PRIMARY KEY,position INTEGER NOT NULL,
                    pending TEXT,token TEXT,last_token TEXT);
                CREATE TABLE IF NOT EXISTS rpc_audit(seq INTEGER PRIMARY KEY AUTOINCREMENT,at REAL NOT NULL,
                    method TEXT NOT NULL,success INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS message_index(event_id TEXT PRIMARY KEY,account TEXT NOT NULL,
                    conversation TEXT NOT NULL,message_id TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS message_lookup ON message_index(account,conversation,message_id);
                CREATE TABLE IF NOT EXISTS outgoing_index(event_id TEXT PRIMARY KEY,account TEXT NOT NULL,
                    conversation TEXT NOT NULL,client_id TEXT NOT NULL,message_id TEXT);
                CREATE INDEX IF NOT EXISTS outgoing_lookup ON outgoing_index(account,conversation,client_id);
                CREATE TABLE IF NOT EXISTS outgoing_sort_index(event_id TEXT PRIMARY KEY,account TEXT NOT NULL,
                    conversation TEXT NOT NULL,sort_seq TEXT NOT NULL,message_id TEXT);
                CREATE INDEX IF NOT EXISTS outgoing_sort_lookup ON outgoing_sort_index(account,conversation,sort_seq);
                CREATE TABLE IF NOT EXISTS resource_grants(trigger_id TEXT NOT NULL,target_id TEXT NOT NULL,
                    resource_id TEXT NOT NULL,PRIMARY KEY(trigger_id,target_id,resource_id));
                CREATE TABLE IF NOT EXISTS seen_events(event_id TEXT PRIMARY KEY,digest TEXT NOT NULL);
            ''')
            # 为现有本地数据库身份保留第三个预留槽位。
            from .schedules import initialize
            initialize(self.db)
            identity = json.dumps([account, bool(simulation), False])
            row = self.db.execute("SELECT value FROM meta WHERE key='identity'").fetchone()
            if row and row[0] != identity:
                raise ValueError('状态目录的账号或模拟模式不匹配；请使用独立目录')
            self.db.execute("INSERT OR IGNORE INTO meta VALUES('identity',?)", (identity,))
            self.db.execute("UPDATE deliveries SET state='pending' WHERE state='delivering'")
            for record in self.db.execute('SELECT event_id,account,conversation,event FROM inbox').fetchall():
                value = json.loads(record['event'])
                self.index_outgoing(self.db, value)
                canonical = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)
                self.db.execute('INSERT OR IGNORE INTO seen_events VALUES(?,?)',
                                (record['event_id'], hashlib.sha256(canonical.encode()).hexdigest()))
                mid = value.get('message_id')
                if isinstance(mid, str) and mid:
                    self.db.execute('INSERT OR IGNORE INTO message_index VALUES(?,?,?,?)',
                                    (record['event_id'], record['account'], record['conversation'], mid))
            if os.name != 'nt':
                (directory / 'bridge.sqlite3').chmod(0o600)
        except BaseException:
            if hasattr(self, 'db'):
                self.db.close()
            self.owner_lock.close()
            raise

    @contextmanager
    def tx(self):
        with self.lock:
            self.db.execute('BEGIN IMMEDIATE')
            try:
                yield self.db
                self.db.execute('COMMIT')
            except BaseException:
                self.db.execute('ROLLBACK')
                raise

    def meta(self, key, value=None):
        with self.lock:
            if value is not None:
                self.db.execute('INSERT OR REPLACE INTO meta VALUES(?,?)', (key, json.dumps(value)))
            row = self.db.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()
            return json.loads(row[0]) if row else None

    def audit(self, method, success):
        with self.tx() as db:
            db.execute('INSERT INTO rpc_audit(at,method,success) VALUES(?,?,?)', (time.time(), method[:128], int(success)))
            db.execute('DELETE FROM rpc_audit WHERE seq < (SELECT COALESCE(MAX(seq),0)-1024 FROM rpc_audit)')

    def ingest(self, events, publish=None):
        with self.tx() as db:
            count = db.execute('SELECT COUNT(*) FROM inbox').fetchone()[0]
            for event in events:
                raw = json.dumps(event, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)
                digest = hashlib.sha256(raw.encode()).hexdigest()
                previous = db.execute('SELECT digest FROM seen_events WHERE event_id=?', (event['event_id'],)).fetchone()
                if previous:
                    if previous[0] != digest:
                        raise ValueError('同一 event_id 的事件内容发生变化')
                    continue
                if count >= MAX_EVENTS:
                    raise ValueError('桥接 inbox 已达 100000 条上限，暂停上游确认；请归档后恢复')
                count += 1
                db.execute('INSERT INTO seen_events VALUES(?,?)', (event['event_id'], digest))
                inserted = db.execute('INSERT INTO inbox(event_id,account,conversation,event,created) VALUES(?,?,?,?,?)',
                                      (event['event_id'], event['account'], event['conversation_id'], raw, time.time()))
                self.index_outgoing(db, event)
                mid = event.get('message_id')
                if isinstance(mid, str) and mid and len(mid) <= 128:
                    db.execute('INSERT OR IGNORE INTO message_index VALUES(?,?,?,?)',
                               (event['event_id'], event['account'], event['conversation_id'], mid))
                if publish is not None and event['event_id'] not in publish:
                    continue
                for sub in db.execute('SELECT * FROM subscriptions WHERE active=1 AND expires>? AND since_seq<?',
                                      (time.time(), inserted.lastrowid)).fetchall():
                    if event['conversation_id'] in json.loads(sub['filters'])['conversation_ids']:
                        db.execute('INSERT OR IGNORE INTO deliveries VALUES(?,?,?,0,?,NULL)',
                                   (sub['id'], event['event_id'], 'pending', time.time()))
        with self.changed:
            self.changed.notify_all()

    @staticmethod
    def index_outgoing(db, event):
        sequence=event.get('native_sort_seq')
        if (event.get('direction')=='outgoing' and event.get('sender_id')==event.get('account') and
                event.get('native_record_metadata_source')=='weixin41930_record_identity_v2' and
                isinstance(sequence,str) and 1<=len(sequence)<=20 and sequence.isascii() and sequence.isdigit() and
                0<int(sequence)<1<<64):
            db.execute('INSERT OR IGNORE INTO outgoing_sort_index VALUES(?,?,?,?,?)',
                       (event['event_id'],event['account'],event['conversation_id'],sequence,event.get('message_id')))
        client_id = event.get('native_client_id')
        if (event.get('direction') == 'outgoing' and event.get('sender_id') == event.get('account') and
                event.get('native_client_id_source') == 'weixin41930_record_client278_v1' and
                isinstance(client_id, str) and 1 <= len(client_id) <= 128):
            db.execute('INSERT OR IGNORE INTO outgoing_index VALUES(?,?,?,?,?)',
                       (event['event_id'], event['account'], event['conversation_id'], client_id, event.get('message_id')))

    def resolve_reference(self, trigger, mid):
        with self.lock:
            rows = self.db.execute('''SELECT i.event FROM message_index m JOIN inbox i USING(event_id)
                WHERE m.account=? AND m.conversation=? AND m.message_id=? AND
                i.seq<(SELECT seq FROM inbox WHERE event_id=?)''',
                (trigger['account'], trigger['conversation_id'], mid, trigger['event_id'])).fetchall()
            if len(rows) != 1:
                raise ValueError('引用原消息缺失、已过期或标识不唯一，请重新发送并引用')
            return json.loads(rows[0][0])

    def bind_resource(self, trigger, target, resource):
        with self.tx() as db:
            db.execute('INSERT OR IGNORE INTO resource_grants VALUES(?,?,?)', (trigger, target, resource))

    def check_resource(self, trigger, target, resource):
        with self.lock:
            if not self.db.execute('SELECT 1 FROM resource_grants WHERE trigger_id=? AND target_id=? AND resource_id=?',
                                   (trigger, target, resource)).fetchone():
                raise ValueError('资源未经此引用指令授权')

    def messages(self, consumer, timeout, limit, start, allowed):
        deadline = time.monotonic() + timeout
        while True:
            with self.changed:
                with self.tx() as db:
                    row = db.execute('SELECT * FROM consumers WHERE id=?', (consumer,)).fetchone()
                    if not row:
                        if db.execute('SELECT COUNT(*) FROM consumers').fetchone()[0] >= 128:
                            raise ValueError('消费者数量已达 128 条上限')
                        gap = db.execute("SELECT value FROM meta WHERE key='pruned_through'").fetchone()
                        boundary = int(gap[0]) if gap else 0
                        position = max(boundary, db.execute('SELECT COALESCE(MAX(seq),0) FROM inbox').fetchone()[0]) if start == 'latest' else boundary
                        db.execute('INSERT INTO consumers VALUES(?,?,NULL,NULL,NULL)', (consumer, position))
                        row = db.execute('SELECT * FROM consumers WHERE id=?', (consumer,)).fetchone()
                    if row['pending']:
                        events = [self.event(x) for x in json.loads(row['pending'])]
                        if any(e['conversation_id'] not in allowed for e in events):
                            raise ValueError('待确认批次包含已撤销会话，需管理员处理进度')
                        return {'events': events, 'ack_token': row['token']}
                    gap = db.execute("SELECT value FROM meta WHERE key='pruned_through'").fetchone()
                    if gap and row['position'] < int(gap[0]):
                        raise ValueError('消费者存在超过7天的消息缺口，请显式重置消费者')
                    places = ','.join('?' for _ in allowed)
                    rows = db.execute('SELECT * FROM inbox WHERE seq>? AND conversation IN (' + places + ') ORDER BY seq LIMIT ?',
                                      (row['position'], *allowed, limit)).fetchall()
                    if rows:
                        token = secrets.token_urlsafe(32)
                        db.execute('UPDATE consumers SET pending=?,token=? WHERE id=?',
                                   (json.dumps([r['event_id'] for r in rows]), token, consumer))
                        return {'events': [json.loads(r['event']) for r in rows], 'ack_token': token}
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return {'events': [], 'ack_token': None}
                self.changed.wait(remaining)

    def ack(self, consumer, token):
        with self.tx() as db:
            row = db.execute('SELECT * FROM consumers WHERE id=?', (consumer,)).fetchone()
            if not row:
                raise ValueError('消费者不存在')
            if row['last_token'] and secrets.compare_digest(row['last_token'], token):
                return {'acknowledged': True}
            if not row['token'] or not secrets.compare_digest(row['token'], token):
                raise ValueError('确认 token 无效')
            last = json.loads(row['pending'])[-1]
            position = db.execute('SELECT seq FROM inbox WHERE event_id=?', (last,)).fetchone()[0]
            db.execute('UPDATE consumers SET position=?,pending=NULL,token=NULL,last_token=? WHERE id=?', (position, token, consumer))
        return {'acknowledged': True}

    def event(self, event_id):
        with self.lock:
            row = self.db.execute('SELECT event FROM inbox WHERE event_id=?', (event_id,)).fetchone()
            if not row:
                raise ValueError('事件不存在或不在此桥接账号中')
            return json.loads(row[0])

    def reset_consumer(self, consumer, start):
        with self.tx() as db:
            if not db.execute('SELECT 1 FROM consumers WHERE id=?', (consumer,)).fetchone():
                raise ValueError('消费者不存在')
            gap = db.execute("SELECT value FROM meta WHERE key='pruned_through'").fetchone()
            position = int(gap[0]) if gap else 0
            if start == 'latest':
                position = max(position, db.execute('SELECT COALESCE(MAX(seq),0) FROM inbox').fetchone()[0])
            db.execute('UPDATE consumers SET position=?,pending=NULL,token=NULL,last_token=NULL WHERE id=?',
                       (position, consumer))
            return {'reset': True, 'start': start, 'position': position}

    def prune(self, now=None):
        cutoff = (time.time() if now is None else now) - 7 * 86400
        with self.tx() as db:
            pinned = {r[0] for r in db.execute("SELECT event_id FROM replies WHERE state NOT IN ('accepted','failed')")}
            pinned.update(r[0] for r in db.execute("SELECT event_id FROM reply_parts WHERE state NOT IN ('accepted','failed')"))
            pinned.update(r[0] for r in db.execute("SELECT event_id FROM deliveries WHERE state IN ('pending','delivering')"))
            for grant in db.execute('''SELECT g.trigger_id,g.target_id FROM resource_grants g
                LEFT JOIN replies r ON r.event_id=g.trigger_id
                WHERE (r.event_id IS NULL OR r.state NOT IN ('accepted','failed'))
                AND NOT EXISTS(SELECT 1 FROM reply_parts p WHERE p.event_id=g.trigger_id
                    AND p.is_final=1 AND p.state IN ('accepted','failed'))'''):
                pinned.update(grant)
            for row in db.execute('SELECT seq,event_id FROM inbox WHERE created<?', (cutoff,)).fetchall():
                if row['event_id'] in pinned:
                    continue
                db.execute('DELETE FROM message_index WHERE event_id=?', (row['event_id'],))
                db.execute('DELETE FROM outgoing_index WHERE event_id=?', (row['event_id'],))
                db.execute('DELETE FROM outgoing_sort_index WHERE event_id=?', (row['event_id'],))
                db.execute('DELETE FROM resource_grants WHERE trigger_id=? OR target_id=?', (row['event_id'], row['event_id']))
                db.execute('DELETE FROM deliveries WHERE event_id=?', (row['event_id'],))
                db.execute('DELETE FROM replies WHERE event_id=?', (row['event_id'],))
                db.execute('DELETE FROM reply_parts WHERE event_id=?', (row['event_id'],))
                db.execute('DELETE FROM inbox WHERE seq=?', (row['seq'],))
                db.execute("INSERT INTO meta VALUES('pruned_through',?) ON CONFLICT(key) DO UPDATE SET "
                           'value=CAST(MAX(CAST(meta.value AS INTEGER),CAST(excluded.value AS INTEGER)) AS TEXT)',
                           (str(row['seq']),))

    def subscribe(self, owner, filters, url, secret, ttl):
        canonical = json.dumps(filters, sort_keys=True, separators=(',', ':'))
        sid = 'sub_' + hashlib.sha256(encode([owner, url, 'wechat.message.received', filters])).hexdigest()[:40]
        now = time.time()
        with self.tx() as db:
            row = db.execute('SELECT * FROM subscriptions WHERE id=?', (sid,)).fetchone()
            if not row and db.execute('SELECT COUNT(*) FROM subscriptions').fetchone()[0] >= MAX_SUBSCRIPTIONS:
                raise ValueError('订阅数量已达 32 条上限')
            since = db.execute('SELECT COALESCE(MAX(seq),0) FROM inbox').fetchone()[0]
            # 活跃订阅续期保留积压；取消或过期后重建不宣称补放历史。
            continuing = row and row['active'] and row['expires'] > now
            old = None
            old_until = 0
            if continuing:
                previous = unseal(row['secret'])
                if previous != secret:
                    old, old_until = row['secret'], now + 300
                elif row['old_until'] > now:
                    old, old_until = row['old_secret'], row['old_until']
                since = row['since_seq']
            else:
                db.execute('DELETE FROM deliveries WHERE subscription_id=?', (sid,))
            db.execute('''INSERT OR REPLACE INTO subscriptions
                (id,owner,filters,url,secret,old_secret,old_until,expires,active,since_seq)
                VALUES(?,?,?,?,?,?,?,?,1,?)''',
                (sid, owner, canonical, url, seal(secret), old, old_until, now + ttl, since))
        return sid, now + ttl

    def unsubscribe(self, owner, filters, url):
        sid = 'sub_' + hashlib.sha256(encode([owner, url, 'wechat.message.received', filters])).hexdigest()[:40]
        with self.tx() as db:
            db.execute('UPDATE subscriptions SET active=0 WHERE id=? AND owner=?', (sid, owner))
            db.execute("UPDATE deliveries SET state='cancelled' WHERE subscription_id=? AND state IN ('pending','delivering')", (sid,))

    def next_delivery(self):
        with self.tx() as db:
            row = db.execute('''SELECT d.*,s.url,s.secret,s.old_secret,s.old_until FROM deliveries d
                JOIN subscriptions s ON s.id=d.subscription_id WHERE d.state='pending' AND d.due<=?
                AND s.active=1 AND s.expires>? ORDER BY d.due,d.rowid LIMIT 1''', (time.time(), time.time())).fetchone()
            if not row:
                return None
            db.execute("UPDATE deliveries SET state='delivering',attempts=attempts+1 WHERE subscription_id=? AND event_id=?",
                       (row['subscription_id'], row['event_id']))
            job = dict(row)
            job['attempts'] += 1
            job['secret'] = unseal(job['secret'])
            job['old_secret'] = unseal(job['old_secret']) if job['old_secret'] and job['old_until'] > time.time() else None
            return job

    def finish_delivery(self, job, error=None, permanent=False):
        attempts = job['attempts']
        state = 'delivered' if error is None else ('failed' if permanent or attempts >= 8 else 'pending')
        with self.tx() as db:
            db.execute('''UPDATE deliveries SET state=?,due=?,error=? WHERE subscription_id=? AND event_id=?
                AND state='delivering' ''', (state, time.time() + min(300, 2 ** attempts), error,
                                           job['subscription_id'], job['event_id']))

    def prepare_reply(self, event_id, account, text):
        rid = 'agent_' + hashlib.sha256(encode([account, event_id, 'reply-v1'])).hexdigest()[:48]
        with self.tx() as db:
            if db.execute('SELECT 1 FROM reply_parts WHERE event_id=?',(event_id,)).fetchone():
                raise ValueError('该事件已采用分段回复，禁止混用单条回复')
            row = db.execute('SELECT * FROM replies WHERE event_id=?', (event_id,)).fetchone()
            if row:
                if row['text'] != text:
                    raise ValueError('该事件已有不同正文的回复意图，禁止覆盖或重复发送')
                return dict(row), False
            db.execute('INSERT INTO replies VALUES(?,?,?,?,NULL,?)', (event_id, rid, text, 'prepared', time.time()))
            return dict(db.execute('SELECT * FROM replies WHERE event_id=?', (event_id,)).fetchone()), True

    def reply_state(self, event_id, state, result=None):
        with self.lock:
            self.db.execute('UPDATE replies SET state=?,result=? WHERE event_id=?',
                            (state, json.dumps(result, ensure_ascii=False) if result is not None else None, event_id))

    def reply(self, event_id):
        with self.lock:
            row = self.db.execute('SELECT * FROM replies WHERE event_id=?', (event_id,)).fetchone()
            return dict(row) if row else None

    def prepare_part(self,event_id,account,index,text,final):
        if isinstance(index,bool) or not isinstance(index,int) or not 0<=index<64 or not isinstance(final,bool):
            raise ValueError('分段序号必须0至63，final必须为布尔值')
        if index==63 and not final:raise ValueError('第64段必须结束')
        with self.tx() as db:
            if db.execute('SELECT 1 FROM replies WHERE event_id=?',(event_id,)).fetchone():
                raise ValueError('该事件已有单条回复，禁止改为分段发送')
            row=db.execute('SELECT * FROM reply_parts WHERE event_id=? AND part_index=?',(event_id,index)).fetchone()
            if row:
                if row['text']!=text or bool(row['is_final'])!=final:raise ValueError('同一段的正文和结束标记不可变')
                return dict(row),False
            if index:
                previous=db.execute('SELECT state,is_final FROM reply_parts WHERE event_id=? AND part_index=?',
                    (event_id,index-1)).fetchone()
                if not previous or previous['state']!='accepted' or previous['is_final']:
                    raise ValueError('仅上一段已接受且未结束时可发送下一段；未知发送禁止越过')
            rid='agentp_'+hashlib.sha256(encode([account,event_id,index,'reply-parts-v1'])).hexdigest()[:48]
            db.execute('INSERT INTO reply_parts VALUES(?,?,?,?,?,?,NULL,?)',
                (event_id,index,rid,text,int(final),'prepared',time.time()))
            return dict(db.execute('SELECT * FROM reply_parts WHERE event_id=? AND part_index=?',(event_id,index)).fetchone()),True

    def part(self,event_id,index):
        with self.lock:
            row=self.db.execute('SELECT * FROM reply_parts WHERE event_id=? AND part_index=?',(event_id,index)).fetchone()
            return dict(row) if row else None

    def part_state(self,event_id,index,state,result=None):
        with self.lock:
            self.db.execute('UPDATE reply_parts SET state=?,result=? WHERE event_id=? AND part_index=?',
                (state,json.dumps(result,ensure_ascii=False) if result is not None else None,event_id,index))

    def send_echo(self, event_id, operation=None):
        """仅关联原生生成标识、账号和原会话。

        事件和发送结果可以按任意顺序到达；此查询从不修改不可变事件，
        也不从回显推断对端送达。
        """
        with self.lock:
            operation = operation or self.reply(event_id)
            if not operation or not operation['result']:
                return {'state': 'identity_unavailable'}
            upstream = json.loads(operation['result'])
            native = upstream.get('result') or {}
            sequence=native.get('native_sort_seq')
            if (native.get('native_sort_seq_source')=='weixin41930_send_task_record110_v1' and
                    isinstance(sequence,str) and 1<=len(sequence)<=20 and sequence.isascii() and sequence.isdigit() and 0<int(sequence)<1<<64):
                trigger=self.event(event_id)
                matches=[]
                for record in self.db.execute('''SELECT i.event FROM outgoing_sort_index o JOIN inbox i USING(event_id)
                        WHERE o.account=? AND o.conversation=? AND o.sort_seq=?''',
                        (trigger['account'],trigger['conversation_id'],sequence)):
                    value=json.loads(record['event'])
                    if (value.get('direction')=='outgoing' and value.get('sender_id')==trigger['account'] and
                            value.get('native_record_metadata_source')=='weixin41930_record_identity_v2' and
                            value.get('native_sort_seq')==sequence):
                        matches.append(value)
                mids={value['message_id'] for value in matches if value.get('message_id')}
                if (native.get('native_message_id_source')=='weixin41930_message_db_exact_sort_seq_v1'
                        and native.get('message_id')):
                    mids.add(native['message_id'])
                return {'state':'ambiguous' if len(mids)>1 else 'matched' if matches else 'waiting',
                        'native_sort_seq':sequence,'source':'weixin41930_exact_send_task_record110',
                        'message_id':next(iter(mids)) if len(mids)==1 else None,
                        'echo_event_ids':[v['event_id'] for v in matches],'delivery_confirmed':False}
            client_id = native.get('native_client_id')
            if native.get('native_client_id_source') != 'weixin41930_send_object_618' or not isinstance(client_id, str) or not 1 <= len(client_id) <= 128:
                return {'state': 'identity_unavailable'}
            trigger = self.event(event_id)
            matches = []
            for row in self.db.execute('''SELECT i.event FROM outgoing_index o JOIN inbox i USING(event_id)
                    WHERE o.account=? AND o.conversation=? AND o.client_id=?''',
                                       (trigger['account'], trigger['conversation_id'], client_id)):
                value = json.loads(row['event'])
                if (value.get('direction') == 'outgoing' and value.get('sender_id') == trigger['account'] and
                        value.get('native_client_id_source') == 'weixin41930_record_client278_v1' and
                        value.get('native_client_id') == client_id):
                    matches.append(value)
            mids = {value['message_id'] for value in matches if value.get('message_id')}
            # 服务器标识碰撞或存在歧义时，必须保持显式状态。
            return {'state': 'ambiguous' if len(mids)>1 else 'matched' if matches else 'waiting',
                    'native_client_id': client_id, 'source': 'weixin41930_exact_native_client_id',
                    'message_id': next(iter(mids)) if len(mids)==1 else None,
                    'echo_event_ids': [v['event_id'] for v in matches],
                    'delivery_confirmed': False}

    def status(self):
        with self.lock:
            return {'events': self.db.execute('SELECT COUNT(*) FROM inbox').fetchone()[0],
                    'subscriptions_active': self.db.execute('SELECT COUNT(*) FROM subscriptions WHERE active=1 AND expires>?', (time.time(),)).fetchone()[0],
                    'deliveries': dict(self.db.execute('SELECT state,COUNT(*) FROM deliveries GROUP BY state').fetchall()),
                    'replies': dict(self.db.execute('SELECT state,COUNT(*) FROM replies GROUP BY state').fetchall()),
                    'source': self.meta('source')}

    def close(self):
        self.db.close()
        self.owner_lock.close()
