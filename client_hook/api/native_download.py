"""精确的原生 CDN 请求，仅能通过已授权的资源获取入口调用。"""
import hashlib
from pathlib import Path
import re
import time
import threading
import uuid

from .errors import ApiError
from .messages import payload_xml

ROOT = Path(__file__).resolve().parents[1]
LIMIT = 64 * 1024 * 1024
_ACTIVE = set()
_ACTIVE_LOCK = threading.RLock()


def prune(account, retention=7 * 86400, now=None):
    """仅使用自有的固定传输命名空间，状态未知的在途操作保持原有绑定。"""
    from .native_assets import account_directory
    now = time.time() if now is None else now
    cutoff = now - retention
    root = account_directory(account) / 'msg/temp/client_hook_downloads'
    if root.is_symlink() or root.is_junction():
        return
    if root.is_dir():
        with _ACTIVE_LOCK:
            for directory in root.iterdir():
                if (not re.fullmatch('[0-9a-f]{64}', directory.name) or directory.is_symlink() or
                        directory.is_junction() or not directory.is_dir() or directory.resolve().parent != root.resolve()):
                    continue
                content = directory / 'content'
                if str(content) in _ACTIVE:
                    continue
                children = list(directory.iterdir())
                if any(p.name != 'content' or p.is_symlink() or p.is_junction() or not p.is_file() for p in children):
                    continue
                if max([directory.stat().st_mtime] + [p.stat().st_mtime for p in children]) >= cutoff:
                    continue
                for path in children:
                    path.unlink()
                directory.rmdir()
    evidence_root = ROOT / 'state/native-download-validation'
    if evidence_root.is_dir():
        for path in evidence_root.glob('*.json'):
            if re.fullmatch('[0-9a-f]{64}\\.json', path.name) and not path.is_symlink() and path.stat().st_mtime < cutoff:
                path.unlink()


def request_spec(event, raw):
    root = payload_xml(raw)
    kind = event['message_type']
    if kind == 'image':
        node = root if root.tag == 'img' else root.find('./img')
        fields = ('cdnbigimgurl', 'aeskey', 'hdlength', 'md5')
        file_type = 1
        if node is not None and not node.get('cdnbigimgurl'):
            fields = ('cdnmidimgurl', 'aeskey', 'length', 'md5')
            file_type = 2
    elif kind == 'video':
        node = root if root.tag == 'videomsg' else root.find('./videomsg')
        fields = ('cdnvideourl', 'aeskey', 'length', 'md5')
        file_type = 4
    elif kind == 'file':
        app = root if root.tag == 'appmsg' else root.find('./appmsg')
        node = app.find('./appattach') if app is not None else None
        fields = ('cdnattachurl', 'aeskey', 'totallen', 'filemd5')
        file_type = 5
    else:
        raise ApiError(400, 'native_download_kind', '仅支持图片、视频、文件的原生CDN请求')
    def value(key):
        if node is None:
            return ''
        return (node.get(key) or node.findtext('./' + key) or '').strip()
    file_id, key, size, digest = [value(k) for k in fields]
    if kind == 'file' and not digest and app is not None:
        digest = (app.findtext('./md5') or '').strip()
    if (not file_id or len(file_id) > 4095 or not file_id.isascii() or
            any(ord(c) < 33 for c in file_id) or not re.fullmatch('[0-9a-fA-F]{32}', key) or
            not size.isdigit() or not 0 < int(size) <= LIMIT or not re.fullmatch('[0-9a-fA-F]{32}', digest)):
        raise ApiError(503, 'native_download_metadata_missing', '原生下载标识、密钥、原图长度或内容校验信息不完整')
    return {'file_id': file_id, 'aes_key': key, 'size': int(size), 'md5': digest.lower(), 'file_type': file_type}


def request_arguments(event, raw, prepare_only=False, attempt_id=''):
    from .native_assets import account_directory
    spec = request_spec(event, raw)
    identity = hashlib.sha256((event['account'] + '\0' + event['conversation_id'] + '\0' +
                               event['event_id'] + ('\0prepare' if prepare_only else '\0download') +
                               '\0native-account-cache-v1\0' + attempt_id).encode()).hexdigest()
    output = account_directory(event['account']) / 'msg/temp/client_hook_downloads' / identity / 'content'
    request_id = 'ch_' + identity[:56]
    arguments = (event['account'], request_id, spec['file_id'], spec['aes_key'], output, spec['size'], spec['file_type'])
    return arguments, output, spec


