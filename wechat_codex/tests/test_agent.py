import asyncio
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from local_agent.mcp import allowed, MCP
from local_agent.runtime import StateLock, identity, read_config
from local_agent.storage import Store
from local_agent.service import Agent
from fakes import ACCOUNT, CONFIG, event, FakeBackend, FakeMCP


class AgentTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(self.temp.name, identity(CONFIG))
        self.mcp, self.backend = FakeMCP(), FakeBackend()
        self.agent = Agent(self.store, self.mcp, self.backend, CONFIG)

    async def asyncTearDown(self):
        self.store.close(); self.temp.cleanup()

    async def next_job(self, value):
        self.store.ingest([value])
        jobs = self.store.ready()
        await self.agent.process(next(j for j in jobs if j['event_id'] == value['event_id']))

    async def test_durable_before_ack_and_replayed_batch_does_not_duplicate(self):
        self.mcp.events = [event()]
        self.mcp.fail_ack = True
        with self.assertRaises(Exception):
            await self.agent.collect_once(0)
        self.assertEqual(self.store.status()['jobs'], {'pending': 1})
        self.mcp.fail_ack = False
        await self.agent.collect_once(0)
        self.assertEqual(self.store.status()['jobs'], {'pending': 1})
        await self.agent.process(self.store.ready()[0])
        self.assertEqual(len(self.mcp.sent), 1)

    async def test_session_isolation_and_resume_after_restart(self):
        await self.next_job(event('a1'))
        await self.next_job(event('b1', '12345@chatroom'))
        a = self.store.conversation('wxid_test_peer')['thread_id']
        b = self.store.conversation('12345@chatroom')['thread_id']
        self.assertNotEqual(a, b)
        self.store.close(); self.store = Store(self.temp.name, identity(CONFIG))
        self.backend = FakeBackend(self.backend.threads)
        self.agent = Agent(self.store, self.mcp, self.backend, CONFIG)
        await self.next_job(event('a2', text='继续'))
        self.assertEqual(self.backend.created, [])
        self.assertEqual(self.backend.resumed, [a])
        self.assertEqual(len(self.backend.threads[a]), 2)

    async def test_text_deadline_and_attachment_deadline_are_separate(self):
        captured = []
        generate = self.backend.generate
        async def observe(thread, value):
            captured.append(value.get('_model_timeout'))
            return await generate(thread, value)
        self.backend.generate = observe
        await self.next_job(event('text'))
        self.assertEqual(captured[-1], 30)
        original = self.mcp.call
        async def resolve(name, **args):
            if name == 'resolve_reference':
                return {'event_id':'picture', 'message_type':'image', 'payload':{}}
            return await original(name, **args)
        self.mcp.call = resolve
        value = event('attachment')
        value['reference'] = {'message_id':'native-id'}
        await self.next_job(value)
        self.assertIsNone(captured[-1])  # Backend retains its longer attachment limit.

    async def test_generated_crash_recovers_without_model_regeneration(self):
        self.store.ingest([event()]); self.store.update('1', 'generated', reply='原回复')
        self.store.recover()
        await self.agent.process(self.store.ready()[0])
        self.assertEqual(self.backend.calls, [])
        self.assertEqual(self.mcp.sent[0]['text'], '原回复')

    async def test_mark_read_failure_does_not_pause_or_prevent_reply(self):
        from local_agent.mcp import MCPError
        original = self.mcp.call
        async def unavailable(name, **args):
            if name in ('mark_conversation_read', 'get_conversation_read_status'):
                raise MCPError('read unavailable')
            return await original(name, **args)
        self.mcp.call = unavailable
        await self.next_job(event('read-failure'))
        self.assertEqual(self.store.status()['jobs'], {'accepted': 1})
        self.assertEqual(len(self.mcp.sent), 1)
        self.assertFalse(self.store.conversation('wxid_test_peer')['paused'])

    async def test_send_response_lost_only_queries_original(self):
        self.mcp.lose_send_response = True
        await self.next_job(event())
        self.assertEqual(self.store.status()['jobs'], {'accepted': 1})
        self.assertEqual(len([c for c in self.mcp.calls if c[0] == 'reply_text']), 1)
        self.assertEqual(len(self.mcp.sent), 1)
        self.assertTrue(any(c[0] == 'get_reply_status' for c in self.mcp.calls))

    async def test_sending_recovery_queries_without_post(self):
        self.store.ingest([event()]); self.store.update('1', 'sending', reply='原回复')
        self.mcp.operations['1'] = {'state': 'accepted'}
        await self.agent.process(self.store.ready()[0])
        self.assertFalse(any(c[0] == 'reply_text' for c in self.mcp.calls))
        self.assertEqual(self.backend.calls, [])

    async def test_unknown_send_pauses_only_its_conversation(self):
        self.mcp.fail_send_unknown = True
        await self.next_job(event())
        self.assertEqual(self.store.status()['jobs'], {'unknown': 1})
        self.mcp.fail_send_unknown = False
        self.store.ingest([event('2'), event('3', 'wxid_test_other')])
        ready = self.store.ready()
        self.assertEqual([j['event_id'] for j in ready], ['3'])

    async def test_incomplete_generation_pauses_and_never_reruns(self):
        self.store.ingest([event()]); self.store.update('1', 'generating')
        self.store.recover()
        self.assertEqual(self.store.status()['jobs'], {'generation_unknown': 1})
        self.assertEqual(self.store.ready(), [])
        self.assertEqual(self.backend.calls, [])

    async def test_model_failure_does_not_send(self):
        self.backend.fail = True
        await self.next_job(event())
        self.assertEqual(self.store.status()['jobs'], {'generation_unknown': 1})
        self.assertEqual(self.mcp.sent, [])

    async def test_model_timeout_sends_one_error_then_continues_conversation(self):
        self.backend.generate = AsyncMock(side_effect=[TimeoutError(), ('下一条正常回复', {'read_attachment':False})])
        await self.next_job(event('timeout'))
        self.assertEqual(self.store.status()['jobs'], {'accepted':1})
        self.assertIn('超时',self.mcp.sent[0]['text'])
        self.assertEqual(self.store.conversation('wxid_test_peer')['paused'],0)
        await self.next_job(event('after-timeout'))
        self.assertEqual(self.store.status()['jobs'], {'accepted':2})
        self.assertEqual(len(self.mcp.sent),2)

    async def test_invalid_group_is_acknowledged_but_not_enqueued(self):
        self.mcp.events = [event(conversation='12345@chatroom', text='@我', mention=False)]
        await self.agent.collect_once(0)
        self.assertEqual(self.store.status()['jobs'], {})
        self.assertEqual(self.mcp.position, 1)

    async def test_wrong_account_no_ack_and_competing_agent_no_start(self):
        value = event(); value['account'] = 'wxid_wrong'
        self.mcp.events = [value]
        with self.assertRaises(ValueError):
            await self.agent.collect_once(0)
        self.assertEqual(self.mcp.position, 0)
        self.mcp.subscriptions = 1
        with self.assertRaises(ValueError):
            await self.agent.verify()

    async def test_group_gate_rechecked_even_for_retained_event(self):
        value = event(conversation='12345@chatroom')
        self.store.ingest([value])
        value['mentions']['wxids'] = []
        self.store.db.execute('UPDATE jobs SET event=?', (__import__('json').dumps(value),))
        await self.agent.process(self.store.ready()[0])
        self.assertEqual(self.mcp.sent, [])
        self.assertEqual(self.backend.calls, [])

    async def test_concurrency_serial_within_conversation_parallel_across(self):
        self.store.ingest([event('a1'), event('a2'), event('b1', 'wxid_test_other')])
        self.backend.gate = asyncio.Event()
        task = asyncio.create_task(self.agent.worker_loop())
        try:
            for _ in range(100):
                if len(self.backend.running) == 2: break
                await asyncio.sleep(.01)
            self.assertEqual(len(self.backend.running), 2)
            self.assertEqual(self.backend.calls, [])
            self.backend.gate.set()
            for _ in range(300):
                self.agent.wake.set()
                if self.store.status()['jobs'] == {'accepted': 3}: break
                await asyncio.sleep(.01)
            self.assertEqual(self.store.status()['jobs'], {'accepted': 3})
            self.assertLess(self.backend.calls.index('a1'), self.backend.calls.index('a2'))
            self.assertEqual(self.backend.max_active, 2)
        finally:
            self.agent.stop.set();self.agent.wake.set();await task

    async def test_reset_starts_new_thread_without_replay_and_old_error_stays_archived(self):
        await self.next_job(event())
        original = self.store.conversation('wxid_test_peer')['thread_id']
        self.store.reset('wxid_test_peer')
        await self.next_job(event('2'))
        self.assertNotEqual(self.store.conversation('wxid_test_peer')['thread_id'], original)
        self.assertEqual(len(self.mcp.sent), 2)
        self.store.update('2', 'unknown', error='unknown')
        self.store.reset('wxid_test_peer');self.store.recover()
        self.assertFalse(self.store.conversation('wxid_test_peer')['paused'])

    async def test_reset_refuses_queued_or_running_work(self):
        self.store.ingest([event()])
        with self.assertRaises(ValueError):self.store.reset('wxid_test_peer')

    async def test_changed_event_and_state_identity_fail_closed(self):
        self.store.ingest([event()])
        changed = event(text='不同正文')
        with self.assertRaises(ValueError):self.store.ingest([changed])
        with self.assertRaises(ValueError):Store(self.temp.name, {**identity(CONFIG), 'account': 'wxid_other'})


