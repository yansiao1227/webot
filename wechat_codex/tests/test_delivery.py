import asyncio
import json
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from local_agent.delivery import defer, due, reconcile, NOTICE, WAIT_SECONDS
from local_agent.progress import ProgressDeliveryError
from local_agent.runtime import identity
from local_agent.service import Agent
from local_agent.storage import Store
from fakes import CONFIG, event, FakeMCP, FakeBackend


class Backend(FakeBackend):
    async def create_task_thread(self, cid, eid, thread_id=None):
        return await self.ensure_thread(cid, thread_id)


class DeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(self.temp.name, identity(CONFIG))
        self.mcp = FakeMCP()
        self.backend = Backend()
        self.agent = Agent(self.store, self.mcp, self.backend, CONFIG)

    async def asyncTearDown(self):
        self.store.close()
        self.temp.cleanup()

    def row(self, eid='root'):
        return dict(self.store.db.execute('SELECT * FROM jobs WHERE event_id=?', (eid,)).fetchone())

    async def progress_wait(self, state='running', interruption=True, on_interrupt=None):
        self.store.ingest([event('root'), event('following')])
        original = self.mcp.call

        async def call(name, **args):
            if name == 'reply_text_part' and args['event_id'] == 'root' and args['part_index'] == 0:
                key = ('root', 0)
                if key not in self.mcp.operations:
                    self.mcp.sent.append(args)
                    self.mcp.operations[key] = {'state': state, 'request_id': 'stable-original'}
                return self.mcp.operations[key]
            return await original(name, **args)

        self.mcp.call = call

        async def stream(tid, value, callback):
            try:
                await callback('原进度', {'key': 'original', 'kind': 'commentary'})
            except ProgressDeliveryError as error:
                self.assertFalse(self.agent.reply_tasks[value['conversation_id']].open)
                if on_interrupt is not None:
                    on_interrupt()
                error.interruption_confirmed = interruption
                raise
            self.fail('进度未确认后不得继续模型')

        self.backend.generate_with_progress = AsyncMock(side_effect=stream)
        with patch('local_agent.staged.PROGRESS_WAIT_SECONDS', 0):
            await self.agent.process(self.store.ready()[0])

    async def test_delayed_progress_queries_original_then_sends_one_final_notice(self):
        await self.progress_wait()
        self.assertEqual(self.row()['state'], 'send_wait')
        self.assertEqual([j['event_id'] for j in self.store.ready()], ['following'])
        self.assertFalse(self.store.conversation(event()['conversation_id'])['paused'])
        self.assertEqual(due(self.store), [])  # 后台查询有退避，不能忙轮询。
        self.mcp.operations[('root', 0)]['state'] = 'accepted'
        await reconcile(self.agent, self.row())
        self.assertEqual(self.row()['state'], 'generated')
        self.assertEqual(self.row()['reply'], NOTICE)
        await self.agent.process(self.row())
        self.assertEqual(self.row()['state'], 'interrupted')
        self.assertEqual([(x['part_index'], x['text']) for x in self.mcp.sent], [(0, '原进度'), (1, NOTICE)])
        self.backend.generate_with_progress.assert_awaited_once()
        self.assertEqual(due(self.store, float('inf')), [])
        self.assertTrue(self.mcp.sent[-1]['final'])

    async def test_persistent_unknown_does_not_resend_or_block_following(self):
        await self.progress_wait('unknown')
        self.assertEqual(self.row()['state'], 'send_held')
        for _ in range(3):
            await reconcile(self.agent, self.row())
        self.backend.generate_with_progress = None
        await self.agent.process(self.store.ready()[0])
        self.assertEqual(self.row('following')['state'], 'accepted')
        self.assertEqual(len([x for x in self.mcp.sent if x['event_id'] == 'root']), 1)
        self.assertEqual(self.row()['state'], 'send_held')
        self.assertEqual(self.store.status()['blocked'], [])

    async def test_failed_progress_is_terminal_without_skipping_failed_index(self):
        await self.progress_wait('failed')
        self.assertEqual(self.row()['state'], 'send_failed')
        self.assertEqual(due(self.store, float('inf')), [])
        self.assertEqual(len(self.mcp.sent), 1)
        self.assertEqual([j['event_id'] for j in self.store.ready()], ['following'])

    async def test_wait_becomes_held_after_deadline_but_still_queries_same_intent(self):
        await self.progress_wait()
        data = json.loads(self.row()['result'])
        with patch('local_agent.delivery.time.time', return_value=data['delivery']['started_at'] + WAIT_SECONDS + 1):
            await reconcile(self.agent, self.row())
        self.assertEqual(self.row()['state'], 'send_held')
        self.assertEqual(len(self.mcp.sent), 1)
        self.assertEqual(due(self.store, float('inf'))[0]['event_id'], 'root')

    async def test_restart_preserves_wait_and_never_regenerates_original(self):
        await self.progress_wait()
        self.store.close()
        self.store = Store(self.temp.name, identity(CONFIG))
        self.agent.store = self.store
        self.store.recover()
        self.assertEqual(self.row()['state'], 'send_wait')
        self.assertEqual([j['event_id'] for j in self.store.ready()], ['following'])
        self.mcp.operations[('root', 0)]['state'] = 'accepted'
        await reconcile(self.agent, self.row())
        self.backend.generate_with_progress = AsyncMock(side_effect=AssertionError('不得重跑'))
        self.backend.create_task_thread = AsyncMock(side_effect=AssertionError('不得恢复模型'))
        await self.agent.process(self.row())
        self.assertEqual(self.row()['state'], 'interrupted')

    async def test_unconfirmed_model_interruption_still_requires_verification(self):
        await self.progress_wait(interruption=False)
        self.assertEqual(self.row()['state'], 'generation_unknown')
        self.assertTrue(self.store.conversation(event()['conversation_id'])['paused'])
        self.assertEqual(self.store.ready(), [])
        self.assertEqual(due(self.store, float('inf')), [])

    async def test_crash_before_interruption_confirmation_is_not_send_only_recovery(self):
        await self.progress_wait()
        data = json.loads(self.row()['result'])
        data['delivery']['interruption_confirmed'] = False
        self.store.update('root', 'send_wait', result=data)
        self.store.recover()
        self.assertEqual(self.row()['state'], 'generation_unknown')
        self.assertEqual(due(self.store, float('inf')), [])

    async def test_notice_delay_never_creates_another_notice(self):
        await self.progress_wait()
        self.mcp.operations[('root', 0)]['state'] = 'accepted'
        await reconcile(self.agent, self.row())
        self.mcp.operations[('root', 1)] = {'state': 'running', 'request_id': 'stable-notice'}
        await self.agent.process(self.row())
        self.assertEqual(self.row()['state'], 'send_wait')
        self.mcp.operations[('root', 1)]['state'] = 'accepted'
        await reconcile(self.agent, self.row())
        self.assertEqual(self.row()['state'], 'interrupted')
        self.assertEqual(json.loads(self.row()['result'])['part_index'], 1)
        self.assertEqual(due(self.store, float('inf')), [])

    async def test_query_failure_is_not_send_failure_and_does_not_post(self):
        await self.progress_wait()
        self.mcp.call = AsyncMock(side_effect=OSError())
        await reconcile(self.agent, self.row())
        self.assertEqual(self.row()['state'], 'send_wait')
        self.assertEqual(len(self.mcp.sent), 1)

    async def test_legacy_final_unknown_is_migrated_without_unpausing_other_errors(self):
        self.store.ingest([event('root'), event('following')])
        self.store.update('root', 'unknown', reply='原答案', result={'operation': {'state': 'running'}})
        self.store.recover()
        self.assertEqual(self.row()['state'], 'send_held')
        self.assertFalse(self.store.conversation(event()['conversation_id'])['paused'])
        self.mcp.operations['root'] = {'state': 'accepted', 'request_id': 'original'}
        await reconcile(self.agent, self.row())
        self.assertEqual(self.row()['state'], 'accepted')
        self.assertEqual(self.backend.calls, [])
        self.assertEqual(self.mcp.sent, [])

    async def test_unresolved_context_survives_recent_window_and_is_conversation_scoped(self):
        await self.progress_wait('unknown')
        self.store.ingest([event('later'+str(i)) for i in range(20)] + [event('other', 'wxid_test_other')])
        ctx = self.store.select_context('later19')
        self.assertEqual(ctx['incomplete_tasks'][0]['event_id'], 'root')
        self.assertFalse(any(x['event_id'] == 'root' for x in ctx['messages']))
        self.assertLessEqual(len(json.dumps(ctx, ensure_ascii=False).encode()), 16384)
        self.assertNotIn('incomplete_tasks', self.store.select_context('other'))

    async def test_isolation_closes_steering_children_without_replaying_them(self):
        await self.progress_wait()
        self.store.db.execute("INSERT INTO steering(event_id,root_id,seq,state) VALUES('following','root',2,'accepted')")
        self.store.db.execute("UPDATE jobs SET state='merged' WHERE event_id='following'")
        defer(self.store, 'root', {'state': 'unknown'})
        self.assertEqual(self.row('following')['state'], 'merged_done')
        self.store.ingest([event('new')])
        self.assertEqual([j['event_id'] for j in self.store.ready()], ['new'])

    async def test_worker_reconciles_without_replaying_and_continues_same_chat(self):
        await self.progress_wait()
        data=json.loads(self.row()['result'])
        data['delivery']['next_check']=0
        self.store.update('root','send_wait',result=data)
        self.mcp.operations[('root',0)]['state']='accepted'
        self.backend.generate_with_progress=None
        worker=asyncio.create_task(self.agent.worker_loop())
        try:
            for _ in range(300):
                self.agent.wake.set()
                if self.row()['state']=='interrupted' and self.row('following')['state']=='accepted':
                    break
                await asyncio.sleep(.01)
            self.assertEqual(self.row()['state'],'interrupted')
            self.assertEqual(self.row('following')['state'],'accepted')
            self.assertEqual(self.backend.calls,['following'])
            self.assertEqual([p['part_index'] for p in self.mcp.sent if p['event_id']=='root'],[0,1])
        finally:
            self.agent.stop.set()
            self.agent.wake.set()
            await worker

    async def test_restart_does_not_clear_independent_model_failure(self):
        self.store.ingest([event('root'),event('other-error')])
        self.store.update('root','unknown',result={'operation':{'state':'running'}})
        self.store.update('other-error','generation_unknown')
        self.store.recover()
        self.assertEqual(self.row()['state'],'send_held')
        self.assertTrue(self.store.conversation(event()['conversation_id'])['paused'])
        self.assertEqual(due(self.store,float('inf')),[])

    async def test_reset_refuses_outstanding_send(self):
        await self.progress_wait()
        with self.assertRaises(ValueError):
            self.store.reset(event()['conversation_id'])

    async def test_large_issue_identifiers_still_fit_context_budget(self):
        values=[event(str(i)+'x'*500,text='文'*3000) for i in range(9)]
        self.store.ingest(values)
        for value in values[:-1]:
            defer(self.store,value['event_id'],{'state':'unknown'})
        ctx=self.store.select_context(values[-1]['event_id'])
        self.assertEqual(len(ctx['incomplete_tasks']),8)
        self.assertLessEqual(len(json.dumps(ctx,ensure_ascii=False).encode()),16384)

    async def test_new_message_during_interruption_is_not_claimed_by_old_task(self):
        def incoming():
            self.store.ingest([event('during-interruption')])
            self.agent.reply_tasks[event()['conversation_id']].claim()
            self.assertEqual(self.row('during-interruption')['state'],'pending')
        await self.progress_wait(on_interrupt=incoming)
        self.assertEqual(self.row('during-interruption')['state'],'pending')

    async def test_cancelled_progress_observation_does_not_pause_after_confirmed_stop(self):
        self.store.ingest([event('root'),event('following')])
        original=self.mcp.call
        async def call(name,**args):
            if name=='reply_text_part':raise asyncio.CancelledError()
            return await original(name,**args)
        self.mcp.call=call
        async def stream(tid,value,callback):
            await callback('原进度',{'key':'original','kind':'commentary'})
        self.backend.generate_with_progress=stream
        await self.agent.process(self.store.ready()[0])
        self.assertEqual(self.row()['state'],'send_held')
        self.assertFalse(self.store.conversation(event()['conversation_id'])['paused'])
        self.assertEqual([j['event_id'] for j in self.store.ready()],['following'])
