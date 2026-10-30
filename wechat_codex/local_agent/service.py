"""本地长轮询 → 持久队列 → Codex → 受门禁保护的 MCP 回复。"""
import asyncio
import json
import time

from .mcp import allowed, context_allowed, MCPError
from . import attachments
from . import media
from .codex_backend import ModelThreadBusy, ModelUnavailable
from .steering import SteeringUnknown


class Agent:
    def __init__(self, store, mcp, backend, config):
        self.store, self.mcp, self.backend, self.config = store, mcp, backend, config
        self.stop = asyncio.Event()
        self.wake = asyncio.Event()
        self.active = {}
        self.reply_tasks = {}
        self.errors = {}
        self.ready = asyncio.Event()
        from .scheduler import Scheduler
        self.scheduler = Scheduler(self)

    async def verify(self):
        status = await self.mcp.call('bridge_status')
        policy = status.get('group_reply_policy', {})
        if (status.get('simulation') != self.config.get('simulation', False) or
                status.get('account') != self.config['account'] or
                not set(self.config['conversations']) <= set(status.get('allowed_conversations', [])) or
                policy.get('require_explicit_self_mention') is not True):
            raise ValueError('桥接账号、模式、会话白名单或群@硬性门禁不匹配')
        if status.get('subscriptions_active', 0):
            raise ValueError('仍有云端事件订阅；请先停用其他自动回复agent')
        return status

    async def collect_once(self, timeout=20):
        batch = await self.mcp.call('get_messages', consumer_id=self.config['consumer_id'],
                                    timeout=timeout, limit=1, start='latest')
        events = batch.get('events', [])
        for event in events:
            if (event.get('account') != self.config['account'] or
                    not isinstance(event.get('event_id'), str) or not 1 <= len(event['event_id']) <= 512 or
                    not isinstance(event.get('payload'), dict) or
                    (event.get('message_type') == 'text' and not isinstance(event['payload'].get('text'), str))):
                raise ValueError('消息身份或结构不匹配，不推进消费者')
        filtered = [e for e in events if context_allowed(e, self.config['account'], self.config['conversations'])]
        self.store.ingest(filtered, self.config['account'], self.config['conversations'])
        for task in self.reply_tasks.values():
            task.claim()
        if events:
            if not batch.get('ack_token'):
                raise ValueError('消息批次缺少确认token')
            await self.mcp.call('ack_messages', consumer_id=self.config['consumer_id'], ack_token=batch['ack_token'])
        self.wake.set()

    async def collect_loop(self):
        while not self.stop.is_set():
            try:
                await self.collect_once()
                self.errors.pop('source', None)
            except Exception as error:
                self.errors['source'] = type(error).__name__
                try:
                    await asyncio.wait_for(self.stop.wait(), 2)
                except TimeoutError:
                    pass

    async def process(self, job):
        from .steering import ReplyTask
        controller = self.reply_tasks.get(job['conversation'])
        if controller is None or controller.job['event_id'] != job['event_id']:
            controller = ReplyTask(self, job)
        self.reply_tasks[job['conversation']] = controller
        try:
            await self._process(job)
        finally:
            state = self.store.db.execute('SELECT state FROM jobs WHERE event_id=?', (job['event_id'],)).fetchone()[0]
            if state in ('accepted','unknown','failed','generation_unknown'):
                self.reply_tasks.pop(job['conversation'], None)

    async def generate(self, tid, event, progress=None):
        async def call(current):
            method = getattr(self.backend, 'generate_with_progress', None)
            if progress is not None and callable(method):
                return await method(tid, current, progress)
            return await self.backend.generate(tid, current)
        controller = self.reply_tasks.get(event['conversation_id'])
        if controller is None or event.get('is_scheduled_task'):
            return await call(event)
        reply, generation = await controller.generate(tid, event, call)
        controller.current = dict(event)
        source_id = generation.get('request_event_id') or event['event_id']
        if source_id not in controller.sources:
            raise ValueError('模型控制动作引用了未授权的输入事件')
        row = self.store.db.execute('SELECT state FROM jobs WHERE event_id=?', (controller.job['event_id'],)).fetchone()
        self.store.update(controller.job['event_id'], row['state'], result={'input_sources':controller.sources})
        return reply, {**generation, 'input_event_id': source_id, 'input_version': controller.version}

    def input_event(self, job, event, generation):
        controller = self.reply_tasks.get(job['conversation'])
        if controller is None:
            return event
        return controller.sources.get(generation.get('input_event_id'), event)

    async def _process(self, job):
        if (self.config.get('adaptive_context', True) and callable(getattr(self.backend,'create_task_thread',None))
                and (job['state']=='pending' or json.loads(job['result'] or '{}').get('adaptive'))):
            from .staged import process
            return await process(self,job)
        eid = job['event_id']
        event = json.loads(job['event'])
        # 仅使用操作员配置，消息正文不能更改时限。
        event.pop('_model_timeout', None)
        for key, default in (('startup_timeout',60),('generation_idle_timeout',30),('turn_timeout',600)):
            event['_'+key] = self.config.get(key,default)
        if event['conversation_id'] in self.config.get('model_timeouts', {}):
            event['_model_timeout'] = self.config['model_timeouts'][event['conversation_id']]
        try:
            if not allowed(event, self.config['account'], self.config['conversations']):
                self.store.update(eid, 'failed', error='会话或@信息不再允许回复')
                return
            state, reply = job['state'], job['reply']
            if state == 'pending':
                if self.config.get('mark_read_on_process', True):
                    try:
                        read_result = await asyncio.wait_for(
                            self.mcp.call('mark_conversation_read', event_id=eid), 5)
                        self.store.update(eid, 'pending', result={'conversation_read': read_result})
                    except (MCPError, TimeoutError):
                        self.store.update(eid, 'pending', result={'conversation_read': {
                            'state': 'unknown', 'error': 'read_status_unconfirmed'}})
                try:
                    context = self.store.context(event['event_id'])
                except ValueError:
                    self.store.update(eid, 'failed', error='待补入上下文缺失或超过512KiB，请人工整理后恢复')
                    return
                event['context'] = context
                if event.get('reference'):
                    try:
                        target = await self.mcp.call('resolve_reference', event_id=event['event_id'])
                        event['reference_target'] = {k: target[k] for k in
                            ('event_id', 'message_type', 'sender_id', 'message_id') if k in target}
                        event['reference_target']['payload'] = {
                            k: target['payload'][k] for k in ('text', 'name', 'mime', 'size') if k in target['payload']}
                        event['reference_resource'] = target
                    except MCPError:
                        target = None
                        event['reference_error'] = '无法精确找到原生引用目标，请重新发送并引用；不要猜测附近消息'
                else:
                    target = None
                if (event['conversation_id'] not in self.config.get('model_timeouts', {}) and
                        (target is None or target.get('message_type') in ('text', 'unknown'))):
                    event['_model_timeout'] = self.config.get('text_model_timeout', 30)
                conversation = self.store.conversation(job['conversation'])
                thread_id = await self.backend.ensure_thread(job['conversation'], conversation['thread_id'])
                self.store.set_thread(job['conversation'], thread_id)
                self.store.update(eid, 'generating')
                try:
                    reply, generation = await self.generate(thread_id, event)
                    reply, generation = await self.scheduler.resolve_actions(job, thread_id, event, reply, generation)
                    controller = self.reply_tasks.get(job['conversation'])
                    if controller is not None and controller.current is not None:
                        event.clear(); event.update(controller.current)
                    if event['event_id'] != eid:
                        target = event.get('reference_resource')
                    while generation.get('read_attachment'):
                        source = self.input_event(job, event, generation)
                        target = source.get('reference_resource', target if source['event_id']==eid else None)
                        already_read = any(r['event_id']==source['event_id'] for r in event.get('resource_results', []))
                        if target is None or target.get('message_type') in ('text', 'unknown') or already_read:
                            reply = '请使用微信“引用”选中要处理的内容，并说明处理要求。'
                            generation['read_attachment'] = False
                        else:
                            self.store.update(eid, 'generating', result={'attachment_plan': generation})
                            followup = dict(event)
                            followup.pop('context', None)
                            followup.pop('attachment', None)
                            followup.pop('attachment_error', None)
                            try:
                                followup['attachment'] = await attachments.fetch(self.mcp, source['event_id'], target,
                                    self.store.path / 'resources', self.config.get('resource_timeout', 60))
                                followup['attachment'] = await asyncio.to_thread(media.prepare, followup['attachment'],
                                    {'read_source': generation.get('read_source', False),
                                     'profile_dir': self.config.get('browser_profile_dir', str(self.store.path / 'browser-profile')),
                                     'channel': self.config.get('browser_channel', 'msedge')})
                            except ValueError as error:
                                followup['attachment_error'] = str(error)
                            except (MCPError, OSError, KeyError) as error:
                                reason = error.strerror if isinstance(error, OSError) else str(error)
                                followup['attachment_error'] = '资源获取或协议校验失败：' + type(error).__name__ + '：' + str(reason or '未知原因')[:500]
                            followup['attachment_event_id'] = source['event_id']
                            followup['resource_results'] = event.get('resource_results', []) + [
                                {'event_id':source['event_id'], **{k:followup[k] for k in ('attachment','attachment_error') if k in followup}}]
                            reply, generation = await self.generate(thread_id, followup)
                            reply, generation = await self.scheduler.resolve_actions(job, thread_id, followup, reply, generation, stage='attachment')
                            event.clear(); event.update(controller.current or followup)
                except ModelUnavailable as error:
                    reply=error.reply
                    generation=error.details()
                except TimeoutError as error:
                    if (getattr(error,'timeout_stage',None)=='startup' and
                            getattr(error,'interruption_confirmed',False) is False):
                        self.store.update(eid,'generation_unknown',error='模型启动超时，轮次状态待核查，禁止自动重跑')
                        return
                    reply = '这次处理超时，未能完成。不会自动重跑；请稍后重试，需要处理引用的内容时请重新引用目标并说明要求。'
                    generation = {'thread_id':thread_id, 'turn_id':getattr(error,'turn_id',None),
                                  'error':'model_timeout', 'read_attachment':False,
                                  'interruption_confirmed':getattr(error,'interruption_confirmed',False)}
                except ModelThreadBusy:
                    reply = '上一次处理尚未确认停止，这条请求暂未执行。请稍后重试；不会把新请求加入旧任务。'
                    generation = {'thread_id':thread_id, 'error':'previous_model_turn_active', 'read_attachment':False}
                except asyncio.CancelledError:
                    self.store.update(eid, 'generation_unknown', error='模型轮次被中断，需人工核查')
                    raise
                except Exception as error:
                    self.store.update(eid, 'generation_unknown', error='模型轮次失败：' + type(error).__name__)
                    return
                if self.config.get('mark_read_on_process', True):
                    try:
                        read_result = await asyncio.wait_for(
                            self.mcp.call('get_conversation_read_status', event_id=eid), 5)
                        self.store.update(eid, 'generating', result={'conversation_read': read_result})
                    except (MCPError, TimeoutError):
                        pass  # 已读状态异常不能改变已经生成的回答。
                self.store.update(eid, 'generated', reply=reply, result={'generation': generation, 'task_input':event}, context=event.get('context',context))
                state = 'generated'
            if state == 'generated':
                if self.stop.is_set():
                    return  # 持久保留尚未发送的回答，等待显式重启。
                await self.verify()  # 每次发送前检查账号、门禁及是否存在竞争代理。
                controller = self.reply_tasks.get(job['conversation'])
                if controller is not None and not await controller.before_send(True):
                    current = controller.current or json.loads(job['result'] or '{}').get('task_input', event)
                    return await self._process({**job, 'state':'pending', 'event':json.dumps(current)})
                self.store.update(eid, 'sending')  # 请求离开本进程前先提交事务。
                try:
                    result = await self.mcp.call('reply_text', event_id=eid, text=reply)
                except Exception:
                    await self.reconcile(eid)
                    return
                self.apply_result(eid, result)
            elif state == 'sending':
                await self.reconcile(eid)
        except asyncio.CancelledError:
            raise
        except SteeringUnknown:
            self.store.update(eid, 'generation_unknown', error='引导接纳结果待核查，禁止自动重投')
        except Exception as error:
            # 恢复失败时不得静默创建其他线程，
            # 也不得回退到其他收件人或不受门禁保护的 HTTP 发送。
            self.store.update(eid, 'failed', error=type(error).__name__)

    def apply_result(self, eid, result):
        state = result.get('state')
        if state == 'accepted':
            self.store.update(eid, 'accepted', result={'operation': result, 'accepted_at': time.time()})
        elif state in ('queued', 'running'):
            self.store.update(eid, 'sending', result={'operation': result})
        else:
            self.store.update(eid, 'unknown' if state == 'unknown' else 'failed', result={'operation': result},
                              error='发送未确认成功，禁止自动重发')

    async def reconcile(self, eid):
        try:
            result = await self.mcp.call('get_reply_status', event_id=eid)
        except Exception:
            self.store.update(eid, 'unknown', error='已提交请求的状态不可确认，禁止自动重发')
            return
        self.apply_result(eid, result)

    async def worker_loop(self):
        last_cleanup = 0
        while not self.stop.is_set():
            await self.scheduler.tick()
            if time.monotonic() - last_cleanup >= 30:
                self.store.prune()
                protected = [r[0] for r in self.store.db.execute("SELECT event_id FROM jobs WHERE state NOT IN ('accepted','merged_done')")]
                attachments.prune(self.store.path / 'resources', protected)
                last_cleanup = time.monotonic()
            for cid, task in list(self.active.items()):
                if task.done():
                    await task
                    del self.active[cid]
            for job in self.store.ready(self.active):
                if len(self.active) >= self.config.get('max_concurrency', 2):
                    break
                cid = job['conversation']
                if cid not in self.active:
                    self.active[cid] = asyncio.create_task(self.process(job))
            for run in self.scheduler.ready():
                if len(self.active) >= self.config.get('max_concurrency', 2):
                    break
                cid = run['conversation']
                if cid not in self.active and (run['state'] == 'sending' or not self.store.conversation(cid)['paused']):
                    self.active[cid] = asyncio.create_task(self.scheduler.process(run))
            self.wake.clear()
            try:
                await asyncio.wait_for(self.wake.wait(), 1)
            except TimeoutError:
                pass

    async def run(self):
        await self.verify()
        self.store.recover()
        from .schedule_store import recover
        recover(self.store, time.time())
        loops = []
        try:
            await self.backend.start()
            # 报告就绪前，将新消费者注册到 latest 位置。
            await self.collect_once(0)
            loops = [asyncio.create_task(self.collect_loop()), asyncio.create_task(self.worker_loop()),
                     asyncio.create_task(self.stop.wait())]
            self.ready.set()
            done, _ = await asyncio.wait(loops, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                await task
        finally:
            for task in loops:
                task.cancel()
            await asyncio.gather(*loops, return_exceptions=True)
            # 正常关闭时允许有界模型轮次执行完成。
            if self.active:
                await asyncio.gather(*self.active.values(), return_exceptions=True)
            await self.backend.close()
