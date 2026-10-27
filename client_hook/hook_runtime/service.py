"""由一个可复用驻留 DLL 支持的本地 NDJSON 接收接口。"""
from __future__ import annotations
import argparse
import ctypes as C
from ctypes import wintypes as W
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import time
import uuid
from hook_runtime.loader import ROOT,Win,normalized,PE,export_rva,same_user,output,MODULE

WORK=Path(os.environ.get('CLIENT_HOOK_BUILD_ROOT', str(MODULE/'build'))).resolve()
STATES={1:'idle',2:'starting',3:'running',4:'stopping',5:'draining',6:'fault'}

class Config(C.Structure):
    _fields_=[('magic',W.DWORD),('version',W.DWORD),('size',W.DWORD),('pid',W.DWORD),
        ('creation_time',C.c_uint64),('duration_ms',W.DWORD),('max_events',W.DWORD),
        ('owner_pid',W.DWORD),('reserved',W.DWORD),('owner_creation',C.c_uint64),
        ('exe_path',W.WCHAR*260),('core_path',W.WCHAR*260),('output_path',W.WCHAR*512),
        ('stop_name',W.WCHAR*128),('endpoint',C.c_char*128)]

class PublicStatus(C.Structure):
    _fields_=[('magic',W.DWORD),('version',W.DWORD),('size',W.DWORD),('state',W.DWORD),
        ('generation',C.c_uint64),('completed',C.c_uint64),('target_pid',W.DWORD),('owner_pid',W.DWORD),
        ('callbacks',W.LONG),('admitted',W.LONG),('entry_hits',W.LONG),('observed',W.LONG),
        ('filtered',W.LONG),('rejected',W.LONG),('dropped',W.LONG),('cancelled',W.LONG),
        ('run_busy',W.LONG),('installed',W.LONG),('last_return_rva',C.c_uint64),
        ('identity_changed',W.LONG),('reserved',W.LONG),('self_id',C.c_char*128)]

def atomic_json(path,value):
    temp=path.with_name(path.name+'.'+uuid.uuid4().hex+'.tmp')
    temp.write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf-8')
    temp.replace(path)

def build_at(directory):
    directory=Path(directory).resolve()
    if directory.parent!=WORK or not directory.name.startswith('runtime-'):
        raise RuntimeError('runtime directory outside fixed workspace')
    pins=json.loads((directory/'pins.json').read_text(encoding='utf-8-sig'))
    if Path(pins['directory']).resolve()!=directory: raise RuntimeError('build identity mismatch')
    return directory,pins

def current_build():
    pins=json.loads((WORK/'runtime-current.json').read_text(encoding='utf-8-sig'))
    return build_at(pins['directory'])

