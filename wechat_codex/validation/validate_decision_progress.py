"""Real SDK streamed decisions + simulated MCP sends; never sends to WeChat."""
import asyncio
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0,str(ROOT/'tests'))
from fakes import CONFIG, FakeMCP, event
from local_agent.codex_backend import CodexBackend
from local_agent.runtime import atomic_json, identity
from local_agent.service import Agent
from local_agent.storage import Store


async def main():
    directory=ROOT/'state'/('decision-progress-validation-'+str(int(time.time())))
    config={**CONFIG,'adaptive_context':True,'decision_timeout':30,'text_model_timeout':30,'tool_timeout':180}
    store=Store(directory,identity(config))
    started=time.monotonic();observations=[]
    class ObservedMCP(FakeMCP):
        async def call(self,name,**args):
            result=await super().call(name,**args)
            if name=='reply_text_part':
                observation={'seconds':round(time.monotonic()-started,3),'part_index':args['part_index'],
                             'final':args['final'],'text':args['text']}
                observations.append(observation)
                print(json.dumps(observation,ensure_ascii=False),flush=True)
                atomic_json(directory/'progress.json',observations)
            return result
    mcp=ObservedMCP();backend=CodexBackend(directory/'workspace',timeout=180)
    try:
        await backend.start()
        store.ingest([event('decision-test',text='有没有Chrome插件可以让网页认为自己在微信浏览器中？我要对微信浏览器做适配。请实际联网核对，给出简洁建议和来源。')])
        await Agent(store,mcp,backend,config).process(store.ready()[0])
        row=store.db.execute("SELECT state,result,error FROM jobs WHERE event_id='decision-test'").fetchone()
        details=json.loads(row['result'] or '{}')
        report={'native_sends':0,'scope':'real_sdk_streamed_decisions_simulated_mcp',
                'decision_timeout':30,'tool_timeout':180,'state':row['state'],'error':row['error'],
                'parts':observations,'generation':details.get('generation'),
                'passed':row['state']=='accepted' and len(observations)>=2 and
                         not observations[0]['final'] and observations[-1]['final'] and
                         not (details.get('generation') or {}).get('error') and
                         (details.get('generation') or {}).get('first_decision_seconds',31)<=30}
        atomic_json(directory/'result.json',report)
        print(json.dumps({'passed':report['passed'],'report':str(directory/'result.json')},ensure_ascii=False),flush=True)
        if not report['passed']:raise RuntimeError('流式进度验证未通过')
    finally:
        await backend.close();store.close()


if __name__=='__main__':
    try:asyncio.run(main())
    except Exception as error:
        print(json.dumps({'error':type(error).__name__,'message':'验证未完成，底层请求资料不输出'},ensure_ascii=False))
        raise SystemExit(1)
