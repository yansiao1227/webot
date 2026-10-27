"""机主CLI：只读查询，管理请求交由已就绪的常驻服务执行。"""
import json
import sqlite3
import time
import uuid
from contextlib import closing
from .scheduler import Scheduler, FIELDS
from .schedule_time import display


def add_parser(sub):
    parser = sub.add_parser('schedules', help='管理原会话定时任务')
    parser.add_argument('operation', choices=['create', 'list', 'show', 'get', 'update', 'pause', 'resume', 'cancel', 'runs', 'request'])
    for field in ('task-id', 'event-id', 'name', 'message', 'ai-task', 'schedule-type', 'schedule-value', 'request-id'):
        parser.add_argument('--' + field)


def execute(directory, args):
    uri = (directory / 'agent.sqlite3').as_uri()
    op = 'get' if args.operation == 'show' else args.operation
    if op not in ('create', 'list', 'request') and not getattr(args, 'task_id', None):
        return {'ok': False, 'error': '请指定--task-id'}
    if op == 'request' and not args.request_id:
        return {'ok': False, 'error': '请指定--request-id'}
    if op in ('list', 'get', 'runs', 'request'):
        with closing(sqlite3.connect(uri + '?mode=ro', uri=True)) as db:
            db.row_factory = sqlite3.Row
            if op == 'request':
                row = db.execute('SELECT id,state,result FROM schedule_commands WHERE id=?', (args.request_id,)).fetchone()
                if not row:
                    raise ValueError('请求不存在')
                return {**dict(row), 'result': json.loads(row['result']) if row['result'] else None}
            if op == 'runs':
                rows = [dict(r) for r in db.execute('SELECT * FROM schedule_runs WHERE schedule_id=? ORDER BY planned DESC LIMIT 30', (args.task_id,))]
                for row in rows:
                    row['planned'] = display(row['planned'])
                    row['details'] = json.loads(row['details'])
                return {'runs': rows}
            rows = db.execute('SELECT * FROM schedules ORDER BY created DESC LIMIT 100' if op == 'list' else
                              'SELECT * FROM schedules WHERE id=?', () if op == 'list' else (args.task_id,)).fetchall()
            return {'tasks': [{**Scheduler.public(r), 'conversation': r['conversation'], 'creator': r['creator']} for r in rows]}
    process_path = directory / 'process.json'
    process = json.loads(process_path.read_text(encoding='utf-8')) if process_path.exists() else {}
    if not process.get('running') or not process.get('ready') or time.time() - process.get('heartbeat', 0) >= 5:
        return {'ok': False, 'error': '定时管理需要已就绪的常驻服务；请先启动服务'}
    if op == 'create' and not args.event_id:
        return {'ok': False, 'error': '创建必须指定--event-id作为原会话授权'}
    action = {key: getattr(args, key, None) for key in FIELDS if key != 'action' and getattr(args, key, None) is not None}
    action['action'] = op
    if any(not isinstance(v, str) or len(v) > 12000 or '\0' in v for v in action.values()):
        return {'ok': False, 'error': '管理参数无效或超限'}
    key = 'cli_' + uuid.uuid4().hex
    event = json.dumps({'event_id': args.event_id}) if args.event_id else None
    with closing(sqlite3.connect(uri + '?mode=rw', uri=True, isolation_level=None)) as db:
        db.execute('INSERT INTO schedule_commands VALUES(?,?,?,?,?,NULL,?)',
                   (key, json.dumps(action, ensure_ascii=False), event, 1, 'pending', time.time()))
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            row = db.execute('SELECT state,result FROM schedule_commands WHERE id=?', (key,)).fetchone()
            if row[0] == 'done':
                return {'request_id': key, **json.loads(row[1])}
            time.sleep(.1)
    return {'request_id': key, 'state': 'pending', 'message': '请求已保存，请用schedules request查询此ID，勿重复创建'}
