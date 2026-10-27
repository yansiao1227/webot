"""Read only the pinned account object's image-key field; never scan memory."""
import struct
import time

from .errors import ApiError


def current_image_key(account, pid=None, full=False):
    from hook_runtime.loader import Win
    from hook_runtime.target import discover
    api = Win()
    target = discover(api, pid)
    modules = api.modules(target['pid'])
    from pathlib import Path
    base = next(base for path, base, _ in modules if Path(path).name.lower() == 'weixin.dll')
    process = api.api.OpenProcess(0x1010, False, target['pid'])
    if not process:
        raise ApiError(503, 'native_image_key_unavailable', '无法只读访问当前微信进程')
    try:
        started = time.monotonic()
        def integer(at):
            return struct.unpack('<Q', api.read(process, at, 8))[0]
        root = integer(base + 0xa6634b8)
        if integer(root) != base + 0x8040838:
            raise ValueError('root')
        pair = api.read(process, root + 0x68, 16)
        obj, control = struct.unpack('<QQ', pair)
        if not control or integer(obj) != base + 0x80e58e8:
            raise ValueError('account')
        def string(at):
            header = api.read(process, at, 32)
            length, capacity = struct.unpack('<QQ', header[16:])
            if length > 128 or length > capacity:
                raise ValueError('length')
            value = header[:length] if capacity < 16 else api.read(process, struct.unpack('<Q', header[:8])[0], length)
            if api.read(process, at, 32) != header:
                raise ValueError('changed')
            return value
        if string(obj + 0x48).decode('ascii') != account:
            raise ValueError('identity')
        # 0x303f80 copies account+0x278 and XORs this exact 32-byte mask.
        encoded = string(obj + 0x278)
        if len(encoded) not in (16, 32):
            raise ValueError('key length')
        mask = struct.pack('<8I', 0x97e0cad9, 0x5a20ea83, 0xb15c0b0b, 0x01aa1eb3,
                           0xfde79963, 0x7ded05c7, 0x6edb53b6, 0x20d54566)
        material = bytes(a ^ b for a, b in zip(encoded, mask))
        if (integer(base + 0xa6634b8) != root or api.read(process, root + 0x68, 16) != pair or
                string(obj + 0x48).decode('ascii') != account or string(obj + 0x278) != encoded or
                time.monotonic() - started > .1):
            raise ValueError('changed')
        return material if full else material[:16]
    except (OSError, ValueError, UnicodeError):
        raise ApiError(503, 'native_image_key_unavailable', '当前账号图片密钥尚未就绪或身份在读取期间发生变化') from None
    finally:
        api.api.CloseHandle(process)
