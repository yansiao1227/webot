import asyncio
import json
from pathlib import Path
import tempfile
import time
import unittest
import argparse
from types import SimpleNamespace
from unittest.mock import AsyncMock

from local_agent.storage import Store
from local_agent.service import Agent
from local_agent.scheduler import Scheduler
from local_agent.schedule_store import claim_due, recover
from local_agent.schedule_time import normalize, next_time, display
from local_agent.schedule_cli import execute, add_parser
from fakes import CONFIG, event, FakeMCP


class ScheduleMCP(FakeMCP):
    def __init__(self):
        super().__init__()
        self.grants = {}
        self.schedule_sends = []
        self.states = {}
        self.unknown = False

    async def call(self, name, **args):
        if name == 'authorize_schedule':
            source = next(e for e in self.events if e['event_id'] == args['event_id'])
            result = {'active': True, 'conversation_id': source['conversation_id'], 'sender_id': source['sender_id']}
            self.grants[args['schedule_id']] = result
            return result
        if name == 'get_message':
            return next(e for e in self.events if e['event_id'] == args['event_id'])
        if name == 'get_schedule_authorization':
            return self.grants[args['schedule_id']]
        if name == 'revoke_schedule':
            self.grants[args['schedule_id']]['active'] = False
            return {'active': False}
        if name == 'send_scheduled_text':
            key = (args['run_id'], args['part_index'])
            if key not in self.states:
                self.schedule_sends.append(args)
                self.states[key] = {'state': 'unknown' if self.unknown else 'accepted'}
            return self.states[key]
        if name == 'get_scheduled_text_status':
            return self.states[(args['run_id'], args['part_index'])]
        return await super().call(name, **args)


class TimeTests(unittest.TestCase):
    def test_beijing_daily_weekly_and_utc(self):
        base = normalize('once', '2026-10-27T08:00:00+08:00', 0)['run_at']
        self.assertEqual(display(next_time(normalize('daily', '09:00', base), base)), '2026-10-27T09:00:00+08:00')
        self.assertEqual(display(next_time(normalize('weekly', '1 09:00', base), base)), '2026-11-02T09:00:00+08:00')
        self.assertEqual(normalize('once', '2026-10-27T00:00:00Z', 0)['run_at'], base)
        self.assertEqual(normalize('once', '2026-10-27T08:00:00', 0)['run_at'], base)
        self.assertEqual(normalize('once', '+10m', base)['run_at'], base + 600)

    def test_interval_anchor_does_not_drift_and_invalid_rules(self):
        rule = normalize('interval', '60', 1000)
        self.assertEqual(next_time(rule, 1121), 1180)
        for kind, value in [('interval', '59'), ('daily', '24:00'), ('weekly', '0 09:00'), ('once', '+0m'), ('once', '2020-01-01'), ('cron', '* * * * *')]:
            with self.subTest(kind=kind, value=value), self.assertRaises(ValueError):
                normalize(kind, value, 1800000000)


class SchedulerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(self.temp.name, {k: CONFIG[k] for k in ('account', 'mcp_url', 'consumer_id')})
        self.mcp = ScheduleMCP()
        self.backend = SimpleNamespace(create_task_thread=AsyncMock(return_value='task-thread'),
            generate=AsyncMock(return_value=('实际成果', {'continue_reply': False, 'context_mode': 'none'})))
        self.agent = Agent(self.store, self.mcp, self.backend, CONFIG)
        self.now = 1800000000.0
        self.scheduler = self.agent.scheduler = Scheduler(self.agent, lambda: self.now)
        self.source = dict(event('source'), sender_id='wxid_test_peer')
        self.mcp.events = [self.source]

    async def asyncTearDown(self):
        self.store.close()
        self.temp.cleanup()

    async def create(self, key='create', source=None, **kwargs):
        source = source or self.source
        if source not in self.mcp.events:
            self.mcp.events.append(source)
        return await self.scheduler.command(key, {'action': 'create', 'name': '提醒', 'message': '休息',
            'schedule_type': 'once', 'schedule_value': '+10m', **kwargs}, source)

    def run_row(self):
        return self.store.db.execute('SELECT * FROM schedule_runs ORDER BY created DESC LIMIT 1').fetchone()

    async def test_duplicate_creation_and_fixed_reminder_never_calls_model(self):
        result = await self.create()
        self.assertEqual(await self.create(), result)
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM schedules').fetchone()[0], 1)
        self.now += 600
        claim_due(self.store, self.now); claim_due(self.store, self.now)
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM schedule_runs').fetchone()[0], 1)
        await self.scheduler.process(self.run_row())
        self.backend.generate.assert_not_awaited()
        self.assertEqual(self.mcp.schedule_sends[0]['text'], '休息')
        self.assertEqual(self.run_row()['state'], 'accepted')
        self.assertEqual(self.scheduler.task(result['task']['task_id'])['status'], 'completed')

    async def test_restart_once_catches_up_and_periodic_skips(self):
        once = await self.create()
        periodic = await self.create('periodic', schedule_type='interval', schedule_value='60')
        self.now += 2000
        recover(self.store, self.now)
        self.assertGreater(self.scheduler.task(periodic['task']['task_id'])['next_run'], self.now)
        claim_due(self.store, self.now)
        self.assertEqual(self.run_row()['schedule_id'], once['task']['task_id'])
        await self.scheduler.process(self.run_row())
        self.assertIn('延迟执行', self.mcp.schedule_sends[0]['text'])

    async def test_group_owner_scope_and_native_mention_gate(self):
        group = dict(event('group', conversation='12345@chatroom'), sender_id='member-a')
        result = await self.create(source=group)
        tid = result['task']['task_id']
        other = {**group, 'event_id': 'other', 'sender_id': 'member-b'}
        result = await self.scheduler.command('other-list', {'action': 'list'}, other)
        self.assertEqual(result['tasks'], [])
        result = await self.scheduler.command('other-pause', {'action': 'pause', 'task_id': tid}, other)
        self.assertFalse(result['ok'])
        bad = dict(event('bad', conversation='12345@chatroom', mention=False), sender_id='member-a')
        self.assertFalse((await self.create('bad', source=bad))['ok'])

    async def test_pause_resume_update_cancel(self):
        tid = (await self.create())['task']['task_id']
        for action in ('pause', 'resume'):
            self.assertTrue((await self.scheduler.command(action, {'action': action, 'task_id': tid}, self.source))['ok'])
        updated = await self.scheduler.command('update', {'action': 'update', 'task_id': tid,
            'schedule_type': 'daily', 'schedule_value': '09:00', 'message': '开会'}, self.source)
        self.assertEqual(updated['task']['message'], '开会')
        self.assertTrue((await self.scheduler.command('cancel', {'action': 'cancel', 'task_id': tid}, self.source))['ok'])
        self.assertFalse(self.mcp.grants[tid]['active'])
        self.assertFalse((await self.scheduler.command('resume2', {'action': 'resume', 'task_id': tid}, self.source))['ok'])

    async def test_unknown_send_and_model_crash_never_replay(self):
        tid = (await self.create())['task']['task_id']
        self.now += 600; claim_due(self.store, self.now)
        self.mcp.unknown = True
        await self.scheduler.process(self.run_row())
        self.assertEqual(self.scheduler.task(tid)['status'], 'blocked')
        recover(self.store, self.now + 1000)
        self.assertEqual(list(self.scheduler.ready()), [])
        self.assertEqual(len(self.mcp.schedule_sends), 1)
        tid2 = (await self.create('ai', message='', ai_task='搜索新闻'))['task']['task_id']
        self.now += 600; claim_due(self.store, self.now)
        self.backend.generate.side_effect = RuntimeError('模型中断')
        await self.scheduler.process(self.run_row())
        self.assertEqual(self.scheduler.task(tid2)['status'], 'blocked')
        self.assertEqual(self.run_row()['state'], 'generation_unknown')
        recover(self.store, self.now)
        self.assertEqual(list(self.scheduler.ready()), [])

    async def test_ai_isolated_context_and_no_recursive_actions(self):
        await self.create(message='', ai_task='整理公开新闻')
        self.now += 600; claim_due(self.store, self.now)
        await self.scheduler.process(self.run_row())
        value = self.backend.generate.call_args.args[1]
        self.assertTrue(value['is_scheduled_task'])
        self.assertNotIn('attachment', value)
        self.assertEqual(self.mcp.schedule_sends[0]['text'], '实际成果')
        await self.create('recursive', message='', ai_task='普通任务')
        self.now += 600; claim_due(self.store, self.now)
        self.backend.generate.return_value = ('创建另一个', {'schedule_action': {'action': 'create'}})
        await self.scheduler.process(self.run_row())
        self.assertEqual(len(self.mcp.schedule_sends), 1)

    async def test_sending_recovery_queries_original_and_retains_body(self):
        await self.create()
        self.now += 600; claim_due(self.store, self.now)
        self.mcp.states[ (self.run_row()['id'], 0) ] = {'state': 'queued'}
        await self.scheduler.process(self.run_row())
        self.assertEqual(self.run_row()['state'], 'sending')
        self.mcp.states[(self.run_row()['id'], 0)] = {'state': 'accepted'}
        recover(self.store, self.now)
        await self.scheduler.process(self.run_row())
        self.assertEqual(self.run_row()['state'], 'accepted')
        self.assertEqual(len(self.mcp.schedule_sends), 0)

    async def test_model_receives_real_management_receipt_before_confirmation(self):
        self.store.ingest([self.source])
        job = self.store.ready()[0]
        action = {'action': 'create', 'name': '休息', 'message': '休息', 'schedule_type': 'once', 'schedule_value': '+10m'}
        self.backend.generate.return_value = ('已经保存', {'schedule_action': None})
        reply, _ = await self.scheduler.resolve_actions(job, 'thread', self.source, '内部说明', {'schedule_action': action})
        self.assertEqual(reply, '已经保存')
        receipt = self.backend.generate.call_args.args[1]['schedule_result']
        self.assertTrue(receipt['ok'])
        self.assertEqual(self.mcp.sent, [])
        self.backend.generate.return_value = ('重复创建', {'schedule_action': action})
        await self.scheduler.resolve_actions(job, 'thread', self.source, '', {'schedule_action': action})
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM schedules').fetchone()[0], 1)

    async def test_recovery_of_interrupted_generation_blocks_task(self):
        tid = (await self.create())['task']['task_id']
        self.now += 600; claim_due(self.store, self.now)
        self.store.db.execute("UPDATE schedule_runs SET state='generating'")
        recover(self.store, self.now)
        self.assertEqual(self.run_row()['state'], 'generation_unknown')
        self.assertEqual(self.scheduler.task(tid)['status'], 'blocked')

    async def test_cross_connection_claim_is_unique_and_cli_requires_service(self):
        await self.create()
        self.now += 600
        second = Store(self.temp.name, {k: CONFIG[k] for k in ('account', 'mcp_url', 'consumer_id')})
        try:
            claim_due(self.store, self.now); claim_due(second, self.now)
        finally:
            second.close()
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM schedule_runs').fetchone()[0], 1)
        args = SimpleNamespace(operation='pause')
        self.assertFalse(execute(Path(self.temp.name), args)['ok'])

    async def test_revocation_prevents_ai_tools_and_later_delivery(self):
        tid = (await self.create(message='', ai_task='整理'))['task']['task_id']
        self.mcp.grants[tid]['active'] = False
        self.now += 600; claim_due(self.store, self.now)
        await self.scheduler.process(self.run_row())
        self.backend.generate.assert_not_awaited()
        self.assertEqual(self.mcp.schedule_sends, [])

    async def test_normal_agent_paths_send_only_real_confirmation(self):
        self.backend.ensure_thread = AsyncMock(return_value='owner-thread')
        action = {'action': 'create', 'name': '提醒', 'message': '休息', 'schedule_type': 'once', 'schedule_value': '+10m'}
        for adaptive in (False, True):
            source = {**self.source, 'event_id': 'path-' + str(adaptive)}
            self.mcp.events.append(source)
            self.store.ingest([source])
            self.agent.config = {**CONFIG, 'adaptive_context': adaptive, 'mark_read_on_process': False}
            self.backend.generate.side_effect = [('仅供内部', {'schedule_action': action}),
                ('计划已保存', {'schedule_action': None, 'continue_reply': False, 'context_mode': 'none'})]
            await self.agent.process(self.store.ready()[0])
            self.assertEqual(self.mcp.sent[-1]['text'], '计划已保存')
            self.assertNotIn('仅供内部', [part['text'] for part in self.mcp.sent])
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM schedules').fetchone()[0], 2)

    async def test_paused_due_once_can_resume_without_losing_pending_run(self):
        tid = (await self.create())['task']['task_id']
        self.now += 600; claim_due(self.store, self.now)
        await self.scheduler.command('pause', {'action': 'pause', 'task_id': tid}, self.source)
        self.assertEqual(list(self.scheduler.ready()), [])
        result = await self.scheduler.command('resume', {'action': 'resume', 'task_id': tid}, self.source)
        self.assertTrue(result['ok'])
        await self.scheduler.process(self.run_row())
        self.assertEqual(self.run_row()['state'], 'accepted')

    async def test_ai_continuations_are_ordered_and_use_same_execution(self):
        await self.create(message='', ai_task='整理')
        self.backend.generate.side_effect = [('第一段', {'continue_reply': True, 'next_step': '继续'}),
                                             ('第二段', {'continue_reply': False})]
        self.now += 600; claim_due(self.store, self.now)
        await self.scheduler.process(self.run_row())
        self.assertEqual([p['text'] for p in self.mcp.schedule_sends], ['第一段', '第二段'])
        self.assertEqual([p['part_index'] for p in self.mcp.schedule_sends], [0, 1])
        self.assertEqual(len({p['run_id'] for p in self.mcp.schedule_sends}), 1)

    async def test_cancel_during_generation_prevents_submission(self):
        tid = (await self.create(message='', ai_task='整理'))['task']['task_id']
        async def generate(*args):
            await self.scheduler.command('cancel-during', {'action': 'cancel', 'task_id': tid}, self.source)
            return '成果', {}
        self.backend.generate.side_effect = generate
        self.now += 600; claim_due(self.store, self.now)
        await self.scheduler.process(self.run_row())
        self.assertEqual(self.mcp.schedule_sends, [])
        self.assertEqual(self.scheduler.task(tid)['status'], 'cancelled')

    async def test_worker_shares_concurrency_and_conversation_lock(self):
        await self.create()
        other = dict(event('other', conversation='wxid_test_other'), sender_id='wxid_test_other')
        await self.create('other-schedule', source=other, message='', ai_task='其他会话定时任务')
        # 把第一个也改成AI任务，便于观察它与同会话普通消息的执行顺序。
        self.store.db.execute("UPDATE schedules SET message='',ai_task='定时任务' WHERE conversation=?", (self.source['conversation_id'],))
        self.store.ingest([self.source])
        self.agent.config = {**CONFIG, 'adaptive_context': False, 'mark_read_on_process': False}
        self.backend.ensure_thread = AsyncMock(return_value='owner')
        running, order, max_active = set(), [], 0
        async def generate(tid, value):
            nonlocal max_active
            cid = value['conversation_id']
            self.assertNotIn(cid, running)
            running.add(cid); max_active = max(max_active, len(running))
            order.append((cid, value.get('is_scheduled_task', False)))
            await asyncio.sleep(.01)
            running.remove(cid)
            if len(order) >= 3:
                self.agent.stop.set(); self.agent.wake.set()
            return '实际成果', {}
        self.backend.generate.side_effect = generate
        self.now += 600
        await asyncio.wait_for(self.agent.worker_loop(), 5)
        await asyncio.gather(*self.agent.active.values())
        peer_order = [scheduled for cid, scheduled in order if cid == self.source['conversation_id']]
        self.assertEqual(peer_order, [False, True])
        self.assertLessEqual(max_active, CONFIG['max_concurrency'])

    async def test_cli_request_is_persisted_and_applied_by_live_service(self):
        self.now = time.time()
        directory = Path(self.temp.name)
        (directory / 'process.json').write_text(json.dumps({'running': True, 'ready': True, 'heartbeat': time.time()}))
        parser = argparse.ArgumentParser()
        add_parser(parser.add_subparsers(dest='command'))
        args = parser.parse_args(['schedules', 'create', '--event-id', 'source', '--name', '提醒',
            '--message', '休息', '--schedule-type', 'once', '--schedule-value', '+10m'])
        async def service():
            for _ in range(100):
                if self.store.db.execute("SELECT 1 FROM schedule_commands WHERE state='pending'").fetchone():
                    await self.scheduler.tick()
                    return
                await asyncio.sleep(.01)
            self.fail('CLI没有持久保存请求')
        result, _ = await asyncio.gather(asyncio.to_thread(execute, directory, args), service())
        self.assertTrue(result['ok'])
        request = parser.parse_args(['schedules', 'request', '--request-id', result['request_id']])
        self.assertEqual(execute(directory, request)['state'], 'done')
        self.assertEqual(len(execute(directory, parser.parse_args(['schedules', 'list']))['tasks']), 1)

    async def test_cancelled_submitted_run_only_queries_original_operation(self):
        tid = (await self.create())['task']['task_id']
        self.now += 600; claim_due(self.store, self.now)
        rid = self.run_row()['id']
        self.mcp.states[(rid, 0)] = {'state': 'queued'}
        await self.scheduler.process(self.run_row())
        await self.scheduler.command('cancel', {'action': 'cancel', 'task_id': tid}, self.source)
        self.mcp.states[(rid, 0)] = {'state': 'accepted'}
        self.assertEqual(len(self.scheduler.ready()), 1)
        await self.scheduler.process(self.run_row())
        self.assertEqual(self.run_row()['state'], 'accepted')
        self.assertEqual(self.scheduler.task(tid)['status'], 'cancelled')
        self.assertEqual(self.mcp.schedule_sends, [])
