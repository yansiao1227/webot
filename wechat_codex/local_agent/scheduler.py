"""持久任务管理和受授权原会话约束的执行集成。"""
import asyncio
import hashlib
import json
import time
from .mcp import allowed
from .schedule_time import normalize, next_time, display
from .schedule_store import claim_due, save_run

ACTIONS = {'create', 'list', 'get', 'update', 'pause', 'resume', 'cancel', 'runs'}
FIELDS = {'action', 'task_id', 'name', 'message', 'ai_task', 'schedule_type', 'schedule_value'}


class Scheduler:
    def __init__(self, agent, now=time.time):
        self.agent, self.store, self.now = agent, agent.store, now
        self.management_lock = asyncio.Lock()

    def submit(self, key, action, event=None, operator=False):
        if not isinstance(action, dict) or set(action) - FIELDS or action.get('action') not in ACTIONS:
            raise ValueError('定时管理动作无效')
        if any(not isinstance(v, str) or len(v) > 12000 or '\0' in v for v in action.values()):
            raise ValueError('定时管理参数必须为有界文字')
        payload = json.dumps(action, ensure_ascii=False, sort_keys=True)
        encoded = json.dumps(event, ensure_ascii=False, sort_keys=True) if event is not None else None
        with self.store.tx() as db:
            row = db.execute('SELECT * FROM schedule_commands WHERE id=?', (key,)).fetchone()
            if row and (row['payload'] != payload or row['event'] != encoded or bool(row['operator']) != operator):
                raise ValueError('同一管理请求不能改变内容或身份')
            db.execute('INSERT OR IGNORE INTO schedule_commands VALUES(?,?,?,?,?,NULL,?)',
                       (key, payload, encoded, int(operator), 'pending', self.now()))

    async def resolve_actions(self, job, tid, event, reply, generation, stage='', generate=None):
        seen = set()
        for _ in range(4):
            action = generation.get('schedule_action')
            if action is None:
                return reply, generation
            if event.get('is_scheduled_task'):
                raise ValueError('定时执行不能递归管理任务')
            canonical = json.dumps(action, ensure_ascii=False, sort_keys=True)
            if canonical in seen:
                return self.receipt_text(receipt), {**generation, 'schedule_action': None, 'continue_reply': False, 'context_mode': 'none'}
            seen.add(canonical)
            # 始终从持久原事件取身份，模型只能提出动作参数。
            source = json.loads(job['event'])
            trusted = {k: source[k] for k in ('event_id', 'account', 'conversation_id', 'sender_id',
                'direction', 'message_type', 'payload', 'mentions', 'captured_at') if k in source}
            key = 'wechat_' + hashlib.sha256((job['event_id'] + ':' + canonical).encode()).hexdigest()
            receipt = await self.command(key, action, trusted)
            self.store.update(job['event_id'], 'generating', result={'schedule_result': receipt})
            current = {**event, 'schedule_result': receipt}
            if generate is None:
                reply, generation = await self.agent.backend.generate(tid, current)
            else:
                reply, generation = await generate(current)
        return self.receipt_text(receipt), {**generation, 'schedule_action': None, 'continue_reply': False, 'context_mode': 'none'}

    @staticmethod
    def receipt_text(receipt):
        if not receipt.get('ok'):
            return receipt['error']
        if 'task' in receipt:
            task = receipt['task']
            return f"任务{task['task_id']}：{task['name']}，状态{task['status']}。" + (
                f"下次执行：{task['next_run_at']}（北京时间）。" if task['next_run_at'] else '')
        return json.dumps(receipt, ensure_ascii=False)[:1200]

    async def command(self, key, action, event=None, operator=False):
        self.submit(key, action, event, operator)
        return await self.apply(key)

    def scoped(self, task, event, operator):
        if task['account'] != self.agent.config['account'] or task['conversation'] not in self.agent.config['conversations']:
            raise ValueError('任务不在当前账号和白名单内')
        if not operator and (task['conversation'] != event['conversation_id'] or task['creator'] != self.creator(event)):
            raise ValueError('只能管理本人在当前会话创建的任务')

    @staticmethod
    def creator(event):
        sender = event.get('sender_id')
        if not sender and not event['conversation_id'].endswith('@chatroom'):
            sender = event['conversation_id']
        if not isinstance(sender, str) or not sender:
            raise ValueError('缺少可信创建者身份')
        return sender

    @staticmethod
    def public(task):
        return {'task_id': task['id'], 'name': task['name'], 'message': task['message'],
                'ai_task': task['ai_task'], 'status': task['status'],
                'schedule': json.loads(task['rule']), 'next_run_at': display(task['next_run']),
                'timezone': 'Asia/Shanghai'}

    async def apply(self, key):
        async with self.management_lock:
            row = self.store.db.execute('SELECT * FROM schedule_commands WHERE id=?', (key,)).fetchone()
            if row['state'] == 'done':
                return json.loads(row['result'])
            self.store.db.execute("UPDATE schedule_commands SET state='applying' WHERE id=?", (key,))
            action = json.loads(row['payload'])
            event = json.loads(row['event']) if row['event'] else None
            operator = bool(row['operator'])
            try:
                if not operator and (not event or not allowed(event, self.agent.config['account'], self.agent.config['conversations'])):
                    raise ValueError('当前事件不具备定时管理权限')
                result = await self._apply(key, action, event, operator, row['created'])
            except ValueError as error:
                result = {'ok': False, 'error': str(error)}
            except Exception:
                result = {'ok': False, 'error': '定时管理未完成，请核对服务和原任务状态；不要重复创建'}
            with self.store.tx() as db:
                db.execute("UPDATE schedule_commands SET state='done',result=? WHERE id=?", (json.dumps(result, ensure_ascii=False), key))
            return result

    async def _apply(self, key, action, event, operator, base):
        verb = action['action']
        if operator and verb == 'create':
            if not event or not event.get('event_id'):
                raise ValueError('创建必须指定授权事件')
            event = await self.agent.mcp.call('get_message', event_id=event['event_id'])
        if verb == 'list':
            rows = self.store.db.execute('SELECT * FROM schedules ORDER BY created DESC').fetchall()
            items = []
            for task in rows:
                try:
                    self.scoped(task, event, operator)
                except ValueError:
                    continue
                items.append(self.public(task))
            return {'ok': True, 'tasks': items[:100], 'truncated': len(items) > 100}
        if verb == 'create':
            if event is None:
                raise ValueError('创建必须指定有效入站授权事件')
            if not allowed(event, self.agent.config['account'], self.agent.config['conversations']):
                raise ValueError('创建事件不在允许范围或缺少群原生@')
            creator = self.creator(event)
            name, message, ai_task = (action.get(k, '').strip() for k in ('name', 'message', 'ai_task'))
            if not name or len(name) > 100 or bool(message) == bool(ai_task) or len(message) > 1200:
                raise ValueError('名称须为1至100字，message与ai_task二选一，提醒最多1200字')
            rule = normalize(action.get('schedule_type'), action.get('schedule_value'), base)
            tid = hashlib.sha256(key.encode()).hexdigest()[:24]
            context = event.get('schedule_context', '')
            if not isinstance(context, str) or len(context) > 12000:
                raise ValueError('保存的文字背景超限')
            with self.store.tx() as db:
                db.execute('INSERT OR IGNORE INTO conversations(id,updated) VALUES(?,?)', (event['conversation_id'], base))
                db.execute('INSERT OR IGNORE INTO schedules VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                    (tid, self.agent.config['account'], event['conversation_id'], creator, event['event_id'],
                     name, message, ai_task, json.dumps(rule), context, 'authorizing', next_time(rule, base), base, base))
            # MCP保存长期证据成功后才激活；中断恢复使用同一任务及授权ID。
            receipt = await self.agent.mcp.call('authorize_schedule', event_id=event['event_id'], schedule_id=tid)
            if not receipt.get('active') or receipt.get('conversation_id') != event['conversation_id'] or receipt.get('sender_id') != creator:
                raise ValueError('授权回执身份不匹配')
            with self.store.tx() as db:
                db.execute("UPDATE schedules SET status='active',updated=? WHERE id=? AND status='authorizing'", (self.now(), tid))
            return {'ok': True, 'task': self.public(self.task(tid))}
        tid = action.get('task_id', '')
        task = self.task(tid)
        self.scoped(task, event, operator)
        if verb == 'get':
            return {'ok': True, 'task': self.public(task)}
        if verb == 'runs':
            runs = [dict(r) for r in self.store.db.execute('SELECT id,planned,state,error FROM schedule_runs WHERE schedule_id=? ORDER BY planned DESC LIMIT 30', (tid,))]
            for run in runs:
                run['planned'] = display(run['planned'])
            return {'ok': True, 'runs': runs}
        if verb == 'cancel':
            self.store.db.execute("UPDATE schedules SET status='cancelled',updated=? WHERE id=?", (self.now(), tid))
            await self.agent.mcp.call('revoke_schedule', schedule_id=tid)
        elif verb == 'pause':
            if task['status'] in ('cancelled', 'completed', 'authorizing'):
                raise ValueError('该状态不能暂停')
            if task['status'] != 'blocked':
                self.store.db.execute("UPDATE schedules SET status='paused',updated=? WHERE id=?", (self.now(), tid))
        elif verb in ('resume', 'update'):
            if task['status'] in ('cancelled', 'authorizing', 'blocked'):
                raise ValueError('已取消、授权未完成或状态待核查的任务不能恢复或修改')
            rule = json.loads(task['rule'])
            if verb == 'update':
                if action.get('schedule_type') or action.get('schedule_value'):
                    rule = normalize(action.get('schedule_type'), action.get('schedule_value'), base)
                name = action.get('name') or task['name']
                message, ai_task = task['message'], task['ai_task']
                if action.get('message') or action.get('ai_task'):
                    message, ai_task = action.get('message', ''), action.get('ai_task', '')
                if not name.strip() or len(name) > 100 or bool(message.strip()) == bool(ai_task.strip()) or len(message) > 1200:
                    raise ValueError('任务名称或内容无效')
                if self.store.db.execute("SELECT 1 FROM schedule_runs WHERE schedule_id=? AND state NOT IN ('accepted','completed','failed','cancelled')", (tid,)).fetchone():
                    raise ValueError('任务尚有未结束的执行，不能修改')
            upcoming = next_time(rule, self.now())
            waiting = self.store.db.execute("SELECT 1 FROM schedule_runs WHERE schedule_id=? AND state IN ('pending','generated','sending','continuing')", (tid,)).fetchone()
            if upcoming is None and not (verb == 'resume' and waiting):
                raise ValueError('一次性时间已过，请修改为未来时间')
            state = 'active' if verb == 'resume' else ('paused' if task['status'] == 'paused' else 'active')
            with self.store.tx() as db:
                if verb == 'update':
                    db.execute('UPDATE schedules SET name=?,message=?,ai_task=?,rule=? WHERE id=?',
                               (name, message, ai_task, json.dumps(rule), tid))
                db.execute('UPDATE schedules SET status=?,next_run=?,updated=? WHERE id=?', (state, upcoming, self.now(), tid))
        return {'ok': True, 'task': self.public(self.task(tid))}

    def task(self, tid):
        row = self.store.db.execute('SELECT * FROM schedules WHERE id=?', (tid,)).fetchone()
        if not row:
            raise ValueError('任务不存在，请先查询任务ID')
        return row

    async def tick(self):
        for row in self.store.db.execute("SELECT id FROM schedule_commands WHERE state='pending' ORDER BY created LIMIT 1").fetchall():
            await self.apply(row['id'])
        claim_due(self.store, self.now())

    def ready(self):
        return self.store.db.execute("""SELECT r.*,s.conversation FROM schedule_runs r JOIN schedules s ON s.id=r.schedule_id
            WHERE (s.status='active' AND r.state IN ('pending','generated','continuing'))
               OR r.state='sending' ORDER BY r.planned""").fetchall()

    async def process(self, run):
        rid = run['id']; task = self.task(run['schedule_id'])
        details = json.loads(run['details']); state = run['state']
        def save(value, error=None):
            save_run(self.store, rid, value, details, self.now(), error)
        try:
            await self.agent.verify()
            self.scoped(task, None, True)
            if state not in ('sending',):
                receipt = await self.agent.mcp.call('get_schedule_authorization', schedule_id=task['id'])
                if (not receipt.get('active') or receipt.get('conversation_id') != task['conversation'] or
                        receipt.get('sender_id') != task['creator']):
                    raise ValueError('定时授权身份不匹配')
            if state != 'sending' and self.store.conversation(task['conversation'])['paused']:
                return
            while not self.agent.stop.is_set():
                if state != 'sending' and self.task(task['id'])['status'] != 'active':
                    return
                index = len(details['parts'])
                if state in ('pending', 'continuing'):
                    if details.get('late') and index == 0:
                        reply = f"原定{display(run['planned'])}，本次延迟执行。"
                        generation = {'continue_reply': True}
                    elif task['message']:
                        reply, generation = task['message'], {}
                    else:
                        event = {'event_id': rid, 'account': task['account'], 'conversation_id': task['conversation'],
                                 'sender_id': task['creator'], 'payload': {'text': task['ai_task']},
                                 'is_scheduled_task': True, 'scheduled_at': display(run['planned']),
                                 'context': {'saved_text': task['context']},
                                 'reply_progress': [p['text'] for p in details['parts']], 'remaining_parts': 64 - index,
                                 '_decision_timeout': self.agent.config.get('decision_timeout', 30),
                                 '_tool_timeout': self.agent.config.get('tool_timeout', 180)}
                        if details.get('next_step'):
                            event['next_step'] = details['next_step']
                        tid = details.get('thread_id')
                        tid = await self.agent.backend.create_task_thread(task['conversation'], rid, tid)
                        details['thread_id'] = tid
                        save('generating')
                        reply, generation = await self.agent.backend.generate(tid, event)
                        if generation.get('schedule_action') or generation.get('read_attachment') or generation.get('context_mode', 'none') != 'none':
                            raise ValueError('定时执行不能管理任务、请求新附件或扩展聊天历史')
                    details['pending'] = {'text': reply[:1200], 'final': not generation.get('continue_reply', False) or index == 63}
                    details['generation'] = generation
                    details['next_step'] = generation.get('next_step', '')
                    save('generated'); state = 'generated'
                if self.agent.stop.is_set() or (state != 'sending' and self.task(task['id'])['status'] != 'active'):
                    return
                await self.agent.verify()
                args = {'schedule_id': task['id'], 'run_id': rid, 'part_index': index}
                if state == 'generated':
                    save('sending'); state = 'sending'
                    try:
                        op = await self.agent.mcp.call('send_scheduled_text', **args, **details['pending'])
                    except Exception:
                        op = await self.agent.mcp.call('get_scheduled_text_status', **args)
                else:
                    op = await self.agent.mcp.call('get_scheduled_text_status', **args)
                details['operation'] = op
                if op.get('state') in ('queued', 'running', 'submitting', 'prepared'):
                    save('sending'); return
                if op.get('state') != 'accepted':
                    save('unknown' if op.get('state') == 'unknown' else 'failed', '定时投递未确认，禁止重发'); return
                details['parts'].append({**details.pop('pending'), 'operation': op})
                if details['parts'][-1]['final']:
                    save('accepted'); return
                save('continuing'); state = 'continuing'
        except asyncio.CancelledError:
            save('unknown' if state == 'sending' else 'generation_unknown', '执行被中断，需核查原操作')
            raise
        except Exception as error:
            save('unknown' if state == 'sending' else 'generation_unknown' if self.store.db.execute('SELECT state FROM schedule_runs WHERE id=?', (rid,)).fetchone()[0] == 'generating' else 'failed', type(error).__name__)
