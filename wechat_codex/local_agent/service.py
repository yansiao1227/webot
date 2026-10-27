"""本地长轮询 → 持久队列 → Codex → 受门禁保护的 MCP 回复。"""
import asyncio
import json
import time

from .mcp import allowed, context_allowed, MCPError
from . import attachments
from . import media
from .codex_backend import ModelThreadBusy, ModelUnavailable


class Agent:
    def __init__(self, store, mcp, backend, config):
        self.store, self.mcp, self.backend, self.config = store, mcp, backend, config
        self.stop = asyncio.Event()
        self.wake = asyncio.Event()
        self.active = {}
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
                    context = self.store.context(eid)
                except ValueError:
                    self.store.update(eid, 'failed', error='待补入上下文缺失或超过512KiB，请人工整理后恢复')
                    return
                event['context'] = context
                if event.get('reference'):
                    try:
                        target = await self.mcp.call('resolve_reference', event_id=eid)
                        event['reference_target'] = {k: target[k] for k in
                            ('event_id', 'message_type', 'sender_id', 'message_id') if k in target}
                        event['reference_target']['payload'] = {
                            k: target['payload'][k] for k in ('text', 'name', 'mime', 'size') if k in target['payload']}
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
                    reply, generation = await self.backend.generate(thread_id, event)
                    reply, generation = await self.scheduler.resolve_actions(job, thread_id, event, reply, generation)
                    if generation.get('read_attachment'):
                        if target is None or target.get('message_type') in ('text', 'unknown'):
                            reply = '请使用微信“引用”选中要处理的内容，并说明处理要求。'
                        else:
                            self.store.update(eid, 'generating', result={'attachment_plan': generation})
                            followup = dict(event)
                            followup.pop('context', None)
                            try:
                                followup['attachment'] = await attachments.fetch(self.mcp, eid, target,
                                    self.store.path / 'resources', self.config.get('resource_timeout', 60))
                                followup['attachment'] = await asyncio.to_thread(media.prepare, followup['attachment'],
                                    {'read_source': generation.get('read_source', False),
                                     'profile_dir': self.config.get('browser_profile_dir', str(self.store.path / 'browser-profile')),
                                     'channel': self.config.get('browser_channel', 'msedge')})
                            except ValueError as error:
                                followup['attachment_error'] = str(error)
                            except (MCPError, OSError, KeyError):
                                followup['attachment_error'] = '资源获取或协议校验失败；原内容尚未读取，不能声称已分析'
                            reply, generation = await self.backend.generate(thread_id, followup)
                            reply, generation = await self.scheduler.resolve_actions(job, thread_id, followup, reply, generation, stage='attachment')
                            if generation.get('read_attachment'):
                                reply = '引用的内容本轮无法完成处理，请检查后重新引用。'
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
                self.store.update(eid, 'generated', reply=reply, result={'generation': generation}, context=context)
                state = 'generated'
            if state == 'generated':
                if self.stop.is_set():
                    return  # 持久保留尚未发送的回答，等待显式重启。
                await self.verify()  # 每次发送前检查账号、门禁及是否存在竞争代理。
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
                protected = [r[0] for r in self.store.db.execute("SELECT event_id FROM jobs WHERE state!='accepted'")]
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
