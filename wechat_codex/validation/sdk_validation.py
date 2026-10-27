"""Real Codex SDK + simulated MCP: no native WeChat messages are sent."""
import asyncio
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tests'))
from fakes import CONFIG, FakeMCP, event
from local_agent.codex_backend import CodexBackend
from local_agent.runtime import identity, atomic_json
from local_agent.service import Agent
from local_agent.storage import Store


async def main():
    directory = ROOT / 'state' / ('sdk-validation-' + str(int(time.time())))
    store = Store(directory, identity(CONFIG))
    mcp = FakeMCP()
    evidence = []
    backend = CodexBackend(directory / 'workspace')
    async def run(e):
        store.ingest([e])
        job = next(j for j in store.ready() if j['event_id'] == e['event_id'])
        started = time.monotonic()
        await Agent(store, mcp, backend, CONFIG).process(job)
        row = dict(store.db.execute('SELECT state,reply,result,error FROM jobs WHERE event_id=?', (e['event_id'],)).fetchone())
        row['seconds'] = round(time.monotonic() - started, 3)
        row['event_id'] = e['event_id']
        evidence.append(row)
        atomic_json(directory / 'progress.json', evidence)
        print(json.dumps({'event_id': e['event_id'], 'state': row['state'], 'seconds': row['seconds'], 'reply': row['reply']}, ensure_ascii=False), flush=True)
        if row['state'] != 'accepted':
            raise RuntimeError('SDK模拟闭环未通过：' + str(row['error']))
    try:
        await backend.start()
        await run(event('sdk-a1', text='请记住本会话的暗号是紫砂42，只回答已记住。'))
        a = store.conversation('wxid_test_peer')['thread_id']
        await run(event('sdk-b1', 'wxid_test_other', '本会话之前告诉过你暗号吗？没有就只回复不知道。'))
        b = store.conversation('wxid_test_other')['thread_id']
        assert a != b and '紫砂42' not in evidence[-1]['reply']
        await backend.close()
        store.close()
        store = Store(directory, identity(CONFIG))
        backend = CodexBackend(directory / 'workspace')
        await backend.start()
        await run(event('sdk-a2', text='请只回复我之前告诉你的本会话暗号。'))
        assert store.conversation('wxid_test_peer')['thread_id'] == a
        assert '紫砂42' in evidence[-1]['reply']
        report = {'passed': True, 'scope': 'real_codex_model_simulated_mcp_no_wechat_sends',
                  'session_isolation': True, 'resume_after_runtime_restart': True,
                  'model_turns': 3, 'simulated_sends': len(mcp.sent), 'native_sends': 0,
                  'threads': {'a': a, 'b': b}, 'evidence': evidence}
        atomic_json(ROOT / 'state/sdk-validation-result.json', report)
        print(json.dumps({'passed': True, 'native_sends': 0, 'session_resume_verified': True}), flush=True)
    finally:
        await backend.close();store.close()


if __name__ == '__main__':
    asyncio.run(main())
