"""供代理和命令行调用方使用的具名工具入口，兼容 JSON。"""
from pathlib import Path
import os
from tool.settings import path_setting

from . import media, web


TOOLS = {
    'read-image-content': '下载HTTP(S)图片到本机并输出可供视觉工具查看的图片路径。',
    'inspect-media': '读取本机图片、表情包、音频或视频，输出采样画面、转写和范围说明。',
    'transcribe-audio': '转换本机音频并进行本地CPU转写，含时间戳，最多180秒。',
    'read-document': '读取本机文本、PDF、DOCX、XLSX；扫描PDF输出采样页面。',
    'read-web-content': '读取HTTP(S)页面，默认支持本机、内网和公网；可选浏览器profile。',
    'read-video-content': '通过浏览器读取B站当前视频的播放器字幕；需指定profile目录。',
}


def invoke(tool, *, path=None, url=None, kind=None, name=None, output_dir=None,
           model_dir=None, browser=False, profile_dir=None, channel=None, public_only=False):
    """返回 read/unavailable 状态的证据，不调用模型或发送消息。

    此 API 不限制本地访问；public_only 是网页读取时可选的调用方策略。
    消息授权由调用它的适配层负责。
    """
    channel = channel or os.environ.get('CONTENT_BROWSER_CHANNEL') or 'msedge'
    if not profile_dir and os.environ.get('CONTENT_BROWSER_PROFILE_DIR'):
        profile_dir = str(path_setting('CONTENT_BROWSER_PROFILE_DIR', ''))
    source = {'path': str(path)} if path is not None else {'url': url}
    try:
        if tool not in TOOLS:
            raise ValueError('未知工具：' + str(tool))
        if tool in ('inspect-media', 'transcribe-audio', 'read-document'):
            if path is None:
                raise ValueError('需要本机文件path')
            source = {'path': str(Path(path).resolve())}
            if tool == 'read-document':
                kind = 'document'
            elif tool == 'transcribe-audio':
                kind = 'audio'
            elif kind not in ('image', 'sticker', 'audio', 'voice', 'video'):
                raise ValueError('媒体kind须为image/sticker/audio/voice/video')
            result = media.prepare_content(path, kind, name=name, output_dir=output_dir, model_dir=model_dir)
            scope = '本机文件提取结果；图片需由调用者的视觉模型理解，采样和转写限制以result为准'
        elif tool == 'read-image-content':
            if not url:
                raise ValueError('需要HTTP(S)来源url')
            result = web.fetch(url, public_only=public_only, output_dir=output_dir, image_only=True)
            scope = result['content_scope']
        else:
            if not url:
                raise ValueError('需要HTTP(S)来源url')
            use_browser = browser or tool == 'read-video-content'
            if use_browser and not profile_dir:
                raise ValueError('浏览器读取需要显式profile_dir')
            values = web.read_sources([url], {'browser': use_browser, 'profile_dir': profile_dir,
                'channel': channel, 'public_only': public_only})
            result = values[0]
            if result['state'] != 'read':
                return {'tool': tool, 'source': source, **result}
            if tool == 'read-video-content':
                video = result.get('video_content', {})
                if video.get('state') != 'read':
                    return {'tool': tool, 'source': source, 'state': 'unavailable', 'result': result,
                            'reason': video.get('reason', '没有取得当前视频字幕；不代表已读取音画')}
            scope = result['content_scope']
        return {'tool': tool, 'state': 'read', 'source': source, 'content_scope': scope, 'result': result}
    except Exception as error:
        return {'tool': tool, 'state': 'unavailable', 'source': source,
                'error': type(error).__name__,
                'reason': str(error) if type(error) is ValueError else '内容读取失败，请检查文件、依赖或运行环境'}