def resolve_target(api,args):
    directory,pins=current_build()
    preflight=None
    if args.fixture_pid:
        pid=args.fixture_pid
        # 先查询路径，确保 runtime-current 改变时测试桩仍然可用。
        h=api.api.OpenProcess(0x1000,False,pid)
        if not h: raise C.WinError(C.get_last_error())
        try:
            buf=C.create_unicode_buffer(32768);n=W.DWORD(len(buf))
            if not api.api.QueryFullProcessImageNameW(h,0,buf,C.byref(n)): raise C.WinError(C.get_last_error())
            exe=Path(buf.value).resolve()
        finally: api.api.CloseHandle(h)
        directory,pins=build_at(exe.parent)
        if exe.name!='hook-host.exe' or hashlib.sha256(exe.read_bytes()).hexdigest()!=pins['files'][exe.name]:
            raise RuntimeError('fixture executable pin mismatch')
        core=exe
    else:
        from hook_runtime.target import discover
        preflight=discover(api,getattr(args,'pid',None))
        pid=preflight['pid'];exe=Path(preflight['exe_path']);core=Path(preflight['core_path'])
    meta=api.inspect(pid,exe)
    if not meta: raise RuntimeError('target identity mismatch')
    created=int(meta['creation_filetime_100ns'])
    key=f'{pid}-{created}'
    resident_path=WORK/f'resident-{key}.json'
    active_path=WORK/f'active-{key}.json'
    reused=False
    if resident_path.exists():
        resident=json.loads(resident_path.read_text(encoding='utf-8'))
        if resident.get('pid')!=pid or int(resident.get('creation_time',0))!=created or resident.get('protocol')!=3:
            raise RuntimeError('resident registration identity/protocol mismatch')
        registered=Path(resident['dll']).resolve()
        if any(m[0]==normalized(registered) for m in api.modules(pid)):
            if registered.parent.parent != WORK:
                raise RuntimeError('微信仍加载旧项目路径的 Hook DLL，请正常退出微信后重新打开并登录，再运行 start_wechat.cmd')
            directory,pins=build_at(registered.parent);reused=True
            expected='hook-fixture.dll' if args.fixture_pid else 'hook-live.dll'
            if registered.name!=expected or resident['dll_sha256']!=pins['files'][expected]:
                raise RuntimeError('registered DLL pin mismatch')
    dll=directory/('hook-fixture.dll' if args.fixture_pid else 'hook-live.dll')
    return dict(pid=pid,created=created,exe=exe,core=core,meta=meta,preflight=preflight,
                directory=directory,pins=pins,dll=dll,resident_path=resident_path,
                active_path=active_path,reused=reused)

def read_status(api,process,base,pe):
    at=export_rva(pe,'HookStatus',code=False)
    raw=api.read(process,base+at,C.sizeof(PublicStatus))
    s=PublicStatus.from_buffer_copy(raw)
    if (s.magic,s.version,s.size)!=(0x43485231,3,C.sizeof(PublicStatus)):
        raise RuntimeError('resident DLL status ABI mismatch')
    result={name:getattr(s,name) for name,_ in PublicStatus._fields_}
    result['self_id']=s.self_id.decode('ascii')
    result['state']=STATES.get(s.state,'unknown')
    result['last_return_rva']=hex(s.last_return_rva)
    result.pop('reserved',None)
    return result

def inspect_status(args):
    api=Win();target=resolve_target(api,args);dll=target['dll'];pid=target['pid']
    modules=[m for m in api.modules(pid) if m[0]==normalized(dll)]
    if not modules: return {'type':'status','pid':pid,'loaded':False,'state':'not_loaded'}
    pe=PE(str(dll))
    if hashlib.sha256(pe.data).hexdigest()!=target['pins']['files'][dll.name]: raise RuntimeError('DLL pin mismatch')
    process=api.api.OpenProcess(0x1000|0x10,False,pid)
    if not process: raise C.WinError(C.get_last_error())
    try:
        same_user(api.api,process)
        if api.creation(process)[0]!=target['created']: raise RuntimeError('process instance changed')
        status=read_status(api,process,modules[0][1],pe)
        return dict(type='status',pid=pid,loaded=True,dll=str(dll),dll_base=hex(modules[0][1]),
                    snapshot_scope='individual_atomic_fields_not_group_atomic',**status)
    finally: api.api.CloseHandle(process)

def stop_current(args):
    api=Win();target=resolve_target(api,args);status=inspect_status(args)
    if status.get('state') in ('idle','not_loaded'):
        output({'type':'stop_requested','already_stopped':True,'pid':target['pid']});return 0
    if status.get('state') in ('fault','draining') and not status.get('run_busy'):
        output({'type':'stop_requested','already_stopped':True,'state':status['state'],'pid':target['pid']});return 0
    active=json.loads(target['active_path'].read_text(encoding='utf-8'))
    if active['pid']!=target['pid'] or int(active['creation_time'])!=target['created'] or not re.fullmatch(r'Local\\WeixinHookStop-[0-9a-f]{32}',active['stop_name']):
        raise RuntimeError('active session identity mismatch')
    api.api.OpenEventW.argtypes=[W.DWORD,W.BOOL,W.LPCWSTR];api.api.OpenEventW.restype=W.HANDLE
    stop=api.api.OpenEventW(2,False,active['stop_name'])
    if not stop: raise C.WinError(C.get_last_error())
    try:
        if not api.api.SetEvent(stop): raise C.WinError(C.get_last_error())
    finally: api.api.CloseHandle(stop)
    output({'type':'stop_requested','pid':target['pid'],'run_id':active['run_id']});return 0

