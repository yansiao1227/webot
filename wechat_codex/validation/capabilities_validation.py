"""真实运行时冒烟测试，不使用微信传输，也不发送消息。"""
import asyncio
import argparse
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from local_agent.codex_backend import CodexBackend
from local_agent.runtime import atomic_json, read_config
from tool.settings import path_setting


async def main(args):
    directory = ROOT / 'state' / ('capabilities-validation-' + str(int(time.time())))
    config = read_config(args.state_dir)
    backend = CodexBackend(directory / 'workspace', config.get('model'),
                           codex_bin=config.get('codex_bin'))
    progress = []
    async def on_progress(text, metadata):
        progress.append({'text': text, **metadata})
    try:
        await backend.start()
        thread = await backend.create_task_thread('validation', 'validation')
        reply, info = await backend.generate_with_progress(thread, {
            'conversation_id': 'validation',
            'payload': {'text': args.request + '请实际联网搜索并给出来源链接。只搜索和读取公开网页，不读写本地文件，不运行命令，不操作微信或其他应用。'},
            **{'_' + key: config.get(key, default) for key, default in (
                ('decision_timeout', 30), ('tool_timeout', 60), ('startup_timeout', 60),
                ('generation_idle_timeout', 30), ('turn_timeout', 600))},
        }, on_progress)
        # 证据包含运行时条目类型，不能仅依赖模型自身声明。
        report = {'passed': info['search_calls'] > 0,
                  'native_sends': 0, 'reply': reply, 'generation': info, 'progress': progress}
        atomic_json(directory / 'result.json', report)
        print(json.dumps(report, ensure_ascii=False), flush=True)
        if not report['passed']:
            raise RuntimeError('No search/tool evidence in runtime results')
    finally:
        await backend.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state-dir', type=Path,
                        default=path_setting('RE_WECHAT_CODEX_STATE_DIR', ROOT / 'state/live'))
    parser.add_argument('--request', default='查找 OpenAI 官方的 Codex web_search 配置说明。')
    asyncio.run(main(parser.parse_args()))
