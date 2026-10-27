"""从账号的原生媒体存储读取已采集语音，不读取聊天历史。

SQLCipher 页校验遵循 v4 配置。密钥和解密后的数据库页仅保留在内存中，
不写出明文数据库或密钥文件。
"""
import ctypes as C
from ctypes import wintypes as W
import hashlib
import hmac
from pathlib import Path
import re
import struct
import time
import sqlite3
import threading

from .errors import ApiError

PAGE = 4096
MAX_DB = 64 * 1024 * 1024
_MEMORY_KEYS = {}
_MEMORY_KEYS_LOCK = threading.Lock()


def hmac_key(key, salt):
    return hashlib.pbkdf2_hmac('sha512', key, bytes(b ^ 0x3a for b in salt), 2, 32)


def valid_page(data, number, key, salt):
    if len(data) != PAGE:
        return False
    offset = 16 if number == 1 else 0
    digest = hmac.new(hmac_key(key, salt), data[offset:-64] + struct.pack('<I', number), hashlib.sha512).digest()
    return hmac.compare_digest(digest, data[-64:])


def read_database_key(account, salt, first_page):
    """有界只读内存搜索，仅接受此媒体数据库的 salt+HMAC。"""
    from hook_runtime.loader import Win
    from hook_runtime.target import discover
    api = Win()
    target = discover(api)
    base = next(base for path, base, _ in api.modules(target['pid']) if Path(path).name.lower() == 'weixin.dll')
    process = api.api.OpenProcess(0x1010, False, target['pid'])
    if not process:
        raise ApiError(503, 'native_media_key_unavailable', '无法只读访问当前账号媒体密钥')
    class Region(C.Structure):
        _fields_ = [('base', C.c_void_p), ('allocation', C.c_void_p), ('allocation_protection', W.DWORD),
                    ('partition', W.WORD), ('size', C.c_size_t), ('state', W.DWORD),
                    ('protection', W.DWORD), ('kind', W.DWORD)]
    query = api.api.VirtualQueryEx
    query.argtypes = [W.HANDLE, C.c_void_p, C.POINTER(Region), C.c_size_t]
    query.restype = C.c_size_t
    def identity():
        root = struct.unpack('<Q', api.read(process, base + 0xa6634b8, 8))[0]
        if struct.unpack('<Q', api.read(process, root, 8))[0] != base + 0x8040838:
            raise ValueError('root')
        pair = api.read(process, root + 0x68, 16)
        obj, ctrl = struct.unpack('<QQ', pair)
        if not ctrl or struct.unpack('<Q', api.read(process, obj, 8))[0] != base + 0x80e58e8:
            raise ValueError('account')
        header = api.read(process, obj + 0x48, 32)
        length, capacity = struct.unpack('<QQ', header[16:])
        if not 5 < length <= 128 or length > capacity:
            raise ValueError('account length')
        value = header[:length] if capacity < 16 else api.read(process, struct.unpack('<Q', header[:8])[0], length)
        if value != account.encode('ascii'):
            raise ValueError('account identity')
        return root, pair
    try:
        owner = identity()
        # 仅在内存中短期、有界缓存；每次复用都重新校验
        # 原生所有者、进程实例、salt 和首页 HMAC；解密后的
        # 页面及 SQLite 连接从不缓存，避免每次出站记录密集出现时
        # 都因 UI 快照而重新扫描整个进程。
        scope=(account,target['pid'],api.creation(process)[0],salt,owner)
        with _MEMORY_KEYS_LOCK:
            now=time.monotonic()
            for expired in [k for k,v in _MEMORY_KEYS.items() if v[1]<=now]:
                del _MEMORY_KEYS[expired]
            cached=_MEMORY_KEYS.get(scope)
        if cached and valid_page(first_page,1,cached[0],salt) and identity()==owner:
            return cached[0]
        address, scanned, deadline = 0, 0, time.monotonic() + 20
        needle = salt.hex().encode()
        while time.monotonic() < deadline and scanned < 1024 * 1024 * 1024:
            region = Region()
            if not query(process, address, C.byref(region), C.sizeof(region)):
                break
            start, size = region.base or 0, region.size
            if region.state == 0x1000 and region.protection & 0xff in (4, 8, 0x40, 0x80) and not region.protection & 0x100:
                for offset in range(0, size, 2 * 1024 * 1024 - 128):
                    if time.monotonic() >= deadline or scanned >= 1024 * 1024 * 1024:
                        break
                    amount = min(2 * 1024 * 1024, size - offset)
                    scanned += amount
                    try:
                        data = api.read(process, start + offset, amount)
                    except OSError:
                        continue
                    for matched in re.finditer(re.escape(needle), data, re.IGNORECASE):
                        at = matched.start()
                        prefix = data[max(0, at - 66):at]
                        if len(prefix) != 66 or prefix[:2].lower() != b"x'" or data[at + 32:at + 33] != b"'":
                            continue
                        try:
                            key = bytes.fromhex(prefix[2:].decode('ascii'))
                        except (ValueError, UnicodeError):
                            continue
                        if len(key) == 32 and valid_page(first_page, 1, key, salt) and identity() == owner:
                            with _MEMORY_KEYS_LOCK:
                                if len(_MEMORY_KEYS)>=8:
                                    del _MEMORY_KEYS[min(_MEMORY_KEYS,key=lambda k:_MEMORY_KEYS[k][1])]
                                _MEMORY_KEYS[scope]=(key,time.monotonic()+120)
                            return key
            next_address = start + size
            if next_address <= address:
                break
            address = next_address
    except (OSError, ValueError, UnicodeError):
        raise ApiError(503, 'native_media_key_unavailable', '媒体存储身份未确认或读取期间发生变化') from None
    finally:
        api.api.CloseHandle(process)
    raise ApiError(503, 'native_media_key_unavailable', '尚未找到通过该媒体库HMAC校验的当前账号密钥')


