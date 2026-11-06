"""只读核对固定版本登录身份；不登录、不注入、不读取消息或密钥。"""
import re
import struct
import time


class LoginUnavailable(RuntimeError):
    pass


def read_identity(api, process, base):
    # 与 native/runtime.c::account_identity 的固定版本结构及二次快照一致。
    started = time.monotonic()
    def u64(address):
        return struct.unpack('<Q', api.read(process, address, 8))[0]
    try:
        root = u64(base + 0xa6634b8)
        if not 0x10000 <= root <= 0x00007ffffffeffff-0x78 or u64(root) != base + 0x8040838:
            raise LoginUnavailable('login_root_unavailable')
        pair = api.read(process, root + 0x68, 16)
        obj, control = struct.unpack('<QQ', pair)
        if not 0x10000 <= obj <= 0x00007ffffffeffff-0x68 or not control or u64(obj) != base + 0x80e58e8:
            raise LoginUnavailable('login_object_unavailable')
        getter = u64(base + 0x80e58e8 + 0x18)
        if getter != base + 0x3035b0 or api.read(process, getter, 5) != bytes.fromhex('488d4148c3'):
            raise LoginUnavailable('login_getter_unavailable')
        header = api.read(process, obj + 0x48, 32)
        length, capacity = struct.unpack('<QQ', header[16:])
        if not 6 <= length <= 69 or length > capacity or capacity == (1 << 64) - 1:
            raise LoginUnavailable('login_string_unavailable')
        data = struct.unpack('<Q', header[:8])[0] if capacity >= 16 else obj + 0x48
        if (capacity < 16 and length > 15) or not 0x10000 <= data <= 0x00007ffffffeffff-length:
            raise LoginUnavailable('login_string_unavailable')
        raw = api.read(process, data, length + 1)
        if raw[-1] or not re.fullmatch(rb'wxid_[A-Za-z0-9_-]+', raw[:-1]):
            raise LoginUnavailable('login_id_unavailable')
        if (api.read(process, obj + 0x48, 32) != header or u64(base + 0xa6634b8) != root or
                api.read(process, root + 0x68, 16) != pair or u64(obj) != base + 0x80e58e8 or
                time.monotonic() - started >= .1):
            raise LoginUnavailable('login_identity_unstable')
        return raw[:-1].decode('ascii')
    except OSError:
        raise LoginUnavailable('login_memory_unavailable') from None
