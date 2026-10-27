"""本地 DPAPI 凭据存储，无需导入桥接模块源码。"""
import base64
import ctypes as c
from ctypes import wintypes as w
import os


def crypt(data, protect):
    if os.name != 'nt':
        raise RuntimeError('持久凭据仅支持Windows DPAPI；其他平台使用环境变量')
    class Blob(c.Structure):
        _fields_ = [('size', w.DWORD), ('data', c.POINTER(c.c_ubyte))]
    buffer = c.create_string_buffer(data)
    source = Blob(len(data), c.cast(buffer, c.POINTER(c.c_ubyte)))
    output = Blob()
    api = c.WinDLL('crypt32', use_last_error=True)
    kernel = c.WinDLL('kernel32', use_last_error=True)
    kernel.LocalFree.argtypes = [c.c_void_p]
    kernel.LocalFree.restype = c.c_void_p
    function = api.CryptProtectData if protect else api.CryptUnprotectData
    function.argtypes = [c.POINTER(Blob), c.c_void_p, c.c_void_p, c.c_void_p, c.c_void_p, w.DWORD, c.POINTER(Blob)]
    function.restype = w.BOOL
    if not function(c.byref(source), None, None, None, None, 1, c.byref(output)):
        raise RuntimeError('DPAPI操作失败')
    try:
        return c.string_at(output.data, output.size)
    finally:
        kernel.LocalFree(output.data)


def seal(value):
    return 'dpapi:' + base64.b64encode(crypt(value.encode(), True)).decode()


def unseal(value):
    if not value.startswith('dpapi:'):
        raise ValueError('不接受未保护的持久凭据')
    return crypt(base64.b64decode(value[6:], validate=True), False).decode()