class Boundaries(unittest.TestCase):
    def test_model_deadlines_are_operator_configuration_for_allowed_sessions(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'config.json'
            for limits in ({'wxid_test_peer': .2}, {}):
                path.write_text(json.dumps({**CONFIG, 'model_timeouts': limits}), encoding='utf-8')
                self.assertEqual(read_config(temp)['model_timeouts'], limits)
            for limits in ({'wxid_not_allowed': 1}, {'wxid_test_peer': True},
                           {'wxid_test_peer': 0}, {'wxid_test_peer': 601}, []):
                path.write_text(json.dumps({**CONFIG, 'model_timeouts': limits}), encoding='utf-8')
                with self.assertRaises(ValueError):
                    read_config(temp)

    def test_local_transport_and_single_process_lock(self):
        for url in ('https://127.0.0.1/mcp','http://8.8.8.8/mcp','http://user@127.0.0.1/mcp'):
            with self.assertRaises(ValueError):MCP(url,'x'*24)
        with tempfile.TemporaryDirectory() as temp:
            first=StateLock(temp)
            try:
                with self.assertRaises(RuntimeError):StateLock(temp)
            finally:first.close()


class HTTPContract(unittest.IsolatedAsyncioTestCase):
    async def test_real_loopback_http_auth_and_mcp_result_contract(self):
        from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
        import threading
        token = 'contract-token-' + 'x'*24
        source = FakeMCP()
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                if self.headers.get('Authorization') != 'Bearer ' + token:
                    self.send_response(401);self.end_headers();return
                value = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                args = value['params']
                result = asyncio.run(source.call(args['name'], **args['arguments']))
                raw = json.dumps({'jsonrpc':'2.0','id':value['id'],'result':{
                    'structuredContent':result,'content':[],'isError':False}}).encode()
                self.send_response(200);self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
            def log_message(self,*args):pass
        server = ThreadingHTTPServer(('127.0.0.1',0),Handler)
        thread = threading.Thread(target=server.serve_forever);thread.start()
        url = f'http://127.0.0.1:{server.server_port}/mcp'
        try:
            client=MCP(url,token)
            self.assertEqual((await client.call('bridge_status'))['account'],ACCOUNT)
            with self.assertRaises(Exception):await MCP(url,'z'*24).call('bridge_status')
        finally:
            await asyncio.to_thread(server.shutdown);server.server_close();thread.join()
