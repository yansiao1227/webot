import json
import asyncio
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
from local_agent.runtime import atomic_json

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from local_agent.codex_backend import CodexBackend, runtime_overrides, ModelTimeout, ModelThreadBusy, ModelUnavailable
from fakes import event
from local_agent.progress import ProgressDeliveryError


class BackendTests(unittest.IsolatedAsyncioTestCase):
    async def test_atomic_status_survives_brief_windows_reader_sharing(self):
        output = Path(self.temp.name) / 'status.json'
        original = Path.replace
        attempts = []
        def replace(source, target):
            attempts.append(1)
            if len(attempts) < 3:
                raise PermissionError('sharing violation')
            return original(source, target)
        with patch.object(Path, 'replace', replace):
            atomic_json(output, {'ready':True})
        self.assertEqual(json.loads(output.read_text()), {'ready':True})
        self.assertEqual(len(attempts), 3)
        self.assertFalse(output.with_suffix('.json.tmp').exists())

    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.thread = SimpleNamespace(id='thread-1', run=AsyncMock())
        self.handle = SimpleNamespace(id='turn-1', run=self.thread.run, interrupt=AsyncMock())
        self.thread.turn = AsyncMock(return_value=self.handle)
        self.thread.read = AsyncMock(return_value=SimpleNamespace(thread=SimpleNamespace(
            status=SimpleNamespace(model_dump=lambda **kwargs: {'type':'idle'}))))
        self.runtime = SimpleNamespace(__aenter__=AsyncMock(), __aexit__=AsyncMock(),
            thread_start=AsyncMock(return_value=self.thread), thread_resume=AsyncMock(return_value=self.thread))
        self.configs = []
        def config(**kwargs):
            self.configs.append(kwargs)
            return kwargs
        self.sdk = SimpleNamespace(AsyncCodex=lambda cfg: self.runtime, CodexConfig=config,
            ApprovalMode=SimpleNamespace(auto_review='auto_review'), Sandbox=SimpleNamespace(full_access='full_access'),
            ExternalMessage=lambda **kwargs: kwargs)
        self.modules = patch.dict(sys.modules, {'openai_codex': self.sdk})
        self.modules.start()
        self.backend = CodexBackend(self.temp.name)
        await self.backend.start()

    async def asyncTearDown(self):
        await self.backend.close()
        self.modules.stop()
        self.temp.cleanup()

    async def test_inherits_tools_and_model_configuration(self):
        self.assertEqual(runtime_overrides(), ('web_search="live"',))
        self.assertEqual(self.configs[0]['env'], {'MCP_BRIDGE_TOKEN': ''})
        await self.backend.ensure_thread('peer')
        options = self.runtime.thread_start.call_args.kwargs
        self.assertNotIn('base_instructions', options)
        self.assertNotIn('model', options)
        self.assertEqual(options['approval_mode'], 'auto_review')
        self.assertEqual(options['sandbox'], 'full_access')

    async def test_explicit_cli_binary_is_passed_to_sdk_without_changing_default(self):
        self.assertNotIn('codex_bin',self.configs[0])
        backend=CodexBackend(self.temp.name,codex_bin='C:/runtime/codex.exe')
        try:
            await backend.start()
            self.assertEqual(self.configs[-1]['codex_bin'],'C:/runtime/codex.exe')
        finally:await backend.close()

    async def test_resume_replaces_legacy_tool_prohibition(self):
        await self.backend.ensure_thread('peer', 'thread-1')
        options = self.runtime.thread_resume.call_args.kwargs
        self.assertIn('base_instructions', options)
        self.assertNotIn('不得调用工具', options['base_instructions'])
        self.assertEqual(options['sandbox'], 'full_access')

    async def test_tool_results_can_be_replied_and_are_counted(self):
        await self.backend.ensure_thread('peer')
        self.thread.run.return_value = SimpleNamespace(status='completed', id='turn', duration_ms=10,
            items=[SimpleNamespace(model_dump=lambda t=t, **kwargs: {'type': t})
                   for t in ('webSearch', 'mcpToolCall', 'commandExecution')],
            final_response=json.dumps({'reply': '已搜索，来源：https://openai.com/'}))
        current = {**event(), 'sender_id':'wxid_participant_A'}
        reply, info = await self.backend.generate('thread-1', current)
        self.assertIn('wxid_participant_A', str(self.thread.turn.call_args.args[0]))
        self.assertIn('https://', reply)
        self.assertEqual(info['tool_calls'], 3)
        self.assertNotIn('effort', self.thread.run.call_args.kwargs)
        self.thread.run.return_value.status = 'failed'
        with self.assertRaises(RuntimeError):
            await self.backend.generate('thread-1', event())


    async def test_timeout_interrupts_exact_turn_and_observes_terminal_state(self):
        await self.backend.ensure_thread('peer')
        stopped = asyncio.Event()
        async def run():
            await stopped.wait()
            return SimpleNamespace(status='interrupted')
        self.thread.run.side_effect = run
        self.handle.interrupt.side_effect = lambda: stopped.set()
        self.backend.timeout = .01
        with self.assertRaises(ModelTimeout) as caught:
            await self.backend.generate('thread-1', event())
        self.assertTrue(caught.exception.interruption_confirmed)
        self.assertEqual(caught.exception.turn_id, 'turn-1')
        self.handle.interrupt.assert_awaited_once()

    async def test_new_request_never_joins_a_still_active_old_turn(self):
        await self.backend.ensure_thread('peer')
        self.thread.read.return_value.thread.status.model_dump = lambda **kwargs: {'type':'active'}
        with self.assertRaises(ModelThreadBusy):
            await self.backend.generate('thread-1', event())
        self.thread.turn.assert_not_awaited()

    async def test_streamed_decision_is_delivered_before_final_collection(self):
        await self.backend.ensure_thread('peer');sent=[]
        async def notifications():
            yield SimpleNamespace(method='item/completed',payload={'turnId':'turn-1',
                'item':{'id':'plan','type':'agentMessage','phase':'commentary','text':'我先联网核对。'}})
            self.assertEqual(sent,['我先联网核对。'])
            yield SimpleNamespace(method='item/started',payload={'turnId':'turn-1',
                'item':{'id':'search','type':'webSearch'}})
            yield SimpleNamespace(method='item/completed',payload={'turnId':'turn-1',
                'item':{'id':'search','type':'webSearch'}})
        self.handle.stream=notifications
        async def collect(stream,turn_id):
            async for _ in stream:pass
            return SimpleNamespace(status='completed',id=turn_id,duration_ms=10,items=[],
                                   final_response=json.dumps({'reply':'最终答案'}))
        async def callback(text,meta):sent.append(text)
        with patch.dict(sys.modules,{'openai_codex._run':SimpleNamespace(_collect_async_turn_result=collect)}):
            reply,info=await self.backend.generate_with_progress('thread-1',event(),callback)
        self.assertEqual(reply,'最终答案')
        self.assertEqual(info['decision_events'],2)
        self.assertIsNotNone(info['first_decision_seconds'])
        self.thread.run.assert_not_awaited()

    async def test_runtime_preparation_does_not_consume_response_budget(self):
        await self.backend.ensure_thread('peer')
        async def start(*args,**kwargs):
            await asyncio.sleep(.04)
            return self.handle
        self.thread.turn.side_effect=start
        async def notifications():
            await asyncio.sleep(.04)
            yield SimpleNamespace(method='item/completed',payload={'turnId':'turn-1',
                'item':{'id':'input','type':'functionCallOutput','name':'wechat_message'}})
            await asyncio.sleep(.015)
            for _ in range(5):
                yield SimpleNamespace(method='item/agentMessage/delta',payload={'turnId':'turn-1',
                    'itemId':'answer','delta':'x'})
                await asyncio.sleep(.015)
        self.handle.stream=notifications
        async def collect(stream,turn_id):
            async for _ in stream:pass
            return SimpleNamespace(status='completed',id=turn_id,duration_ms=100,items=[],
                final_response=json.dumps({'reply':'完整答案'}))
        callback=AsyncMock()
        with patch.dict(sys.modules,{'openai_codex._run':SimpleNamespace(_collect_async_turn_result=collect)}):
            reply,_=await self.backend.generate_with_progress('thread-1',
                {**event(),'_decision_timeout':.03,'_startup_timeout':.2,'_generation_idle_timeout':.06},callback)
        self.assertEqual(reply,'完整答案')
        callback.assert_not_awaited()
        self.handle.interrupt.assert_not_awaited()

    async def test_startup_timeout_interrupts_known_turn(self):
        await self.backend.ensure_thread('peer')
        stopped=asyncio.Event()
        async def notifications():
            await stopped.wait()
            if False:yield None
        self.handle.stream=notifications
        self.handle.interrupt.side_effect=lambda:stopped.set()
        async def collect(stream,turn_id):
            async for _ in stream:pass
            return SimpleNamespace(status='interrupted')
        with patch.dict(sys.modules,{'openai_codex._run':SimpleNamespace(_collect_async_turn_result=collect)}):
            with self.assertRaises(ModelTimeout) as caught:
                await self.backend.generate('thread-1',{**event(),'_startup_timeout':.03})
        self.assertEqual(caught.exception.timeout_stage,'startup')
        self.assertTrue(caught.exception.interruption_confirmed)
        self.handle.interrupt.assert_awaited_once()

    async def test_start_rpc_timeout_preserves_uncertainty_without_retry(self):
        await self.backend.ensure_thread('peer')
        async def start(*args,**kwargs):await asyncio.sleep(.3)
        self.thread.turn.side_effect=start
        with self.assertRaises(ModelTimeout) as caught:
            await self.backend.generate('thread-1',{**event(),'_startup_timeout':.03})
        self.assertEqual(caught.exception.timeout_stage,'startup')
        self.assertFalse(caught.exception.interruption_confirmed)
        self.assertIsNone(caught.exception.turn_id)
        self.thread.turn.assert_awaited_once()
        self.handle.interrupt.assert_not_awaited()

    async def test_unknown_progress_interrupts_the_exact_live_turn(self):
        await self.backend.ensure_thread('peer')
        async def notifications():
            yield SimpleNamespace(method='item/started',payload={'turnId':'turn-1',
                'item':{'id':'search','type':'webSearch'}})
            self.fail('must stop before consuming more model output')
        self.handle.stream=notifications
        async def collect(stream,turn_id):
            async for _ in stream:pass
        with patch.dict(sys.modules,{'openai_codex._run':SimpleNamespace(_collect_async_turn_result=collect)}):
            with self.assertRaises(ProgressDeliveryError):
                await self.backend.generate_with_progress('thread-1',event(),
                    AsyncMock(side_effect=ProgressDeliveryError('unknown send')))
        self.handle.interrupt.assert_awaited_once()

    async def test_terminal_capacity_failure_before_tools_is_known_and_does_not_interrupt_finished_turn(self):
        await self.backend.ensure_thread('peer')
        async def notifications():
            yield SimpleNamespace(method='turn/completed',payload={'turn':{'id':'turn-1','status':'failed',
                'error':{'codexErrorInfo':'serverOverloaded','message':'provider details'}}})
        self.handle.stream=notifications
        async def collect(stream,turn_id):
            async for _ in stream:pass
            raise RuntimeError('provider details')
        with patch.dict(sys.modules,{'openai_codex._run':SimpleNamespace(_collect_async_turn_result=collect)}):
            with self.assertRaises(ModelUnavailable) as caught:
                await self.backend.generate('thread-1',event())
        self.assertEqual(caught.exception.details()['provider_code'],'server_overloaded')
        self.assertNotIn('provider details',str(caught.exception.details()))
        self.handle.interrupt.assert_not_awaited()

    async def test_capacity_error_after_tool_started_stays_unknown(self):
        await self.backend.ensure_thread('peer')
        async def notifications():
            yield SimpleNamespace(method='item/started',payload={'turnId':'turn-1',
                'item':{'id':'tool','type':'commandExecution'}})
            yield SimpleNamespace(method='turn/completed',payload={'turn':{'id':'turn-1','status':'failed',
                'error':{'codexErrorInfo':'serverOverloaded'}}})
        self.handle.stream=notifications
        async def collect(stream,turn_id):
            async for _ in stream:pass
            raise RuntimeError('provider failed')
        with patch.dict(sys.modules,{'openai_codex._run':SimpleNamespace(_collect_async_turn_result=collect)}):
            with self.assertRaises(RuntimeError) as caught:await self.backend.generate('thread-1',event())
        self.assertNotIsInstance(caught.exception,ModelUnavailable)
        self.handle.interrupt.assert_awaited_once()


if __name__ == '__main__':
    unittest.main()
