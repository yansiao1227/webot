"""独立环境中的 MCP 客户端，只暴露固定读取操作。"""
import asyncio
from contextlib import AsyncExitStack
import ipaddress
import json
import os
import sys
from urllib.parse import urlsplit

from tool.settings import path_setting
from . import platforms, web


async def call(session, name, arguments):
    result = await session.call_tool(name, arguments)
    if result.isError:
        raise ValueError('平台工具读取失败，可能需要登录')
    text = '\n'.join(item.text for item in result.content if item.type == 'text')
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        raise ValueError('平台未返回有效来源内容') from None
    if not isinstance(value, dict) or not value or value.get('error'):
        raise ValueError('平台未取得来源内容')
    return value


async def retrieve(url):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    from mcp.client.streamable_http import streamablehttp_client
    provider = platforms.platform(url)
    final = url
    host = urlsplit(url).hostname.lower().rstrip('.')
    if host == 'b23.tv':
        final = await asyncio.to_thread(platforms.resolve_bilibili, url)
    elif host == 'xhslink.com' or host.endswith('.xhslink.com'):
        final = await asyncio.to_thread(platforms.resolve_xiaohongshu, url)
    arguments = platforms.target(final, provider)
    async with AsyncExitStack() as stack:
        if provider == 'bilibili':
            script = path_setting('CONTENT_BILIBILI_MCP_SCRIPT', 'tool/state/platform-mcp/bilibili-mcp/mcp_server.py')
            streams = await stack.enter_async_context(stdio_client(StdioServerParameters(
                command=sys.executable, args=['-B', '-X', 'utf8', str(script)], cwd=str(script.parent)),
                errlog=sys.stderr))
        else:
            endpoint = os.environ.get('CONTENT_XIAOHONGSHU_MCP_URL') or 'http://127.0.0.1:18060/mcp'
            parsed = urlsplit(endpoint)
            if (parsed.scheme != 'http' or not ipaddress.ip_address(parsed.hostname).is_loopback or
                    parsed.username or parsed.password or parsed.query or parsed.fragment):
                raise ValueError('小红书MCP必须为回环HTTP地址')
            streams = await stack.enter_async_context(streamablehttp_client(endpoint))
        session = await stack.enter_async_context(ClientSession(streams[0], streams[1]))
        await session.initialize()
        if provider == 'xiaohongshu':
            data = await call(session, 'get_feed_detail', arguments)
            # 视频资源单独交给调用方处理，避免JSON截断丢失媒体字段。
            note = data.get('data', {}).get('note', {})
            media_resource = note.pop('video', None)
            video = media_resource if note.get('type') == 'video' else None
            note.pop('xsecToken', None)
            raw = json.dumps(data, ensure_ascii=False)
            return {'url': url, 'final_url': final, 'state': 'read', 'method': 'xiaohongshu-mcp',
                'video_resources': video,
                'text': raw[:24000], 'truncated': len(raw) > 24000,
                'content_scope': '平台笔记详情和已加载评论；视频读取范围以video_content为准'}
        result = {'url': url, 'final_url': final, 'method': 'bilibili-mcp', 'bvid': arguments['bvid'], 'text': '',
            'content_scope': '视频简介、字幕与观众讨论分别记录；未读取音画'}
        for name, key, extra in [('bili_video_info', 'metadata', {}),
                ('bili_subtitle', 'video_content', {}), ('bili_comments', 'discussion_content', {'num': 20})]:
            try:
                data = await call(session, name, {**arguments, **extra})
                raw = json.dumps(data, ensure_ascii=False)
                usable = bool(data.get('text', '').strip()) if key == 'video_content' else (
                    bool(data.get('comments')) if key == 'discussion_content' else bool(data.get('title')))
                result[key] = {'tool': name, 'state': 'read' if usable else 'unavailable',
                    'text': raw[:12000], 'truncated': len(raw) > 12000}
                if key == 'metadata' and usable:
                    result['text'] = raw[:4000]
            except Exception:
                result[key] = {'tool': name, 'state': 'unavailable', 'reason': '平台读取失败，可能需登录或来源未提供内容'}
        result['state'] = 'read' if any(result[k]['state'] == 'read' for k in
            ('video_content', 'discussion_content')) else 'unavailable'
        if result['state'] == 'unavailable':
            result['reason'] = '未取得字幕或评论，简介单独保留，需回退网页读取'
        return result


def main():
    try:
        request = json.loads(sys.stdin.read(16000))
        result = asyncio.run(asyncio.wait_for(retrieve(request['url']), timeout=45))
    except Exception:
        result = {'state': 'unavailable', 'reason': '平台MCP未取得来源，请检查服务、登录及完整分享链接'}
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
