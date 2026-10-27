import json
import tempfile
import unittest
from unittest.mock import AsyncMock, patch
from local_agent.storage import Store
from local_agent.runtime import identity
from local_agent.service import Agent
from local_agent.codex_backend import ModelUnavailable
from fakes import CONFIG,ACCOUNT,event,FakeBackend,FakeMCP

class AdaptiveBackend(FakeBackend):
    async def create_task_thread(self,cid,eid,thread_id=None):
        return await self.ensure_thread(cid,thread_id)

class AdaptiveTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory();self.store=Store(self.temp.name,identity(CONFIG))
        self.backend=AdaptiveBackend();self.mcp=FakeMCP();self.agent=Agent(self.store,self.mcp,self.backend,CONFIG)
    async def asyncTearDown(self):self.store.close();self.temp.cleanup()

    async def test_card_source_intent_and_profile_reach_preparer(self):
        value=event('card-request',text='分析引用链接正文')
        value['reference']={'source':'weixin41930_native_reference_v1','message_id':'123'}
        self.store.ingest([value])
        original=self.mcp.call
        async def call(name,**args):
            if name=='resolve_reference':
                return dict(event('card'),message_type='card',payload={})
            return await original(name,**args)
        self.mcp.call=call
        self.backend.generate=AsyncMock(side_effect=[('读取',{'read_attachment':True,'read_source':True}),
            ('正文分析结果',{'continue_reply':False})])
        with patch('local_agent.staged.attachments.fetch',new=AsyncMock(return_value={'message_type':'card'})), \
                patch('local_agent.staged.media.prepare',return_value={'prepared':{'source_contents':[{'text':'正文'}]}}) as prepare:
            await self.agent.process(self.store.ready()[0])
        self.assertTrue(prepare.call_args.args[1]['read_source'])
        self.assertEqual(prepare.call_args.args[1]['channel'],'msedge')
        self.assertTrue(prepare.call_args.args[1]['profile_dir'].endswith('browser-profile'))
        self.assertFalse(self.mcp.sent[0]['final'])
        self.assertIn('读取',self.mcp.sent[0]['text'])
        self.assertEqual(self.mcp.sent[1]['text'],'正文分析结果')
        self.assertEqual(self.store.status()['jobs'],{'accepted':1})

    async def test_question_has_bounded_history_and_own_task_thread(self):
        self.store.ingest([event('old',text='secret'),event('new',text='你好')])
        self.store.update('old','accepted',reply='earlier')
        job=next(x for x in self.store.ready() if x['event_id']=='new')
        await self.agent.process(job)
        result=json.loads(self.store.db.execute("SELECT result FROM jobs WHERE event_id='new'").fetchone()[0])
        self.assertEqual([m['event_id'] for m in result['task_input']['context']['messages']],['old'])
        self.assertEqual(result['task_input']['context']['messages'][0]['assistant_reply'],'earlier')
        self.assertNotEqual(result['task_thread_id'],self.store.conversation('wxid_test_peer')['thread_id'])
        self.assertEqual(len(self.mcp.sent),1)

    async def test_known_capacity_failure_sends_explanation_and_does_not_pause_next_request(self):
        self.store.ingest([event('capacity'),event('after-capacity')])
        self.backend.generate=AsyncMock(side_effect=[ModelUnavailable('thread','turn'),('下一条正常',{})])
        for eid in ('capacity','after-capacity'):
            job=next(j for j in self.store.ready() if j['event_id']==eid)
            await self.agent.process(job)
        self.assertIn('容量不足',self.mcp.sent[0]['text'])
        self.assertEqual(self.mcp.sent[1]['text'],'下一条正常')
        self.assertFalse(self.store.conversation('wxid_test_peer')['paused'])
        self.assertEqual(self.store.status()['jobs'],{'accepted':2})

    async def test_execution_decision_is_sent_before_same_turn_finishes(self):
        self.store.ingest([event('stream',text='帮我联网核对')])
        async def stream(tid, request, callback):
            self.assertEqual(request['_decision_timeout'],30)
            self.assertEqual(request['_tool_timeout'],180)
            await callback('我先联网搜索。',{'key':'turn:decision','kind':'commentary'})
            self.assertEqual(len(self.mcp.sent),1)
            self.assertFalse(self.mcp.sent[0]['final'])
            # Duplicate SDK notification must not create a second send.
            await callback('我先联网搜索。',{'key':'turn:decision','kind':'commentary'})
            self.assertEqual(len(self.mcp.sent),1)
            # Distinct search calls with the same wording are also suppressed.
            await callback('我先联网搜索。',{'key':'turn:search-2','kind':'tool_decision'})
            await callback(' 我先联网搜索。 ',{'key':'turn:search-3','kind':'tool_decision'})
            self.assertEqual(len(self.mcp.sent),1)
            return '查到了，结论如下。',{}
        self.backend.generate_with_progress=stream
        await self.agent.process(self.store.ready()[0])
        self.assertEqual([p['part_index'] for p in self.mcp.sent],[0,1])
        self.assertEqual([p['final'] for p in self.mcp.sent],[False,True])
        self.assertEqual(self.store.status()['jobs'],{'accepted':1})

    async def test_duplicate_progress_does_not_skip_index_or_suppress_distinct_update_or_final(self):
        self.store.ingest([event('dedup-progress')])
        async def stream(tid,request,callback):
            await callback('我先联网搜索。',{'key':'search-1','kind':'tool_decision'})
            await callback('我先联网搜索。',{'key':'search-2','kind':'tool_decision'})
            await callback('我再打开来源网页。',{'key':'open','kind':'tool_decision'})
            await callback('我先联网搜索。',{'key':'search-3','kind':'tool_decision'})
            return '最终结论',{}
        self.backend.generate_with_progress=stream
        await self.agent.process(self.store.ready()[0])
        self.assertEqual([p['text'] for p in self.mcp.sent],['我先联网搜索。','我再打开来源网页。','我先联网搜索。','最终结论'])
        self.assertEqual([p['part_index'] for p in self.mcp.sent],[0,1,2,3])
        self.assertEqual([p['final'] for p in self.mcp.sent],[False,False,False,True])

    async def test_last_sent_text_survives_safe_resume_and_allows_a_b_a(self):
        value=event('resume-progress');self.store.ingest([value])
        tid=await self.backend.create_task_thread(value['conversation_id'],value['event_id'])
        self.store.update('resume-progress','continuing',result={'adaptive':True,'task_thread_id':tid,
            'task_input':value,'part_index':1,'parts':[{'part_index':0,'text':'A','state':'accepted','progress_key':'old-a'}],
            'generation':{'continue_reply':True,'context_mode':'none'}})
        self.store.close()
        self.store=Store(self.temp.name,identity(CONFIG));self.agent.store=self.store
        self.store.recover()
        async def stream(tid,request,callback):
            await callback('A',{'key':'new-a','kind':'commentary'})
            self.assertEqual(self.mcp.sent,[])
            await callback('B',{'key':'b','kind':'commentary'})
            await callback('B',{'key':'duplicate-b','kind':'commentary'})
            await callback('A',{'key':'back-to-a','kind':'commentary'})
            # Replaying the same SDK event stays idempotent even after other text.
            await callback('A',{'key':'old-a','kind':'commentary'})
            return '最终结论',{}
        self.backend.generate_with_progress=stream
        await self.agent.process(self.store.ready()[0])
        self.assertEqual([p['text'] for p in self.mcp.sent],['B','A','最终结论'])
        self.assertEqual([p['part_index'] for p in self.mcp.sent],[1,2,3])
        self.assertEqual(self.store.status()['jobs'],{'accepted':1})

    async def test_progress_text_dedup_is_scoped_to_current_request(self):
        self.store.ingest([event('request-one'),event('request-two')])
        async def stream(tid,request,callback):
            await callback('我先联网搜索。',{'key':'search','kind':'tool_decision'})
            return '最终结论',{}
        self.backend.generate_with_progress=stream
        for eid in ('request-one','request-two'):
            job=next(j for j in self.store.ready() if j['event_id']==eid)
            await self.agent.process(job)
        self.assertEqual(sum(p['text']=='我先联网搜索。' for p in self.mcp.sent),2)
        self.assertEqual([p['part_index'] for p in self.mcp.sent],[0,1,0,1])

    async def test_unknown_progress_stops_generation_without_final_or_index_skip(self):
        self.store.ingest([event('stream-unknown')]);continued=[]
        original=self.mcp.call
        async def call(name,**args):
            if name=='reply_text_part':return {'state':'unknown'}
            return await original(name,**args)
        self.mcp.call=call
        async def stream(tid,request,callback):
            await callback('我先联网搜索。',{'key':'turn:decision','kind':'commentary'})
            continued.append(True)
            return '不应到达',{}
        self.backend.generate_with_progress=stream
        await self.agent.process(self.store.ready()[0])
        self.assertEqual(continued,[])
        self.assertEqual(self.store.status()['jobs'],{'unknown':1})
        self.assertTrue(self.store.conversation('wxid_test_peer')['paused'])

    async def test_crash_after_progress_never_regenerates_or_resends(self):
        self.store.ingest([event('stream-crash')])
        self.store.update('stream-crash','generating',result={'adaptive':True,
            'parts':[{'part_index':0,'state':'accepted','text':'已开始搜索'}],
            'progress_pending':{'part_index':1,'text':'第二项决定'}})
        self.store.recover()
        self.assertEqual(self.store.status()['jobs'],{'generation_unknown':1})
        self.assertEqual(self.store.ready(),[])

    async def test_two_substantive_parts_are_durable_and_ordered(self):
        self.store.ingest([event('multi',text='给我详细计划')])
        self.backend.generate=AsyncMock(side_effect=[('第一步：确定目标',{'continue_reply':True,'next_step':'给第二步'}),
            ('第二步：实际执行',{'continue_reply':False})])
        await self.agent.process(self.store.ready()[0])
        row=self.store.db.execute("SELECT state,result FROM jobs WHERE event_id='multi'").fetchone()
        self.assertEqual(row[0],'accepted')
        self.assertEqual([x['part_index'] for x in self.mcp.sent],[0,1])
        self.assertEqual([x['final'] for x in self.mcp.sent],[False,True])
        self.assertEqual(len(json.loads(row[1])['parts']),2)
        for call in self.backend.generate.call_args_list:self.assertEqual(call.args[1]['_model_timeout'],30)

    async def test_selection_never_crosses_conversation_or_trigger_boundary(self):
        self.store.ingest([event('old',text='本轮代号青松'),event('trigger',text='请复述本轮代号'),
            event('future',text='future'),event('other','wxid_test_other',text='cross')])
        context=self.store.select_context('trigger')
        self.assertEqual([x['event_id'] for x in context['messages']],['old'])
        self.assertEqual(context['selection']['mode'],'recent')

    async def test_group_implicit_followup_keeps_other_participants_answers(self):
        cid='12345@chatroom'
        first=event('first',cid,'初始化尺度是0.02')
        first['sender_id']='participant-a'
        trigger=event('followup',cid,'那范围大概多大')
        trigger['sender_id']='participant-b'
        self.store.ingest([first,event('silent',cid,'我也想知道',False),trigger,
                           event('future',cid,'后续消息'),event('other',text='其他会话')])
        self.store.update('first','accepted',reply='这是初始化标准差。')
        job=next(j for j in self.store.ready() if j['event_id']=='followup')
        self.backend.generate=AsyncMock(return_value=('结合群背景回答',{'context_mode':'none'}))
        await self.agent.process(job)
        context=self.backend.generate.call_args.args[1]['context']
        self.assertEqual([m['event_id'] for m in context['messages']],['first','silent'])
        self.assertEqual(context['messages'][0]['assistant_reply'],'这是初始化标准差。')
        self.assertEqual(context['messages'][0]['sender_id'],'participant-a')
        self.assertEqual(context['selection']['limit'],12)

    async def test_native_reference_to_own_echo_is_not_dropped(self):
        cid='12345@chatroom'
        self.store.ingest([event('original',cid,'问题')])
        self.store.update('original','accepted',reply='已发送答案',result={'operation':{'result':{'result':{
            'native_sort_seq':'12345','native_sort_seq_source':'weixin41930_send_task_record110_v1'}}}})
        echo=dict(event('echo',cid,'已发送答案'),direction='outgoing',sender_id=ACCOUNT,
                  message_id='quoted-mid',native_sort_seq='12345')
        trigger=event('quote',cid,'详细解释')
        trigger['reference']={'message_id':'quoted-mid','source':'weixin41930_native_reference_v1'}
        self.store.ingest([echo,trigger])
        self.assertEqual([m['event_id'] for m in self.store.select_context('quote')['messages']],['original','echo'])
        self.assertNotIn('echo',[m['event_id'] for m in self.store.select_context('quote',mode='recent')['messages']])

    async def test_default_recent_limit_is_twelve_for_private_and_group(self):
        for cid in ('wxid_test_peer','12345@chatroom'):
            with self.subTest(conversation=cid):
                prefix=cid+'-'
                self.store.ingest([event(prefix+str(i),cid) for i in range(20)]+
                                  [event(prefix+'trigger',cid)])
                context=self.store.select_context(prefix+'trigger')
                self.assertEqual([m['event_id'] for m in context['messages']],
                                 [prefix+str(i) for i in range(8,20)])

    async def test_reference_window_has_twelve_each_side_and_excludes_future_and_other_chat(self):
        for cid in ('wxid_test_peer','12345@chatroom'):
            with self.subTest(conversation=cid):
                prefix=cid+'-'
                target=dict(event(prefix+'target',cid),message_id=prefix+'mid')
                trigger=dict(event(prefix+'trigger',cid),reference={'message_id':prefix+'mid'})
                self.store.ingest([event(prefix+'before'+str(i),cid) for i in range(20)]+
                    [target,event(prefix+'other','wxid_test_other')]+
                    [event(prefix+'after'+str(i),cid) for i in range(300)]+
                    [trigger,event(prefix+'future',cid)])
                context=self.store.select_context(prefix+'trigger')
                self.assertEqual([m['event_id'] for m in context['messages']],
                    [prefix+'before'+str(i) for i in range(8,20)]+[prefix+'target']+
                    [prefix+'after'+str(i) for i in range(12)])
                limited=self.store.select_context(prefix+'trigger',mode='reference',limit=2)
                self.assertEqual([m['event_id'] for m in limited['messages']],
                    [prefix+'before18',prefix+'before19',prefix+'target',prefix+'after0',prefix+'after1'])

    async def test_reference_window_keeps_target_under_byte_budget_without_filling_short_side(self):
        target=dict(event('budget-target',text='目标'*1500),message_id='budget-mid')
        trigger=dict(event('budget-trigger'),reference={'message_id':'budget-mid'})
        self.store.ingest([event('one-before'),target]+
            [event('long'+str(i),text='背景'*1500) for i in range(20)]+[trigger])
        context=self.store.select_context('budget-trigger')
        ids=[m['event_id'] for m in context['messages']]
        self.assertIn('budget-target',ids)
        self.assertIn('one-before',ids)
        self.assertNotIn('long12',ids)
        self.assertLessEqual(len(json.dumps(context,ensure_ascii=False).encode()),16384)

    async def test_model_can_search_outside_default_window_then_finish_without_more_context(self):
        self.store.ingest([event('older',text='项目代号青松')]+
                          [dict(event('background'+str(i)),direction='outgoing') for i in range(20)]+[event('ask')])
        self.store.update('older','accepted',reply='已确认代号。')
        snapshots=[]
        async def generate(tid,request):
            snapshots.append(json.loads(json.dumps(request)))
            if len(snapshots)==1:
                return '我先核对之前的项目代号。',{'context_mode':'search','context_limit':2,
                    'context_query':'青松','continue_reply':True,'next_step':'结合查询结果回答'}
            return '代号是青松。',{'context_mode':'none','continue_reply':False}
        self.backend.generate=generate
        job=next(j for j in self.store.ready() if j['event_id']=='ask')
        await self.agent.process(job)
        self.assertNotIn('older',[m['event_id'] for m in snapshots[0]['context']['messages']])
        self.assertEqual([m['event_id'] for m in snapshots[1]['context']['messages']],['older'])
        self.assertEqual(snapshots[1]['context']['messages'][0]['assistant_reply'],'已确认代号。')
        self.assertEqual(len(snapshots),2)
        self.assertTrue(self.mcp.sent[-1]['final'])

    async def test_missing_reference_target_does_not_guess_window(self):
        self.store.ingest([event('background'),dict(event('missing'),reference={'message_id':'absent'})])
        context=self.store.select_context('missing')
        self.assertEqual(context['messages'],[])
        self.assertEqual(context['selection']['reason'],'reference_target_missing')
        self.assertEqual(self.store.select_context('background',mode='reference')['selection']['reason'],
                         'native_reference_missing')

    async def test_default_group_snapshot_is_bounded_even_without_keywords(self):
        cid='12345@chatroom'
        self.store.ingest([event(str(i),cid,'背景'*1500,False) for i in range(30)]+
                          [event('plain',cid,'qwen3.5权重值多少')])
        context=self.store.select_context('plain')
        self.assertEqual(context['selection']['mode'],'recent')
        self.assertGreater(len(context['messages']),0)
        self.assertLessEqual(len(context['messages']),12)
        self.assertLessEqual(len(json.dumps(context,ensure_ascii=False).encode()),16384)
        self.assertEqual(self.store.select_context('plain',mode='none')['messages'],[])

    async def test_unknown_part_prevents_next_model_step_and_pauses(self):
        self.store.ingest([event('unknown-part')])
        original=self.mcp.call
        async def unknown(name,**args):
            if name=='reply_text_part':return {'state':'unknown','request_id':'original'}
            return await original(name,**args)
        self.mcp.call=unknown
        self.backend.generate=AsyncMock(return_value=('已完成第一步',{'continue_reply':True,'next_step':'第二步'}))
        await self.agent.process(self.store.ready()[0])
        self.assertEqual(self.backend.generate.await_count,1)
        self.assertEqual(self.store.status()['jobs'],{'unknown':1})
        self.assertTrue(self.store.conversation('wxid_test_peer')['paused'])

    async def test_later_step_timeout_sends_one_final_error_without_repeating_first(self):
        self.store.ingest([event('timeout-part')])
        self.backend.generate=AsyncMock(side_effect=[('第一步已完成',{'continue_reply':True}),TimeoutError()])
        await self.agent.process(self.store.ready()[0])
        self.assertEqual([x['part_index'] for x in self.mcp.sent],[0,1])
        self.assertTrue(self.mcp.sent[1]['final'])
        self.assertIn('超时',self.mcp.sent[1]['text'])
        self.assertEqual(self.store.status()['jobs'],{'accepted':1})

    async def test_context_requests_and_results_are_bounded(self):
        self.store.ingest([event(str(i),text='x'*3000) for i in range(20)]+[event('now',text='继续')])
        context=self.store.select_context('now',mode='recent',limit=12)
        self.assertLessEqual(len(context['messages']),12)
        self.assertLessEqual(context['selection']['bytes'],16384)
