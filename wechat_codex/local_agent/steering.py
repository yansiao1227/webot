"""同一回复任务的有序引导；发送身份始终属于根事件。"""
import asyncio
import json

from .mcp import allowed, MCPError


class SteeringUnknown(RuntimeError):
    """输入可能已被模型接纳，不能自动重投。"""


class ReplyTask:
    def __init__(self, agent, job):
        self.agent, self.store, self.job = agent, agent.store, job
        self.root = json.loads(job['event'])
        details = json.loads(job['result'] or '{}')
        self.cutoff = self.store.db.execute('SELECT COALESCE(MAX(seq),0) FROM jobs').fetchone()[0]
        self.open = job['state'] != 'sending' or (details.get('adaptive') and not details.get('part_final',True))
        self.lock = asyncio.Lock()
        self.changed = asyncio.Event()
        self.version = self.store.db.execute('SELECT COUNT(*) FROM steering WHERE root_id=?', (job['event_id'],)).fetchone()[0]
        self.thread_id = None
        self.sources = {self.root['event_id']: self.root, **details.get('input_sources', {})}
        self.current = None

    def claim(self):
        # 无 await：与最终发送关闭窗口处于同一事件循环的原子边界。
        if not self.open or self.store.conversation(self.job['conversation'])['paused']:
            return
        rows = self.store.db.execute("SELECT * FROM jobs WHERE conversation=? AND epoch=? AND seq>? AND state='pending' ORDER BY seq",
                                     (self.job['conversation'], self.job['epoch'], self.cutoff)).fetchall()
        with self.store.tx() as db:
            for row in rows:
                event = json.loads(row['event'])
                if not allowed(event, self.agent.config['account'], self.agent.config['conversations']):
                    continue
                if self.root['conversation_id'].endswith('@chatroom'):
                    sender = self.root.get('sender_id')
                    if not isinstance(sender, str) or not sender or event.get('sender_id') != sender:
                        continue
                db.execute("INSERT INTO steering(event_id,root_id,seq,state) VALUES(?,?,?,'pending')",
                           (row['event_id'], self.job['event_id'], row['seq']))
                db.execute("UPDATE jobs SET state='merged' WHERE event_id=?", (row['event_id'],))
                self.version += 1
                self.changed.set()

    def pending(self):
        return self.store.db.execute("SELECT s.*,j.event FROM steering s JOIN jobs j USING(event_id) WHERE root_id=? AND s.state='pending' ORDER BY s.seq",
                                     (self.job['event_id'],)).fetchall()

    async def prepare(self, raw):
        event = json.loads(raw)
        event['context'] = self.store.select_context(event['event_id'])
        if event.get('reference'):
            try:
                target = await self.agent.mcp.call('resolve_reference', event_id=event['event_id'])
                event['reference_resource'] = target
                event['reference_target'] = {k: target[k] for k in ('event_id','message_type','sender_id','message_id') if k in target}
                event['reference_target']['payload'] = {k: target['payload'][k] for k in ('text','name','mime','size') if k in target['payload']}
            except MCPError:
                event['reference_error'] = '无法精确找到原生引用目标，请重新引用，不能猜测附近内容'
        self.sources[event['event_id']] = dict(event)
        return event

    def adopt(self, event, incoming):
        original = event.get('original_request', {k: event[k] for k in
            ('event_id','sender_id','payload','reference_target','reference_error') if k in event})
        guidance = event.get('guidance', []) + [{k: incoming[k] for k in
            ('event_id','sender_id','payload','reference_target','reference_error') if k in incoming}]
        for key in ('event_id','sender_id','message_id','payload','reference','reference_target',
                    'reference_error','reference_resource','attachment','attachment_error','context'):
            event.pop(key, None)
            if key in incoming:
                event[key] = incoming[key]
        event.update(original_request=original, guidance=guidance)

    def record(self, eid, state, turn_id=None):
        self.store.db.execute('UPDATE steering SET state=?,thread_id=?,turn_id=? WHERE event_id=?',
                              (state, self.thread_id, turn_id, eid))

    async def generate(self, tid, event, call):
        self.thread_id = tid
        self.sources[event['event_id']] = dict(event)
        async with self.lock:
            while True:
                # 无活动轮次时，把待处理引导作为下一轮输入；启动前先持久化意图。
                queued = list(self.pending())
                for row in queued:
                    self.adopt(event, await self.prepare(row['event']))
                for row in queued:
                    self.record(row['event_id'], 'submitting')
                request = dict(event)
                notifications = getattr(self.agent.backend, 'turn_changed', None)
                if notifications is not None:
                    notifications[tid] = self.changed.set
                running = asyncio.create_task(call(request))
                try:
                    while not running.done():
                        self.changed.clear()
                        turn = getattr(self.agent.backend, 'active_turns', {}).get(tid)
                        if turn is not None:
                            for row in queued:
                                self.record(row['event_id'], 'accepted', turn.id)
                            queued = []
                        pending = self.pending()
                        if turn is not None and pending:
                            row = pending[0]
                            incoming = await self.prepare(row['event'])
                            # 解析引用期间轮次可能结束；保留输入到下一轮。
                            if running.done() or getattr(self.agent.backend, 'active_turns', {}).get(tid) is not turn:
                                break
                            self.record(row['event_id'], 'submitting', turn.id)
                            try:
                                accepted = await self.agent.backend.steer(tid, turn.id, incoming)
                            except BaseException:
                                self.record(row['event_id'], 'unknown', turn.id)
                                raise SteeringUnknown('引导提交结果未知，需核查原轮次') from None
                            if not accepted:
                                self.record(row['event_id'], 'pending')
                                # 明确拒绝的输入等原轮次结束后再提交。
                                break
                            self.record(row['event_id'], 'accepted', turn.id)
                            self.adopt(event, incoming)
                            continue
                        waiter = asyncio.create_task(self.changed.wait())
                        try:
                            await asyncio.wait((running, waiter), return_when=asyncio.FIRST_COMPLETED)
                        finally:
                            waiter.cancel()
                            await asyncio.gather(waiter, return_exceptions=True)
                    result = await running
                    for row in queued:
                        self.record(row['event_id'], 'accepted', result[1].get('turn_id'))
                except BaseException as error:
                    if notifications is not None:
                        notifications.pop(tid, None)
                    running.cancel()
                    await asyncio.gather(running, return_exceptions=True)
                    for row in queued:
                        self.record(row['event_id'], 'unknown')
                    if queued or isinstance(error, SteeringUnknown):
                        self.open = False
                        self.store.update(self.job['event_id'], 'generation_unknown',
                                          error='模型或引导执行结果待核查，禁止自动重投')
                        raise SteeringUnknown('引导执行结果待核查') from error
                    raise
                if notifications is not None:
                    notifications.pop(tid, None)
                if not self.pending():
                    return result
                # 新消息未进入已结束轮次：丢弃该轮草稿及未执行控制动作。

    async def before_send(self, final):
        async with self.lock:
            if self.store.conversation(self.job['conversation'])['paused']:
                raise SteeringUnknown('会话已暂停，禁止提交新回复')
            if self.pending():
                return False
            if final:
                self.open = False
            return True
