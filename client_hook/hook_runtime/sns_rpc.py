"""固定版本 SNS 队列 RPC；每个意图绑定构建，查询不重新提交。"""
import ctypes as C
from ctypes import wintypes as W
import hashlib
import json
from pathlib import Path
import re
import threading
from types import SimpleNamespace

from hook_runtime.loader import Win, PE, export_rva, normalized, same_user
from hook_runtime.service import resolve_target

ROOT = Path(__file__).resolve().parents[1]
LOCK = threading.RLock()
_BUILDS = {}


class SnsRpc(C.Structure):
    _fields_ = [(n, W.DWORD) for n in ('magic', 'version', 'size', 'pid', 'mode', 'error', 'state',
                                     'submitted', 'completed', 'cleaned', 'calls', 'tid')] + [
        ('action', W.DWORD), ('status0', C.c_int32), ('status1', C.c_int32), ('post_id', C.c_uint64),
        ('server_post_id', C.c_uint64),
        ('account', C.c_char * 128), ('request_id', C.c_char * 64),
        ('author', C.c_char * 128), ('body', C.c_char * 65537)]


def invoke(account, request_id, post_id=0, mode=0, *, action=1, author='', body=''):
    if (not re.fullmatch(r'wxid_[A-Za-z0-9_-]{1,120}', account) or
            not re.fullmatch(r'[A-Za-z0-9_-]{1,63}', request_id) or
            type(post_id) is not int or not 0 <= post_id < 1 << 64 or mode not in (0, 1, 2) or
            action not in (1, 2, 3, 4) or not isinstance(body, str) or '\0' in body or len(body.encode('utf-8')) > 65536 or
            (author and not re.fullmatch(r'wxid_[A-Za-z0-9_-]{1,120}', author))):
        raise ValueError('SNS RPC 参数无效')
    with LOCK:
        if request_id not in _BUILDS:
            if len(_BUILDS) >= 4096:
                raise RuntimeError('SNS RPC 意图数已达上限')
            _BUILDS[request_id] = json.loads((ROOT / 'build/sns-current.json').read_text())
        pins = _BUILDS[request_id]
        directory = Path(pins['directory']).resolve(strict=True)
        if directory.parent != ROOT / 'build' or not directory.name.startswith('sns-'):
            raise ValueError('SNS 模块路径不符')
        dll = directory / pins['dll_name']
        pe = PE(str(dll))
        if hashlib.sha256(pe.data).hexdigest() != pins['dll_sha256']:
            raise ValueError('SNS 模块摘要不符')
        api = Win()
        target = resolve_target(api, SimpleNamespace(pid=None, fixture_pid=None))
        if hashlib.sha256(target['core'].read_bytes()).hexdigest() != pins['core_sha256']:
            raise ValueError('SNS 核心版本不符')
        process = api.api.OpenProcess(0x1000 | 0x400 | 2 | 8 | 0x10 | 0x20 | 0x100000, False, target['pid'])
        if not process:
            raise C.WinError(C.get_last_error())
        remote = thread = None
        pending = False
        try:
            same_user(api.api, process)
            if api.creation(process)[0] != target['created']:
                raise ValueError('SNS 进程实例已变化')
            matches = [m for m in api.modules(target['pid']) if m[0] == normalized(dll)]
            if not matches:
                if mode == 1:
                    raise RuntimeError('原 SNS 模块不在此进程，状态未知')
                remote = api.allocate(process, str(dll).encode('utf-16-le') + b'\0\0')
                thread = api.thread(process, api.loader(target['pid'], process), remote)
                pending = True
                if api.api.WaitForSingleObject(thread, 6000) != 0:
                    raise RuntimeError('SNS 加载状态未知，不重复加载')
                pending = False
                api.api.CloseHandle(thread); thread = None
                api.api.VirtualFreeEx(process, remote, 0, 0x8000); remote = None
                matches = [m for m in api.modules(target['pid']) if m[0] == normalized(dll)]
            if len(matches) != 1:
                raise RuntimeError('SNS 模块未唯一加载')
            entry = export_rva(pe, 'HookSns')
            if api.read(process, matches[0][1] + entry, 16) != pe.data[pe.rva_to_offset(entry):][:16]:
                raise ValueError('SNS RPC 入口不符')
            rpc = SnsRpc(magic=0x534e5331, version=3, size=C.sizeof(SnsRpc), pid=target['pid'],
                         mode=mode, account=account.encode('ascii'), request_id=request_id.encode('ascii'), post_id=post_id,
                         action=action, author=author.encode('ascii'), body=body.encode('utf-8'))
            remote = api.allocate(process, bytes(rpc))
            thread = api.thread(process, matches[0][1] + entry, remote)
            pending = True
            if api.api.WaitForSingleObject(thread, 6000) != 0:
                raise RuntimeError('SNS 提交状态未知，不自动重发')
            pending = False
            exit_code = W.DWORD()
            if not api.api.GetExitCodeThread(thread, C.byref(exit_code)):
                raise C.WinError(C.get_last_error())
            rpc = SnsRpc.from_buffer_copy(api.read(process, remote, C.sizeof(rpc)))
            return {n: int(getattr(rpc, n)) for n in ('error', 'state', 'submitted', 'completed', 'cleaned',
                    'calls', 'tid', 'status0', 'status1')} | {'thread_error': exit_code.value, 'pid': target['pid'],
                                                         'server_post_id': str(rpc.server_post_id)}
        finally:
            if not pending and remote:
                api.api.VirtualFreeEx(process, remote, 0, 0x8000)
            if thread:
                api.api.CloseHandle(thread)
            api.api.CloseHandle(process)
