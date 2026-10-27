import base64
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import AsyncMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from local_agent.storage import Store
from local_agent.runtime import identity
from local_agent.service import Agent
from local_agent.attachments import fetch
from local_agent.mcp import MCPError
from fakes import ACCOUNT, CONFIG, FakeMCP, FakeBackend, event


class ContextTests(unittest.IsolatedAsyncioTestCase):
    async def test_task_record_sort_echo_and_manual_same_text_are_separate(self):
        self.store.ingest([event('sort-trigger')],ACCOUNT,CONFIG['conversations'])
        self.store.update('sort-trigger','accepted',reply='相同文字',result={'operation':{'result':{'result':{
            'native_sort_seq':'1792847037000','native_sort_seq_source':'weixin41930_send_task_record110_v1'}}}})
        echo=dict(event('sort-echo',text='相同文字'),direction='outgoing',sender_id=ACCOUNT,
                  native_sort_seq='1792847037000',native_record_metadata_source='weixin41930_record_identity_v2')
        manual={**echo,'event_id':'sort-manual','native_sort_seq':'1792847037001'}
        self.store.ingest([echo,manual,{**manual,'event_id':'sort-manual-final','message_id':'123'},event('sort-next')],
                          ACCOUNT,CONFIG['conversations'])
        context=self.store.context('sort-next');ids=[x['event_id'] for x in context['messages']]
        self.assertNotIn('sort-echo',ids)
        self.assertNotIn('sort-manual',ids)
        self.assertIn('sort-manual-final',ids)
        self.store.update('sort-next','accepted',context=context)
        self.store.ingest([{**manual,'event_id':'sort-manual-repeat'},event('sort-followup')],ACCOUNT,CONFIG['conversations'])
        self.assertNotIn('sort-manual-repeat',[x['event_id'] for x in self.store.context('sort-followup')['messages']])

    async def test_native_echo_is_skipped_but_identical_manual_text_is_background(self):
        trigger = event('native-trigger')
        self.store.ingest([trigger], ACCOUNT, CONFIG['conversations'])
        self.store.update('native-trigger', 'accepted', reply='相同文字', result={'operation':{'result':{'result':{
            'native_client_id':'own-A','native_client_id_source':'weixin41930_send_object_618'}}}})
        echo = dict(event('own-echo', text='相同文字'), direction='outgoing',sender_id=ACCOUNT,
                    native_client_id='own-A',native_client_id_source='weixin41930_record_client278_v1')
        manual = {**echo,'event_id':'manual-initial','native_client_id':'manual-B'}
        self.store.ingest([echo,manual,{**manual,'event_id':'manual-with-mid','message_id':'123'},event('next-trigger')],
                          ACCOUNT,CONFIG['conversations'])
        context = self.store.context('next-trigger')
        ids = [e['event_id'] for e in context['messages']]
        self.assertNotIn('own-echo', ids)
        self.assertNotIn('manual-initial', ids)
        self.assertIn('manual-with-mid', ids)
        self.store.update('next-trigger','accepted',context=context)
        self.store.ingest([{**manual,'event_id':'manual-late-copy','message_id':'123'},event('followup')],
                          ACCOUNT,CONFIG['conversations'])
        self.assertNotIn('manual-late-copy',[e['event_id'] for e in self.store.context('followup')['messages']])

    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(self.temp.name, identity(CONFIG))
        self.mcp, self.backend = FakeMCP(), FakeBackend()
        self.agent = Agent(self.store, self.mcp, self.backend, CONFIG)

    async def asyncTearDown(self):
        self.store.close()
        self.temp.cleanup()

    async def test_media_and_background_are_durable_but_silent(self):
        for value in (dict(event('image'), message_type='image', payload={'name': '图.png', 'resource_id': 'r'}),
                      event('background', '12345@chatroom', mention=False),
                      dict(event('outgoing'), direction='outgoing')):
            self.mcp.events.append(value)
            await self.agent.collect_once(0)
        self.assertEqual(self.store.status()['jobs'], {})
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM messages').fetchone()[0], 3)
        self.assertEqual(self.backend.calls, [])
        self.assertEqual(self.mcp.sent, [])

    async def test_context_snapshot_excludes_future_and_other_conversations(self):
        cid = '12345@chatroom'
        self.store.ingest([event('a', cid, '早先群内背景', False), event('b', text='另一私聊'),
                           event('trigger', cid, '请继续'), event('future', cid, '之后才到的消息', False)],
                          ACCOUNT, CONFIG['conversations'])
        self.backend.generate = AsyncMock(return_value=('已结合背景回复', {}))
        job = next(j for j in self.store.ready() if j['event_id'] == 'trigger')
        await self.agent.process(job)
        value = self.backend.generate.call_args.args[1]
        self.assertEqual([m['event_id'] for m in value['context']['messages']], ['a', 'trigger'])
        self.store.ingest([event('next', cid, '再继续')])
        self.assertEqual([m['event_id'] for m in self.store.context('next')['messages']], ['future', 'next'])

    async def test_unquoted_processing_request_never_fetches_attachment(self):
        value = event('request', text='分析一下刚才的文件')
        self.store.ingest([value])
        self.backend.generate = AsyncMock(return_value=('内部说明', {'read_attachment': True}))
        await self.agent.process(self.store.ready()[0])
        self.assertIn('引用', self.mcp.sent[0]['text'])
        self.assertFalse(any('resource' in name for name, _ in self.mcp.calls))

    async def test_quoted_attachment_without_processing_intent_stays_unread(self):
        value = event('request', text='这个文件先放这里')
        value['reference'] = {'source': 'weixin41930_native_reference_v1', 'message_id': '123'}
        self.store.ingest([value])
        original = self.mcp.call
        async def call(name, **args):
            if name == 'resolve_reference':
                return dict(event('file'), message_type='file', payload={'name': 'report.pdf', 'resource_id': 'r'})
            return await original(name, **args)
        self.mcp.call = AsyncMock(side_effect=call)
        self.backend.generate = AsyncMock(return_value=('需要我怎样处理这个文件？', {}))
        await self.agent.process(self.store.ready()[0])
        self.assertFalse(any(c.args[0] == 'acquire_reference_resource' for c in self.mcp.call.call_args_list))

    async def test_restart_keeps_silent_context_and_completed_cursor(self):
        self.store.ingest([dict(event('silent'), direction='outgoing'), event('trigger')])
        await self.agent.process(self.store.ready()[0])
        thread_id = self.store.conversation('wxid_test_peer')['thread_id']
        self.store.close()
        self.store = Store(self.temp.name, identity(CONFIG))
        self.store.ingest([event('next')])
        self.assertEqual(self.store.conversation('wxid_test_peer')['thread_id'], thread_id)
        self.assertEqual([m['event_id'] for m in self.store.context('next')['messages']], ['next'])

    async def test_retention_removes_raw_data_but_preserves_deduplication(self):
        value = dict(event('old'), direction='outgoing')
        self.store.ingest([value])
        self.store.prune(time.time() + 8 * 86400)
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM messages').fetchone()[0], 0)
        self.store.ingest([value])
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM messages').fetchone()[0], 0)

    async def test_old_schema_migration_preserves_thread_without_reinjecting_old_turns(self):
        self.store.ingest([event('old')])
        await self.agent.process(self.store.ready()[0])
        thread_id = self.store.conversation('wxid_test_peer')['thread_id']
        self.store.db.executescript('DROP TABLE messages; DROP TABLE context_progress; DROP TABLE seen_events;')
        self.store.close()
        self.store = Store(self.temp.name, identity(CONFIG))
        self.store.ingest([event('new')])
        self.assertEqual(self.store.conversation('wxid_test_peer')['thread_id'], thread_id)
        self.assertEqual([m['event_id'] for m in self.store.context('new')['messages']], ['new'])

    async def test_resource_failure_can_be_replied_without_pausing_conversation(self):
        value = event('request', text='分析引用的图片')
        value['reference'] = {'source': 'weixin41930_native_reference_v1', 'message_id': '123'}
        self.store.ingest([value])
        original = self.mcp.call
        async def call(name, **args):
            if name == 'resolve_reference':
                return dict(event('image'), message_type='image', payload={'resource_id': 'r'})
            if name == 'acquire_reference_resource':
                raise MCPError('not_implemented')
            return await original(name, **args)
        self.mcp.call = AsyncMock(side_effect=call)
        self.backend.generate = AsyncMock(side_effect=[('计划读取', {'read_attachment': True}), ('原附件暂时不可获取', {})])
        await self.agent.process(self.store.ready()[0])
        self.assertEqual(self.store.status()['jobs'], {'accepted': 1})
        self.assertEqual(self.store.conversation('wxid_test_peer')['paused'], 0)
        self.assertIn('attachment_error', self.backend.generate.call_args.args[1])
        self.assertEqual(len(self.mcp.sent), 1)

    async def test_transfer_verifies_chunks_and_hash_before_exposing_path(self):
        data = b'hello file'
        digest = hashlib.sha256(data).hexdigest()
        async def call(name, **args):
            if name == 'acquire_reference_resource':
                return {'id': 'r', 'state': 'ready', 'size': len(data), 'sha256': digest}
            return {'data': base64.b64encode(data).decode(), 'offset': 0, 'next_offset': len(data)}
        self.mcp.call = AsyncMock(side_effect=call)
        target = {'message_type': 'file', 'payload': {'name': '../../unsafe.txt'}}
        result = await fetch(self.mcp, 'request', target, Path(self.temp.name) / 'resources')
        self.assertEqual(Path(result['path']).read_bytes(), data)
        self.assertTrue(Path(result['path']).is_relative_to(Path(self.temp.name)))
        digest = 'bad'
        with self.assertRaises(ValueError):
            await fetch(self.mcp, 'other', target, Path(self.temp.name) / 'resources')
