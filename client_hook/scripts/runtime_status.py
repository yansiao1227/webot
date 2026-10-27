"""Read only our DLL's fixed diagnostic counters. Never reads chat records."""

from pathlib import Path
import sys

MODULE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MODULE_DIR.parent))
sys.path.insert(0, str(MODULE_DIR))

import argparse
import ctypes as C
from tool.settings import executable_setting, path_setting

import hashlib
import json
import re
import struct
import subprocess
from receiver import ROOT, Win, normalized, PE, export_rva
from hook_runtime.service import WORK


def inspect(run_dir):
    run_dir=Path(run_dir).resolve()
    if run_dir.parent.parent != WORK or not run_dir.name.startswith('run-'):
        raise ValueError('run directory outside fixed runtime workspace')
    pins=json.loads((run_dir.parent/'pins.json').read_text(encoding='utf-8-sig'))
    preflight=json.loads((run_dir/'preflight.json').read_text(encoding='utf-8'))
    pid=preflight['pid'];dll=run_dir.parent/'hook-live.dll'
    pe=PE(str(dll))
    if hashlib.sha256(pe.data).hexdigest()!=pins['files'][dll.name]: raise ValueError('DLL hash mismatch')
    api=Win();meta=api.inspect(pid,Path(preflight['exe_path']))
    if not meta or meta['creation_filetime_100ns']!=preflight['creation_filetime_100ns']:
        raise ValueError('target process instance changed')
    modules=[m for m in api.modules(pid) if m[0]==normalized(dll)]
    if len(modules)!=1: raise ValueError('fixed runtime DLL is not uniquely loaded')
    try:
        export_rva(pe,'HookStatus',code=False)
    except RuntimeError:
        pass # Retained v1 evidence uses private diagnostic symbols.
    else:
        from hook_runtime.service import read_status
        process=api.api.OpenProcess(0x1000|0x10,False,pid)
        if not process: raise C.WinError(C.get_last_error())
        try:
            status=read_status(api,process,modules[0][1],pe)
            return {'schema':'client-hook-counter-status-v3','pid':pid,'read_only':True,
                    'scope':'own_runtime_public_status_only','status':status}
        finally: api.api.CloseHandle(process)
    sizes={name:4 for name in ('accepting','active','entry_hits','route_missed','force_missed',
                              'observed','filtered','rejected','dropped','identity_changed')}
    sizes['last_return_rva']=8
    symbols={}
    raw=subprocess.check_output([executable_setting('RE_WECHAT_NM', 'nm'),'--defined-only',str(dll)],text=True)
    for line in raw.splitlines():
        m=re.fullmatch(r'([0-9a-fA-F]+)\s+[bBdD]\s+(\w+)',line)
        if m and m[2] in sizes: symbols[m[2]]=int(m[1],16)-pe.image_base
    if set(symbols)!=set(sizes): raise ValueError('fixed diagnostic symbols incomplete')
    process=api.api.OpenProcess(0x1000|0x10,False,pid)
    if not process: raise C.WinError(C.get_last_error())
    try:
        values={}
        for name,rva in symbols.items():
            if not any(s['name'] in ('.data','.bss') and s['rva']<=rva and
                       rva+sizes[name]<=s['rva']+s['virtual_size'] for s in pe.sections):
                raise ValueError('counter symbol outside fixed data section')
            value=api.read(process,modules[0][1]+rva,sizes[name])
            values[name]=int.from_bytes(value,'little')
        values['last_return_rva']=hex(values['last_return_rva'])
        return {'schema':'client-hook-counter-status-v1','pid':pid,'read_only':True,
                'scope':'own_runtime_counters_only_non_atomic_group_snapshot','counters':values}
    finally: api.api.CloseHandle(process)


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('run_dir');args=p.parse_args()
    print(json.dumps(inspect(args.run_dir)))
