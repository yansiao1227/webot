"""Pinned DLL loader and bounded, local receive window. No send operations."""
from __future__ import annotations
import argparse
import ctypes as C
from ctypes import wintypes as W
import hashlib
import json
import os
from pathlib import Path
import re
import struct
import sys
import threading
import time
import uuid

ROOT = Path(__file__).resolve().parents[2]
from hook_runtime.platform_meta import MetadataReader, MODULEENTRY32W, normalized
from hook_runtime.pe_local import PE
MODULE = Path(__file__).resolve().parents[1]


def output(item):
    print(json.dumps(item, ensure_ascii=False, separators=(',',':')), flush=True)


class Win(MetadataReader):
    def __init__(self):
        super().__init__()
        definitions = {
            'VirtualAllocEx': ([W.HANDLE,C.c_void_p,C.c_size_t,W.DWORD,W.DWORD],C.c_void_p),
            'VirtualFreeEx': ([W.HANDLE,C.c_void_p,C.c_size_t,W.DWORD],W.BOOL),
            'WriteProcessMemory': ([W.HANDLE,C.c_void_p,C.c_void_p,C.c_size_t,C.POINTER(C.c_size_t)],W.BOOL),
            'ReadProcessMemory': ([W.HANDLE,C.c_void_p,C.c_void_p,C.c_size_t,C.POINTER(C.c_size_t)],W.BOOL),
            'CreateRemoteThread': ([W.HANDLE,C.c_void_p,C.c_size_t,C.c_void_p,C.c_void_p,W.DWORD,C.c_void_p],W.HANDLE),
            'WaitForSingleObject': ([W.HANDLE,W.DWORD],W.DWORD),
            'GetExitCodeThread': ([W.HANDLE,C.POINTER(W.DWORD)],W.BOOL),
            'CreateEventW': ([C.c_void_p,W.BOOL,W.BOOL,W.LPCWSTR],W.HANDLE),
            'SetEvent': ([W.HANDLE],W.BOOL),
            'GetModuleHandleW': ([W.LPCWSTR],W.HMODULE),
            'GetProcAddress': ([W.HMODULE,C.c_char_p],C.c_void_p),
            'GetModuleHandleExW': ([W.DWORD,C.c_void_p,C.POINTER(W.HMODULE)],W.BOOL),
            'GetModuleFileNameW': ([W.HMODULE,W.LPWSTR,W.DWORD],W.DWORD),
            'CreateFileW': ([W.LPCWSTR,W.DWORD,W.DWORD,C.c_void_p,W.DWORD,W.DWORD,W.HANDLE],W.HANDLE),
            'GetCurrentProcess': ([],W.HANDLE),
            'CheckRemoteDebuggerPresent': ([W.HANDLE,C.POINTER(W.BOOL)],W.BOOL),
            'CreateMutexW': ([C.c_void_p,W.BOOL,W.LPCWSTR],W.HANDLE),
            'ReleaseMutex': ([W.HANDLE],W.BOOL),
        }
        for name,(args,result) in definitions.items():
            fn=getattr(self.api,name); fn.argtypes=args; fn.restype=result

    def modules(self,pid):
        h=self.snapshot(0x18,pid); result=[]
        try:
            m=MODULEENTRY32W();m.dwSize=C.sizeof(m)
            present=self.api.Module32FirstW(h,C.byref(m))
            while present:
                result.append((normalized(m.szExePath),int(m.modBaseAddr),m.modBaseSize))
                present=self.api.Module32NextW(h,C.byref(m))
            if C.get_last_error()!=18: raise C.WinError(C.get_last_error())
        finally: self.api.CloseHandle(h)
        return result

    def read(self,process,at,count):
        buf=C.create_string_buffer(count);done=C.c_size_t()
        if not self.api.ReadProcessMemory(process,at,buf,count,C.byref(done)) or done.value!=count:
            raise C.WinError(C.get_last_error())
        return buf.raw

    def allocate(self,process,data):
        at=self.api.VirtualAllocEx(process,None,len(data),0x3000,4)
        if not at: raise C.WinError(C.get_last_error())
        done=C.c_size_t();buf=C.create_string_buffer(data)
        if not self.api.WriteProcessMemory(process,at,buf,len(data),C.byref(done)) or done.value!=len(data):
            self.api.VirtualFreeEx(process,at,0,0x8000)
            raise C.WinError(C.get_last_error())
        return at

    def thread(self,process,at,arg):
        h=self.api.CreateRemoteThread(process,None,0,at,arg,0,None)
        if not h: raise C.WinError(C.get_last_error())
        return h

    def loader(self,pid,process):
        at=self.api.GetProcAddress(self.api.GetModuleHandleW('kernel32.dll'),b'LoadLibraryW')
        owner=W.HMODULE()
        if not at or not self.api.GetModuleHandleExW(6,at,C.byref(owner)):
            raise C.WinError(C.get_last_error())
        path=C.create_unicode_buffer(32768)
        if not self.api.GetModuleFileNameW(owner,path,len(path)): raise C.WinError(C.get_last_error())
        candidates=[m for m in self.modules(pid) if m[0]==normalized(path.value)]
        if len(candidates)!=1: raise RuntimeError('loader owner module mismatch')
        remote=candidates[0][1]+at-owner.value
        if self.read(process,remote,16)!=self.read(self.api.GetCurrentProcess(),at,16):
            raise RuntimeError('loader code mismatch')
        return remote


