"""Real SDK timeout/interrupt/recovery check. No WeChat transport or sends."""
import asyncio
import json
from pathlib import Path
import sys
import time
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from local_agent.codex_backend import CodexBackend, ModelTimeout
from local_agent.runtime import atomic_json



async def main():
    root = ROOT / 'state/timeout-validation'
    root.mkdir(exist_ok=True)
    backend = CodexBackend(root / 'workspace', timeout=.2)
    await backend.start()
    report = {'scope':'real_codex_sdk_timeout_interrupt_and_same_thread_recovery','wechat_sends':0,'started_at':time.time()}
    try:
        thread = await backend.ensure_thread('timeout-self-validation')
        report['thread_id'] = thread
        event = {'conversation_id':'self_validation','payload':{'text':'请给出一篇详细的中文学习计划，分十个阶段说明，每阶段包含多个具体练习。'}}
        try:
            await backend.generate(thread, event)
            raise RuntimeError('模型未超时，未覆盖验收条件')
        except ModelTimeout as error:
            report.update(timeout_observed=True,turn_id=error.turn_id,interruption_confirmed=error.interruption_confirmed)
        backend.timeout = 60
        reply, generation = await backend.generate(thread, {'conversation_id':'self_validation','payload':{'text':'仅回复“超时后已恢复”，不要继续上一个任务。'}})
        report.update(recovered='超时后已恢复' in reply,recovery_turn=generation['turn_id'],completed_at=time.time())
        atomic_json(root / 'result.json', report)
        print(json.dumps(report,ensure_ascii=True))
        if not report['interruption_confirmed'] or not report['recovered']:
            raise RuntimeError('真实中止或恢复未通过')
    finally:
        await backend.close()


if __name__ == '__main__':
    asyncio.run(main())
