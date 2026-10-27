"""按账号限定的原生资源定位器；字节内容必须匹配原生内容 MD5。"""
import hashlib
from pathlib import Path
import re
from xml.etree import ElementTree as ET

from .errors import ApiError
from .messages import payload_xml

LIMIT = 64 * 1024 * 1024


def account_directory(account, configured=None):
    if not re.fullmatch(r'wxid_[A-Za-z0-9_-]+', account):
        raise ApiError(409, 'invalid_account', '当前账号标识无效')
    if configured:
        roots = [Path(configured)]
    else:
        import os
        import winreg
        roots = []
        config = Path.home() / 'AppData/Roaming/Tencent/xwechat/config'
        for path in config.glob('*.ini'):
            if path.is_file() and path.stat().st_size <= 1024:
                try:
                    value = path.read_text(encoding='utf-8-sig').strip('\0\r\n ')
                    if re.fullmatch(r'[A-Za-z]:[\\/][^\0\r\n]+', value):
                        roots.append(Path(value) / 'xwechat_files')
                except (OSError, UnicodeError):
                    pass
        for key, name in [(r'Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders', 'Personal'),
                          (r'Software\Tencent\Weixin', 'FileSavePath')]:
            try:
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as handle:
                    value = str(winreg.QueryValueEx(handle, name)[0])
                if value != '1':
                    roots.append(Path(os.path.expandvars(value)) / 'xwechat_files')
            except OSError:
                pass
    matches = {p.resolve() for root in roots if root.is_dir() for p in root.glob(account + '_*')
               if p.is_dir() and not p.is_symlink()}
    if len(matches) != 1:
        raise ApiError(409, 'data_directory_ambiguous', '需要唯一且匹配当前账号的xwechat_files数据目录')
    return matches.pop()


def _md5(value):
    return isinstance(value, str) and re.fullmatch(r'[a-fA-F0-9]{32}', value)


def decode_image(data, expected_hashes=None, key_provider=None):
    if data.startswith((b'\x07\x08V1\x08\x07', b'\x07\x08V2\x08\x07')):
        return decode_dat(data, expected_hashes, key_provider)
    signatures = (b'\xff\xd8\xff', b'\x89PNG\r\n\x1a\n', b'GIF87a', b'GIF89a', b'RIFF')
    if any(data.startswith(s) for s in signatures):
        return data
    for signature in signatures:
        if len(data) < len(signature):
            continue
        key = data[0] ^ signature[0]
        if all(data[i] ^ key == b for i, b in enumerate(signature)):
            return bytes(b ^ key for b in data)
    raise ApiError(503, 'native_image_decoder_required', '该图片需要进一步适配原生解密或WXGF解码')


def decode_dat(data, expected_hashes, key_provider):
    """仅在完整内容匹配原生 MD5 时接受 V1/V2 结果。"""
    import struct
    if len(data) < 31 or not expected_hashes:
        raise ApiError(503, 'native_image_integrity_required', '新版DAT需要完整的原生内容校验标识')
    aes_size, xor_size = struct.unpack('<II', data[6:14])
    aes_end = 15 + aes_size + 16 - aes_size % 16
    raw_end = len(data) - xor_size
    if not 0 < aes_size <= LIMIT or not aes_end <= raw_end <= len(data):
        raise ApiError(503, 'native_image_format_invalid', 'DAT分段长度无效')
    if data[3] == ord('1'):
        key = b'cfcd208495d565ef'
    elif key_provider:
        key = key_provider()
    else:
        raise ApiError(503, 'native_image_key_unavailable', '尚未取得当前账号图片密钥')
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    decryptor = Cipher(algorithms.AES(key), modes.ECB()).decryptor()
    clear = decryptor.update(data[15:aes_end]) + decryptor.finalize()
    pad = clear[-1]
    if not 1 <= pad <= 16 or clear[-pad:] != bytes([pad]) * pad or len(clear) - pad != aes_size:
        raise ApiError(503, 'native_image_decryption_failed', 'DAT解密完整性验证失败')
    head = clear[:-pad] + data[aes_end:raw_end]
    digest = hashlib.md5(head)
    # 不使用账号级默认 XOR 常量，必须针对本事件校验候选值。
    tail = data[raw_end:]
    if len(tail) > 1024 * 1024:
        raise ApiError(503, 'native_image_format_invalid', 'DAT尾段超过有界解码上限')
    for xor_key in range(256) if tail else (0,):
        decoded_tail = tail.translate(bytes(b ^ xor_key for b in range(256)))
        candidate = digest.copy()
        candidate.update(decoded_tail)
        if candidate.hexdigest() in expected_hashes:
            return head + decoded_tail
    raise ApiError(503, 'native_image_decryption_failed', 'DAT解码结果与原消息内容标识不一致')


