import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from local_agent.progress import DecisionClock, ProgressDeliveryError, is_web_search


def notification(method, kind, item_id, turn='turn', **fields):
    return SimpleNamespace(method=method, payload={'turnId':turn,
        'item':{'id':item_id,'type':kind,**fields}})


class ProgressTests(unittest.IsolatedAsyncioTestCase):
    async def test_connected_search_has_search_notice_and_bounded_tool_budget(self):
        callback = AsyncMock()
        clock = DecisionClock('turn', 1, .03, callback)
        search = dict(server='codex_apps', tool='search_service.web_run')
        await clock.observe(notification('item/started', 'mcpToolCall', 'search', **search))
        self.assertEqual(callback.call_args.args[0], clock.TOOL_TEXT['webSearch'])
        self.assertTrue(is_web_search({'type':'mcpToolCall', **search}))
        self.assertFalse(is_web_search({'type':'mcpToolCall', 'server':'codex_apps', 'tool':'gmail.search'}))
        self.assertFalse(is_web_search({'type':'mcpToolCall', **search, 'server':'other'}))
        task = asyncio.create_task(asyncio.sleep(.3))
        try:
            with self.assertRaises(TimeoutError):
                await clock.wait(task)
            self.assertEqual(clock.timeout_stage, 'tool')
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_long_stream_notice_is_delayed_and_sent_once(self):
        callback=AsyncMock()
        clock=DecisionClock('turn',1,1,callback,generation_idle_timeout=2)
        async def delta(turn='turn',text='{"reply":'):
            await clock.observe(SimpleNamespace(method='item/agentMessage/delta',
                payload={'turnId':turn,'itemId':'answer','delta':text}))
        await delta()
        callback.assert_not_awaited()
        clock.stream_started_at -= 31
        await delta('other')
        await delta(text='')
        callback.assert_not_awaited()
        await delta()
        callback.assert_awaited_once_with('内容过长，正在输出，请稍等……',
            {'key':'streaming_long_reply','kind':'streaming_notice'})
        self.assertGreater(clock.idle_deadline-clock.loop.time(),1.5)
        await clock.observe(notification('item/completed','agentMessage','answer',phase='final_answer',text='完成'))
        await delta()
        clock.stream_started_at -= 31
        await delta()
        self.assertEqual(callback.await_count,1)
        self.assertEqual(clock.decision_count,0)

    async def test_long_stream_notice_does_not_extend_total_and_failure_propagates(self):
        callback=AsyncMock(side_effect=ProgressDeliveryError('unknown'))
        clock=DecisionClock('turn',1,1,callback,total_deadline=asyncio.get_running_loop().time()+60)
        deadline=clock.total_deadline
        clock.stream_started_at=clock.loop.time()-31
        with self.assertRaises(ProgressDeliveryError):
            await clock.observe(SimpleNamespace(method='item/agentMessage/delta',
                payload={'turnId':'turn','itemId':'answer','delta':'x'}))
        self.assertEqual(clock.total_deadline,deadline)
        self.assertIsNone(clock.sending_deadline)

    async def test_tool_can_outlast_decision_budget_and_then_finish(self):
        callback=AsyncMock()
        clock=DecisionClock('turn',.04,.3,callback)
        async def run():
            await clock.observe(notification('item/started','webSearch','search'))
            await asyncio.sleep(.1)
            await clock.observe(notification('item/completed','webSearch','search'))
            await asyncio.sleep(.01)
            return 'final'
        self.assertEqual(await clock.wait(asyncio.create_task(run())),'final')
        callback.assert_awaited_once()

    async def test_completed_commentary_is_sent_immediately_and_tool_does_not_repeat_it(self):
        callback=AsyncMock()
        clock=DecisionClock('turn',1,1,callback)
        event=notification('item/completed','agentMessage','plan',phase='commentary',text='我先查官方文档。')
        await clock.observe(event)
        callback.assert_awaited_once()
        await clock.observe(event)
        await clock.observe(notification('item/started','webSearch','search'))
        self.assertEqual(callback.await_count,1)
        self.assertIn('search',clock.tools)

    async def test_raw_reasoning_and_malformed_deltas_do_not_reset_decision_deadline(self):
        callback=AsyncMock()
        clock=DecisionClock('turn',.03,.2,callback)
        original=clock.idle_deadline
        async def run():
            for i in range(20):
                await clock.observe(notification('item/completed','reasoning',str(i),text='private'))
                await clock.observe(notification('item/agentMessage/delta','agentMessage',str(i),text='unfinished'))
                await asyncio.sleep(.01)
        task=asyncio.create_task(run())
        try:
            with self.assertRaises(TimeoutError):await clock.wait(task)
            self.assertEqual(clock.idle_deadline,original)
            self.assertEqual(clock.timeout_stage,'decision')
            callback.assert_not_awaited()
        finally:
            task.cancel();await asyncio.gather(task,return_exceptions=True)

    async def test_tool_completion_starts_fresh_decision_deadline(self):
        clock=DecisionClock('turn',.04,.5)
        async def run():
            await clock.observe(notification('item/started','webSearch','search'))
            await asyncio.sleep(.08)
            await clock.observe(notification('item/completed','webSearch','search'))
            await asyncio.sleep(.5)
        task=asyncio.create_task(run())
        started=asyncio.get_running_loop().time()
        try:
            with self.assertRaises(TimeoutError):await clock.wait(task)
            self.assertLess(asyncio.get_running_loop().time()-started,.35)
            self.assertEqual(clock.timeout_stage,'decision')
        finally:
            task.cancel();await asyncio.gather(task,return_exceptions=True)

    async def test_hung_tool_uses_separate_tool_budget(self):
        clock=DecisionClock('turn',.2,.03)
        async def run():
            await clock.observe(notification('item/started','webSearch','search'))
            await asyncio.sleep(.5)
        task=asyncio.create_task(run())
        try:
            with self.assertRaises(TimeoutError):await clock.wait(task)
            self.assertEqual(clock.timeout_stage,'tool')
        finally:
            task.cancel();await asyncio.gather(task,return_exceptions=True)

    async def test_other_turn_cannot_reset_clock_or_send_messages(self):
        callback=AsyncMock();clock=DecisionClock('turn',1,1,callback)
        deadline=clock.deadline()
        await clock.observe(notification('item/started','webSearch','other',turn='other'))
        await clock.observe(notification('item/completed','agentMessage','other',turn='other',phase='commentary',text='other'))
        self.assertEqual(clock.deadline(),deadline)
        callback.assert_not_awaited()

    async def test_tool_fallback_never_exposes_command_or_secret(self):
        callback=AsyncMock();clock=DecisionClock('turn',1,1,callback)
        await clock.observe(notification('item/started','commandExecution','cmd',command='secret-token'))
        self.assertNotIn('secret-token',str(callback.call_args))

    async def test_open_page_announcement_describes_actual_action(self):
        callback=AsyncMock();clock=DecisionClock('turn',1,1,callback)
        await clock.observe(notification('item/started','webSearch','open',action={'type':'openPage','url':'secret-url'}))
        self.assertIn('打开来源网页',callback.call_args.args[0])
        self.assertNotIn('secret-url',str(callback.call_args))

    async def test_progress_delivery_failure_propagates(self):
        clock=DecisionClock('turn',1,1,AsyncMock(side_effect=ProgressDeliveryError('unknown')))
        with self.assertRaises(ProgressDeliveryError):
            await clock.observe(notification('item/started','webSearch','search'))

    async def test_streaming_text_outlasts_decision_budget_without_publishing_json(self):
        callback=AsyncMock()
        clock=DecisionClock('turn',.03,.3,callback,generation_idle_timeout=.08)
        async def run():
            for _ in range(8):
                await clock.observe(SimpleNamespace(method='item/agentMessage/delta',
                    payload={'turnId':'turn','itemId':'answer','delta':'{"reply":'}))
                await asyncio.sleep(.015)
            return 'final'
        self.assertEqual(await clock.wait(asyncio.create_task(run())),'final')
        callback.assert_not_awaited()

    async def test_streaming_stall_and_total_limit_are_bounded(self):
        for total, stage in ((None,'generation'),(.07,'total')):
            with self.subTest(stage=stage):
                loop=asyncio.get_running_loop()
                clock=DecisionClock('turn',.03,.3,generation_idle_timeout=.04,
                    total_deadline=loop.time()+total if total else None)
                async def run():
                    while True:
                        await clock.observe(SimpleNamespace(method='item/agentMessage/delta',
                            payload={'turnId':'turn','itemId':'answer','delta':'x'}))
                        await asyncio.sleep(.01 if total else .2)
                task=asyncio.create_task(run())
                try:
                    with self.assertRaises(TimeoutError):await clock.wait(task)
                    self.assertEqual(clock.timeout_stage,stage)
                finally:
                    task.cancel();await asyncio.gather(task,return_exceptions=True)

    async def test_startup_is_separate_and_readiness_only_resets_once(self):
        loop=asyncio.get_running_loop()
        clock=DecisionClock('turn',.04,.3,startup_deadline=loop.time()+.2)
        async def run():
            await asyncio.sleep(.07)
            await clock.observe(notification('item/completed','functionCallOutput','input',name='wechat_message'))
            deadline=clock.idle_deadline
            await asyncio.sleep(.01)
            await clock.observe(notification('item/started','reasoning','reasoning'))
            self.assertEqual(clock.idle_deadline,deadline)
            await asyncio.sleep(.01)
            return 'final'
        self.assertEqual(await clock.wait(asyncio.create_task(run())),'final')

    async def test_startup_timeout_and_foreign_or_empty_deltas(self):
        loop=asyncio.get_running_loop()
        clock=DecisionClock('turn',.2,.3,startup_deadline=loop.time()+.03)
        original=clock.deadline()
        for turn,delta in (('other','x'),('turn','')):
            await clock.observe(SimpleNamespace(method='item/agentMessage/delta',
                payload={'turnId':turn,'itemId':'answer','delta':delta}))
        self.assertEqual(clock.deadline(),original)
        task=asyncio.create_task(asyncio.sleep(.3))
        try:
            with self.assertRaises(TimeoutError):await clock.wait(task)
            self.assertEqual(clock.timeout_stage,'startup')
        finally:
            task.cancel();await asyncio.gather(task,return_exceptions=True)

    async def test_text_deltas_cannot_extend_active_tool_or_total_budget(self):
        for total, stage in ((None,'tool'),(.03,'total')):
            with self.subTest(stage=stage):
                loop=asyncio.get_running_loop()
                clock=DecisionClock('turn',.2,.06,generation_idle_timeout=.2,
                    total_deadline=loop.time()+total if total else None)
                async def run():
                    await clock.observe(notification('item/started','webSearch','search'))
                    while True:
                        await clock.observe(SimpleNamespace(method='item/agentMessage/delta',
                            payload={'turnId':'turn','itemId':'answer','delta':'x'}))
                        await asyncio.sleep(.01)
                task=asyncio.create_task(run())
                try:
                    with self.assertRaises(TimeoutError):await clock.wait(task)
                    self.assertEqual(clock.timeout_stage,stage)
                finally:
                    task.cancel();await asyncio.gather(task,return_exceptions=True)
