"""固定版本原生已读封装 RPC；不卸载模块，不重试提交状态未知的操作。"""
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

ROOT=Path(__file__).resolve().parents[1]
LOCK=threading.RLock()
class ReadRpc(C.Structure):
    _fields_=[(n,W.DWORD) for n in ('magic','version','size','pid','mode','error','calls','submitted')]+[
        ('account',C.c_char*128),('request_id',C.c_char*64),('chat',C.c_char*128)]

def invoke(account,chat,request_id,mode=0):
    if (not re.fullmatch(r'wxid_[A-Za-z0-9_-]{1,120}',account) or
        not re.fullmatch(r'(?:wxid_[A-Za-z0-9_-]+|[0-9]+@chatroom)',chat) or len(chat)>127 or
        not re.fullmatch(r'read_[0-9a-f]{56}',request_id) or mode not in (0,1)):
        raise ValueError('已读身份参数无效')
    with LOCK:
        pins=json.loads((ROOT/'build/read-current.json').read_text())
        directory=Path(pins['directory']).resolve(strict=True)
        if directory.parent!=ROOT/'build' or not directory.name.startswith('read-'):
            raise ValueError('已读模块路径不符')
        dll=directory/pins['dll_name'];pe=PE(str(dll))
        if hashlib.sha256(pe.data).hexdigest()!=pins['dll_sha256']:
            raise ValueError('已读模块摘要不符')
        api=Win();target=resolve_target(api,SimpleNamespace(pid=None,fixture_pid=None))
        if hashlib.sha256(target['core'].read_bytes()).hexdigest()!=pins['core_sha256']:
            raise ValueError('微信核心摘要不符')
        process=api.api.OpenProcess(0x1000|0x400|2|8|0x10|0x20|0x100000,False,target['pid'])
        if not process:raise C.WinError(C.get_last_error())
        remote=thread=None;pending=False
        try:
            same_user(api.api,process)
            if api.creation(process)[0]!=target['created']:raise ValueError('微信进程已改变')
            matches=[m for m in api.modules(target['pid']) if m[0]==normalized(dll)]
            if not matches:
                if mode:raise ValueError('原已读模块不存在，状态未知')
                remote=api.allocate(process,str(dll).encode('utf-16-le')+b'\0\0')
                thread=api.thread(process,api.loader(target['pid'],process),remote);pending=True
                if api.api.WaitForSingleObject(thread,6000)!=0:raise RuntimeError('模块加载状态未知')
                pending=False;api.api.CloseHandle(thread);thread=None
                api.api.VirtualFreeEx(process,remote,0,0x8000);remote=None
                matches=[m for m in api.modules(target['pid']) if m[0]==normalized(dll)]
            if len(matches)!=1:raise RuntimeError('已读模块不唯一')
            entry=export_rva(pe,'HookMarkRead')
            if api.read(process,matches[0][1]+entry,16)!=pe.data[pe.rva_to_offset(entry):][:16]:
                raise RuntimeError('已读RPC入口不符')
            rpc=ReadRpc(magic=0x43524d31,version=1,size=C.sizeof(ReadRpc),pid=target['pid'],mode=mode,
                account=account.encode('ascii'),request_id=request_id.encode('ascii'),chat=chat.encode('ascii'))
            remote=api.allocate(process,bytes(rpc));thread=api.thread(process,matches[0][1]+entry,remote);pending=True
            if api.api.WaitForSingleObject(thread,6000)!=0:raise RuntimeError('原生已读提交未知，不自动重试')
            pending=False
            exit_code=W.DWORD()
            if not api.api.GetExitCodeThread(thread,C.byref(exit_code)):raise C.WinError(C.get_last_error())
            rpc=ReadRpc.from_buffer_copy(api.read(process,remote,C.sizeof(rpc)))
            return {'error':int(rpc.error),'thread_error':exit_code.value,'calls':int(rpc.calls),
                'submitted':bool(rpc.submitted),'pid':target['pid'],'request_id':request_id}
        finally:
            if not pending and remote:api.api.VirtualFreeEx(process,remote,0,0x8000)
            if thread:api.api.CloseHandle(thread)
            api.api.CloseHandle(process)
