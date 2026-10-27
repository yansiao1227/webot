"""通过真实 SDK 处理合成视频和公开页面，不连接微信。"""
import asyncio
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from local_agent import media, web_content
from local_agent.codex_backend import CodexBackend
from local_agent.runtime import atomic_json


async def main():
    from PIL import Image, ImageDraw, ImageFont
    directory = ROOT / 'state' / ('content-validation-' + str(int(time.time())))
    directory.mkdir(parents=True)
    image = Image.new('RGB', (960, 540), '#cc2222')
    font = ImageFont.truetype('C:/Windows/Fonts/arial.ttf', 100)
    ImageDraw.Draw(image).text((240, 200), '48271', font=font, fill='white')
    image.save(directory / 'source.png')
    clip = directory / 'sample.mp4'
    media._run([media._ffmpeg(), '-nostdin', '-y', '-v', 'error', '-loop', '1', '-i',
                str(directory / 'source.png'), '-t', '3', '-c:v', 'libx264', '-pix_fmt', 'yuv420p', str(clip)])
    video = media.prepare({'path': str(clip), 'message_type': 'video', 'name': 'sample.mp4'})
    source = await asyncio.to_thread(web_content.browser, 'https://example.com/',
                                    {'profile_dir': str(directory / 'profile'), 'channel': 'msedge'})
    card = {'message_type': 'card', 'prepared': {'source_urls': ['https://example.com/'],
            'card_metadata': {'title': 'Example Domain'}, 'source_contents': [{**source, 'state': 'read'}]}}
    backend = CodexBackend(directory / 'workspace', timeout=60)
    report = {'scope': 'synthetic_video_public_page_real_sdk_no_wechat', 'native_sends': 0, 'results': []}
    try:
        await backend.start()
        for name, attachment, text in (
                ('video', video, '直接根据已提供视频抽帧回答：背景是什么颜色，画面中的五位数字是什么？不调用工具。'),
                ('card', card, '根据已提供页面正文，说明该链接页面用途，并给出来源链接。不调用工具，不重新打开网页。')):
            thread = await backend.create_task_thread('validation', name)
            started = time.monotonic()
            reply, info = await backend.generate(thread, {'conversation_id': 'validation',
                'payload': {'text': text}, 'attachment': attachment})
            result = {'kind': name, 'reply': reply, 'generation': info,
                      'seconds': round(time.monotonic() - started, 3)}
            report['results'].append(result)
            atomic_json(directory / 'result.json', report)
            print(json.dumps(result, ensure_ascii=False), flush=True)
        report['passed'] = ('48271' in report['results'][0]['reply'] and
                            '红' in report['results'][0]['reply'] and
                            'example.com' in report['results'][1]['reply'] and
                            all(item['generation']['tool_calls'] == 0 for item in report['results']))
        atomic_json(directory / 'result.json', report)
        if not report['passed']:
            raise RuntimeError('内容结果需要人工核对')
    finally:
        await backend.close()


if __name__ == '__main__':
    asyncio.run(main())
