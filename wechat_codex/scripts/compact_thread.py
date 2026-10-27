"""Operator maintenance: compact one idle, authorized existing thread; no WeChat send."""
import argparse
import asyncio
import json
from pathlib import Path
import sys
import sqlite3
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from local_agent.codex_backend import CodexBackend
from local_agent.runtime import StateLock,read_config,atomic_json

async def compact(directory,conversation):
    config=read_config(directory)
    if conversation not in config['conversations']:raise ValueError('会话不在白名单')
    lock=StateLock(directory)
    backend=CodexBackend(directory/'workspace',config.get('model'))
    report={'conversation':conversation,'started_at':time.time(),'source':'sdk_thread_compact',
        'state':'unknown','model_tasks_replayed':False,'wechat_send':False}
    output=directory/'compaction-validation.json'
    try:
        connection=sqlite3.connect('file:'+str(directory/'agent.sqlite3')+'?mode=ro',uri=True)
        try:
            tid=connection.execute('SELECT thread_id FROM conversations WHERE id=?',(conversation,)).fetchone()[0]
            if connection.execute("SELECT 1 FROM jobs WHERE conversation=? AND state IN ('generating','sending')",
                    (conversation,)).fetchone():raise ValueError('会话仍有执行中的任务')
        finally:connection.close()
        report['thread_id']=tid
        await backend.start();await backend.ensure_thread(conversation,tid)
        thread=backend.threads[tid]
        before=await thread.read()
        if before.thread.status.model_dump(mode='json').get('type')!='idle':raise ValueError('thread不是空闲')
        atomic_json(output,report)
        # Compaction's turn notifications are scoped by the SDK router, not
        # delivered on next_notification(). Buffer before submitting the RPC.
        from openai_codex.api import AsyncTurnHandle
        router=backend.codex._client._sync._router
        with router.pending_turn(tid) as cursors:
            await thread.compact()
            deadline=time.monotonic()+5
            compact_id=None
            while time.monotonic()<deadline:
                with router._lock:
                    ids=[key for key,value in router._turn_states.items()
                         if value.thread_id==tid and key not in cursors]
                if len(ids)==1:
                    compact_id=ids[0];break
                if len(ids)>1:raise ValueError('压缩轮次身份不唯一')
                await asyncio.sleep(.05)
            if not compact_id:raise TimeoutError('压缩轮次身份尚未确认')
            subscription=router.prepare_turn(compact_id,tid,cursors,for_handle=True)
        handle=AsyncTurnHandle(backend.codex,tid,compact_id,_subscription=subscription)
        report['state']='started';atomic_json(output,report)
        result=await asyncio.wait_for(handle.run(),150)
        report['turn_id']=result.id
        report['turn_status']=getattr(result.status,'value',result.status)
        item_completed=any(item.model_dump(mode='json').get('type')=='contextCompaction' for item in result.items)
        if not item_completed or report['turn_status']!='completed':raise ValueError('压缩未完成')
        report['state']='completed'
        report['context_compaction_completed']=item_completed
        report['idle_after']=(await thread.read()).thread.status.model_dump(mode='json').get('type')=='idle'
    except BaseException as error:
        report['error']=type(error).__name__
        # No new model request, compaction retry, reset, or WeChat reply here.
        raise
    finally:
        report['ended_at']=time.time();atomic_json(output,report)
        await backend.close();lock.close()
        print(json.dumps(report,ensure_ascii=True),flush=True)

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state-dir',type=Path,default=ROOT/'state/live')
    parser.add_argument('--conversation',required=True)
    args=parser.parse_args();asyncio.run(compact(args.state_dir.resolve(),args.conversation))
