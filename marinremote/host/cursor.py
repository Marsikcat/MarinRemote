"""
The host's mouse pointer, sent apart from the picture: where it is (often, by datagram) and what
it looks like (when it changes, on the control stream). The viewer draws it over the video, or
uses it as its own pointer while controlling.

A shape is drawn by Windows itself (DrawIconEx) twice — on black and on white. How much the
background shows through gives the alpha; the drawing on black is the colour, already
premultiplied. That covers colour, monochrome and animated pointers alike. "Invert the screen"
pixels (the classic text I-beam) can't be drawn as such: they become white with a dark outline.
"""

import base64
import ctypes
import ctypes.wintypes as wt
import hashlib
import zlib

import numpy as np

CURSOR_SHOWING = 1
DI_NORMAL = 3


class _CURSORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wt.DWORD), ("flags", wt.DWORD), ("hCursor", wt.HANDLE), ("pt", wt.POINT)]


class _ICONINFO(ctypes.Structure):
    _fields_ = [("fIcon", wt.BOOL), ("xHotspot", wt.DWORD), ("yHotspot", wt.DWORD),
                ("hbmMask", wt.HBITMAP), ("hbmColor", wt.HBITMAP)]


class _BITMAP(ctypes.Structure):
    _fields_ = [("bmType", ctypes.c_long), ("bmWidth", ctypes.c_long), ("bmHeight", ctypes.c_long),
                ("bmWidthBytes", ctypes.c_long), ("bmPlanes", wt.WORD), ("bmBitsPixel", wt.WORD),
                ("bmBits", ctypes.c_void_p)]


class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wt.DWORD), ("biWidth", ctypes.c_long), ("biHeight", ctypes.c_long),
                ("biPlanes", wt.WORD), ("biBitCount", wt.WORD), ("biCompression", wt.DWORD),
                ("biSizeImage", wt.DWORD), ("biXPelsPerMeter", ctypes.c_long),
                ("biYPelsPerMeter", ctypes.c_long), ("biClrUsed", wt.DWORD), ("biClrImportant", wt.DWORD)]


def _prototypes():
    """Handles can be above 2**31: every call that takes one must say it is a handle."""
    try:
        user32, gdi32 = ctypes.windll.user32, ctypes.windll.gdi32
    except AttributeError:
        return
    user32.GetIconInfo.argtypes = [wt.HANDLE, ctypes.POINTER(_ICONINFO)]
    user32.GetDC.restype = wt.HDC
    user32.ReleaseDC.argtypes = [wt.HWND, wt.HDC]
    user32.DrawIconEx.argtypes = [wt.HDC, ctypes.c_int, ctypes.c_int, wt.HANDLE, ctypes.c_int, ctypes.c_int,
                                  wt.UINT, wt.HBRUSH, wt.UINT]
    gdi32.GetObjectW.argtypes = [wt.HANDLE, ctypes.c_int, ctypes.c_void_p]
    gdi32.DeleteObject.argtypes = [wt.HANDLE]
    gdi32.CreateCompatibleDC.argtypes = [wt.HDC]
    gdi32.CreateCompatibleDC.restype = wt.HDC
    gdi32.DeleteDC.argtypes = [wt.HDC]
    gdi32.CreateDIBSection.argtypes = [wt.HDC, ctypes.c_void_p, wt.UINT, ctypes.POINTER(ctypes.c_void_p),
                                       wt.HANDLE, wt.DWORD]
    gdi32.CreateDIBSection.restype = wt.HBITMAP
    gdi32.SelectObject.argtypes = [wt.HDC, wt.HANDLE]
    gdi32.SelectObject.restype = wt.HANDLE


_prototypes()


def current():
    """(visible, x, y, handle) — x, y in physical pixels of the virtual desktop."""
    info = _CURSORINFO()
    info.cbSize = ctypes.sizeof(info)
    try:
        if not ctypes.windll.user32.GetCursorInfo(ctypes.byref(info)):
            return False, 0, 0, 0
    except OSError:
        return False, 0, 0, 0
    return bool(info.flags & CURSOR_SHOWING), info.pt.x, info.pt.y, int(info.hCursor or 0)