class NativeAssets:
    def __init__(self, store, account, configured=None):
        self.store, self.account = store, account
        self.directory = account_directory(account, configured)

    def fetch(self, event, allow_download=True, cached_paths=None):
        rid = event['payload']['resource_id']
        with self.store.condition:
            row = self.store.db.execute('SELECT * FROM native_resources WHERE id=? AND account=? AND conversation=?',
                (rid, self.account, event['conversation_id'])).fetchone()
        if not row or row['kind'] != event['message_type']:
            raise ApiError(403, 'native_resource_mismatch', '资源与原生消息身份不匹配')
        raw, kind = row['xml'], row['kind']
        if kind == 'card':
            return raw.encode('utf-8')
        if kind == 'voice':
            from .native_media import voice_bytes
            from .codecs import silk_to_wav
            return silk_to_wav(voice_bytes(self.directory, event, self.account))
        root = payload_xml(raw)
        hashes = set()
        for node in root.iter():
            for key in ('md5', 'filemd5', 'rawmd5'):
                if _md5(node.get(key)):
                    hashes.add(node.get(key).lower())
            if node.tag in ('md5', 'filemd5', 'rawmd5') and _md5(node.text):
                hashes.add(node.text.lower())
        if not hashes:
            raise ApiError(503, 'native_resource_locator_required', '原消息缺少已验证的资源内容标识，需要适配原生定位入口')
        cid_hash = hashlib.md5(event['conversation_id'].encode()).hexdigest()
        base = self.directory / 'msg' / 'attach' / cid_hash
        bases = [base]
        if kind == 'file':
            bases += [self.directory / 'msg/file']
        if kind in ('video', 'voice'):
            bases += [self.directory / 'msg' / kind]
        if kind == 'sticker':
            bases += [self.directory / 'resource' / 'emoji']
        name = event['payload'].get('name', '')
        name = Path(name.replace('\\', '/')).name if isinstance(name, str) else ''
        expected_size = event['payload'].get('size')
        if isinstance(expected_size, bool) or not isinstance(expected_size, int):
            expected_size = None
        expected_sizes = {expected_size} if expected_size else set()
        if kind == 'image':
            image = root if root.tag == 'img' else root.find('img')
            if image is not None:
                for field in ('length', 'hdlength', 'hevc_mid_size', 'hevc_hd_size'):
                    value = image.get(field, '')
                    if value.isdigit() and 0 < int(value) <= LIMIT:
                        expected_sizes.add(int(value))
        inspected = 0
        decode_failure = None
        matched_data = None
        for base in bases:
            if not base.is_dir() or base.is_symlink():
                continue
            for path in base.rglob('*'):
                inspected += 1
                if inspected > 50000:
                    raise ApiError(429, 'native_locator_limit', '附件定位目录超过本轮扫描上限')
                if not path.is_file() or path.is_symlink() or not path.resolve().is_relative_to(base.resolve()):
                    continue
                size = path.stat().st_size
                # V1/V2 增加 15 字节头部和 1 至 16 字节的 AES 填充。
                # 大小仅用于在已授权会话内缩小候选范围，
                # 不能据此确认身份；解码后的 MD5 仍须精确匹配。
                sized_candidate = ((kind == 'image' and path.suffix.lower() == '.dat' and
                                    any(size == n or 16 <= size - n <= 31 for n in expected_sizes)) or
                                   (kind == 'video' and path.suffix.lower() == '.mp4' and size == expected_size))
                if not (any(path.name.lower().startswith(h) for h in hashes) or
                        (name and path.name == name) or sized_candidate):
                    continue
                if not 0 < size <= LIMIT:
                    continue
                data = path.read_bytes()
                if kind in ('image', 'sticker'):
                    try:
                        from .image_key import current_image_key
                        data = decode_image(data, hashes, lambda: current_image_key(self.account))
                    except ApiError as error:
                        decode_failure = error
                        continue
                if hashlib.md5(data).hexdigest() in hashes:
                    if cached_paths is not None:
                        cached_paths.append(path.resolve())
                        matched_data = data
                        continue
                    if kind == 'voice':
                        from .codecs import silk_to_wav
                        return silk_to_wav(data)
                    return data
        if matched_data is not None:
            return matched_data
        if kind == 'sticker':
            emoji = root if root.tag == 'emoji' else root.find('emoji')
            if emoji is not None and _md5(emoji.get('md5')) and emoji.get('cdnurl'):
                from .cdn import sticker_bytes
                return sticker_bytes(emoji.get('cdnurl'), emoji.get('md5').lower())
        if allow_download and kind in ('image', 'video', 'file'):
            from .native_download import download
            return download(event, raw)
        if decode_failure:
            raise decode_failure
        raise ApiError(503, 'native_download_required', '未找到内容校验匹配的本地资源，需要原生下载或解密入口')
