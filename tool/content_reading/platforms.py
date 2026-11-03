"""平台来源的只读 MCP 适配；第三方依赖运行在独立 Python 环境。"""
import json
import subprocess
from urllib.parse import parse_qs, urlsplit

from tool.settings import ROOT, path_setting
from .urls import public_url


def platform(url):
    if not public_url(url):
        return None
    host = urlsplit(url).hostname.lower().rstrip('.')
    if host == 'b23.tv' or host == 'bilibili.com' or host.endswith('.bilibili.com'):
        return 'bilibili'
    if host == 'xhslink.com' or host.endswith('.xhslink.com') or host == 'xiaohongshu.com' or host.endswith('.xiaohongshu.com'):
        return 'xiaohongshu'
    return None


def target(url, expected):
    """短链解析后仍校验平台；不从标题猜测 ID。"""
    import re
    if platform(url) != expected:
        raise ValueError('平台来源不匹配')
    parsed = urlsplit(url)
    if expected == 'bilibili':
        match = re.fullmatch(r'/video/(BV[0-9A-Za-z]{10})/?', parsed.path)
        if not match:
            raise ValueError('当前仅支持含BV号的B站视频链接')
        page = parse_qs(parsed.query).get('p', ['1'])[0]
        if page != '1':
            raise ValueError('平台工具仅读取第一分P，当前链接需使用网页读取')
        return {'bvid': match[1]}
    match = re.fullmatch(r'/(?:explore|discovery/item)/([0-9a-fA-F]{24})/?', parsed.path)
    token = parse_qs(parsed.query).get('xsec_token', [''])[0]
    if not match or not token:
        raise ValueError('小红书来源缺少笔记ID或xsec_token，需完整分享链接')
    return {'feed_id': match[1], 'xsec_token': token, 'load_all_comments': False}


def read(url, output_dir=None, model_dir=None):
    provider = platform(url)
    if not provider:
        raise ValueError('不是支持的平台来源')
    python = path_setting('CONTENT_PLATFORM_MCP_PYTHON', 'tool/state/platform-mcp/venv/Scripts/python.exe')
    try:
        process = subprocess.run([str(python), '-B', '-X', 'utf8', '-m',
            'tool.content_reading.platform_worker'], input=json.dumps({'url': url}),
            cwd=ROOT, capture_output=True, text=True, encoding='utf-8', timeout=50,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        if process.returncode or len(process.stdout) > 200000:
            raise ValueError('平台MCP执行失败或响应超限')
        result = json.loads(process.stdout)
        if not isinstance(result, dict) or result.get('state') not in ('read', 'unavailable'):
            raise ValueError('平台MCP返回无效结果')
        video = result.pop('video_resources', None)
        if result.get('method') == 'xiaohongshu-mcp' and video:
            from . import xiaohongshu
            result['video_content'] = xiaohongshu.prepare(video, output_dir=output_dir, model_dir=model_dir)
        return result
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
        raise ValueError('平台MCP未安装、不可用或读取超时') from None


def resolve_bilibili(url):
    """只跟随平台跳转；取得可用BV号后不再请求视频网页。"""
    from . import web

    def video_target(value):
        if platform(value) != 'bilibili':
            raise ValueError('B站短链接跳转到其他平台')
        if urlsplit(value).hostname.lower().rstrip('.') == 'b23.tv':
            return False
        # 非首分P、无效BV号等限制仍由target检查，不为解析地址读完整网页。
        target(value, 'bilibili')
        return True

    result = web.fetch(url, 10, public_only=True, stop_at=video_target)
    final = result['final_url']
    target(final, 'bilibili')
    return final


def resolve_xiaohongshu(url):
    """分享跳转给出笔记ID及签名后，不再请求详情网页。"""
    from . import web
    def note_target(value):
        if platform(value) != 'xiaohongshu':
            raise ValueError('小红书短链接跳转到其他平台')
        host = urlsplit(value).hostname.lower().rstrip('.')
        if host == 'xhslink.com' or host.endswith('.xhslink.com'):
            return False
        target(value, 'xiaohongshu')
        return True
    final = web.fetch(url, 10, public_only=True, stop_at=note_target)['final_url']
    target(final, 'xiaohongshu')
    return final
