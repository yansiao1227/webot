import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from local_agent.progress import DecisionClock, ProgressDeliveryError


def notification(method, kind, item_id, turn='turn', **fields):
    return SimpleNamespace(method=method, payload={'turnId':turn,
        'item':{'id':item_id,'type':kind,**fields}})


class ProgressTests(unittest.IsolatedAsyncioTestCase):
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

    async def test_raw_reasoning_and_deltas_do_not_reset_decision_deadline(self):
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