def _render(handle, w, h, background, step=0):
    """The cursor (frame `step` of an animated one) drawn by Windows onto a w×h square of
    `background` (0 or 255): BGRA rows."""
    user32, gdi32 = ctypes.windll.user32, ctypes.windll.gdi32
    screen = user32.GetDC(None)
    hdc = gdi32.CreateCompatibleDC(screen)
    bih = _BITMAPINFOHEADER()
    bih.biSize = ctypes.sizeof(bih)
    bih.biWidth, bih.biHeight, bih.biPlanes, bih.biBitCount = w, -h, 1, 32
    bits = ctypes.c_void_p()
    hbm = gdi32.CreateDIBSection(hdc, ctypes.byref(bih), 0, ctypes.byref(bits), None, 0)
    try:
        if not hbm or not bits.value:
            return None
        old = gdi32.SelectObject(hdc, hbm)
        ctypes.memset(bits, background, w * h * 4)
        ok = user32.DrawIconEx(hdc, 0, 0, handle, w, h, step, None, DI_NORMAL)
        gdi32.GdiFlush()
        out = np.frombuffer((ctypes.c_ubyte * (w * h * 4)).from_address(bits.value), np.uint8)
        out = out.reshape(h, w, 4).copy()
        gdi32.SelectObject(hdc, old)
        return out if ok else None
    finally:
        if hbm:
            gdi32.DeleteObject(hbm)
        gdi32.DeleteDC(hdc)
        user32.ReleaseDC(None, screen)


def combine(on_black, on_white):
    """Two drawings of a pointer → premultiplied BGRA (see the module notes)."""
    b = on_black[..., :3].astype(np.int16)
    w = on_white[..., :3].astype(np.int16)
    alpha = np.clip(255 - (w - b).max(axis=2), 0, 255).astype(np.uint8)
    out = np.zeros(on_black.shape, np.uint8)
    out[..., :3] = np.minimum(b, alpha[..., None]).astype(np.uint8)   # premultiplied: colour ≤ alpha
    out[..., 3] = alpha
    inverted = (b - w).min(axis=2) > 64          # lighter on black than on white
    if inverted.any():
        out[inverted] = (255, 255, 255, 255)
        ring = np.zeros_like(inverted)
        for dy, dx in ((-1, 0), (1, 0), (0, -1), (0, 1), (-1, -1), (-1, 1), (1, -1), (1, 1)):
            ring |= np.roll(np.roll(inverted, dy, 0), dx, 1)
        ring &= ~inverted & (alpha < 128)
        out[ring] = (0, 0, 0, 255)
    return out


def shape(handle):
    """{"id", "w", "h", "hx", "hy", "bgra": premultiplied bytes} of a cursor handle, or None."""
    if not handle:
        return None
    user32, gdi32 = ctypes.windll.user32, ctypes.windll.gdi32
    ii = _ICONINFO()
    if not user32.GetIconInfo(handle, ctypes.byref(ii)):
        return None
    try:
        bm = _BITMAP()
        gdi32.GetObjectW(ii.hbmMask, ctypes.sizeof(bm), ctypes.byref(bm))
        w = bm.bmWidth
        h = bm.bmHeight if ii.hbmColor else bm.bmHeight // 2     # monochrome: AND over XOR mask
    finally:
        for hbm in (ii.hbmMask, ii.hbmColor):
            if hbm:
                gdi32.DeleteObject(hbm)
    if not (0 < w <= 256 and 0 < h <= 256):
        return None
    img = None
    for step in (0, 8, 4, 12, 2):           # an animated pointer may start with an empty frame
        black, white = _render(handle, w, h, 0, step), _render(handle, w, h, 255, step)
        if black is None or white is None:
            return None
        img = combine(black, white)
        if img[..., 3].any():
            break
    data = img.tobytes()
    ident = hashlib.sha1(data + bytes([ii.xHotspot & 255, ii.yHotspot & 255])).hexdigest()[:12]
    return {"id": ident, "w": w, "h": h, "hx": int(ii.xHotspot), "hy": int(ii.yHotspot), "bgra": data}


def packed(sh):
    """The shape for the control stream: pixels zlib-compressed, base64."""
    return {"id": sh["id"], "w": sh["w"], "h": sh["h"], "hx": sh["hx"], "hy": sh["hy"],
            "bgra": base64.b64encode(zlib.compress(sh["bgra"], 6)).decode("ascii")}


def unpacked(msg):
    """(w, h, hx, hy, premultiplied BGRA bytes) from a cursor message with pixels, or None."""
    try:
        w, h = int(msg["w"]), int(msg["h"])
        if not (0 < w <= 256 and 0 < h <= 256):
            return None
        d = zlib.decompressobj()
        data = d.decompress(base64.b64decode(msg["bgra"]), w * h * 4)     # never more than that
        if len(data) != w * h * 4 or d.unconsumed_tail:
            return None
        return w, h, int(msg.get("hx", 0)), int(msg.get("hy", 0)), data
    except (KeyError, ValueError, TypeError, zlib.error):
        return None