def export_rva(pe,name,code=True):
    rva,size=pe.directories[0]; at=pe.rva_to_offset(rva)
    count=pe.u32(at+24)
    functions=pe.rva_to_offset(pe.u32(at+28)); names=pe.rva_to_offset(pe.u32(at+32))
    ordinals=pe.rva_to_offset(pe.u32(at+36))
    for i in range(count):
        n=pe.rva_to_offset(pe.u32(names+4*i)); end=pe.data.index(b'\0',n)
        if pe.data[n:end]==name.encode():
            ordinal=struct.unpack_from('<H',pe.data,ordinals+2*i)[0]
            fn=pe.u32(functions+4*ordinal)
            if rva<=fn<rva+size: raise RuntimeError('forwarded runtime export rejected')
            if not any((s['executable'] if code else s['name'] in ('.data','.rdata','.bss')) and s['rva']<=fn<s['rva']+s['virtual_size'] for s in pe.sections):
                raise RuntimeError('runtime export is not code')
            return fn
    raise RuntimeError('runtime export missing')


def same_user(api, process):
    adv=C.WinDLL('advapi32',use_last_error=True)
    adv.OpenProcessToken.argtypes=[W.HANDLE,W.DWORD,C.POINTER(W.HANDLE)];adv.OpenProcessToken.restype=W.BOOL
    adv.GetTokenInformation.argtypes=[W.HANDLE,C.c_int,C.c_void_p,W.DWORD,C.POINTER(W.DWORD)];adv.GetTokenInformation.restype=W.BOOL
    adv.GetLengthSid.argtypes=[C.c_void_p];adv.GetLengthSid.restype=W.DWORD
    def sid(p):
        token=W.HANDLE()
        if not adv.OpenProcessToken(p,8,C.byref(token)): raise C.WinError(C.get_last_error())
        try:
            needed=W.DWORD();adv.GetTokenInformation(token,1,None,0,C.byref(needed))
            buf=C.create_string_buffer(needed.value)
            if not adv.GetTokenInformation(token,1,buf,len(buf),C.byref(needed)): raise C.WinError(C.get_last_error())
            pointer=C.c_void_p.from_buffer(buf).value
            return C.string_at(pointer,adv.GetLengthSid(pointer))
        finally: api.CloseHandle(token)
    if sid(process)!=sid(api.GetCurrentProcess()): raise RuntimeError('target user SID mismatch')