def query(event, raw, attempt_id=''):
    from hook_runtime.resource_rpc import invoke
    arguments, _, _ = request_arguments(event, raw, attempt_id=attempt_id)
    return invoke(*arguments, mode=2)


def finalize(arguments):
    from hook_runtime.resource_rpc import invoke
    result = invoke(*arguments, mode=3)
    until = time.monotonic() + 10
    while result['stage'] != 6 and time.monotonic() < until:
        time.sleep(.05)
        result = invoke(*arguments, mode=2)
    if result['stage'] != 6 or not result['cleanup_ok'] or result['thread_error'] or result['error']:
        raise ApiError(503, 'native_download_cleanup_unconfirmed', '原生下载已结束，但资源释放尚未确认')
    with _ACTIVE_LOCK:
        _ACTIVE.discard(str(arguments[4]))
    return result


def download(event, raw, prepare_only=False, timeout=45, attempt_id=None):
    from hook_runtime.resource_rpc import invoke
    # 每次显式获取创建一个新操作，状态轮询不创建操作。
    # 重试失败的获取时，不得复用已失效的原生 CDN 映射条目。
    attempt_id = uuid.uuid4().hex if attempt_id is None else attempt_id
    arguments, output, spec = request_arguments(event, raw, prepare_only, attempt_id)
    with _ACTIVE_LOCK:
        _ACTIVE.add(str(output))
    status = invoke(*arguments, mode=0 if prepare_only else 1)
    deadline = time.monotonic() + timeout
    while not status['done'] and status['thread_error'] in (0, 997):
        if time.monotonic() >= deadline:
            raise ApiError(504, 'native_download_submission_unknown', '原生下载提交结果尚未确认，不自动重发')
        time.sleep(.2)
        status = invoke(*arguments, mode=2)
    if status['thread_error'] or status['error'] or not status['cleanup_ok']:
        raise ApiError(503, 'native_download_submit_failed', '原生下载入口拒绝请求',
                       stage=status['stage'], native_error=status['thread_error'] or status['error'])
    if prepare_only:
        return finalize(arguments)
    while time.monotonic() < deadline:
        status = invoke(*arguments, mode=2)
        if status['native_done'] and (status['native_status_0'] or status['native_status_1']):
            finalize(arguments)
            raise ApiError(503, 'native_download_failed', '微信原生下载返回失败',
                           native_status_0=status['native_status_0'], native_status_1=status['native_status_1'])
        if output.is_file() and not output.is_symlink():
            size = output.stat().st_size
            if size > LIMIT:
                raise ApiError(503, 'native_download_oversize', '原生下载文件超过资源上限')
            if status['native_done'] and size > 0:
                data = output.read_bytes()
                message_hash_matches = hashlib.md5(data).hexdigest() == spec['md5']
                native_transfer_complete = status['total'] == status['transferred'] == len(data)
                # 缓存搜索需要消息 MD5，因为名称和大小不能
                # 证明身份。新下载已绑定到本事件的
                # 精确 CDN 文件 ID 与 AES 密钥。对于格式变体，必须检查实际嵌套的原生
                # 完成结果及完整字节数。
                if message_hash_matches or native_transfer_complete:
                    evidence = {'event_id': event['event_id'], 'message_type': event['message_type'],
                                'completed_at': time.time(), 'operation_id': arguments[1],
                                'binding': 'exact_native_event_cdn_id_and_key',
                                'source_md5_matches': message_hash_matches, 'source_size': spec['size'],
                                'actual_size': len(data), 'sha256': hashlib.sha256(data).hexdigest(),
                                'native': {k: status[k] for k in ('native_done', 'native_status_0',
                                    'native_status_1', 'transferred', 'total', 'callback_tid')}}
                    directory = ROOT / 'state/native-download-validation'
                    directory.mkdir(exist_ok=True)
                    import json
                    (directory / (hashlib.sha256(event['event_id'].encode()).hexdigest() + '.json')).write_text(
                        json.dumps(evidence, ensure_ascii=False, indent=2), encoding='utf-8')
                    finalize(arguments)
                    # 传输完成后不再需要此私有传输副本。
                    output.unlink()
                    return data
        time.sleep(.25)
    raise ApiError(504, 'native_download_incomplete', '微信原生下载未在时限内产生完整且内容校验匹配的资源',
                   native=status)
