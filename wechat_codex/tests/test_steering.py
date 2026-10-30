import asyncio
import json
import tempfile
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fakes import CONFIG, FakeBackend, FakeMCP, event
from local_agent.runtime import identity
from local_agent.service import Agent
from local_agent.storage import Store


class LiveBackend(FakeBackend):
    def __init__(self):
        super().__init__()
        self.active_turns, self.turn_changed = {}, {}
        self.started, self.steered = asyncio.Queue(), asyncio.Queue()
        self.release = asyncio.Event()
        self.inputs, self.guides = [], []
        self.live, self.reject, self.unknown = True, False, False
        self.answer = None
        self.response = None

    async def generate(self, tid, value):
        index = len(self.inputs)
        self.inputs.append(json.loads(json.dumps(value)))
        turn = SimpleNamespace(id='turn-'+str(index))
        if self.live:
            self.active_turns[tid] = turn
            if tid in self.turn_changed:
                self.turn_changed[tid]()
        self.started.put_nowait((tid, turn.id))
        try:
            if index == 0:
                await self.release.wait()
            if self.response is not None:
                reply, info = self.response(index, value)
            else:
                reply, info = self.answer or value['payload']['text'], {}
            return reply, {'thread_id':tid, 'turn_id':turn.id, **info}
        finally:
            self.active_turns.pop(tid, None)
            if tid in self.turn_changed:
                self.turn_changed[tid]()

    async def steer(self, tid, turn_id, value):
        self.guides.append((tid,turn_id,json.loads(json.dumps(value))))
        self.steered.put_nowait(value['event_id'])
        if self.unknown:
            raise TimeoutError('模拟提交响应丢失')
        if self.reject:
            return False
        self.answer = value['payload']['text']
        return True


