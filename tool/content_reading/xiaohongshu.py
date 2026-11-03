"""将小红书视频详情归一化后交给通用视频工具。"""
import math
from . import video as video_tool

parse_subtitles = video_tool.parse_subtitles


def prepare(video, output_dir=None, model_dir=None):
    duration = video.get('capa', {}).get('duration')
    if (isinstance(duration, bool) or not isinstance(duration, (int, float)) or
            not math.isfinite(duration) or not 0 < duration <= 600):
        return {'state': 'unavailable', 'method': 'xiaohongshu_video',
                'reason': '视频时长未知或超过10分钟处理范围'}
    resources = [{'url': stream['masterUrl'], 'size': stream['size']}
        for streams in video.get('media', {}).get('stream', {}).values()
        for stream in streams if isinstance(stream, dict) and stream.get('format') == 'mp4'
        and stream.get('masterUrl') and stream.get('drmType', 0) == 0
        and isinstance(stream.get('size'), (int, float))]
    subtitles = [{**item, 'language': lang}
        for lang, items in (video.get('subtitles') or {}).items() if isinstance(items, list)
        for item in items if isinstance(item, dict) and item.get('url')]
    result = None
    subtitle_attempt = {'state': 'unavailable', 'reason': '平台没有提供字幕地址'}
    if subtitles:
        result = video_tool.prepare([], subtitles=subtitles, duration=duration,
                                    output_dir=output_dir, model_dir=model_dir)
        subtitle_attempt = result.get('subtitle_attempt', subtitle_attempt)
    if result is None or result.get('state') != 'read':
        candidates = [r for r in resources if 0 < r['size'] <= video_tool.MAX_VIDEO_BYTES]
        if not candidates:
            return {'state': 'unavailable', 'method': 'xiaohongshu_video',
                    'subtitle_attempt': subtitle_attempt, 'reason': '平台没有可下载的MP4资源，或资源超过64MiB'}
        selected = min(candidates, key=lambda r: r['size'])
        result = video_tool.read_link(selected['url'], output_dir=output_dir, model_dir=model_dir)
        result['tool'] = 'read-video-link'
        result['subtitle_attempt'] = subtitle_attempt
    # 保留已建立的微信来源封套和调用记录。
    result['method'] = {'video_subtitles': 'xiaohongshu_subtitles',
        'local_video': 'xiaohongshu_local_video'}.get(result['method'], 'xiaohongshu_video')
    return result
