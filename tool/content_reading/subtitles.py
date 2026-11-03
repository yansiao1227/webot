"""通用时间段字幕校验及有界文本提取。"""
import math

MAX_CAPTION_TEXT = 24000


def parse_captions(body, duration):
    if not isinstance(body, list) or not body or len(body) > 10000:
        raise ValueError('字幕结构为空或超限')
    lines = []
    chars = 0
    start = None
    end = 0
    truncated = False
    for item in body:
        begin, finish, text = item.get('from'), item.get('to'), item.get('content')
        if (any(isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x)
                for x in (begin, finish)) or not 0 <= begin <= finish <= duration + 2 or
                not isinstance(text, str)):
            raise ValueError('字幕时间与当前视频不匹配或结构无效')
        line = f'[{begin:.2f}-{finish:.2f}秒] {text.strip()}'
        if chars + len(line) + 1 > MAX_CAPTION_TEXT:
            truncated = True
            continue
        if not truncated:
            lines.append(line)
            chars += len(line) + 1
            start = begin if start is None else min(start, begin)
            end = max(end, finish)
    if not lines:
        raise ValueError('字幕没有可读取文本')
    return {'text': '\n'.join(lines), 'segment_count': len(lines), 'available_segment_count': len(body),
            'time_range_seconds': [start, end], 'truncated': truncated}
