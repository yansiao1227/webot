"""有界读取来源，页面文本始终属于不可信外部内容。"""
import http.client
import ipaddress
import json
import socket
import ssl
import threading
import time
import tempfile
from pathlib import Path
from html.parser import HTMLParser
from urllib.parse import quote, urljoin, urlsplit

from .urls import public_url
from tool.settings import cache_directory

MAX_BYTES = 2 * 1024 * 1024
MAX_TEXT = 24000
_profile_lock = threading.Lock()
_fake_network = ipaddress.ip_network('198.18.0.0/15')
_dns_cache = {}


def _public_dns(host):
    # TUN 代理常返回基准测试网段的 Fake-IP 地址，应通过 TLS 校验
    # 真实公网目标，不能将所有私有 IP 视为安全。
    cached = _dns_cache.get(host)
    if cached and time.monotonic() - cached[0] < 60:
        return cached[1]
    for resolver, prefix in (('dns.alidns.com', '/resolve?name='), ('1.1.1.1', '/dns-query?name=')):
        connection = http.client.HTTPSConnection(resolver, timeout=3)
        try:
            connection.request('GET', prefix + quote(host, safe='') + '&type=A',
                               headers={'Accept': 'application/dns-json'})
            response = connection.getresponse()
            if response.status != 200:
                continue
            raw = response.read(65537)
            if len(raw) > 65536:
                continue
            answers = json.loads(raw)
            ips = [item['data'] for item in answers.get('Answer', []) if item.get('type') == 1]
            if not ips or any(not ipaddress.ip_address(ip).is_global for ip in ips):
                raise ValueError('来源没有可核验的公网地址')
            if len(_dns_cache) >= 512:
                _dns_cache.clear()
            _dns_cache[host] = (time.monotonic(), ips[0])
            return ips[0]
        except (OSError, http.client.HTTPException, json.JSONDecodeError):
            continue
        finally:
            connection.close()
    raise ValueError('无法通过可用DoH服务核验Fake-IP来源')


def validate_url(url):
    """接受 HTTP(S)，包括 localhost 和局域网；文件系统输入使用媒体工具处理。"""
    parsed = urlsplit(url)
    if (parsed.scheme not in ('http', 'https') or not parsed.hostname or
            parsed.username is not None or parsed.password is not None or
            any(ord(c) < 33 for c in url) or '\\' in url):
        raise ValueError('来源必须为有效HTTP(S)链接')
    parsed.port  # 建立连接前拒绝格式错误的端口。


def resolve_public(url):
    if not public_url(url):
        raise ValueError('来源必须为公网HTTP链接')
    parsed = urlsplit(url)
    addresses = socket.getaddrinfo(parsed.hostname, parsed.port or (443 if parsed.scheme == 'https' else 80),
                                   type=socket.SOCK_STREAM)
    if addresses and all(ipaddress.ip_address(item[4][0]) in _fake_network for item in addresses):
        return _public_dns(parsed.hostname)
    if not addresses or any(not ipaddress.ip_address(item[4][0]).is_global for item in addresses):
        raise ValueError('来源解析到非公网地址')
    return addresses[0][4][0]