def run(args):
    if os.name!='nt' or C.sizeof(C.c_void_p)!=8 or C.sizeof(Config)!=2496 or C.sizeof(PublicStatus)!=224:
        raise RuntimeError('Windows x64 v3 ABI required')
    api=Win();t=resolve_target(api,args)
    pid,created,dll,pins=t['pid'],t['created'],t['dll'],t['pins']
    run_id=uuid.uuid4().hex[:12];run_dir=t['directory']/('run-'+run_id);run_dir.mkdir()
    logfile=run_dir/'events.ndjson'
    if t['preflight']: atomic_json(run_dir/'preflight.json',t['preflight'])
    process=stop=worker=load_thread=lock=mutex=None
    remote_path=remote_cfg=None;mutex_owned=False
    pending='none';status=None;memory_before={};core_base=None
    message_count=0;last_event={};reused=False;dll_loaded=False
    try:
        lock=api.api.CreateFileW(str(dll),0x80000000,1,None,3,0x80,None)
        if lock in (None,C.c_void_p(-1).value): raise C.WinError(C.get_last_error())
        pe=PE(str(dll))
        if pe.machine!=0x8664 or hashlib.sha256(pe.data).hexdigest()!=pins['files'][dll.name]: raise RuntimeError('runtime DLL pin mismatch')
        entry=export_rva(pe,'HookRun');export_rva(pe,'HookStatus',code=False)
        process=api.api.OpenProcess(0x1000|0x400|2|8|0x10|0x20|0x100000,False,pid)
        if not process: raise C.WinError(C.get_last_error())
        same_user(api.api,process)
        if api.creation(process)[0]!=created: raise RuntimeError('process instance changed')
        debugged=W.BOOL()
        if not api.api.CheckRemoteDebuggerPresent(process,C.byref(debugged)) or debugged.value:
            raise RuntimeError('target debugger active or unavailable')
        C.set_last_error(0);mutex=api.api.CreateMutexW(None,True,f'Local\\ReWeChatReceive_{pid}_{created}')
        if not mutex: raise C.WinError(C.get_last_error())
        if C.get_last_error()==183: raise RuntimeError('another receive observer owns this process instance')
        mutex_owned=True
        if not args.fixture_pid:
            cores=[m for m in api.modules(pid) if m[0]==normalized(t['core'])]
            if len(cores)!=1: raise RuntimeError('core path not unique')
            core_base=cores[0][1]
            for rva,expected in ((0x9dbbf0,'5657534883ec204889ce84d2750980be'),(0x9dacd0,'e81b0f000090488b85600100004881c4')):
                value=api.read(process,core_base+rva,16).hex()
                if value!=expected: raise RuntimeError('live entry/call bytes mismatch')
                memory_before[hex(rva)]=value
        for module_path,_,_ in api.modules(pid):
            if Path(module_path).name in ('hook-live.dll','hook-fixture.dll') and module_path!=normalized(dll):
                raise RuntimeError('incompatible resident DLL loaded; restart client normally before upgrade')
        matches=[m for m in api.modules(pid) if m[0]==normalized(dll)]
        if matches: base=matches[0][1];reused=True
        else:
            remote_path=api.allocate(process,str(dll).encode('utf-16-le')+b'\0\0')
            load_thread=api.thread(process,api.loader(pid,process),remote_path);pending='LoadLibraryW'
            if api.api.WaitForSingleObject(load_thread,15000)!=0: raise RuntimeError('loader pending; memory retained')
            pending='none';api.api.CloseHandle(load_thread);load_thread=None
            api.api.VirtualFreeEx(process,remote_path,0,0x8000);remote_path=None
            matches=[m for m in api.modules(pid) if m[0]==normalized(dll)]
            if len(matches)!=1: raise RuntimeError('DLL not present after load')
            base=matches[0][1]
        if api.read(process,base+entry,16)!=pe.data[pe.rva_to_offset(entry):][:16]: raise RuntimeError('runtime code mismatch')
        dll_loaded=True
        public=read_status(api,process,base,pe)
        if public['run_busy'] or public['state']=='fault': raise RuntimeError('resident engine busy or faulted')
        registration=dict(protocol=3,pid=pid,creation_time=str(created),dll=str(dll),dll_sha256=pins['files'][dll.name])
        atomic_json(t['resident_path'],registration)
        stop_name='Local\\WeixinHookStop-'+uuid.uuid4().hex
        stop=api.api.CreateEventW(None,True,False,stop_name)
        if not stop: raise C.WinError(C.get_last_error())
        owner_created=api.creation(api.api.GetCurrentProcess())[0]
        cfg=Config(0x43485231,3,C.sizeof(Config),pid,created,args.duration*1000,args.max_events,
                   os.getpid(),0,owner_created,str(t['exe']),str(t['core']),str(logfile),stop_name,args.endpoint.encode('ascii'))
        active=dict(registration,stop_name=stop_name,run_id=run_id,run_dir=str(run_dir),owner_pid=os.getpid(),owner_creation=str(owner_created))
        remote_cfg=api.allocate(process,bytes(cfg))
        worker=api.thread(process,base+entry,remote_cfg);pending='HookRun'
        atomic_json(t['active_path'],active)
        output({'type':'loading','pid':pid,'event_file':str(logfile),'duration':args.duration,
                'run_id':run_id,'dll_reused':reused,'dll_base':hex(base)})
        def stdin_stop():
            try:
                for line in sys.stdin:
                    if line.strip()=='stop': api.api.SetEvent(stop);return
            except (OSError,ValueError): pass
            finally:
                # API 父进程退出会关闭管道，否则此子进程可能
                # 失去所有者后仍存活，并永久持有 DLL 租约。
                api.api.SetEvent(stop)
        threading.Thread(target=stdin_stop,daemon=True).start()
        consumed=0;buffer=b'';started=time.monotonic();stopping=False
        initial_logfile=logfile;retired_logs=[]
        def consume(chunk):
            nonlocal buffer,message_count,last_event,remote_cfg,logfile,consumed
            buffer+=chunk
            while b'\n' in buffer:
                line,buffer=buffer.split(b'\n',1)
                if line:
                    item=json.loads(line.decode('utf-8'));last_event=item
                    if item.get('type')=='rotation':
                        index=item['index']
                        if type(index) is not int or not 1<=index<=100000000:
                            raise RuntimeError('invalid event file rotation')
                        retired_logs.append(logfile)
                        logfile=Path(str(initial_logfile)+'.'+str(index));consumed=0
                        continue
                    # ready 证明工作线程已复制不含指针的配置。
                    # 现在即可释放 RPC 存储；即使长会话的
                    # 控制器之后被终止、未执行 finally，也不会泄漏该存储。
                    if item.get('type')=='ready' and remote_cfg:
                        if not api.api.VirtualFreeEx(process,remote_cfg,0,0x8000):
                            raise C.WinError(C.get_last_error())
                        remote_cfg=None
                    if item.get('type')=='message': message_count+=1
                    output(item)
            if len(buffer)>524288: raise RuntimeError('oversized runtime event line')
        while True:
            for old in retired_logs[:]:
                try:
                    old.unlink(missing_ok=True);retired_logs.remove(old)
                except OSError: pass
            if len(retired_logs)>2: raise RuntimeError('cannot reclaim consumed runtime logs')
            if logfile.exists():
                with logfile.open('rb') as file:
                    file.seek(consumed);chunk=file.read(65536);consumed+=len(chunk)
                consume(chunk)
            rc=api.api.WaitForSingleObject(worker,50)
            if rc==0:
                pending='none'
                while logfile.exists():
                    drained_path=logfile
                    with logfile.open('rb') as file:
                        file.seek(consumed)
                        while chunk:=file.read(65536):
                            consumed+=len(chunk);consume(chunk)
                    if logfile==drained_path: break
                break
            if rc!=258: raise RuntimeError('worker wait failed')
            if args.duration and time.monotonic()-started>args.duration+5 and not stopping:
                api.api.SetEvent(stop);stopping=True
            if args.duration and time.monotonic()-started>args.duration+15: raise RuntimeError('worker did not exit; config retained')
        exit_code=W.DWORD()
        if not api.api.GetExitCodeThread(worker,C.byref(exit_code)): raise C.WinError(C.get_last_error())
        status=exit_code.value
        if last_event.get('type')!='end':
            raise RuntimeError(f'worker rejected start with code {status}; busy/draining=5, fault=6')
        if not last_event.get('restored') or last_event.get('active')!=0:
            raise RuntimeError('session restoration/drain not confirmed')
        if core_base:
            after={rva:api.read(process,core_base+int(rva,0),16).hex() for rva in memory_before}
            atomic_json(run_dir/'memory-check.json',dict(before=memory_before,after=after,unchanged=memory_before==after))
            if after!=memory_before: raise RuntimeError('independent instruction restoration failed')
        public=read_status(api,process,base,pe)
        output({'type':'verified_end','event_file':str(logfile),'messages':message_count,'code':status,
                'generation':last_event.get('generation'),'dll_reused':reused,'dll_base':hex(base),
                'state':public['state'],'dll_resident':True})
        return 0 if status==0 else 4
    finally:
        if stop and worker and pending=='HookRun':
            api.api.SetEvent(stop)
            if api.api.WaitForSingleObject(worker,5000)==0: pending='none'
        if process and pending=='none':
            for at in (remote_cfg,remote_path):
                if at: api.api.VirtualFreeEx(process,at,0,0x8000)
        if mutex_owned: api.api.ReleaseMutex(mutex)
        for h in (worker,load_thread,stop,process,lock,mutex):
            if h and h!=C.c_void_p(-1).value: api.api.CloseHandle(h)
        atomic_json(run_dir/'controller.json',dict(pid=pid,worker_exit=status,pending=pending,
            event_file=str(logfile),dll_resident=dll_loaded,dll_reused=reused))

