"""Real runtime smoke test; uses no WeChat transport and sends no messages."""
import asyncio
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from local_agent.codex_backend import CodexBackend
from local_agent.runtime import atomic_json


async def main():
    directory = ROOT / 'state' / ('capabilities-validation-' + str(int(time.time())))
    backend = CodexBackend(directory / 'workspace', timeout=300)
    try:
        await backend.start()
        thread = await backend.ensure_thread('validation')
        reply, info = await backend.generate(thread, {
            'conversation_id': 'validation',
            'payload': {'text': '请实际调用联网搜索，查找 OpenAI 官方的 Codex web_search 配置说明，并给出官方来源链接。只搜索，不读写本地文件，不运行命令，不操作其他应用。'},
        })
        # Evidence includes runtime item types, not just the model's claim.
        report = {'passed': 'webSearch' in info['tool_types'], 'native_sends': 0,
                  'reply': reply, 'generation': info}
        atomic_json(directory / 'result.json', report)
        print(json.dumps(report, ensure_ascii=False), flush=True)
        if not report['passed']:
            raise RuntimeError('No search/tool evidence in runtime results')
    finally:
        await backend.close()


if __name__ == '__main__':
    asyncio.run(main())