class _Text(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.hidden = 0
        self.parts = []
        self.images = []

    def handle_starttag(self, tag, attrs):
        if tag in ('script', 'style', 'noscript', 'template'):
            self.hidden += 1
        if tag == 'img' and not self.hidden and len(self.images) < 8:
            attrs = dict(attrs)
            source = attrs.get('data-src') or attrs.get('src')
            if source:
                self.images.append(source)

    def handle_endtag(self, tag):
        if tag in ('script', 'style', 'noscript', 'template'):
            self.hidden = max(0, self.hidden - 1)

    def handle_data(self, data):
        if not self.hidden and data.strip():
            self.parts.append(data.strip())


def fetch(url, timeout=20, *, public_only=False, output_dir=None, image_only=False):
    deadline = time.monotonic() + timeout
    original = url
    for _ in range(6):
        validate_url(url)
        address = resolve_public(url) if public_only else None
        parsed = urlsplit(url)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ValueError('网页读取超时')
        port = parsed.port or (443 if parsed.scheme == 'https' else 80)
        connection = http.client.HTTPConnection(parsed.hostname, port, timeout=remaining)
        # 固定已校验的地址，同时保留 Host、TLS SNI 和证书校验。
        sock = socket.create_connection((address or parsed.hostname, port), timeout=remaining)
        try:
            if parsed.scheme == 'https':
                sock = ssl.create_default_context().wrap_socket(sock, server_hostname=parsed.hostname)
            connection.sock = sock
            connection.request('GET', (parsed.path or '/') + ('?' + parsed.query if parsed.query else ''),
                               headers={'User-Agent': 'Mozilla/5.0 AgentContentReader', 'Accept-Encoding': 'identity'})
            response = connection.getresponse()
            if response.status in (301, 302, 303, 307, 308):
                location = response.getheader('Location')
                if not location:
                    raise ValueError('短链接重定向缺少目标')
                url = urljoin(url, location)
                continue
            if response.status != 200:
                raise ValueError('来源返回HTTP ' + str(response.status))
            content_type = response.getheader('Content-Type', '')
            is_image = content_type.split(';')[0].strip().lower() in (
                'image/png', 'image/jpeg', 'image/gif', 'image/webp', 'image/bmp', 'image/tiff')
            if image_only and not is_image:
                raise ValueError('来源不是支持的图片响应')
            if not is_image and not any(kind in content_type.lower() for kind in ('text/html', 'text/plain', 'application/xhtml+xml')):
                raise ValueError('来源不是可读取网页')
            limit = 20 * 1024 * 1024 if is_image else MAX_BYTES
            raw = bytearray()
            while len(raw) <= limit:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ValueError('网页读取超时')
                sock.settimeout(remaining)
                chunk = response.read1(min(65536, limit + 1 - len(raw)))
                if not chunk:
                    break
                raw.extend(chunk)
                # Content-Length 指定的内容读尽后，read1 会关闭底层套接字，
                # 下一次循环不能再次设置其超时时间。
                if response.isclosed():
                    break
            if len(raw) > limit:
                raise ValueError('图片超过20MiB读取上限' if is_image else '网页超过2MiB读取上限')
            if is_image:
                from PIL import Image
                from io import BytesIO
                # 留下文件供视觉检查前，先校验实际图像数据。
                with Image.open(BytesIO(raw)) as image:
                    image.verify()
                output = Path(output_dir) if output_dir else cache_directory('content-image-')
                output.mkdir(parents=True, exist_ok=True)
                with tempfile.NamedTemporaryFile(dir=output, suffix='.image', delete=False) as target:
                    target.write(raw)
                    path = Path(target.name)
                from .media import prepare_content
                prepared = prepare_content(path, 'image', output_dir=output / path.stem)
                return {'url': original, 'final_url': url, 'method': 'fetch_image',
                        'text': '', 'path': str(path.resolve()), **prepared,
                        'content_scope': '图片已下载到本机；必须使用视觉工具查看images后才能分析内容'}
            encoding = response.headers.get_content_charset() or 'utf-8'
            text = raw.decode(encoding, errors='replace')
            image_urls = []
            if 'html' in content_type.lower():
                parser = _Text()
                parser.feed(text)
                text = '\n'.join(parser.parts)
                image_urls = [urljoin(url, source) for source in parser.images
                              if urlsplit(urljoin(url, source)).scheme in ('http', 'https')]
            return {'url': original, 'final_url': url, 'method': 'fetch',
                    'image_urls': image_urls,
                    'text': text[:MAX_TEXT], 'truncated': len(text) > MAX_TEXT,
                    'content_scope': '网页静态文本；不代表已读取视频画面、音轨或字幕'}
        finally:
            connection.close()
            sock.close()
    raise ValueError('来源重定向次数超限')


def browser(url, options):
    from playwright.sync_api import sync_playwright
    validate_url(url)
    public_only = options.get('public_only', False)
    if public_only:
        resolve_public(url)
    # 会话工作线程共享持久浏览器配置时串行访问。
    if not _profile_lock.acquire(timeout=5):
        raise ValueError('浏览器profile正在处理另一条卡片，请稍后重试')
    try:
        with sync_playwright() as playwright:
            with playwright.chromium.launch_persistent_context(
                    options['profile_dir'], channel=options.get('channel', 'msedge'),
                    headless=True, accept_downloads=False, service_workers='block',
                    timeout=15000) as context:
                context.set_default_timeout(20000)
                checked_hosts = {urlsplit(url).netloc}
                bili_source = urlsplit(url).hostname in ('b23.tv', 'www.bilibili.com', 'm.bilibili.com', 'bilibili.com')
                network_deadline = time.monotonic() + 45
                def guard(route):
                    try:
                        target = route.request.url
                        validate_url(target)
                        if public_only and not public_url(target):
                            raise ValueError('非公网链接')
                        if time.monotonic() > network_deadline:
                            raise ValueError('网页资源读取超时')
                        if bili_source:
                            host_name = urlsplit(target).hostname
                            if (route.request.resource_type in ('image', 'media', 'font') or
                                    not any(host_name == h or host_name.endswith('.' + h)
                                            for h in ('b23.tv', 'bilibili.com', 'hdslb.com', 'biliimg.com'))):
                                raise ValueError('无需加载的视频页面资源')
                        host = urlsplit(target).netloc
                        if public_only and host not in checked_hosts:
                            resolve_public(target)
                            checked_hosts.add(host)
                    except (ValueError, OSError):
                        route.abort()
                    else:
                        route.continue_()
                context.route('**/*', guard)
                page = context.new_page()
                player_responses = []
                def observe(response):
                    parsed = urlsplit(response.url)
                    if (parsed.hostname == 'api.bilibili.com' and parsed.path == '/x/player/wbi/v2' and
                            len(player_responses) < 20):
                        player_responses.append(response)
                page.on('response', observe)
                for attempt in range(2):
                    try:
                        response = page.goto(url, wait_until='domcontentloaded', timeout=20000)
                        break
                    except Exception:
                        if attempt:
                            raise
                if response and response.status >= 400:
                    raise ValueError('来源返回HTTP ' + str(response.status))
                # 读取渲染后的文章正文，包括客户端延迟渲染的内容。
                selector = '#js_content, article, main'
                try:
                    page.locator(selector).first.wait_for(state='visible', timeout=4000)
                except Exception:
                    pass
                if public_only:
                    resolve_public(page.url)
                data = page.evaluate('''() => {
                    const root = document.querySelector('#js_content') || document.querySelector('article') ||
                                 document.querySelector('main') || document.body;
                    const text = root ? root.innerText : '';
                    const image_urls = root ? Array.from(root.querySelectorAll('img'))
                        .map(img => img.dataset.src || img.currentSrc || img.src)
                        .filter(src => /^https?:/.test(src)).slice(0, 8) : [];
                    return {title: document.title.slice(0, 256), text: text.slice(0, 24000),
                            image_urls, truncated: text.length > 24000};
                }''')
                result = {'url': url, 'final_url': page.url, 'method': 'playwright', **data,
                          'content_scope': '当前渲染页面可见文本；不代表已读取视频画面或音轨'}
                from .bilibili import video_url, read_subtitles, read_discussion
                if video_url(page.url):
                    network_deadline = time.monotonic() + 35
                    # 从视频来源材料中排除推荐和评论。
                    result['text'] = data['title']
                    result['image_urls'] = []
                    try:
                        result['video_content'] = read_subtitles(context, page, player_responses)
                    except Exception as error:
                        result['video_content'] = {'state': 'unavailable', 'error': type(error).__name__,
                            'reason': str(error) if type(error) is ValueError else '视频字幕读取失败，尚未取得音画，不能仅据标题概括视频'}
                    try:
                        network_deadline = time.monotonic() + 10
                        result['discussion_content'] = read_discussion(page)
                    except Exception as error:
                        result['discussion_content'] = {'state': 'unavailable', 'error': type(error).__name__,
                            'reason': '未取得页面评论或弹幕文字'}
                return result
    finally:
        _profile_lock.release()


def read_sources(urls, options=None):
    options = options or {}
    results = []
    for url in list(urls)[:2]:
        # 调用方显式选择浏览器渲染，普通链接使用 fetch。
        method = 'playwright' if options.get('browser') and options.get('profile_dir') else 'fetch'
        try:
            value = browser(url, options) if method == 'playwright' else fetch(url, public_only=options.get('public_only', False))
            if not value.get('text', '').strip() and not value.get('images') and not value.get('image_urls'):
                raise ValueError('页面没有可读取的文本')
            results.append({**value, 'state': 'read'})
        except Exception as error:
            # 不泄露浏览器命令行、Cookie 或提供方的任意异常文本。
            results.append({'url': url, 'method': method, 'state': 'unavailable',
                            'error': type(error).__name__,
                            'reason': str(error) if type(error) is ValueError else '来源读取失败，可能需要登录、浏览器依赖或站点允许访问'})
    return results
