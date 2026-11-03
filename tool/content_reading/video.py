"""平台无关的视频资源处理：字幕优先，缺失时下载、抽帧和转写。"""
import html
import math
from pathlib import Path
import re
import tempfile

from tool.settings import cache_directory
from .download import download
from . import media
from .subtitles import parse_captions

MAX_VIDEO_BYTES = 64 * 1024 * 1024


def read_link(url, *, subtitle_url=None, output_dir=None, model_dir=None):
    """处理MP4媒体地址或HTTP重定向；可先读取调用方提供的SRT字幕。"""
    from .urls import public_url
    if not public_url(url):
        raise ValueError('视频来源必须为公网HTTP(S)链接')
    subtitles = [{'url': subtitle_url}] if subtitle_url else []
    return prepare([{'url': url}], subtitles=subtitles, output_dir=output_dir, model_dir=model_dir)


def parse_subtitles(text, duration):
    pattern = r'(?m)^\s*(\d{1,2}:\d{2}:\d{2}[,.]\d{3})\s*-->\s*(\d{1,2}:\d{2}:\d{2}[,.]\d{3})[^\n]*\n'
    matches = list(re.finditer(pattern, text))
    body = []
    def seconds(value):
        h, m, s = value.replace(',', '.').split(':')
        if int(m) >= 60 or float(s) >= 60:
            raise ValueError('字幕时间格式无效')
        return int(h) * 3600 + int(m) * 60 + float(s)
    for i, match in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        value = text[match.end():end].strip()
        # 下一段的编号不是台词；仅接受实际SRT时间段，不把HTML错误页当字幕。
        value = re.sub(r'\n\s*\n\s*\d+\s*$', '', value)
        value = html.unescape(re.sub(r'<[^>]+>', '', value)).strip()
        if value:
            body.append({'from': seconds(match[1]), 'to': seconds(match[2]), 'content': value})
    return parse_captions(body, duration)


def prepare(resources, *, subtitles=(), duration=None, output_dir=None, model_dir=None):
    subtitle_attempt = {'state': 'unavailable', 'reason': '未提供字幕地址'}
    try:
        if duration is not None and (isinstance(duration, bool) or not isinstance(duration, (int, float)) or
                not math.isfinite(duration) or not 0 < duration <= 600):
            raise ValueError('视频时长无效或超过10分钟处理范围')
        if output_dir:
            parent = Path(output_dir)
            parent.mkdir(parents=True, exist_ok=True)
            output = Path(tempfile.mkdtemp(prefix='video-', dir=parent))
        else:
            output = cache_directory('video-')
        candidates = sorted(subtitles, key=lambda item: 0 if item.get('language', '').lower().startswith(('zh', 'cn')) else 1)
        for item in candidates[:2]:
            lang = item.get('language', '')
            try:
                path = output / 'subtitles.srt'
                download(item['url'], path, max_bytes=2 * 1024 * 1024, timeout=15)
                parsed = parse_subtitles(path.read_text(encoding='utf-8-sig'), duration or 600)
                return {'state': 'read', 'method': 'video_subtitles', 'language': lang,
                    'duration_seconds': duration,
                    'content_scope': '站点字幕；未读取画面或原始音轨，字幕可能存在识别误差', **parsed}
            except Exception:
                subtitle_attempt = {'state': 'unavailable', 'reason': '站点字幕下载或解析失败'}
        candidates = [r for r in resources if r.get('url') and
                      (r.get('size') is None or 0 < r['size'] <= MAX_VIDEO_BYTES)]
        if not candidates:
            raise ValueError('没有可下载的视频资源，或资源超过64MiB')
        selected = min(candidates, key=lambda r: r.get('size') or MAX_VIDEO_BYTES)
        path = output / 'video.mp4'
        downloaded = download(selected['url'], path, max_bytes=MAX_VIDEO_BYTES)
        with path.open('rb') as stream:
            header = stream.read(12)
        if header[4:8] != b'ftyp':
            raise ValueError('下载结果不是MP4媒体，拒绝将网页或播放列表交给解码器')
        prepared = media.prepare_content(path, 'video', output_dir=output, model_dir=model_dir)
        if 'transcript' in prepared:
            prepared['transcription_state'] = 'read' if prepared['transcript'].strip() else 'empty'
            if prepared['transcription_state'] == 'empty':
                prepared['transcription_reason'] = '转写已执行，但未识别出可用语音文本'
        else:
            prepared['transcription_state'] = 'unavailable'
            prepared['transcription_reason'] = prepared.get('audio_error', '视频没有可转写音轨')
        return {'state': 'read', 'method': 'local_video',
            'subtitle_attempt': subtitle_attempt, 'download_bytes': downloaded['bytes'],
            'content_scope': '视频均匀抽样至多8帧，音轨转写至多180秒；不能声称逐帧看完整片', **prepared}
    except Exception as error:
        return {'state': 'unavailable', 'method': 'video',
            'subtitle_attempt': subtitle_attempt, 'error': type(error).__name__,
            'reason': str(error) if type(error) is ValueError else '视频下载或预处理失败，请检查站点资源与本机媒体依赖'}
