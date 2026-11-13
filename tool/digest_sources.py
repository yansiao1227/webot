"""公开 RSS / JSON 的有界读取；不执行来源内容或读取本机业务数据。"""
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
import json
import re
import socket
import ssl
import http.client
import time
from urllib.parse import urljoin, urlsplit, urlunsplit, parse_qsl, urlencode
import xml.etree.ElementTree as ET

from .content_reading.urls import public_url
from .content_reading.web import resolve_public, _http_user_agent

MAX_BYTES = 2 * 1024 * 1024


def article_image(raw, page_url, title=''):
    """从原文公开元数据及正文查找配图；不把站标、头像或推荐区图片当新闻图。"""
    class Images(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=True)
            self.metadata, self.body = [], []
            self.depth = 0
            self.article_depth = None
            self.blocked_depth = None

        def handle_starttag(self, tag, attrs):
            attrs = dict(attrs)
            blocked = tag in ('aside', 'nav', 'header', 'footer') or bool(re.search(
                r'(?:^|[\s_-])(?:related|recommend|avatar|share)(?:$|[\s_-])',
                attrs.get('class', '') + ' ' + attrs.get('id', ''), re.I))
            if tag == 'meta' and attrs.get('property', attrs.get('name', '')).lower() in ('og:image', 'og:image:url', 'twitter:image', 'twitter:image:src'):
                self.metadata.append((attrs.get('content', ''), title))
            if tag not in ArticleParser.VOID:
                self.depth += 1
                if blocked and self.blocked_depth is None:
                    self.blocked_depth = self.depth
                if self.article_depth is None and (tag == 'article' or set(attrs.get('class', '').lower().split()) & ArticleParser.BODY_CLASSES):
                    self.article_depth = self.depth
            if tag == 'img' and self.article_depth is not None and self.blocked_depth is None and not blocked:
                # 明确的小缩略图不作为候选；懒加载原图优先。
                width, height = attrs.get('width', ''), attrs.get('height', '')
                if (width.isdigit() and int(width) < 300) or (height.isdigit() and int(height) < 120):
                    return
                self.body.append((attrs.get('data-src') or attrs.get('src', ''), attrs.get('alt') or title))

        def handle_endtag(self, tag):
            if tag not in ArticleParser.VOID:
                if self.article_depth == self.depth:
                    self.article_depth = None
                if self.blocked_depth == self.depth:
                    self.blocked_depth = None
                self.depth = max(0, self.depth - 1)

    parser = Images()
    parser.feed(raw.decode('utf-8', errors='replace') if isinstance(raw, bytes) else raw)
    for candidate, caption in parser.metadata + parser.body:
        url = urljoin(page_url, candidate.strip())
        filename = urlsplit(url).path.rsplit('/', 1)[-1].lower()
        if not candidate or not public_url(url) or re.search(r'(?:logo|favicon|avatar|qrcode|sprite|tracking|spacer)', filename):
            continue
        return {'url': url, 'caption': caption[:400], 'credit': urlsplit(page_url).hostname}
    return None


def article_image_evidence(raw, page_url, image):
    """保存采集程序已核实的原页→图片关联，供视觉审核使用，不新增事实来源。"""
    from hashlib import sha256
    if not image:
        return None
    selected = article_image(raw, page_url)
    if not selected or selected['url'] != image['url']:
        return None
    text = raw.decode('utf-8', errors='replace') if isinstance(raw, bytes) else raw
    class Evidence(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=True)
            self.tag = None
        def handle_starttag(self, tag, attrs):
            values = dict(attrs)
            candidate = (values.get('content') if tag == 'meta' and
                values.get('property', values.get('name', '')).lower() in ('og:image', 'og:image:url', 'twitter:image', 'twitter:image:src')
                else values.get('data-src') or values.get('src') if tag == 'img' else None)
            if candidate and urljoin(page_url, candidate.strip()) == image['url']:
                self.tag = self.get_starttag_text()[:4000]
    parser = Evidence()
    parser.feed(text)
    if not parser.tag:
        return None
    return {'method': 'original_page_html', 'page_url': page_url, 'image_url': image['url'],
            'page_sha256': sha256(raw if isinstance(raw, bytes) else raw.encode()).hexdigest(),
            'html_tag': parser.tag}


