"""仅在引用授权后获取原生签名的表情 CDN 资源。"""
import hashlib
import http.client
import time
from urllib.parse import urlsplit, urlunsplit

from .errors import ApiError

LIMIT = 64 * 1024 * 1024


def sticker_bytes(url, expected_md5):
    value = urlsplit(url)
    if (value.scheme not in ('http', 'https') or value.hostname != 'vweixinf.tc.qq.com' or
            value.username or value.password or value.port not in (None, 80, 443) or value.fragment or
            value.path != '/110/20401/stodownload' or len(value.query) > 4096):
        raise ApiError(403, 'native_cdn_not_allowed', '原生表情资源地址不在已验证的下载范围')
    # 保留原生传输方式，不禁用 HTTPS URL 的 TLS 证书校验；
    # 此版本的签名表情 URL 通常使用 HTTP。
    connection = (http.client.HTTPSConnection if value.scheme == 'https' else
                  http.client.HTTPConnection)(value.hostname, timeout=15)
    try:
        connection.request('GET', urlunsplit(('', '', value.path, value.query, '')),
                           headers={'User-Agent': 'MicroMessenger/4.1.9.30'})
        response = connection.getresponse()
        if response.status != 200:
            raise ApiError(503, 'native_cdn_download_failed', '原生表情资源下载未成功，不跟随重定向')
        declared = response.getheader('Content-Length')
        if declared and (not declared.isdigit() or not 0 < int(declared) <= LIMIT):
            raise ApiError(503, 'native_cdn_size_invalid', '原生表情资源长度无效或超过上限')
        result = bytearray()
        deadline = time.monotonic() + 40
        while True:
            chunk = response.read(min(256 * 1024, LIMIT + 1 - len(result)))
            if not chunk:
                break
            result.extend(chunk)
            if len(result) > LIMIT or time.monotonic() > deadline:
                raise ApiError(503, 'native_cdn_limit', '原生表情资源下载超时或超过上限')
        data = bytes(result)
        if not data or hashlib.md5(data).hexdigest() != expected_md5:
            raise ApiError(503, 'native_cdn_integrity_failed', '原生表情资源与引用消息的MD5不匹配')
        return data
    except (OSError, http.client.HTTPException):
        raise ApiError(503, 'native_cdn_download_failed', '原生表情资源网络下载失败') from None
    finally:
        connection.close()
