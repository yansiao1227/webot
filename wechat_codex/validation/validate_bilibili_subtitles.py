"""Real profile/captions/model validation of one explicit URL; never sends WeChat."""
import argparse
import asyncio
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from local_agent.codex_backend import CodexBackend
from local_agent.runtime import atomic_json, read_config
from local_agent.web_content import browser


async def main(url):
    root = ROOT
    config = read_config(root / 'state/live')
    directory = root / 'state' / ('bilibili-validation-' + str(int(time.time())))
    directory.mkdir(parents=True)
    started = time.monotonic()
    source = await asyncio.to_thread(browser, url, {
        'profile_dir': config.get('browser_profile_dir', str(root / 'state/live/browser-profile')),
        'channel': config.get('browser_channel', 'msedge')})
    captions = source.get('video_content', {})
    report = {'scope': 'explicit_bilibili_url_real_profile_subtitles_sdk_no_wechat_sends',
              'native_sends': 0, 'source_url': url, 'retrieval_seconds': round(time.monotonic() - started, 3),
              'captions': {key: value for key, value in captions.items() if key != 'text'}}
    atomic_json(directory / 'result.json', report)
    print(json.dumps(report, ensure_ascii=False), flush=True)
    if captions.get('state') != 'read':
        raise ValueError('没有取得当前视频字幕')
    backend = CodexBackend(directory / 'workspace', timeout=60)
    try:
        await backend.start()
        thread = await backend.create_task_thread('validation', 'bilibili-subtitles')
        reply, generation = await backend.generate(thread, {'conversation_id': 'validation',
            'payload': {'text': '概括这条B站视频，必须根据实际取得的字幕，列出主要内容及几个能定位的时间点。不要只复述标题。直接分析已提供的字幕，不再调用工具。'},
            'attachment': {'message_type': 'card', 'prepared': {'source_urls': [url],
                'card_metadata': {'title': source.get('title')}, 'source_contents': [{**source, 'state': 'read'}]}}})
        report.update(reply=reply, generation=generation,
                      passed=generation.get('tool_calls') == 0 and not generation.get('read_attachment'))
        atomic_json(directory / 'result.json', report)
        print(json.dumps({'report': str(directory / 'result.json'), 'reply': reply, 'generation': generation}, ensure_ascii=False), flush=True)
    finally:
        await backend.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('url')
    try:
        asyncio.run(main(parser.parse_args().url))
    except Exception as error:
        # Playwright exceptions can contain authenticated request headers.
        print(json.dumps({'error': type(error).__name__, 'message': '验证未完成，凭据与底层请求日志不输出'}, ensure_ascii=False))
        raise SystemExit(1)