def read_bytes(url, timeout=8):
    """限8秒、2MiB、3次公网重定向，连接已核对的IP，保留TLS校验。"""
    deadline = time.monotonic() + timeout
    for _ in range(4):
        if not public_url(url):
            raise ValueError('来源必须是标准公开HTTP(S)网址')
        parsed = urlsplit(url)
        address = resolve_public(url)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError('source_timeout')
        connection = http.client.HTTPConnection(parsed.hostname, parsed.port or 443, timeout=remaining)
        sock = socket.create_connection((address, parsed.port or (443 if parsed.scheme == 'https' else 80)), timeout=remaining)
        try:
            if parsed.scheme == 'https':
                sock = ssl.create_default_context().wrap_socket(sock, server_hostname=parsed.hostname)
            connection.sock = sock
            headers = {'User-Agent': _http_user_agent(), 'Accept-Encoding': 'identity'}
            if parsed.hostname == 'hq.sinajs.cn':
                # 新浪官方行情页面的公开防盗链要求，不含登录或授权凭据。
                headers['Referer'] = 'https://finance.sina.com.cn/'
            connection.request('GET', (parsed.path or '/') + ('?' + parsed.query if parsed.query else ''), headers=headers)
            response = connection.getresponse()
            if response.status in (301, 302, 303, 307, 308):
                target = response.getheader('Location')
                if not target:
                    raise ValueError('source_redirect_missing')
                url = urljoin(url, target)
                continue
            if response.status != 200:
                raise ValueError('source_http_' + str(response.status))
            raw = bytearray()
            while len(raw) <= MAX_BYTES:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError('source_timeout')
                sock.settimeout(remaining)
                part = response.read1(min(65536, MAX_BYTES + 1 - len(raw)))
                if not part:
                    break
                raw.extend(part)
                if response.isclosed():
                    break
            if len(raw) > MAX_BYTES:
                raise ValueError('source_too_large')
            return bytes(raw)
        finally:
            connection.close()
            sock.close()
    raise ValueError('source_redirect_limit')


def read_with_retry(url, reader=read_bytes):
    for attempt in range(2):
        try:
            return reader(url)
        except (ValueError, OSError, TimeoutError):
            if attempt:
                raise


class TextParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.skip, self.parts = 0, []

    def handle_starttag(self, tag, attrs):
        if tag in ('script', 'style', 'noscript'):
            self.skip += 1

    def handle_endtag(self, tag):
        if tag in ('script', 'style', 'noscript'):
            self.skip = max(0, self.skip - 1)

    def handle_data(self, value):
        if not self.skip and value.strip():
            self.parts.append(value.strip())


def plain_text(raw):
    value = raw.decode('utf-8', errors='replace') if isinstance(raw, bytes) else raw
    parser = TextParser()
    parser.feed(value)
    return re.sub(r'\s+', ' ', ' '.join(parser.parts)).strip()


