"""带逐跳公网校验、大小和总时限的资源下载，不向重定向转发凭据。"""
import http.client
from pathlib import Path
import socket
import ssl
import time
from urllib.parse import urljoin, urlsplit

from . import web


def download(url, path, *, max_bytes, timeout=30):
    path = Path(path)
    temporary = path.with_suffix(path.suffix + '.part')
    deadline = time.monotonic() + timeout
    try:
        for _ in range(6):
            address = web.resolve_public(url)
            parsed = urlsplit(url)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ValueError('资源下载超时')
            port = parsed.port or (443 if parsed.scheme == 'https' else 80)
            connection = http.client.HTTPConnection(parsed.hostname, port, timeout=remaining)
            sock = socket.create_connection((address, port), timeout=remaining)
            try:
                if parsed.scheme == 'https':
                    sock = ssl.create_default_context().wrap_socket(sock, server_hostname=parsed.hostname)
                connection.sock = sock
                connection.request('GET', (parsed.path or '/') + ('?' + parsed.query if parsed.query else ''),
                    headers={'User-Agent': web._http_user_agent(), 'Accept-Encoding': 'identity'})
                response = connection.getresponse()
                if response.status in (301, 302, 303, 307, 308):
                    location = response.getheader('Location')
                    if not location:
                        raise ValueError('资源跳转缺少目标')
                    url = urljoin(url, location)
                    continue
                if response.status != 200:
                    raise ValueError('资源返回HTTP ' + str(response.status))
                size = response.getheader('Content-Length')
                if size is not None and (not size.isdigit() or not 0 < int(size) <= max_bytes):
                    raise ValueError('资源为空或超过下载上限')
                count = 0
                with temporary.open('wb') as stream:
                    while True:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise ValueError('资源下载超时')
                        sock.settimeout(remaining)
                        chunk = response.read1(min(65536, max_bytes + 1 - count))
                        if not chunk:
                            break
                        count += len(chunk)
                        if count > max_bytes:
                            raise ValueError('资源超过下载上限')
                        stream.write(chunk)
                        if response.isclosed():
                            break
                if not count or (size is not None and count != int(size)):
                    raise ValueError('资源下载不完整')
                temporary.replace(path)
                return {'path': str(path.resolve()), 'bytes': count}
            finally:
                connection.close()
                sock.close()
        raise ValueError('资源重定向次数超限')
    finally:
        temporary.unlink(missing_ok=True)
