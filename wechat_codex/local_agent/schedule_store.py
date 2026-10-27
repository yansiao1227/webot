"""调度任务、管理请求和执行记录，与既有agent共用事务连接。"""
import json
from .schedule_time import next_time


def initialize(db):
    db.executescript('''
        CREATE TABLE IF NOT EXISTS schedules(id TEXT PRIMARY KEY,account TEXT NOT NULL,
            conversation TEXT NOT NULL,creator TEXT NOT NULL,event_id TEXT NOT NULL,
            name TEXT NOT NULL,message TEXT NOT NULL,ai_task TEXT NOT NULL,rule TEXT NOT NULL,
            context TEXT NOT NULL,status TEXT NOT NULL,next_run REAL,created REAL NOT NULL,updated REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS schedule_commands(id TEXT PRIMARY KEY,payload TEXT NOT NULL,
            event TEXT,operator INTEGER NOT NULL,state TEXT NOT NULL,result TEXT,created REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS schedule_runs(id TEXT PRIMARY KEY,schedule_id TEXT NOT NULL,
            planned REAL NOT NULL,state TEXT NOT NULL,details TEXT NOT NULL,error TEXT,
            created REAL NOT NULL,updated REAL NOT NULL,UNIQUE(schedule_id,planned));
        CREATE INDEX IF NOT EXISTS schedule_due ON schedules(status,next_run);
    ''')


def recover(store, now):
    with store.tx() as db:
        db.execute("UPDATE schedule_commands SET state='pending' WHERE state='applying'")
        db.execute("UPDATE schedule_runs SET state='generation_unknown',error='进程中断，禁止自动重跑',updated=? WHERE state='generating'", (now,))
        db.execute("UPDATE schedules SET status='blocked' WHERE status!='cancelled' AND id IN (SELECT schedule_id FROM schedule_runs WHERE state IN ('generation_unknown','unknown'))")
        for row in db.execute("SELECT * FROM schedules WHERE status='active' AND next_run<=?", (now,)).fetchall():
            rule = json.loads(row['rule'])
            if rule['type'] != 'once':
                db.execute('UPDATE schedules SET next_run=?,updated=? WHERE id=?', (next_time(rule, now), now, row['id']))


def claim_due(store, now):
    import hashlib
    with store.tx() as db:
        for task in db.execute("SELECT * FROM schedules WHERE status='active' AND next_run<=? ORDER BY next_run", (now,)).fetchall():
            busy = db.execute("SELECT 1 FROM schedule_runs WHERE schedule_id=? AND state NOT IN ('accepted','completed','failed','cancelled')", (task['id'],)).fetchone()
            if busy:
                continue
            planned = task['next_run']
            rid = hashlib.sha256(f"{task['id']}:{planned!r}".encode()).hexdigest()[:40]
            details = {'parts': [], 'late': json.loads(task['rule'])['type'] == 'once' and now - planned > 5}
            db.execute('INSERT OR IGNORE INTO schedule_runs VALUES(?,?,?,?,?,NULL,?,?)',
                       (rid, task['id'], planned, 'pending', json.dumps(details), now, now))
            db.execute('UPDATE schedules SET next_run=?,updated=? WHERE id=?',
                       (next_time(json.loads(task['rule']), now), now, task['id']))


def save_run(store, rid, state, details, now, error=None):
    with store.tx() as db:
        db.execute('UPDATE schedule_runs SET state=?,details=?,error=?,updated=? WHERE id=?',
                   (state, json.dumps(details, ensure_ascii=False), error, now, rid))
        if state in ('generation_unknown', 'unknown', 'failed'):
            db.execute("UPDATE schedules SET status='blocked',updated=? WHERE id=(SELECT schedule_id FROM schedule_runs WHERE id=?) AND status!='cancelled'", (now, rid))
        elif state in ('accepted', 'completed'):
            db.execute("UPDATE schedules SET status='completed',updated=? WHERE id=(SELECT schedule_id FROM schedule_runs WHERE id=?) AND next_run IS NULL AND status='active'", (now, rid))