class SteeringTests(unittest.IsolatedAsyncioTestCase):
    adaptive = False

    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(self.temp.name, identity(CONFIG))
        self.mcp, self.backend = FakeMCP(), LiveBackend()
        if self.adaptive:
            async def create(cid, eid, thread_id=None):
                return await self.backend.ensure_thread(cid, thread_id)
            self.backend.create_task_thread = create
        self.agent = Agent(self.store, self.mcp, self.backend, CONFIG)
        self.tasks = []

    async def asyncTearDown(self):
        for task in self.tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        self.store.close()
        self.temp.cleanup()

    async def begin(self, root=None, prequeued=()):
        while not self.backend.started.empty():
            self.backend.started.get_nowait()
        self.root = root or event('root', text='原始问题')
        self.store.ingest([self.root, *prequeued])
        self.task = asyncio.create_task(self.agent.process(self.store.ready()[0]))
        self.tasks.append(self.task)
        await asyncio.wait_for(self.backend.started.get(), 2)

    async def feed(self, *values):
        self.mcp.events.extend(values)
        for _ in values:
            await self.agent.collect_once(0)

    async def end(self):
        self.backend.release.set()
        await asyncio.wait_for(self.task, 2)

    def state(self, eid):
        return self.store.db.execute('SELECT state FROM jobs WHERE event_id=?', (eid,)).fetchone()[0]

    async def test_private_continuous_guidance_uses_same_turn_and_one_reply(self):
        await self.begin()
        await self.feed(event('g1', text='先查原因'), event('g2', text='回答简短些'))
        self.assertEqual(await asyncio.wait_for(self.backend.steered.get(), 2), 'g1')
        self.assertEqual(await asyncio.wait_for(self.backend.steered.get(), 2), 'g2')
        await self.end()
        self.assertEqual(len(self.backend.inputs), 1)
        self.assertEqual({(t,r) for t,r,_ in self.backend.guides}, {(self.backend.guides[0][0], 'turn-0')})
        self.assertEqual([x['event_id'] for x in self.mcp.sent], ['root'])
        self.assertEqual(self.mcp.sent[-1]['text'], '回答简短些')
        self.assertEqual(self.state('g1'), 'merged_done')
        self.assertEqual(self.state('g2'), 'merged_done')

    async def test_group_same_sender_bypasses_other_sender_without_reordering_queue(self):
        cid = '12345@chatroom'
        await self.begin(dict(event('root',cid), sender_id='a'))
        await self.feed(dict(event('b1',cid),sender_id='b'),dict(event('g',cid),sender_id='a'),dict(event('c1',cid),sender_id='c'))
        self.assertEqual(await asyncio.wait_for(self.backend.steered.get(),2), 'g')
        await self.end()
        self.assertEqual([x['event_id'] for x in self.store.ready()], ['b1'])
        await self.agent.process(self.store.ready()[0])
        self.assertEqual([x['event_id'] for x in self.store.ready()], ['c1'])

    async def test_group_requires_sender_and_native_self_mention(self):
        cid = '12345@chatroom'
        await self.begin(dict(event('root',cid),sender_id='a'))
        all_only=dict(event('all',cid,mention=False),sender_id='a')
        all_only['mentions']['all']=True
        await self.feed(event('missing',cid),dict(event('plain',cid,text='@机器人',mention=False),sender_id='a'),all_only)
        await self.end()
        self.assertEqual(self.backend.guides, [])
        self.assertEqual(self.state('missing'), 'pending')
        self.assertIsNone(self.store.db.execute("SELECT 1 FROM jobs WHERE event_id='plain'").fetchone())
        self.assertIsNone(self.store.db.execute("SELECT 1 FROM jobs WHERE event_id='all'").fetchone())

    async def test_preexisting_queue_is_not_retroactively_merged(self):
        await self.begin(prequeued=[event('older')])
        await self.feed(event('new',text='新引导'))
        await asyncio.wait_for(self.backend.steered.get(),2)
        await self.end()
        self.assertEqual(self.state('older'),'pending')
        self.assertEqual(self.state('new'),'merged_done')

    async def test_no_active_turn_and_explicit_rejection_use_same_thread_next_turn(self):
        for live in (False,True):
            with self.subTest(live=live):
                # 每个子场景独立会话。
                self.backend.inputs.clear();self.backend.answer=None
                self.backend.live=live;self.backend.reject=True;self.backend.release.clear()
                await self.begin(event('root'+str(live), 'wxid_test_peer' if live else 'wxid_test_other'))
                guide=event('guide'+str(live),self.root['conversation_id'],text='修正要求')
                await self.feed(guide)
                if live:
                    await asyncio.wait_for(self.backend.steered.get(),2)
                await self.end()
                self.assertEqual(len(self.backend.inputs),2)
                self.assertEqual(self.mcp.sent[-1]['text'],'修正要求')
                self.assertEqual(self.state(guide['event_id']),'merged_done')

    async def test_unknown_submission_pauses_without_send_or_retry(self):
        self.backend.unknown=True
        await self.begin()
        await self.feed(event('guide'))
        await asyncio.wait_for(self.task,2)
        self.assertEqual(self.state('root'),'generation_unknown')
        self.assertTrue(self.store.conversation(self.root['conversation_id'])['paused'])
        self.assertEqual(self.mcp.sent,[])
        self.assertEqual(len(self.backend.guides),1)
        self.store.recover()
        self.assertEqual(self.store.ready(),[])

    async def test_ack_replay_does_not_duplicate_guidance(self):
        await self.begin()
        self.mcp.fail_ack=True
        with self.assertRaises(Exception):
            await self.feed(event('guide'))
        self.mcp.fail_ack=False
        await self.agent.collect_once(0)
        await asyncio.wait_for(self.backend.steered.get(),2)
        await self.end()
        self.assertEqual(len(self.backend.guides),1)

    async def test_final_verify_gap_discards_old_draft(self):
        original=self.agent.verify
        injected=False
        async def verify():
            nonlocal injected
            if not injected:
                injected=True
                await self.feed(event('late',text='最终只回答新要求'))
            return await original()
        self.agent.verify=verify
        await self.begin()
        await self.end()
        self.assertEqual(len(self.backend.inputs),2)
        self.assertEqual([s['text'] for s in self.mcp.sent],['最终只回答新要求'])

    async def test_after_final_submission_new_message_remains_queued(self):
        original=self.mcp.call
        async def send(name,**args):
            if name in ('reply_text','reply_text_part'):
                await self.feed(event('after'))
            return await original(name,**args)
        self.mcp.call=send
        await self.begin()
        await self.end()
        self.assertEqual(self.state('after'),'pending')
        self.assertEqual(self.backend.guides,[])

    async def test_guidance_reference_fetch_uses_its_own_event_authorization(self):
        original=self.mcp.call
        async def resolve(name,**args):
            if name=='resolve_reference':
                return {'event_id':'image','message_type':'image','payload':{}}
            return await original(name,**args)
        self.mcp.call=resolve
        self.backend.response=lambda i,v: ('读取',{'read_attachment':True,'request_event_id':'guide'}) if i==0 else ('图片结论',{})
        await self.begin()
        await self.feed(dict(event('guide',text='看这张图'),reference={'message_id':'image-mid'}))
        await asyncio.wait_for(self.backend.steered.get(),2)
        with patch('local_agent.attachments.fetch',new=AsyncMock(return_value={'message_type':'image'})) as fetch, \
                patch('local_agent.media.prepare',return_value={'prepared':{'text':'内容'}}):
            await self.end()
        self.assertEqual(fetch.await_args.args[1],'guide')
        self.assertEqual(self.mcp.sent[-1]['event_id'],'root')
        self.assertEqual(self.mcp.sent[-1]['text'],'图片结论')

    async def test_merged_context_has_one_complete_answer_and_queue_unblocks(self):
        await self.begin()
        await self.feed(event('guide',text='补充'))
        await asyncio.wait_for(self.backend.steered.get(),2)
        await self.end()
        self.store.ingest([event('next')])
        context=self.store.select_context('next')
        self.assertEqual(sum('assistant_reply' in m for m in context['messages']),1)
        self.assertEqual(context['messages'][-1]['assistant_reply'],'补充')
        self.assertEqual(self.store.ready()[0]['event_id'],'next')

    async def test_messages_during_resource_read_preserve_both_exact_authorizations(self):
        original=self.mcp.call
        async def resolve(name,**args):
            if name=='resolve_reference':
                return {'event_id':'image-'+args['event_id'],'message_type':'image','payload':{}}
            return await original(name,**args)
        self.mcp.call=resolve
        self.backend.response=lambda i,v: ('读取',{'read_attachment':True,'request_event_id':'root' if i==0 else 'guide'}) if i<2 else ('结合两张图的结论',{})
        reading,complete=asyncio.Event(),asyncio.Event()
        async def fetch(mcp,eid,*args):
            if eid=='root':
                reading.set()
                await complete.wait()
            return {'message_type':'image'}
        with patch('local_agent.attachments.fetch',new=AsyncMock(side_effect=fetch)) as fetch_mock, \
                patch('local_agent.media.prepare',return_value={'prepared':{'text':'图片内容'}}):
            await self.begin(dict(event('root',text='分析这张图'),reference={'message_id':'root-image'}))
            self.backend.release.set()
            await asyncio.wait_for(reading.wait(),2)
            await self.feed(dict(event('guide',text='再比较这一张'),reference={'message_id':'guide-image'}))
            complete.set()
            await asyncio.wait_for(self.task,2)
        self.assertEqual([c.args[1] for c in fetch_mock.await_args_list],['root','guide'])
        self.assertEqual(self.backend.guides,[])
        self.assertEqual(self.backend.inputs[1]['event_id'],'guide')
        self.assertEqual(self.backend.inputs[1]['resource_results'][0]['event_id'],'root')
        self.assertEqual(self.mcp.sent[-1]['text'],'结合两张图的结论')
        self.assertEqual(self.state('guide'),'merged_done')

    async def test_schedule_action_uses_guidance_source_and_stable_root_reply(self):
        action={'action':'list'}
        self.backend.response=lambda i,v: ('查询',{'schedule_action':action,'request_event_id':'guide'}) if i==0 else ('查询结果',{})
        self.agent.scheduler.command=AsyncMock(return_value={'ok':True,'tasks':[]})
        await self.begin()
        await self.feed(event('guide',text='查一下我的定时任务'))
        await asyncio.wait_for(self.backend.steered.get(),2)
        await self.end()
        source=self.agent.scheduler.command.await_args.args[2]
        self.assertEqual(source['event_id'],'guide')
        self.assertEqual(self.mcp.sent[-1]['event_id'],'root')
        self.assertEqual(self.mcp.sent[-1]['text'],'查询结果')

    async def test_non_text_and_other_conversation_do_not_steer(self):
        await self.begin()
        await self.feed(dict(event('image'),message_type='image',payload={'name':'image'}),
                        event('other','wxid_test_other'))
        await self.end()
        self.assertEqual(self.backend.guides,[])
        self.assertEqual(self.state('other'),'pending')
        self.assertIsNone(self.store.db.execute("SELECT 1 FROM jobs WHERE event_id='image'").fetchone())

    async def test_restart_uncertain_submission_blocks_root_and_keeps_ids(self):
        await self.begin()
        await self.feed(event('guide'))
        await asyncio.wait_for(self.backend.steered.get(),2)
        self.store.db.execute("UPDATE steering SET state='submitting' WHERE event_id='guide'")
        self.task.cancel()
        await asyncio.gather(self.task,return_exceptions=True)
        self.store.recover()
        self.assertEqual(self.state('root'),'generation_unknown')
        row=self.store.db.execute("SELECT * FROM steering WHERE event_id='guide'").fetchone()
        self.assertEqual(row['state'],'unknown')
        self.assertEqual(row['root_id'],'root')
        self.assertEqual(row['turn_id'],'turn-0')
        self.assertEqual(self.store.ready(),[])

    async def test_retention_keeps_link_until_entire_completed_task_expires(self):
        await self.begin()
        await self.feed(event('guide'))
        await asyncio.wait_for(self.backend.steered.get(),2)
        await self.end()
        self.store.db.execute("UPDATE jobs SET created=0 WHERE event_id='root'")
        self.store.db.execute("UPDATE jobs SET created=100 WHERE event_id='guide'")
        self.store.prune(7*86400+50)
        self.assertEqual(self.state('root'),'accepted')
        self.assertEqual(self.state('guide'),'merged_done')
        self.store.prune(7*86400+200)
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM steering').fetchone()[0],0)
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0],0)

    async def test_guidance_arriving_during_model_startup_joins_exact_started_turn(self):
        startup,ready=asyncio.Event(),asyncio.Event()
        original=self.backend.generate
        async def generate(tid,value):
            startup.set()
            await ready.wait()
            return await original(tid,value)
        self.backend.generate=generate
        self.root=event('root')
        self.store.ingest([self.root])
        self.task=asyncio.create_task(self.agent.process(self.store.ready()[0]))
        self.tasks.append(self.task)
        await asyncio.wait_for(startup.wait(),2)
        await self.feed(event('startup-guide',text='启动期间的新要求'))
        ready.set()
        self.assertEqual(await asyncio.wait_for(self.backend.steered.get(),2),'startup-guide')
        await self.end()
        self.assertEqual(len(self.backend.inputs),1)
        self.assertEqual(self.mcp.sent[-1]['text'],'启动期间的新要求')

    async def test_foreign_control_source_does_not_borrow_root_authority(self):
        self.backend.response=lambda i,v: ('读取',{'read_attachment':True,'request_event_id':'foreign-event'})
        await self.begin()
        with patch('local_agent.attachments.fetch',new=AsyncMock()) as fetch:
            await self.end()
        fetch.assert_not_awaited()
        self.assertEqual(self.mcp.sent,[])
        self.assertEqual(self.state('root'),'generation_unknown')

    async def test_missing_root_group_sender_never_merges_another_missing_sender(self):
        await self.begin(event('root','12345@chatroom'))
        await self.feed(event('missing','12345@chatroom'))
        await self.end()
        self.assertEqual(self.backend.guides,[])
        self.assertEqual(self.state('missing'),'pending')

    async def test_unquoted_read_request_finishes_without_resource_loop(self):
        self.backend.response=lambda i,v: ('读取',{'read_attachment':True})
        await self.begin()
        with patch('local_agent.attachments.fetch',new=AsyncMock()) as fetch:
            await self.end()
        fetch.assert_not_awaited()
        self.assertEqual(self.state('root'),'accepted')

    async def test_original_reference_remains_authorized_after_plain_guidance(self):
        original=self.mcp.call
        async def resolve(name,**args):
            if name=='resolve_reference':
                return {'event_id':'image','message_type':'image','payload':{}}
            return await original(name,**args)
        self.mcp.call=resolve
        self.backend.response=lambda i,v: ('读取',{'read_attachment':True,'request_event_id':'root'}) if i==0 else ('简短图片结论',{})
        await self.begin(dict(event('root'),reference={'message_id':'image'}))
        await self.feed(event('guide',text='说简单点'))
        await asyncio.wait_for(self.backend.steered.get(),2)
        with patch('local_agent.attachments.fetch',new=AsyncMock(return_value={'message_type':'image'})) as fetch, \
                patch('local_agent.media.prepare',return_value={'prepared':{'text':'内容'}}):
            await self.end()
        self.assertEqual(fetch.await_args.args[1],'root')
        self.assertEqual(self.mcp.sent[-1]['text'],'简短图片结论')

    async def test_final_send_recovery_only_queries_original_operation(self):
        original=self.mcp.call
        async def queued_send(name,**args):
            result=await original(name,**args)
            if name in ('reply_text','reply_text_part'):
                result['state']='queued'
            return result
        self.mcp.call=queued_send
        await self.begin()
        await self.feed(event('guide',text='补充'))
        await asyncio.wait_for(self.backend.steered.get(),2)
        await self.end()
        self.assertEqual(self.state('root'),'sending')
        calls=len(self.backend.inputs)
        for operation in self.mcp.operations.values():
            operation['state']='accepted'
        self.mcp.call=original
        self.store.recover()
        restarted=Agent(self.store,self.mcp,self.backend,CONFIG)
        await restarted.process(self.store.ready()[0])
        self.assertEqual(len(self.backend.inputs),calls)
        self.assertEqual(len(self.mcp.sent),1)
        self.assertEqual(self.state('guide'),'merged_done')


class AdaptiveSteeringTests(SteeringTests):
    adaptive=True

    async def test_pending_nonfinal_send_keeps_guidance_window_and_segment_numbers(self):
        self.backend.response=lambda i,v: ('第一段',{'continue_reply':True}) if i==0 else (v['payload']['text'],{})
        original=self.mcp.call
        async def queued_send(name,**args):
            result=await original(name,**args)
            if name=='reply_text_part' and args['part_index']==0:
                result['state']='queued'
            return result
        self.mcp.call=queued_send
        await self.begin()
        await self.end()
        self.assertEqual(self.state('root'),'sending')
        self.assertTrue(self.agent.reply_tasks[self.root['conversation_id']].open)
        await self.feed(event('guide',text='后面改用简短结论'))
        self.assertEqual(self.state('guide'),'merged')
        self.mcp.operations[('root',0)]['state']='accepted'
        self.mcp.call=original
        await self.agent.process(self.store.ready()[0])
        self.assertEqual([s['part_index'] for s in self.mcp.sent],[0,1])
        self.assertEqual(self.mcp.sent[-1]['text'],'后面改用简短结论')
        self.assertEqual(self.state('guide'),'merged_done')
