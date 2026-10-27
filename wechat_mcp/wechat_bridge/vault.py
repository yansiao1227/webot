"""Windows 签名密钥以当前用户 DPAPI 封装；其他平台依赖状态目录权限。"""
import base64
import os


def _dpapi(data, protect):
    import ctypes as c
    from ctypes import wintypes as w
    class Blob(c.Structure):
        _fields_ = [('size', w.DWORD), ('data', c.POINTER(c.c_ubyte))]
    buffer = c.create_string_buffer(data)
    source = Blob(len(data), c.cast(buffer, c.POINTER(c.c_ubyte)))
    output = Blob()
    crypt = c.WinDLL('crypt32', use_last_error=True)
    kernel = c.WinDLL('kernel32', use_last_error=True)
    kernel.LocalFree.argtypes = [c.c_void_p]
    kernel.LocalFree.restype = c.c_void_p
    function = crypt.CryptProtectData if protect else crypt.CryptUnprotectData
    function.argtypes = [c.POINTER(Blob), c.c_void_p, c.c_void_p, c.c_void_p, c.c_void_p, w.DWORD, c.POINTER(Blob)]
    function.restype = w.BOOL
    if not function(c.byref(source), None, None, None, None, 1, c.byref(output)):
        raise OSError(c.get_last_error(), 'DPAPI 操作失败')
    try:
        return c.string_at(output.data, output.size)
    finally:
        kernel.LocalFree(output.data)


def seal(secret):
    raw = secret.encode()
    return ('dpapi:' if os.name == 'nt' else 'local:') + base64.b64encode(_dpapi(raw, True) if os.name == 'nt' else raw).decode()


def unseal(value):
    kind, raw = value.split(':', 1)
    data = base64.b64decode(raw)
    if kind == 'dpapi':
        if os.name != 'nt':
            raise ValueError('DPAPI 状态只能在原 Windows 用户下打开')
        data = _dpapi(data, False)
    return data.decode()
