"""
Windows DPAPI: a secret encrypted for this Windows user on this PC (CryptProtectData). The device's
private key is kept this way, so a copy of settings.json is worth nothing elsewhere — without it,
whoever copied the file could pass for this device, a remembered one, and get into other PCs
without a confirmation.
"""

import ctypes
import ctypes.wintypes as wt

ENTROPY = b"MarinRemote device key v1"
CRYPTPROTECT_UI_FORBIDDEN = 0x1


class _BLOB(ctypes.Structure):
    _fields_ = [("cbData", wt.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


_crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
_k32 = ctypes.WinDLL("kernel32")
_PROTO = [ctypes.POINTER(_BLOB), wt.LPCWSTR, ctypes.POINTER(_BLOB), ctypes.c_void_p, ctypes.c_void_p, wt.DWORD,
          ctypes.POINTER(_BLOB)]
_crypt32.CryptProtectData.argtypes = _PROTO
_crypt32.CryptUnprotectData.argtypes = [ctypes.POINTER(_BLOB), ctypes.c_void_p] + _PROTO[2:]
_k32.LocalFree.argtypes = [ctypes.c_void_p]


def _blob(data):
    buf = ctypes.create_string_buffer(bytes(data), len(data))
    return _BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), buf


def _take(out):
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        _k32.LocalFree(ctypes.cast(out.pbData, ctypes.c_void_p))


def protect(data, entropy=ENTROPY):
    src, keep1 = _blob(data)
    ent, keep2 = _blob(entropy)
    out = _BLOB()
    if not _crypt32.CryptProtectData(ctypes.byref(src), "MarinRemote", ctypes.byref(ent), None, None,
                                     CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(out)):
        raise OSError(f"CryptProtectData: ошибка {ctypes.get_last_error()}")
    return _take(out)


def unprotect(blob, entropy=ENTROPY):
    """The secret back — OSError if it was protected for another user, another PC or another purpose."""
    src, keep1 = _blob(blob)
    ent, keep2 = _blob(entropy)
    out = _BLOB()
    if not _crypt32.CryptUnprotectData(ctypes.byref(src), None, ctypes.byref(ent), None, None,
                                       CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(out)):
        raise OSError(f"CryptUnprotectData: ошибка {ctypes.get_last_error()}")
    return _take(out)