class ArticleParser(HTMLParser):
    """仅抽取可信结构的正文；全页文本不能作为正文兜底。"""
    VOID = {'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link', 'meta', 'param', 'source', 'track', 'wbr'}
    BLOCK_TAGS = {'script', 'style', 'noscript', 'nav', 'header', 'footer', 'aside', 'form', 'button', 'select', 'template'}
    BLOCK_ROLES = {'navigation', 'banner', 'contentinfo', 'complementary', 'menu', 'menubar', 'search', 'dialog'}
    BODY_CLASSES = {'left_zw', 'article-body', 'article__body', 'article-content', 'article__content',
                    'field--name-body', 'blog-content', 'news-article--body', 'news-article--body-content',
                    'entry-content', 'wp-block-post-content'}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack, self.candidates = [], []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        classes = set(attrs.get('class', '').lower().split())
        marker = ' '.join([attrs.get('class', ''), attrs.get('id', ''), attrs.get('aria-label', '')]).lower()
        excluded = bool(re.search(r'(?:^|[\s_-])(?:menu|breadcrumb|share|related|newsletter|cookie|press-inquiries|recent-news|download-images|authored-by)(?:$|[\s_-])', marker))
        blocked = bool((self.stack and self.stack[-1]['blocked']) or tag in self.BLOCK_TAGS or
                       attrs.get('role', '').lower() in self.BLOCK_ROLES or attrs.get('aria-hidden') == 'true' or excluded)
        priority = 0
        if not blocked:
            if (classes & self.BODY_CLASSES or {'content-area', 'full-width-layout'} <= classes or
                    attrs.get('itemprop', '').lower() == 'articlebody' or
                    attrs.get('data-component') == 'uni-article-body'):
                priority = 3
            elif tag == 'article' or (tag == 'div' and 'article' in classes):
                priority = 2
            elif tag == 'main' or attrs.get('role') == 'main':
                priority = 1
        frame = {'tag': tag, 'blocked': blocked, 'candidate': None}
        if priority:
            candidate = {'priority': priority, 'parts': [], 'length': 0}
            self.candidates.append(candidate)
            frame['candidate'] = candidate
        if tag not in self.VOID:
            self.stack.append(frame)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in self.VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index]['tag'] == tag:
                del self.stack[index:]
                return

    def handle_data(self, value):
        if not value.strip() or not self.stack or self.stack[-1]['blocked']:
            return
        for frame in self.stack:
            candidate = frame['candidate']
            if candidate is not None and candidate['length'] < 32000:
                candidate['parts'].append(value.strip())
                candidate['length'] += len(value)


def article_text(raw, minimum=120):
    """返回实际正文，拒绝只有菜单、无正文容器、验证码或不足长度的页面。"""
    value = raw.decode('utf-8', errors='replace') if isinstance(raw, bytes) else raw
    parser = ArticleParser()
    parser.feed(value)
    candidates = []
    for candidate in parser.candidates:
        text = re.sub(r'\s+', ' ', ' '.join(candidate['parts'])).strip()
        if len(text) >= minimum:
            candidates.append((candidate['priority'], len(text), text))
    if not candidates:
        raise ValueError('article_body_unavailable')
    text = max(candidates, key=lambda x: (x[0], x[1]))[2]
    if any(x in text[:500].lower() for x in ('captcha', 'access denied', 'just a moment')):
        raise ValueError('article_body_unavailable')
    return text


def canonical_url(url):
    parsed = urlsplit(url)
    query = [(k, v) for k, v in parse_qsl(parsed.query) if not k.lower().startswith(('utm_', 'fbclid', 'gclid'))]
    return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path.rstrip('/'), urlencode(query), ''))


def timestamp(value):
    try:
        result = datetime.fromisoformat(value.strip().replace('Z', '+00:00'))
    except (ValueError, AttributeError):
        result = parsedate_to_datetime(value)
    if result.tzinfo is None:
        raise ValueError('source_missing_timezone')
    return result.astimezone(timezone.utc)


def parse_feed(raw):
    if b'<!DOCTYPE' in raw.upper() or b'<!ENTITY' in raw.upper():
        raise ValueError('source_xml_entities_not_allowed')
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        raise ValueError('source_xml_invalid') from None
    ns = '{http://www.w3.org/2005/Atom}'
    entries = root.findall('./channel/item') if root.tag != ns + 'feed' else root.findall(ns + 'entry')
    items = []
    for entry in entries[:100]:
        title = entry.findtext('title') or entry.findtext(ns + 'title') or ''
        link = entry.findtext('link')
        if link is None:
            link = next((x.get('href') for x in entry.findall(ns + 'link') if x.get('rel', 'alternate') == 'alternate'), '')
        published = entry.findtext('pubDate') or entry.findtext(ns + 'published') or entry.findtext(ns + 'updated')
        try:
            at = timestamp(published)
        except (ValueError, TypeError, AttributeError):
            continue
        if title.strip() and public_url(link):
            items.append({'title': plain_text(title)[:300], 'url': canonical_url(link),
                          'published_at': at.isoformat().replace('+00:00', 'Z')})
    return items


def read_json(url, reader=read_bytes):
    return json.loads(read_with_retry(url, reader).decode('utf-8'))
