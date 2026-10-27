"""Self-contained Win32 metadata support, derived from this project's wxcli."""
from __future__ import annotations
import ctypes
from ctypes import wintypes
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
def normalized(path) -> str:
    value = str(Path(path).resolve(strict=False))
    if value.startswith("\\\\?\\UNC\\"):
        value = "\\\\" + value[8:]
    elif value.startswith("\\\\?\\"):
        value = value[4:]
    return os.path.normcase(value)


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ProcessID", wintypes.DWORD),
                ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", wintypes.LONG),
                ("dwFlags", wintypes.DWORD), ("szExeFile", wintypes.WCHAR * 260)]


class MODULEENTRY32W(ctypes.Structure):
    _fields_ = [("dwSize", wintypes.DWORD), ("th32ModuleID", wintypes.DWORD), ("th32ProcessID", wintypes.DWORD),
                ("GlblcntUsage", wintypes.DWORD), ("ProccntUsage", wintypes.DWORD), ("modBaseAddr", ctypes.c_void_p),
                ("modBaseSize", wintypes.DWORD), ("hModule", wintypes.HMODULE),
                ("szModule", wintypes.WCHAR * 256), ("szExePath", wintypes.WCHAR * 260)]


class MetadataReader:
    """Only OS metadata APIs; no target-memory or remote-execution API."""
    def __init__(self):
        self.api = ctypes.WinDLL("kernel32", use_last_error=True)
        definitions = {
            "CreateToolhelp32Snapshot": ([wintypes.DWORD, wintypes.DWORD], wintypes.HANDLE),
            "Process32FirstW": ([wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)], wintypes.BOOL),
            "Process32NextW": ([wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)], wintypes.BOOL),
            "Module32FirstW": ([wintypes.HANDLE, ctypes.POINTER(MODULEENTRY32W)], wintypes.BOOL),
            "Module32NextW": ([wintypes.HANDLE, ctypes.POINTER(MODULEENTRY32W)], wintypes.BOOL),
            "OpenProcess": ([wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE),
            "CloseHandle": ([wintypes.HANDLE], wintypes.BOOL),
            "GetProcessId": ([wintypes.HANDLE], wintypes.DWORD),
            "QueryFullProcessImageNameW": ([wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)], wintypes.BOOL),
            "GetProcessTimes": ([wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4, wintypes.BOOL),
            "GetExitCodeProcess": ([wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)], wintypes.BOOL),
        }
        for name, (args, result) in definitions.items():
            function = getattr(self.api, name)
            function.argtypes, function.restype = args, result

    def snapshot(self, flags, pid=0):
        for _ in range(3):
            handle = self.api.CreateToolhelp32Snapshot(flags, pid)
            if handle != ctypes.c_void_p(-1).value:
                return handle
            if ctypes.get_last_error() != 24:  # ERROR_BAD_LENGTH
                break
        raise ctypes.WinError(ctypes.get_last_error())

    def named_processes(self, name):
        snapshot = self.snapshot(0x2)  # TH32CS_SNAPPROCESS
        entries = []
        try:
            entry = PROCESSENTRY32W()
            entry.dwSize = ctypes.sizeof(entry)
            present = self.api.Process32FirstW(snapshot, ctypes.byref(entry))
            while present:
                if entry.szExeFile.casefold() == name.casefold():
                    entries.append(entry.th32ProcessID)
                present = self.api.Process32NextW(snapshot, ctypes.byref(entry))
            if ctypes.get_last_error() != 18:  # ERROR_NO_MORE_FILES
                raise ctypes.WinError(ctypes.get_last_error())
        finally:
            self.api.CloseHandle(snapshot)
        return sorted(entries)

    def creation(self, handle):
        times = [wintypes.FILETIME() for _ in range(4)]
        if not self.api.GetProcessTimes(handle, *(ctypes.byref(value) for value in times)):
            raise ctypes.WinError(ctypes.get_last_error())
        ticks = times[0].dwLowDateTime | times[0].dwHighDateTime << 32
        date = datetime(1601, 1, 1, tzinfo=timezone.utc) + timedelta(microseconds=ticks // 10)
        return ticks, date.isoformat(timespec="microseconds").replace("+00:00", "Z")

    def inspect(self, pid, expected_exe):
        handle = self.api.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION only
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            created, created_utc = self.creation(handle)
            path = ctypes.create_unicode_buffer(32768)
            length = wintypes.DWORD(len(path))
            if not self.api.QueryFullProcessImageNameW(handle, 0, path, ctypes.byref(length)):
                raise ctypes.WinError(ctypes.get_last_error())
            if normalized(path.value) != normalized(expected_exe):
                return None
            modules = []
            snapshot = self.snapshot(0x8 | 0x10, pid)
            try:
                entry = MODULEENTRY32W()
                entry.dwSize = ctypes.sizeof(entry)
                present = self.api.Module32FirstW(snapshot, ctypes.byref(entry))
                while present:
                    modules.append({"path": entry.szExePath, "size_bytes": entry.modBaseSize})
                    present = self.api.Module32NextW(snapshot, ctypes.byref(entry))
                if ctypes.get_last_error() != 18:
                    raise ctypes.WinError(ctypes.get_last_error())
            finally:
                self.api.CloseHandle(snapshot)
            exit_code = wintypes.DWORD()
            if (self.api.GetProcessId(handle) != pid or self.creation(handle)[0] != created or
                    not self.api.GetExitCodeProcess(handle, ctypes.byref(exit_code)) or exit_code.value != 259):
                raise RuntimeError("process_changed_or_exited")
            return {"pid": pid, "creation_time_utc": created_utc, "creation_filetime_100ns": str(created),
                    "process_instance_id": f"{pid}:{created}", "exe_path": path.value, "modules": modules}
        finally:
            self.api.CloseHandle(handle)


