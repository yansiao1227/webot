"""URL 语法与可选的公开来源策略。"""
import ipaddress
from urllib.parse import urlsplit


def public_url(value):
    value = (value or '').strip()
    try:
        parsed = urlsplit(value)
        host = parsed.hostname
        if (parsed.scheme not in ('http', 'https') or not host or parsed.username is not None or
                parsed.password is not None or parsed.port not in (None, 80, 443) or
                any(ord(c) < 33 for c in value) or '\\' in value):
            return None
        host = host.rstrip('.').lower()
        if host == 'localhost' or host.endswith(('.localhost', '.local', '.internal')) or '.' not in host:
            return None
        try:
            if not ipaddress.ip_address(host).is_global:
                return None
        except ValueError:
            pass
        return value
    except ValueError:
        return None

