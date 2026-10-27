"""Pinned sidecar RPC. No native detours, dynamic instrumentation or unloads."""
import ctypes as C
from ctypes import wintypes as W
import hashlib
import json
from pathlib import Path
import threading
from types import SimpleNamespace

from hook_runtime.loader import Win, PE, export_rva, normalized, same_user
from hook_runtime.service import resolve_target

LOCK = threading.RLock()
ROOT = Path(__file__).resolve().parents[1]
_BUILDS = {}


class ResourceRpc(C.Structure):
    _fields_ = [(n, W.DWORD) for n in ('magic', 'version', 'size', 'pid', 'mode', 'error', 'stage', 'calls')] + [
        ('account', C.c_char * 128), ('request_id', C.c_char * 64), ('file_id', C.c_char * 4096),
        ('aes_key', C.c_char * 128), ('output', C.c_char * 2048), ('expected_size', C.c_uint64)] + [
        (n, W.DWORD) for n in ('file_type', 'done', 'callback_tid', 'cleanup_ok',
                              'native_done', 'native_status_0', 'native_status_1', 'future_state')] + [
        ('transferred', C.c_uint64), ('total', C.c_uint64)] + [
        (n, W.DWORD) for n in ('task_found', 'task_state', 'task_phase', 'task_count')] + [('native_md5', C.c_char * 33)]


def invoke(account, request_id, file_id, aes_key, output, size, file_type, mode=1):
    with LOCK:
        return _invoke(account, request_id, file_id, aes_key, output, size, file_type, mode)


def _invoke(account, request_id, file_id, aes_key, output, size, file_type, mode):
    pins = _BUILDS.get(request_id)
    if pins is None:
        pins = json.loads((ROOT / 'build/resource-current.json').read_text())
        if len(_BUILDS) >= 4096:
            raise RuntimeError('下载操作记录达到本进程上限')
        _BUILDS[request_id] = pins
    directory = Path(pins['directory']).resolve(strict=True)
    if directory.parent != ROOT / 'build' or not directory.name.startswith('resource-'):
        raise RuntimeError('下载模块路径不符')
    dll = directory / pins['dll_name']
    pe = PE(str(dll))
    if hashlib.sha256(pe.data).hexdigest() != pins['dll_sha256']:
        raise RuntimeError('下载模块摘要不符')
    output = Path(output).resolve()
    from api.native_assets import account_directory
    owned = (account_directory(account) / 'msg/temp/client_hook_downloads').resolve()
    if output.parent.parent != owned or output.name != 'content' or len(output.parent.name) != 64:
        raise ValueError('下载路径不属于受控资源目录')
    output.parent.mkdir(parents=True, exist_ok=True)
    if mode == 1 and not output.exists():
        with output.open('xb'):
            pass
    api = Win()
    target = resolve_target(api, SimpleNamespace(pid=None, fixture_pid=None))
    if hashlib.sha256(target['core'].read_bytes()).hexdigest() != pins['core_sha256']:
        raise RuntimeError('下载核心版本不符')
    process = api.api.OpenProcess(0x1000 | 0x400 | 2 | 8 | 0x10 | 0x20 | 0x100000, False, target['pid'])
    if not process:
        raise C.WinError(C.get_last_error())
    remote = thread = None
    pending = False
    try:
        same_user(api.api, process)
        if api.creation(process)[0] != target['created']:
            raise RuntimeError('下载进程实例已改变')
        modules = api.modules(target['pid'])
        matches = [m for m in modules if m[0] == normalized(dll)]
        # Every version has a distinct name and remains resident. No existing
        # hook, callback, allocation or module is replaced or unloaded.
        if not matches:
            remote = api.allocate(process, str(dll).encode('utf-16-le') + b'\0\0')
            thread = api.thread(process, api.loader(target['pid'], process), remote)
            pending = True
            if api.api.WaitForSingleObject(thread, 15000) != 0:
                raise RuntimeError('下载模块加载状态未知，保留参数，不重复加载')
            pending = False
            api.api.CloseHandle(thread); thread = None
            api.api.VirtualFreeEx(process, remote, 0, 0x8000); remote = None
            matches = [m for m in api.modules(target['pid']) if m[0] == normalized(dll)]
        if len(matches) != 1:
            raise RuntimeError('下载模块未加载')
        entry = export_rva(pe, 'HookResource')
        if api.read(process, matches[0][1] + entry, 16) != pe.data[pe.rva_to_offset(entry):][:16]:
            raise RuntimeError('下载入口不匹配')
        rpc = ResourceRpc(magic=0x43524431, version=5, size=C.sizeof(ResourceRpc), pid=target['pid'],
                          mode=mode, account=account.encode('ascii'), request_id=request_id.encode('ascii'),
                          file_id=file_id.encode('ascii'), aes_key=aes_key.encode('ascii'),
                          output=str(output).encode('utf-8'), expected_size=size, file_type=file_type)
        remote = api.allocate(process, bytes(rpc))
        thread = api.thread(process, matches[0][1] + entry, remote)
        pending = True
        if api.api.WaitForSingleObject(thread, 6000) != 0:
            raise RuntimeError('下载提交状态未知，保留参数，不自动重发')
        pending = False
        exit_code = W.DWORD()
        if not api.api.GetExitCodeThread(thread, C.byref(exit_code)):
            raise C.WinError(C.get_last_error())
        rpc = ResourceRpc.from_buffer_copy(api.read(process, remote, C.sizeof(rpc)))
        return {n: int(getattr(rpc, n)) for n in ('error', 'stage', 'calls', 'done', 'callback_tid', 'cleanup_ok',
                'native_done', 'native_status_0', 'native_status_1', 'future_state', 'transferred', 'total',
                'task_found', 'task_state', 'task_phase', 'task_count')} | {
            'thread_error': exit_code.value, 'pid': target['pid'], 'native_md5': rpc.native_md5.decode('ascii')}
    finally:
        if not pending and remote:
            api.api.VirtualFreeEx(process, remote, 0, 0x8000)
        if thread:
            api.api.CloseHandle(thread)
        api.api.CloseHandle(process)