def main():
    p=argparse.ArgumentParser(description=__doc__)
    sub=p.add_subparsers(dest='command',required=True)
    events=sub.add_parser('events',aliases=['start'])
    events.add_argument('--fixture-pid',type=int)
    events.add_argument('--endpoint',default='*')
    events.add_argument('--pid',type=int)
    events.add_argument('--duration',type=int,default=0,help='0 = until stop/controller exit')
    events.add_argument('--max-events',type=int,default=0,help='0 = unlimited')
    for name in ('status','stop'):
        parser=sub.add_parser(name);parser.add_argument('--fixture-pid',type=int);parser.add_argument('--pid',type=int)
    argv=sys.argv[1:]
    if argv and argv[0].startswith('-') and argv[0] not in ('-h','--help'): argv=['events']+argv
    args=p.parse_args(argv)
    if args.command in ('events','start') and (not 0<=args.duration<=600 or not 0<=args.max_events<=1000 or
        not re.fullmatch(r'(?:\*|wxid_[A-Za-z0-9_-]+|[0-9]+@chatroom)',args.endpoint) or len(args.endpoint)>127):
        p.error('invalid endpoint or duration/event bound')
    try:
        if args.command=='status': output(inspect_status(args));return 0
        if args.command=='stop': return stop_current(args)
        return run(args)
    except (OSError,ValueError,RuntimeError,KeyError) as error:
        output({'type':'controller_error','error':str(error)});return 4