def _checksum(data, order, initial=(0, 0)):
    a, b = initial
    for x, y in struct.iter_unpack(order + 'II', data):
        a = (a + x + b) & 0xffffffff
        b = (b + y + a) & 0xffffffff
    return a, b


def wal_pages(data):
    """校验加密 WAL，仅暴露到最后一次提交为止的页面。"""
    if not data:
        return {}, None
    if len(data) < 32:
        raise ValueError('incomplete WAL header')
    magic, version, size = struct.unpack('>III', data[:12])
    if magic not in (0x377f0682, 0x377f0683) or version != 3007000 or size != PAGE:
        raise ValueError('unsupported WAL')
    order = '<' if magic == 0x377f0682 else '>'
    checksum = _checksum(data[:24], order)
    if checksum != struct.unpack('>II', data[24:32]):
        raise ValueError('WAL header checksum')
    pending, committed, page_count = {}, {}, None
    for at in range(32, len(data) - PAGE - 23, PAGE + 24):
        frame = data[at:at + PAGE + 24]
        page, count = struct.unpack('>II', frame[:8])
        if frame[8:16] != data[16:24] or not 0 < page <= MAX_DB // PAGE:
            break
        candidate = _checksum(frame[:8] + frame[24:], order, checksum)
        if candidate != struct.unpack('>II', frame[16:24]):
            break  # 忽略并且不暴露不完整或未提交的尾部。
        checksum = candidate
        pending[page] = frame[24:]
        if count:
            if count > MAX_DB // PAGE:
                raise ValueError('WAL database size limit')
            committed.update(pending)
            pending.clear()
            page_count = count
    return committed, page_count


