"""Durable inbox, per-conversation thread mapping and conservative recovery."""
from contextlib import contextmanager
import json
import hashlib
from pathlib import Path
import sqlite3
import time
from .mcp import allowed


class Store:
    def __init__(self, directory, identity):
        self.path = Path(directory)
        self.path.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path / 'agent.sqlite3', isolation_level=None)
        self.db.row_factory = sqlite3.Row
        had_messages = self.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='messages'").fetchone()
        self.db.executescript('''
            PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL;
            CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS conversations(id TEXT PRIMARY KEY,thread_id TEXT,epoch INTEGER NOT NULL DEFAULT 1,
                paused INTEGER NOT NULL DEFAULT 0,updated REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS jobs(seq INTEGER PRIMARY KEY AUTOINCREMENT,event_id TEXT UNIQUE NOT NULL,
                conversation TEXT NOT NULL,epoch INTEGER NOT NULL,event TEXT NOT NULL,state TEXT NOT NULL,
                reply TEXT,result TEXT,error TEXT,created REAL NOT NULL,updated REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS messages(seq INTEGER PRIMARY KEY AUTOINCREMENT,event_id TEXT UNIQUE NOT NULL,
                conversation TEXT NOT NULL,event TEXT NOT NULL,created REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS context_progress(conversation TEXT PRIMARY KEY,epoch INTEGER NOT NULL,
                through_seq INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS seen_events(event_id TEXT PRIMARY KEY,digest TEXT NOT NULL);
        ''')
        from .schedule_store import initialize
        initialize(self.db)
        encoded = json.dumps(identity, sort_keys=True)
        old = self.db.execute("SELECT value FROM meta WHERE key='identity'").fetchone()
        if old and old[0] != encoded:
            self.db.close()
            raise ValueError('状态目录账号、MCP或consumer身份不匹配')
        self.db.execute("INSERT OR IGNORE INTO meta VALUES('identity',?)", (encoded,))
        # Migrate old jobs without replacing threads or replaying model turns.
        for row in self.db.execute('SELECT event_id,conversation,event,created FROM jobs ORDER BY seq').fetchall():
            self.db.execute('INSERT OR IGNORE INTO messages(event_id,conversation,event,created) VALUES(?,?,?,?)', tuple(row))
            self.db.execute('INSERT OR IGNORE INTO seen_events VALUES(?,?)',
                            (row['event_id'], hashlib.sha256(row['event'].encode()).hexdigest()))
        if not had_messages:
            for row in self.db.execute('''SELECT c.id,c.epoch,MAX(m.seq) AS through_seq FROM conversations c
                JOIN jobs j ON j.conversation=c.id AND j.epoch=c.epoch AND j.state='accepted'
                JOIN messages m ON m.event_id=j.event_id GROUP BY c.id,c.epoch''').fetchall():
                self.db.execute('INSERT OR IGNORE INTO context_progress VALUES(?,?,?)', tuple(row))

    @contextmanager
    def tx(self):
        self.db.execute('BEGIN IMMEDIATE')
        try:
            yield self.db
            self.db.execute('COMMIT')
        except BaseException:
            self.db.execute('ROLLBACK')
            raise

    def ingest(self, events, account=None, conversations=None):
        with self.tx() as db:
            if db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0] + len(events) > 100000:
                raise ValueError('本地队列容量已满，保留上游未确认批次')
            for event in events:
                eid, conversation = event['event_id'], event['conversation_id']
                raw = json.dumps(event, ensure_ascii=False, sort_keys=True)
                digest = hashlib.sha256(raw.encode()).hexdigest()
                old = db.execute('SELECT digest FROM seen_events WHERE event_id=?', (eid,)).fetchone()
                if old:
                    if old[0] != digest:
                        raise ValueError('同一event_id的内容发生变化')
                    continue
                if db.execute('SELECT COUNT(*) FROM messages').fetchone()[0] >= 100000:
                    raise ValueError('本地上下文容量已满，保留上游未确认批次')
                db.execute('INSERT OR IGNORE INTO conversations(id,updated) VALUES(?,?)', (conversation, time.time()))
                epoch = db.execute('SELECT epoch FROM conversations WHERE id=?', (conversation,)).fetchone()[0]
                now = time.time()
                db.execute('INSERT INTO seen_events VALUES(?,?)', (eid, digest))
                db.execute('INSERT INTO messages(event_id,conversation,event,created) VALUES(?,?,?,?)',
                           (eid, conversation, raw, now))
                if not allowed(event, account or event['account'], conversations or [conversation]):
                    continue
                db.execute('INSERT INTO jobs(event_id,conversation,epoch,event,state,created,updated) VALUES(?,?,?,?,?,?,?)',
                           (eid, conversation, epoch, raw, 'pending', now, now))

    def context(self, event_id):
        row = self.db.execute('SELECT seq,conversation FROM messages WHERE event_id=?', (event_id,)).fetchone()
        if not row:
            raise ValueError('当前消息上下文不存在')
        conversation = self.conversation(row['conversation'])
        progress = self.db.execute('SELECT epoch,through_seq FROM context_progress WHERE conversation=?',
                                   (row['conversation'],)).fetchone()
        after = progress['through_seq'] if progress and progress['epoch'] == conversation['epoch'] else 0
        rows = self.db.execute('SELECT seq,event FROM messages WHERE conversation=? AND seq>? AND seq<=? ORDER BY seq',
                               (row['conversation'], after, row['seq'])).fetchall()
        # Generated replies already live in this thread. Suppress only echoes
        # carrying the same verified native identity, never equal body text.
        own_ids = set()
        for job in self.db.execute('''SELECT j.result FROM jobs j JOIN messages m ON m.event_id=j.event_id
                WHERE j.conversation=? AND m.seq<? AND j.result IS NOT NULL''', (row['conversation'], row['seq'])):
            operation = json.loads(job['result']).get('operation') or {}
            native = ((operation.get('result') or {}).get('result') or {})
            if native.get('native_client_id_source') == 'weixin41930_send_object_618' and native.get('native_client_id'):
                own_ids.add(('client',native['native_client_id']))
            if native.get('native_sort_seq_source')=='weixin41930_send_task_record110_v1' and native.get('native_sort_seq'):
                own_ids.add(('sort',native['native_sort_seq']))
        def outgoing_id(event):
            if (event.get('direction')=='outgoing' and event.get('sender_id')==event.get('account') and
                    event.get('native_record_metadata_source')=='weixin41930_record_identity_v2' and event.get('native_sort_seq')):
                return ('sort',event['native_sort_seq'])
            if (event.get('direction') == 'outgoing' and event.get('sender_id') == event.get('account') and
                    event.get('native_client_id_source') == 'weixin41930_record_client278_v1'):
                value=event.get('native_client_id')
                return ('client',value) if value else None
        prior_ids = set()
        for previous in self.db.execute('SELECT event FROM messages WHERE conversation=? AND seq<=?', (row['conversation'], after)):
            key = outgoing_id(json.loads(previous['event']))
            if key:
                prior_ids.add(key)
        latest = {}
        for item in rows:
            key = outgoing_id(json.loads(item['event']))
            if key:
                latest[key] = item['seq']
        values = []
        for item in rows:
            event = json.loads(item['event'])
            key = outgoing_id(event)
            if key and (key in own_ids or key in prior_ids or latest[key] != item['seq']):
                continue
            # Raw XML, content, URLs and file paths are never injected as attachment metadata.
            value = {k: event[k] for k in ('event_id', 'sender_id', 'message_id', 'direction', 'message_type', 'captured_at') if k in event}
            payload = event['payload']
            value['payload'] = ({'text': payload['text']} if event['message_type'] == 'text' else
                                {k: payload[k] for k in ('name', 'mime', 'size') if k in payload})
            values.append(value)
        # Do not silently discard background messages. An oversized batch is explicit.
        if len(json.dumps(values, ensure_ascii=False).encode()) > 512 * 1024:
            raise ValueError('待补入上下文超过512KiB，需要显式整理会话后恢复')
        return {'messages': values, 'through_seq': row['seq'], 'epoch': conversation['epoch']}

    def select_context(self, event_id, **selection):
        from .context import choose
        return choose(self,event_id,**selection)

    def prune(self, now=None):
        cutoff = (time.time() if now is None else now) - 7 * 86400
        with self.tx() as db:
            # Preserve whole conversations with unfinished/uncertain work until resolved.
            db.execute('''DELETE FROM messages WHERE created<? AND NOT EXISTS
                (SELECT 1 FROM jobs WHERE conversation=messages.conversation AND state!='accepted')''', (cutoff,))
            db.execute("DELETE FROM jobs WHERE state='accepted' AND created<?", (cutoff,))

    def recover(self):
        # An interrupted model turn may already have appended to its thread.
        # Do not generate a second answer or continue that conversation blindly.
        with self.tx() as db:
            db.execute("UPDATE jobs SET state='generation_unknown',error='进程中断，模型轮次结果待核查',updated=? WHERE state='generating'", (time.time(),))
            db.execute("UPDATE conversations SET paused=1 WHERE EXISTS (SELECT 1 FROM jobs WHERE conversation=conversations.id AND epoch=conversations.epoch AND state IN ('generation_unknown','unknown','failed'))")

    def ready(self, exclude=()):
        rows = self.db.execute('''SELECT j.* FROM jobs j JOIN conversations c ON c.id=j.conversation
            WHERE c.paused=0 AND j.epoch=c.epoch AND j.state IN ('pending','generated','sending','continuing')
            AND NOT EXISTS(SELECT 1 FROM jobs older WHERE older.conversation=j.conversation
                AND older.epoch=j.epoch AND older.seq<j.seq AND older.state!='accepted')
            ORDER BY j.seq''').fetchall()
        return [dict(row) for row in rows if row['conversation'] not in exclude]

    def conversation(self, cid):
        return dict(self.db.execute('SELECT * FROM conversations WHERE id=?', (cid,)).fetchone())

    def set_thread(self, cid, thread_id):
        with self.tx() as db:
            db.execute('UPDATE conversations SET thread_id=?,updated=? WHERE id=?', (thread_id, time.time(), cid))

    def update(self, eid, state, reply=None, result=None, error=None, context=None):
        with self.tx() as db:
            if result is not None:
                prior = db.execute('SELECT result FROM jobs WHERE event_id=?', (eid,)).fetchone()
                result = {**(json.loads(prior[0]) if prior and prior[0] else {}), **result}
            db.execute('UPDATE jobs SET state=?,reply=COALESCE(?,reply),result=COALESCE(?,result),error=?,updated=? WHERE event_id=?',
                       (state, reply, json.dumps(result, ensure_ascii=False) if result is not None else None,
                        error, time.time(), eid))
            if context is not None:
                cid = db.execute('SELECT conversation FROM jobs WHERE event_id=?', (eid,)).fetchone()[0]
                db.execute('INSERT INTO context_progress VALUES(?,?,?) ON CONFLICT(conversation) DO UPDATE SET '
                           'epoch=excluded.epoch,through_seq=excluded.through_seq',
                           (cid, context['epoch'], context['through_seq']))
            if state in ('generation_unknown', 'unknown', 'failed'):
                db.execute('UPDATE conversations SET paused=1 WHERE id=(SELECT conversation FROM jobs WHERE event_id=?)', (eid,))

    def reset(self, cid):
        with self.tx() as db:
            if db.execute("SELECT 1 FROM jobs WHERE conversation=? AND state IN ('pending','generating','generated','sending')", (cid,)).fetchone():
                raise ValueError('该会话仍有排队或执行中的消息，不能重置')
            changed = db.execute('UPDATE conversations SET thread_id=NULL,epoch=epoch+1,paused=0,updated=? WHERE id=?', (time.time(), cid)).rowcount
            if not changed:
                raise ValueError('会话不存在')

    def status(self):
        return {'jobs': dict(self.db.execute('SELECT state,COUNT(*) FROM jobs GROUP BY state')),
                'schedules': dict(self.db.execute('SELECT status,COUNT(*) FROM schedules GROUP BY status')),
                'scheduled_runs': dict(self.db.execute('SELECT state,COUNT(*) FROM schedule_runs GROUP BY state')),
                'context_messages': self.db.execute('SELECT COUNT(*) FROM messages').fetchone()[0],
                'conversations': [dict(r) for r in self.db.execute('SELECT * FROM conversations')],
                'blocked': [dict(r) for r in self.db.execute("SELECT event_id,conversation,state,error FROM jobs WHERE state IN ('generation_unknown','unknown','failed')")]}

    def close(self):
        self.db.close()
