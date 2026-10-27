"""从当前已认证播放器读取字幕，不使用搜索摘要。"""
import json
import math
import re
import time
from urllib.parse import parse_qs, urlsplit

MAX_SUBTITLE_BYTES = 2 * 1024 * 1024
MAX_CAPTION_TEXT = 24000


def video_url(url):
    parsed = urlsplit(url)
    return (parsed.hostname in ('www.bilibili.com', 'm.bilibili.com', 'bilibili.com') and
            re.match(r'^/video/BV[0-9A-Za-z]+/?$', parsed.path) is not None)


def player_response(url, identity):
    parsed = urlsplit(url)
    query = parse_qs(parsed.query)
    return (parsed.hostname == 'api.bilibili.com' and parsed.path == '/x/player/wbi/v2' and
            query.get('cid') == [str(identity['cid'])] and
            (query.get('aid') == [str(identity['aid'])] or query.get('bvid') == [identity['bvid']]))


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


def read_subtitles(context, page, responses):
    identity = page.evaluate('''() => {
        const state = window.__INITIAL_STATE__ || {}, v = state.videoData || {};
        const cid = state.cid || v.cid;
        const part = (v.pages || []).find(p => String(p.cid) === String(cid));
        return {bvid:v.bvid, aid:v.aid, cid, duration:part?.duration || v.duration};
    }''')
    bvid = re.search(r'/video/(BV[0-9A-Za-z]+)', page.url)
    duration = identity.get('duration')
    if (not bvid or identity.get('bvid') != bvid[1] or not identity.get('aid') or not identity.get('cid') or
            isinstance(duration, bool) or not isinstance(duration, (int, float)) or not 0 < duration <= 600):
        raise ValueError('无法验证B站视频身份，或视频超过10分钟处理范围')
    deadline = time.monotonic() + 12
    matched = []
    while time.monotonic() < deadline:
        matched = [r for r in responses if player_response(r.url, identity)]
        if matched:
            break
        page.wait_for_timeout(250)
    if not matched:
        raise ValueError('没有取得与当前视频匹配的播放器字幕响应')
    response = matched[-1]
    raw = response.body()
    if response.status != 200 or len(raw) > MAX_SUBTITLE_BYTES:
        raise ValueError('播放器字幕响应不可用或超限')
    payload = json.loads(raw)
    if payload.get('code') != 0:
        raise ValueError('播放器未授予字幕访问')
    subtitles = payload.get('data', {}).get('subtitle', {}).get('subtitles', [])
    subtitles = [s for s in subtitles if s.get('subtitle_url') and s.get('lan') != 'danmaku']
    if not subtitles:
        raise ValueError('当前视频没有可读取字幕；尚未取得音视频内容')
    priority = {'zh-CN': 0, 'zh-Hans': 0, 'zh': 0, 'ai-zh': 1, 'en': 2, 'ai-en': 3}
    selected = min(subtitles, key=lambda s: priority.get(s.get('lan'), 4))
    url = selected['subtitle_url']
    url = 'https:' + url if url.startswith('//') else url
    # web_content 已为此浏览器上下文安装访问门禁和 DNS 检查。
    from .web import resolve_public
    resolve_public(url)
    tab = context.new_page()
    try:
        response = tab.goto(url, wait_until='domcontentloaded', timeout=20000)
        if response is None or response.status != 200:
            raise ValueError('字幕来源返回错误')
        raw = response.body()
        if len(raw) > MAX_SUBTITLE_BYTES:
            raise ValueError('字幕超过2MiB读取上限')
        parsed = parse_captions(json.loads(raw).get('body'), duration)
    finally:
        tab.close()
    return {'state': 'read', 'method': 'bilibili_player_subtitles',
            'bvid': identity['bvid'], 'cid': str(identity['cid']), 'duration_seconds': duration,
            'language': selected.get('lan'), 'automatic': selected.get('lan', '').startswith('ai-'),
            'content_scope': '当前视频字幕；自动字幕可能误识别，仅据此分析台词，未读取画面或原始音轨',
            **parsed}


def read_discussion(page):
    """有界读取渲染后的评论或弹幕，包括哔哩哔哩的影子 DOM。

    结果属于观众讨论证据，不能当作字幕或视频证据。
    仅读取明确的讨论容器，继续排除推荐内容。
    """
    page.evaluate('''() => {
        document.querySelector('#comment, .comment-container, bili-comments')?.scrollIntoView();
    }''')
    page.wait_for_timeout(1500)
    data = page.evaluate(r'''() => {
        const selectors = {
            comments: '#comment, .comment-container, .reply-list, bili-comments',
            danmaku: '.bpx-player-dm-wrap, .bpx-player-dm-container, .bilibili-player-video-danmaku'
        };
        const collect = node => {
            if (node.nodeType === Node.TEXT_NODE) return node.textContent || '';
            if (node.nodeType !== Node.ELEMENT_NODE && node.nodeType !== Node.DOCUMENT_FRAGMENT_NODE) return '';
            if (node.nodeType === Node.ELEMENT_NODE &&
                (node.matches('script, style, template, noscript') || getComputedStyle(node).display === 'none')) return '';
            return Array.from((node.shadowRoot || node).childNodes).map(collect).join('\n');
        };
        const result = {};
        for (const [kind, selector] of Object.entries(selectors)) {
            const roots = Array.from(document.querySelectorAll(selector));
            const text = [...new Set(roots.filter(root => !roots.some(other => other !== root && other.contains(root)))
                .map(collect).map(value => value.trim()).filter(Boolean))].join('\n');
            result[kind] = {text: text.slice(0, 12000), truncated: text.length > 12000};
        }
        return result;
    }''')
    sections = {}
    for kind in ('comments', 'danmaku'):
        item = data.get(kind, {})
        text = item.get('text', '').strip()
        sections[kind] = {'state': 'read' if text else 'unavailable',
                          'text': text[:12000],
                          'truncated': bool(item.get('truncated')) or len(text) > 12000}
    return {'state': 'read' if any(item['state'] == 'read' for item in sections.values()) else 'unavailable',
            'method': 'rendered_audience_discussion', 'url': page.url, **sections,
            'content_scope': '当前页面已加载的评论/弹幕文字样本，可能含界面提示；不是字幕或视频正文，不代表全部观众观点'}
