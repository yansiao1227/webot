"""日报事务账本；日期、发布时间与发布意图不可通过重启重置。"""
from contextlib import contextmanager
from datetime import datetime, time as daytime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import secrets
import re
import sqlite3
import threading
import time

BEIJING = timezone(timedelta(hours=8))
TERMINAL = {'confirmed', 'failed', 'expired', 'generation_unknown'}


class RunnerLock:
    """只锁执行器，CLI仍可并发读取账本或暂停任务。"""
    def __init__(self, directory):
        self.file = (Path(directory) / 'runner.lock').open('a+b')
        if self.file.tell() == 0:
            self.file.write(b'0')
            self.file.flush()
        self.file.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException:
            self.file.close()
            raise RuntimeError('同一日报目录已有执行器') from None

    def close(self):
        self.file.close()


class DigestStore:
    def __init__(self, directory, account):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(self.directory / 'digest.sqlite3', isolation_level=None, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
            PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL;
            CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS runs(date TEXT PRIMARY KEY,intent_id TEXT UNIQUE NOT NULL,
                planned_at REAL NOT NULL,deadline REAL NOT NULL,state TEXT NOT NULL,
                packet TEXT,edition TEXT,post_text TEXT,operation TEXT,error TEXT,
                thread_id TEXT,turn_id TEXT,created REAL NOT NULL,updated REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS history(date TEXT PRIMARY KEY,content_hash TEXT NOT NULL,
                edition TEXT NOT NULL,verified_at REAL NOT NULL);
            INSERT OR IGNORE INTO meta VALUES('paused','false');
        ''')
        row = self.db.execute("SELECT value FROM meta WHERE key='account'").fetchone()
        if row and json.loads(row[0]) != account:
            self.db.close()
            raise ValueError('日报状态目录账号不匹配')
        self.db.execute("INSERT OR IGNORE INTO meta VALUES('account',?)", (json.dumps(account),))
        self.account = account

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

    def ensure_today(self, now, choose=secrets.randbelow):
        local = datetime.fromtimestamp(now, BEIJING)
        start = datetime.combine(local.date(), daytime(16, 30), BEIJING).timestamp()
        deadline = datetime.combine(local.date(), daytime(18), BEIJING).timestamp()
        if now < start or now >= deadline:
            return None
        date = local.date().isoformat()
        with self.tx() as db:
            if not db.execute('SELECT 1 FROM runs WHERE date=?', (date,)).fetchone():
                planned = datetime.combine(local.date(), daytime(17), BEIJING).timestamp() + choose(3600)
                intent = 'digest-' + hashlib.sha256((self.account + ':daily-observer:' + date).encode()).hexdigest()[:48]
                db.execute('INSERT INTO runs(date,intent_id,planned_at,deadline,state,created,updated) VALUES(?,?,?,?,?,?,?)',
                           (date, intent, planned, deadline, 'pending', now, now))
        return self.run(date)

    def run(self, date):
        with self.lock:
            row = self.db.execute('SELECT * FROM runs WHERE date=?', (date,)).fetchone()
            if not row:
                return None
            value = dict(row)
            for name in ('packet', 'edition', 'operation'):
                if value[name] is not None:
                    value[name] = json.loads(value[name])
            return value

    def update(self, date, state, **values):
        allowed = {'packet', 'edition', 'post_text', 'operation', 'error', 'thread_id', 'turn_id'}
        if set(values) - allowed:
            raise ValueError('日报账本字段无效')
        for key in ('packet', 'edition', 'operation'):
            if key in values:
                values[key] = json.dumps(values[key], ensure_ascii=False, allow_nan=False)
        with self.tx() as db:
            columns = ['state=?', 'updated=?'] + [key + '=?' for key in values]
            db.execute('UPDATE runs SET ' + ','.join(columns) + ' WHERE date=?',
                       (state, time.time(), *values.values(), date))
        return self.run(date)

    def recover(self):
        with self.tx() as db:
            # 提交轮次前已保存generating，崩溃后不推断没有调用模型。
            db.execute("UPDATE runs SET state='generation_unknown',error='interrupted_generation' WHERE state='generating'")
            db.execute("UPDATE runs SET state='pending' WHERE state='collecting'")

    def expire(self, now):
        with self.tx() as db:
            db.execute("UPDATE runs SET state='expired',error='publish_window_closed',updated=? WHERE deadline<=? "
                       "AND state IN ('pending','collected','imaged','generated','validated','uploading_media','media_ready','uploading','website_unknown','waiting')", (now, now))

    def sync_history(self, editions, now):
        with self.tx() as db:
            for edition in editions:
                previous = db.execute('SELECT content_hash FROM history WHERE date=?', (edition['date'],)).fetchone()
                if previous and previous[0] != edition['content_hash']:
                    raise ValueError('已验证网站历史内容发生冲突')
                db.execute('INSERT INTO history VALUES(?,?,?,?) ON CONFLICT(date) DO UPDATE SET verified_at=excluded.verified_at',
                    (edition['date'], edition['content_hash'], json.dumps(edition, ensure_ascii=False, allow_nan=False), now))

    def historical_edition(self, date):
        with self.lock:
            row = self.db.execute('SELECT edition FROM history WHERE date=?', (date,)).fetchone()
            return json.loads(row[0]) if row else None

    def next_unsettled(self):
        with self.lock:
            row = self.db.execute("SELECT date FROM runs WHERE state IN ('submitting','queued','running','accepted','unknown') "
                                  'ORDER BY date LIMIT 1').fetchone()
        return self.run(row[0]) if row else None

    def runs(self, limit=30):
        with self.lock:
            dates = [r[0] for r in self.db.execute('SELECT date FROM runs ORDER BY date DESC LIMIT ?', (limit,))]
        return [self.run(date) for date in dates]

    def published_article_urls(self, before_date):
        """仅排除已读回网页中实际选用的新闻，不消耗未采用的采集候选。"""
        with self.lock:
            rows = self.db.execute("SELECT edition FROM runs WHERE date<? AND edition IS NOT NULL "
                "AND post_text IS NOT NULL AND post_text!='' UNION ALL SELECT edition FROM history WHERE date<?",
                (before_date, before_date)).fetchall()
        urls = set()
        for row in rows:
            edition = json.loads(row[0])
            references = {sid for articles in edition.get('articles', {}).values()
                          for article in articles for sid in article.get('source_ids', [])}
            urls.update(source['url'] for source in edition.get('sources', [])
                        if source.get('id') in references and isinstance(source.get('url'), str))
        return urls

    def status(self):
        runs = self.runs(1)
        last = runs[0] if runs else None
        with self.lock:
            history_count, history_latest = self.db.execute('SELECT COUNT(*),MAX(date) FROM history').fetchone()
        latest = {k: last[k] for k in ('date', 'state', 'planned_at', 'deadline', 'error', 'intent_id')} if last else None
        if last:
            operation = last.get('operation') or {}
            moment_state = operation.get('state')
            if moment_state not in ('confirmed', 'failed', 'queued', 'running', 'accepted', 'unknown'):
                moment_state = last['state'] if last['state'] in ('submitting', 'unknown') else 'not_submitted'
            def reason(value):
                if isinstance(value, dict):
                    value = value.get('code')
                if isinstance(value, str) and re.fullmatch(r'[A-Za-z][A-Za-z0-9_.:-]{0,127}', value):
                    return value
                return 'operation_error' if value else None
            nested = operation.get('operation') if isinstance(operation.get('operation'), dict) else {}
            result = nested.get('result') if isinstance(nested.get('result'), dict) else {}
            error = reason(operation.get('error') or nested.get('error') or result.get('error'))
            if not error and moment_state in ('submitting', 'unknown'):
                error = reason(last.get('error'))
            request_id = operation.get('request_id')
            if not isinstance(request_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', request_id):
                request_id = None
            latest['moment'] = {'state': moment_state,
                'server_confirmed': last['state'] == 'confirmed' and operation.get('server_confirmed') is True,
                'local_accepted': operation.get('local_accepted') is True,
                'request_id': request_id, 'error': error}
        return {'account': self.account, 'paused': bool(self.meta('paused')),
                'history_count': history_count, 'history_latest': history_latest,
                'history_sync': self.meta('history_sync'), 'latest': latest}

    def close(self):
        self.db.close()
