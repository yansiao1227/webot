"""Question-scoped generation and durable ordered reply parts."""
import asyncio
import json
import time
from .mcp import allowed,MCPError
from .codex_backend import ModelThreadBusy, ModelUnavailable
from .progress import ProgressDeliveryError
from . import attachments,media

MAX_PARTS = 64

async def process(agent,job):
    store=agent.store;eid=job['event_id'];event=json.loads(job['event'])
    details=json.loads(job['result'] or '{}');state=job['state']
    event.pop('_model_timeout',None)
    if not allowed(event,agent.config['account'],agent.config['conversations']):
        store.update(eid,'failed',error='分段任务不再符合会话或群@门禁');return
    parts=details.get('parts',[])
    async def progress(text, metadata):
        if agent.stop.is_set():
            raise ProgressDeliveryError('服务停止，暂停进度发送')
        text = text.strip()
        key = metadata['key']
        # Event identity remains idempotent. Wording only compares with the last
        # accepted message, including messages persisted before a safe resume.
        if any(p.get('state') == 'accepted' and p.get('progress_key') == key for p in parts):
            return
        previous = next((p for p in reversed(parts) if p.get('state') == 'accepted'), None)
        if previous is not None and previous.get('text', '').strip() == text:
            return
        index = details.get('part_index',0)
        if index >= MAX_PARTS - 1:
            raise ProgressDeliveryError('本轮执行消息达到上限')
        pending = {'part_index':index,'text':text,'progress_key':key,'kind':metadata['kind']}
        details['progress_pending'] = pending
        store.update(eid,'generating',result=details)
        try:
            await agent.verify()
            try:
                operation=await agent.mcp.call('reply_text_part',event_id=eid,part_index=index,text=text,final=False)
            except Exception:
                try:operation=await agent.mcp.call('get_reply_part_status',event_id=eid,part_index=index)
                except Exception:operation={'state':'unknown'}
            deadline=time.monotonic()+8
            while operation.get('state') in ('queued','running','submitting','prepared') and time.monotonic()<deadline:
                await asyncio.sleep(.2)
                operation=await agent.mcp.call('get_reply_part_status',event_id=eid,part_index=index)
        except asyncio.CancelledError:
            store.update(eid,'unknown',result={'progress_pending':pending},
                         error='进度发送观察被中断，原段需核查，禁止重发')
            raise ProgressDeliveryError('进度发送观察未确认') from None
        # A queued/unknown progress message cannot be skipped while the model keeps running.
        if operation.get('state')!='accepted':
            store.update(eid,'unknown',result={'progress_pending':{**pending,'operation':operation}},
                         error='进度发送未确认成功，已停止模型，禁止重发或跳段')
            raise ProgressDeliveryError('进度发送未确认')
        parts.append({**pending,'state':'accepted','operation':operation,
                      'elapsed_seconds':round(time.time()-details.get('part_started_at',time.time()),3)})
        details.update(parts=parts,part_index=index+1,progress_pending=None)
        store.update(eid,'generating',result=details)

    async def generate(tid, event):
        event['_decision_timeout']=agent.config.get('decision_timeout',agent.config.get('text_model_timeout',30))
        event['_tool_timeout']=agent.config.get('tool_timeout',agent.config.get('model_timeout',180))
        method=getattr(agent.backend,'generate_with_progress',None)
        async def call(current):
            if callable(method):return await method(tid,current,progress)
            return await agent.backend.generate(tid,current)
        reply,generation=await call(event)
        return await agent.scheduler.resolve_actions(job,tid,event,reply,generation,
            stage=str(details.get('part_index',0)), generate=call)
    try:
        if state=='pending':
            details['part_started_at']=time.time()
            if agent.config.get('mark_read_on_process',True):
                try:
                    details['conversation_read']=await asyncio.wait_for(agent.mcp.call('mark_conversation_read',event_id=eid),3)
                except (MCPError,TimeoutError):details['conversation_read']={'state':'unknown'}
            event['context']=store.select_context(eid)
            target=None
            if event.get('reference'):
                try:
                    target=await agent.mcp.call('resolve_reference',event_id=eid)
                    event['reference_target']={k:target[k] for k in ('event_id','message_type','sender_id','message_id') if k in target}
                    event['reference_target']['payload']={k:target['payload'][k] for k in ('text','name','mime','size') if k in target['payload']}
                except MCPError:event['reference_error']='无法精确找到原生引用目标，请重新引用，不能猜测附近附件'
            conversation=store.conversation(job['conversation'])
            # Preserve the existing logical conversation thread mapping. It is
            # no longer the automatically included model history of each question.
            if not conversation['thread_id']:
                owner=await agent.backend.ensure_thread(job['conversation'])
                store.set_thread(job['conversation'],owner)
            tid=await agent.backend.create_task_thread(job['conversation'],eid)
            details.update(adaptive=True,task_thread_id=tid,task_input=event,parts=parts,
                attachment_target=target,part_index=0)
            store.update(eid,'continuing',result=details)
            state='continuing'
        else:
            tid=await agent.backend.create_task_thread(job['conversation'],eid,details['task_thread_id'])
            event=details['task_input']
        while not agent.stop.is_set():
            index=details.get('part_index',0)
            if state=='continuing':
                if parts:
                    previous=details.get('generation',{})
                    mode=previous.get('context_mode','none')
                    if mode!='none':
                        event['context']=store.select_context(eid,mode=mode,limit=previous.get('context_limit'),
                            query=previous.get('context_query',''))
                    else:event.pop('context',None)
                    event['next_step']=previous.get('next_step','继续完成尚未完成的部分')
                event['reply_progress']=[p['text'] for p in parts if p['state']=='accepted']
                event['remaining_parts']=MAX_PARTS-index
                target=details.get('attachment_target')
                attachment_request=target is not None and target.get('message_type') not in ('text','unknown')
                event['_model_timeout']=agent.config.get('model_timeouts',{}).get(event['conversation_id'],
                    agent.config.get('model_timeout',180) if event.get('attachment') else agent.config.get('text_model_timeout',30))
                store.update(eid,'generating',result={'task_input':event})
                try:
                    before_progress=len(parts)
                    reply,generation=await generate(tid,event)
                    if generation.get('read_attachment'):
                        if not attachment_request or event.get('attachment') or event.get('attachment_error'):
                            reply='请用微信“引用”选中要处理的内容，并说明处理要求。'
                            generation['continue_reply']=False;generation['context_mode']='none'
                        else:
                            if len(parts)==before_progress:
                                await progress('我先读取你引用的内容，再根据实际内容分析。',
                                    {'key':str(generation.get('turn_id',tid))+':attachment','kind':'attachment_decision'})
                            try:
                                resource=await attachments.fetch(agent.mcp,eid,target,store.path/'resources',
                                    agent.config.get('resource_timeout',60))
                                event['attachment']=await asyncio.to_thread(media.prepare,resource,
                                    {'read_source':generation.get('read_source',False),
                                     'profile_dir':agent.config.get('browser_profile_dir',str(store.path/'browser-profile')),
                                     'channel':agent.config.get('browser_channel','msedge')})
                            except (ValueError,MCPError,OSError,KeyError) as error:
                                event['attachment_error']='附件获取或解析未完成：'+type(error).__name__
                            store.update(eid,'generating',result={'task_input':event})
                            event['_model_timeout']=agent.config.get('model_timeout',180)
                            reply,generation=await generate(tid,event)
                            if generation.get('read_attachment'):
                                reply='本轮引用的内容未能完成处理，请检查后重新引用。'
                                generation['continue_reply']=False;generation['context_mode']='none'
                except ModelUnavailable as error:
                    reply=error.reply
                    generation=error.details()
                except ProgressDeliveryError as error:
                    row=store.db.execute('SELECT state FROM jobs WHERE event_id=?',(eid,)).fetchone()
                    if row and row[0]=='unknown':return
                    if getattr(error,'interruption_confirmed',True) is False:
                        store.update(eid,'generation_unknown',error='进度已停止，但模型中止未确认，需核查原轮次');return
                    reply='本轮进度消息未能继续发送，执行已停止；已发出的内容保留。'
                    generation={'error':'progress_stopped','continue_reply':False,'context_mode':'none'}
                except TimeoutError as error:
                    stage=getattr(error,'timeout_stage','decision')
                    if details.get('progress_pending') is not None:
                        store.update(eid,'unknown',result=details,error='进度发送超时或结果未确认，禁止改写原段或跳段');return
                    reply=('工具执行等待超时' if stage=='tool' else '等待下一执行决定超时')+'，本轮已停止；已发出的内容保留，不会自动重跑。'
                    generation={'error':'model_timeout','turn_id':getattr(error,'turn_id',None),
                        'timeout_stage':stage,
                        'interruption_confirmed':getattr(error,'interruption_confirmed',False),
                        'continue_reply':False,'context_mode':'none','read_attachment':False}
                except ModelThreadBusy:
                    reply='上一步尚未确认停止，这一步暂未执行，请稍后重试。'
                    generation={'error':'previous_model_turn_active','continue_reply':False,'context_mode':'none'}
                index=details.get('part_index',0)
                if index==MAX_PARTS-1 and (generation.get('continue_reply') or generation.get('context_mode','none')!='none'):
                    reply=reply[:1100]+'\n本轮已达分段上限，后续步骤尚未完成。'
                    generation['continue_reply']=False;generation['context_mode']='none'
                more=bool(generation.get('continue_reply') or generation.get('context_mode','none')!='none')
                details.update(generation=generation,part_final=not more,task_input=event,part_index=index)
                store.update(eid,'generated',reply=reply,result=details,context=event.get('context'))
                state='generated'
            else:reply=job['reply']
            if agent.stop.is_set():return
            if state=='generated':
                await agent.verify()
                store.update(eid,'sending')
                try:
                    operation=await agent.mcp.call('reply_text_part',event_id=eid,part_index=index,
                        text=reply,final=details['part_final'])
                except Exception:
                    operation=await agent.mcp.call('get_reply_part_status',event_id=eid,part_index=index)
            else:
                operation=await agent.mcp.call('get_reply_part_status',event_id=eid,part_index=index)
            send_state=operation.get('state')
            if send_state in ('queued','running','submitting','prepared'):
                store.update(eid,'sending',result={'operation':operation});return
            if send_state!='accepted':
                store.update(eid,'unknown' if send_state=='unknown' else 'failed',
                    result={'operation':operation},error='该段发送未确认成功，禁止跳段或重发');return
            if not any(p['part_index']==index for p in parts):
                parts.append({'part_index':index,'text':reply,'state':'accepted','operation':operation,
                    'elapsed_seconds':round(time.time()-details.get('part_started_at',time.time()),3)})
            details.update(parts=parts,operation=operation)
            if details['part_final']:
                if agent.config.get('mark_read_on_process',True):
                    try:details['conversation_read']=await asyncio.wait_for(agent.mcp.call('get_conversation_read_status',event_id=eid),2)
                    except (MCPError,TimeoutError):pass
                store.update(eid,'accepted',result=details);return
            details['part_index']=index+1
            details['part_started_at']=time.time()
            store.update(eid,'continuing',result=details)
            state='continuing'
    except asyncio.CancelledError:
        store.update(eid,'generation_unknown',error='分段任务被中断，请核对原轮次');raise
    except Exception as error:
        row=store.db.execute('SELECT state FROM jobs WHERE event_id=?',(eid,)).fetchone()
        state=row[0] if row else ''
        store.update(eid,'unknown' if state=='sending' else 'generation_unknown' if state=='generating' else 'failed',
            error='分段任务状态待核查：'+type(error).__name__)