def media_snapshot(path, account):
    """一致的加密快照，仅在此提供方的内存中解密。"""
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    path = Path(path)
    wal = Path(str(path) + '-wal')
    def stamp(item):
        return (item.stat().st_size, item.stat().st_mtime_ns) if item.exists() else None
    for _ in range(3):
        before = stamp(path), stamp(wal)
        if not before[0] or not PAGE <= before[0][0] <= MAX_DB or before[0][0] % PAGE:
            raise ApiError(503, 'native_media_format_invalid', '媒体存储大小或分页格式未通过校验')
        if before[1] and before[1][0] > MAX_DB:
            raise ApiError(503, 'native_media_limit', '媒体存储WAL超过有界读取上限')
        encrypted = path.read_bytes()
        journal = wal.read_bytes() if before[1] else b''
        if before == (stamp(path), stamp(wal)):
            break
    else:
        raise ApiError(503, 'native_media_busy', '媒体存储正在更新，请稍后重试')
    try:
        overlay, count = wal_pages(journal)
        count = count if count is not None else len(encrypted) // PAGE
        salt = encrypted[:16]
        key = read_database_key(account, salt, encrypted[:PAGE])
        result = bytearray()
        for number in range(1, count + 1):
            page = overlay.get(number, encrypted[(number - 1) * PAGE:number * PAGE])
            if not valid_page(page, number, key, salt):
                raise ValueError('media page HMAC mismatch')
            decryptor = Cipher(algorithms.AES(key), modes.CBC(page[-80:-64])).decryptor()
            offset = 16 if number == 1 else 0
            clear = decryptor.update(page[offset:-80]) + decryptor.finalize()
            result.extend((b'SQLite format 3\0' if number == 1 else b'') + clear + page[-80:])
        result[18:20] = b'\1\1'  # 仅此私有内存快照使用回滚日志模式。
        result[28:32] = struct.pack('>I', count)
        connection = sqlite3.connect(':memory:')
        try:
            connection.deserialize(bytes(result))
            connection.execute('PRAGMA query_only=ON')
            return connection
        except BaseException:
            connection.close()
            raise
    except (ValueError, sqlite3.Error):
        raise ApiError(503, 'native_media_integrity_failed', '媒体库或WAL的完整性验证未通过') from None


def voice_bytes(directory, event, account):
    """身份由精确账号、聊天记录行和已采集的服务器消息 ID 共同确定。"""
    mid = event.get('message_id')
    if not isinstance(mid, str) or not re.fullmatch(r'[1-9][0-9]{0,19}', mid) or int(mid) >= 1 << 64:
        raise ApiError(403, 'native_voice_identity_required', '语音缺少可靠原生消息标识')
    message_id = int(mid)
    if message_id >= 1 << 63:
        message_id -= 1 << 64
    root = Path(directory) / 'db_storage/message'
    paths = sorted(root.glob('media_*.db'))
    if len(paths) > 16:
        raise ApiError(503, 'native_media_limit', '原生媒体分片数量超过已验证上限')
    matches = []
    for path in paths:
        if path.is_symlink() or path.resolve().parent != root.resolve():
            continue
        connection = media_snapshot(path, account)
        try:
            rows = connection.execute('''SELECT data_index,voice_data FROM VoiceInfo
                WHERE chat_name_id=(SELECT rowid FROM Name2Id WHERE user_name=?) AND svr_id=? LIMIT 2''',
                (event['conversation_id'], message_id)).fetchall()
            matches.extend(rows)
        finally:
            connection.close()
    if not matches:
        raise ApiError(503, 'native_voice_download_required', '引用语音尚未出现在该会话的原生媒体缓存')
    if len(matches) != 1 or str(matches[0][0]) != '0':
        raise ApiError(503, 'native_voice_ambiguous', '语音缓存不唯一或属于尚未适配的分段格式')
    data = matches[0][1]
    expected = event.get('payload', {}).get('size')
    if (not isinstance(data, bytes) or not 0 < len(data) <= MAX_DB or
            (isinstance(expected, int) and len(data) != expected) or
            not data.startswith((b'\x02#!SILK_V3', b'#!SILK_V3'))):
        raise ApiError(503, 'native_voice_integrity_failed', '语音长度或SILK格式与原生事件不匹配')
    return data
