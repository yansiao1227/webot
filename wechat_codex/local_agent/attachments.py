"""通过 MCP 有界传输资源，不访问 Hook 数据库。"""
import asyncio
import base64
import hashlib
import math
import os
import re
import shutil
import time
from pathlib import Path

MAX_BYTES = 64 * 1024 * 1024


async def fetch(mcp, event_id, target, directory, timeout=60):
    result = await mcp.call('acquire_reference_resource', event_id=event_id)
    resource_id = result['id']
    deadline = time.monotonic() + timeout
    while result['state'] == 'running':
        if time.monotonic() >= deadline:
            raise ValueError('资源仍在获取，请稍后重新引用并请求处理')
        await asyncio.sleep(.25)
        result = await mcp.call('get_reference_resource', event_id=event_id, resource_id=resource_id)
    if result['state'] != 'ready':
        raise ValueError('资源不可用：' + str(result['state']) + '，原因：' + str(result.get('error') or '未知'))
    size, digest = result.get('size'), result.get('sha256')
    if isinstance(size, bool) or not isinstance(size, int) or not 0 < size <= MAX_BYTES:
        raise ValueError('附件为空或超过64MiB限制')
    root = Path(directory) / hashlib.sha256(event_id.encode()).hexdigest()
    root.mkdir(parents=True, exist_ok=True)
    name = Path(str(target['payload'].get('name', 'attachment.bin')).replace('\\', '/')).name
    # 不得将入站文件名用作目录或可执行指令。
    suffix = Path(name).suffix.lower()
    suffix = suffix if suffix.isascii() and len(suffix) <= 12 and suffix[1:].isalnum() else '.bin'
    path = root / ('content' + suffix)
    temporary = root / 'content.tmp'
    offset, checksum = 0, hashlib.sha256()
    try:
        with temporary.open('wb') as stream:
            while offset < size:
                if time.monotonic() >= deadline:
                    raise ValueError('资源传输超时')
                chunk = await mcp.call('get_reference_resource', event_id=event_id, resource_id=resource_id, offset=offset)
                data = base64.b64decode(chunk['data'], validate=True)
                if (not data or len(data) > 256 * 1024 or chunk.get('offset') != offset or
                        chunk.get('next_offset') != offset + len(data) or offset + len(data) > size):
                    raise ValueError('资源分块不完整')
                stream.write(data)
                checksum.update(data)
                offset += len(data)
        if checksum.hexdigest() != digest:
            raise ValueError('资源校验不匹配')
        temporary.replace(path)
        created = result.get('created', time.time())
        if isinstance(created, bool) or not isinstance(created, (int, float)) or not math.isfinite(created):
            raise ValueError('资源时间无效')
        captured = max(0, min(created, time.time()))
        os.utime(path, (captured, captured))
    finally:
        temporary.unlink(missing_ok=True)
    return {'path': str(path.resolve()), 'message_type': target['message_type'], 'name': name,
            'size': size, 'sha256': digest}


def prune(directory, protected=(), now=None):
    root = Path(directory)
    if not root.exists():
        return
    protected = {hashlib.sha256(eid.encode()).hexdigest() for eid in protected}
    cutoff = (time.time() if now is None else now) - 7 * 86400
    for folder in root.iterdir():
        if folder.is_symlink() or not folder.is_dir() or folder.name in protected or not re.fullmatch(r'[0-9a-f]{64}', folder.name):
            continue
        # 仅处理自有的固定缓存布局，不沿入站路径递归。
        entries = list(folder.rglob('*'))
        files = [f for f in entries if f.is_file()]
        # 递归前检查最终 Windows 删除目标及每个子项。
        if (folder.resolve().parent == root.resolve() and
                all(not f.is_symlink() and f.resolve().is_relative_to(folder.resolve()) for f in entries) and
                files and all(f.stat().st_mtime < cutoff for f in files)):
            shutil.rmtree(folder)
