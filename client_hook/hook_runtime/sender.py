"""固定 profile 驻留引擎的文本 RPC。仅调用本模块构建的 HookSend。"""
import ctypes as C
from ctypes import wintypes as W
import hashlib
import struct
import os
import json
from pathlib import Path

from hook_runtime.loader import Win, PE, export_rva, normalized, same_user
from hook_runtime.service import resolve_target

_SIDECAR_BUILDS = {}


class SendRpc(C.Structure):
    _fields_ = [('magic', W.DWORD), ('version', W.DWORD), ('size', W.DWORD),
                ('pid', W.DWORD), ('query', W.DWORD), ('error', W.DWORD),
                ('request_id', C.c_char * 64), ('account', C.c_char * 128),
                ('target', C.c_char * 128), ('body', C.c_char * 65537),
                ('result', C.c_uint64 * 36)]


class SendRpcV4(C.Structure):
    _fields_ = SendRpc._fields_ + [('native_client_id', C.c_char * 129)]


def send_rpc(args, request_id, account, target, body, query=False):
    api = Win()
    t = resolve_target(api, args)
    sidecar = os.environ.get('CLIENT_HOOK_SEND_SIDECAR') == '1' and not getattr(args, 'fixture_pid', None)
    if sidecar:
        root = Path(__file__).resolve().parents[1]
        key = (account, request_id)
        if key not in _SIDECAR_BUILDS:
            if len(_SIDECAR_BUILDS) >= 65536:
                raise RuntimeError('sender sidecar operation limit')
            _SIDECAR_BUILDS[key] = json.loads((root / 'build/resource-current.json').read_text())
        pins = _SIDECAR_BUILDS[key]
        directory = Path(pins['directory']).resolve(strict=True)
        if directory.parent != root / 'build' or not directory.name.startswith('resource-'):
            raise RuntimeError('sender sidecar directory mismatch')
        if hashlib.sha256(t['core'].read_bytes()).hexdigest() != pins['core_sha256']:
            raise RuntimeError('sender sidecar core mismatch')
        t['dll'] = directory / pins['dll_name']
        t['pins'] = {'files':{pins['dll_name']:pins['dll_sha256']},'text_rpc_version':4}
    pe = PE(str(t['dll']))
    if hashlib.sha256(pe.data).hexdigest() != t['pins']['files'][t['dll'].name]:
        raise RuntimeError('sender DLL hash mismatch')
    modules = [m for m in api.modules(t['pid']) if m[0] == normalized(t['dll'])]
    if len(modules) != 1 and not sidecar:
        raise RuntimeError('receiver must load the resident engine before send')
    entry = export_rva(pe, 'HookSend')
    process = api.api.OpenProcess(0x1000 | 0x400 | 2 | 8 | 0x10 | 0x20 | 0x100000, False, t['pid'])
    if not process:
        raise C.WinError(C.get_last_error())
    remote = thread = None
    finished = False
    try:
        same_user(api.api, process)
        if api.creation(process)[0] != t['created']:
            raise RuntimeError('sender target instance changed')
        if sidecar and not modules:
            remote = api.allocate(process, str(t['dll']).encode('utf-16-le') + b'\0\0')
            thread = api.thread(process, api.loader(t['pid'],process),remote)
            if api.api.WaitForSingleObject(thread,15000) != 0:
                return {'state':'unknown','error':'sender_module_loading_unknown','allocation_retained':True}
            api.api.CloseHandle(thread); thread=None
            api.api.VirtualFreeEx(process,remote,0,0x8000);remote=None
            modules=[m for m in api.modules(t['pid']) if m[0]==normalized(t['dll'])]
        if len(modules)!=1:
            raise RuntimeError('sender sidecar not loaded')
        if api.read(process, modules[0][1] + entry, 16) != pe.data[pe.rva_to_offset(entry):][:16]:
            raise RuntimeError('sender export bytes mismatch')
        version = t['pins'].get('text_rpc_version', 3)
        rpc_type = SendRpcV4 if version == 4 else SendRpc
        rpc = rpc_type(0x43535433, version, C.sizeof(rpc_type), t['pid'], int(query), 0,
                      request_id.encode('ascii'), account.encode('ascii'),
                      target.encode('ascii'), body.encode('utf-8'))
        remote = api.allocate(process, bytes(rpc))
        thread = api.thread(process, modules[0][1] + entry, remote)
        if api.api.WaitForSingleObject(thread, 6000) != 0:
            # The remote thread may still be using this allocation. Never free it
            # or submit again on the assumption that timeout means cancellation.
            return {'error': 'rpc_thread_pending', 'state': 'unknown', 'allocation_retained': True}
        finished = True
        rpc = rpc_type.from_buffer_copy(api.read(process, remote, C.sizeof(rpc)))
        diag = bytes(rpc.result)
        get32 = lambda at: struct.unpack_from('<I', diag, at)[0]
        result = {'win32_error': rpc.error, 'callback_done': bool(get32(80)),
                  'callback_tid': get32(64), 'invocations': get32(68),
                  'queue_gate': get32(72), 'posted': bool(get32(76)), 'fn_consumed': bool(get32(88)),
                  'native_submit_invocations': get32(224), 'local_accepted': bool(get32(248)),
                  'cleanup_ok': bool(get32(184)), 'input_consumed': bool(get32(240)),
                  'out_cleaned': bool(get32(244)), 'error_phase': get32(108),
                  'key_length': struct.unpack_from('<Q', diag, 128)[0],
                  'body_length': struct.unpack_from('<Q', diag, 144)[0]}
        if version == 4 and rpc.native_client_id:
            result['native_client_id'] = bytes(rpc.native_client_id).decode('utf-8', 'strict')
            result['native_client_id_source'] = 'weixin41930_send_object_618'
        if sidecar:
            result['sender_sidecar_sha256'] = t['pins']['files'][t['dll'].name]
        if sidecar or t['pins'].get('send_task_observer'):
            result['native_identity_diagnostic_profile'] = struct.unpack_from('<Q',diag,272)[0]
            result['native_record_110_candidate'] = str(struct.unpack_from('<Q',diag,280)[0])
            observer_pinned = pins.get('task_observer') if sidecar else t['pins'].get('send_task_observer')
            if observer_pinned and result['native_identity_diagnostic_profile']==0x41930002:
                result['native_sort_seq']=result['native_record_110_candidate']
                result['native_sort_seq_source']='weixin41930_send_task_record110_v1'
        if rpc.error == 21:
            result['state'] = 'preparing'
        elif rpc.error == 997:
            result['state'] = 'pending'
        elif rpc.error == 0 and result['local_accepted'] and result['cleanup_ok']:
            result['state'] = 'accepted'
        else:
            uncertain = (result['native_submit_invocations'] and not result['cleanup_ok']) or (
                result['fn_consumed'] and not result['callback_done'])
            result['state'] = 'unknown' if uncertain else 'failed'
        return result
    finally:
        if finished and remote:
            api.api.VirtualFreeEx(process, remote, 0, 0x8000)
        if thread:
            api.api.CloseHandle(thread)
        api.api.CloseHandle(process)
