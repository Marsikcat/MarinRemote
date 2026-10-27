"""
Monitor capture through Windows.Graphics.Capture (Windows 10 1903+), for monitors where desktop
duplication keeps being taken back (one on another video adapter, say) — instead of GDI.

Unlike GDI it is fed by the compositor itself, so it costs little, and it hands out a frame only
when the picture changed: a still screen costs next to nothing here too. Plain ctypes, no extra
packages: WinRT activation (combase), a capture item for the monitor (IGraphicsCaptureItemInterop
::CreateForMonitor), a free-threaded frame pool on our own D3D11 device; each new frame is copied
into a CPU-readable texture and handed out as BGRA pixels. The pointer is left out (it travels
separately) and, on Windows 11, so is the yellow capture border.

Taken from MarinCall's window capture (wgc.py) and turned to monitors.
"""

import ctypes
import ctypes.wintypes as wt
import uuid

import numpy as np

HRESULT = ctypes.c_long
PIXEL_BGRA = 87                       # DirectXPixelFormat / DXGI_FORMAT B8G8R8A8_UNORM


class GUID(ctypes.Structure):
    _fields_ = [("Data1", ctypes.c_uint32), ("Data2", ctypes.c_uint16), ("Data3", ctypes.c_uint16),
                ("Data4", ctypes.c_ubyte * 8)]

    @classmethod
    def of(cls, text):
        g = cls()
        ctypes.memmove(ctypes.byref(g), uuid.UUID(text).bytes_le, 16)
        return g


class SizeInt32(ctypes.Structure):
    _fields_ = [("Width", ctypes.c_int32), ("Height", ctypes.c_int32)]


class _TextureDesc(ctypes.Structure):
    _fields_ = [("Width", wt.UINT), ("Height", wt.UINT), ("MipLevels", wt.UINT), ("ArraySize", wt.UINT),
                ("Format", wt.UINT), ("SampleCount", wt.UINT), ("SampleQuality", wt.UINT),
                ("Usage", wt.UINT), ("BindFlags", wt.UINT), ("CPUAccessFlags", wt.UINT),
                ("MiscFlags", wt.UINT)]


class _Mapped(ctypes.Structure):
    _fields_ = [("pData", ctypes.c_void_p), ("RowPitch", wt.UINT), ("DepthPitch", wt.UINT)]


IID_ITEM_INTEROP = GUID.of("3628E81B-3CAC-4C60-B7F4-23CE0E0C3356")
IID_ITEM = GUID.of("79C3F95B-31F7-4EC2-A464-632EF5D30760")
IID_POOL_STATICS2 = GUID.of("589B103F-6BBC-5DF5-A991-02E28B3B66D5")
IID_SESSION2 = GUID.of("2C39AE40-7D2E-5044-804E-8B6799D4CF9E")
IID_SESSION3 = GUID.of("F2CDD966-22AE-5EA1-9596-3A289344C3BE")
IID_D3D_DEVICE = GUID.of("A37624AB-8D5F-4650-9D3E-9EAE3D9BC670")
IID_DXGI_ACCESS = GUID.of("A9B3D012-3DF2-4EE3-B8D1-8695F457D3C1")
IID_DXGI_DEVICE = GUID.of("54EC77FA-1377-44E6-8C32-88FD5F44C84C")
IID_TEXTURE2D = GUID.of("6F15AAF2-D208-4E89-9AB4-489535D34F9C")
IID_CLOSABLE = GUID.of("30D5A829-7FA4-4026-83BB-D75BAE4EA99E")


def _fn(ptr, index, restype, *argtypes):
    vtbl = ctypes.cast(ptr, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    return ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)(vtbl[index])


def _call(ptr, index, argtypes, *args):
    hr = _fn(ptr, index, HRESULT, *argtypes)(ptr, *args)
    if hr < 0:
        raise OSError(f"WinRT 0x{hr & 0xFFFFFFFF:08X}")
    return hr


def _release(ptr):
    if ptr:
        _fn(ptr, 2, ctypes.c_ulong)(ptr)


def _qi(ptr, iid):
    out = ctypes.c_void_p()
    _call(ptr, 0, [ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p)], ctypes.byref(iid), ctypes.byref(out))
    return out


def _close(ptr):
    """IClosable.Close: give a frame back to the pool now / end a session (then Release)."""
    if not ptr:
        return
    try:
        closable = _qi(ptr, IID_CLOSABLE)
        _call(closable, 6, [])
        _release(closable)
    except OSError:
        pass


def _factory(class_name, iid):
    combase = ctypes.windll.combase
    combase.WindowsCreateString.argtypes = [wt.LPCWSTR, ctypes.c_uint32, ctypes.POINTER(ctypes.c_void_p)]
    combase.RoGetActivationFactory.argtypes = [ctypes.c_void_p, ctypes.POINTER(GUID),
                                               ctypes.POINTER(ctypes.c_void_p)]
    combase.WindowsDeleteString.argtypes = [ctypes.c_void_p]
    hs = ctypes.c_void_p()
    combase.WindowsCreateString(class_name, len(class_name), ctypes.byref(hs))
    out = ctypes.c_void_p()
    hr = combase.RoGetActivationFactory(hs, ctypes.byref(iid), ctypes.byref(out))
    combase.WindowsDeleteString(hs)
    if hr < 0 or not out:
        raise OSError(f"нет {class_name} (0x{hr & 0xFFFFFFFF:08X})")
    return out


def monitor_at(x, y):
    """The HMONITOR of the monitor holding a point of the virtual desktop (physical pixels)."""
    user32 = ctypes.windll.user32
    user32.MonitorFromPoint.argtypes = [wt.POINT, wt.DWORD]
    user32.MonitorFromPoint.restype = wt.HMONITOR
    return user32.MonitorFromPoint(wt.POINT(int(x), int(y)), 2)          # MONITOR_DEFAULTTONEAREST


class MonitorCapture:
    """One monitor, frame by frame: grab() -> BGRA ndarray (h, w, 4), or None if nothing new."""

    def __init__(self, hmonitor):
        self._objs = []
        self._staging = ctypes.c_void_p()
        self._staging_size = (0, 0)
        ctypes.windll.combase.RoInitialize(1)                         # multithreaded; fine if done
        d3d11 = ctypes.windll.d3d11
        d3d11.D3D11CreateDevice.argtypes = [ctypes.c_void_p, wt.UINT, ctypes.c_void_p, wt.UINT, ctypes.c_void_p,
                                            wt.UINT, wt.UINT, ctypes.POINTER(ctypes.c_void_p),
                                            ctypes.POINTER(wt.UINT), ctypes.POINTER(ctypes.c_void_p)]
        self.device, self.context, level = ctypes.c_void_p(), ctypes.c_void_p(), wt.UINT()
        hr = d3d11.D3D11CreateDevice(None, 1, None, 0x20, None, 0, 7, ctypes.byref(self.device),
                                     ctypes.byref(level), ctypes.byref(self.context))   # hardware, BGRA
        if hr < 0:
            raise OSError(f"D3D11 0x{hr & 0xFFFFFFFF:08X}")
        self._objs += [self.context, self.device]
        dxgi = _qi(self.device, IID_DXGI_DEVICE)
        self._objs.append(dxgi)
        d3d11.CreateDirect3D11DeviceFromDXGIDevice.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
        inspectable = ctypes.c_void_p()
        hr = d3d11.CreateDirect3D11DeviceFromDXGIDevice(dxgi, ctypes.byref(inspectable))
        if hr < 0:
            raise OSError(f"WinRT device 0x{hr & 0xFFFFFFFF:08X}")
        self._objs.append(inspectable)
        self.rt_device = _qi(inspectable, IID_D3D_DEVICE)
        self._objs.append(self.rt_device)

        interop = _factory("Windows.Graphics.Capture.GraphicsCaptureItem", IID_ITEM_INTEROP)
        self._objs.append(interop)
        self.item = ctypes.c_void_p()
        _call(interop, 4, [wt.HMONITOR, ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p)],
              hmonitor, ctypes.byref(IID_ITEM), ctypes.byref(self.item))     # CreateForMonitor
        self._objs.append(self.item)
        self.size = SizeInt32()
        _call(self.item, 7, [ctypes.POINTER(SizeInt32)], ctypes.byref(self.size))   # get_Size

        statics = _factory("Windows.Graphics.Capture.Direct3D11CaptureFramePool", IID_POOL_STATICS2)
        self._objs.append(statics)
        self.pool = ctypes.c_void_p()
        _call(statics, 6, [ctypes.c_void_p, ctypes.c_int, ctypes.c_int32, SizeInt32,
                           ctypes.POINTER(ctypes.c_void_p)],
              self.rt_device, PIXEL_BGRA, 2, self.size, ctypes.byref(self.pool))      # CreateFreeThreaded
        self.session = ctypes.c_void_p()
        _call(self.pool, 10, [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)],
              self.item, ctypes.byref(self.session))                                 # CreateCaptureSession
        self._set_flag(IID_SESSION2, False)               # IsCursorCaptureEnabled: the pointer goes apart
        self._set_flag(IID_SESSION3, False)               # IsBorderRequired: no yellow frame (Windows 11)
        _call(self.session, 6, [])                                                   # StartCapture

    def _set_flag(self, iid, value):
        try:
            iface = _qi(self.session, iid)
        except OSError:
            return
        try:
            _call(iface, 7, [ctypes.c_ubyte], 1 if value else 0)
        except OSError:
            pass
        _release(iface)

    def grab(self):
        frame = ctypes.c_void_p()
        _call(self.pool, 7, [ctypes.POINTER(ctypes.c_void_p)], ctypes.byref(frame))  # TryGetNextFrame
        if not frame:
            return None
        surface = access = texture = ctypes.c_void_p()
        try:
            content = SizeInt32()
            _call(frame, 8, [ctypes.POINTER(SizeInt32)], ctypes.byref(content))      # get_ContentSize
            surface = ctypes.c_void_p()
            _call(frame, 6, [ctypes.POINTER(ctypes.c_void_p)], ctypes.byref(surface))  # get_Surface
            access = _qi(surface, IID_DXGI_ACCESS)
            texture = ctypes.c_void_p()
            _call(access, 3, [ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p)],
                  ctypes.byref(IID_TEXTURE2D), ctypes.byref(texture))
            desc = _TextureDesc()
            _fn(texture, 10, None, ctypes.POINTER(_TextureDesc))(texture, ctypes.byref(desc))   # GetDesc
            self._ensure_staging(desc)
            _fn(self.context, 47, None, ctypes.c_void_p, ctypes.c_void_p)(self.context, self._staging, texture)
            mapped = _Mapped()
            _call(self.context, 14, [ctypes.c_void_p, wt.UINT, wt.UINT, wt.UINT, ctypes.POINTER(_Mapped)],
                  self._staging, 0, 1, 0, ctypes.byref(mapped))                     # Map(READ)
            try:
                w = max(2, min(content.Width, desc.Width))
                h = max(2, min(content.Height, desc.Height))
                rows = (ctypes.c_ubyte * (mapped.RowPitch * h)).from_address(mapped.pData)
                pixels = np.frombuffer(rows, np.uint8).reshape(h, mapped.RowPitch)[:, :w * 4]
                image = pixels.reshape(h, w, 4).copy()
            finally:
                _fn(self.context, 15, None, ctypes.c_void_p, wt.UINT)(self.context, self._staging, 0)  # Unmap
        finally:
            for p in (texture, access, surface):
                _release(p)
            _close(frame)
            _release(frame)
        if (content.Width, content.Height) != (self.size.Width, self.size.Height) \
                and content.Width > 1 and content.Height > 1:
            # the monitor's mode changed: the pool makes frames of the new size from now on
            self.size = SizeInt32(content.Width, content.Height)
            _call(self.pool, 6, [ctypes.c_void_p, ctypes.c_int, ctypes.c_int32, SizeInt32],
                  self.rt_device, PIXEL_BGRA, 2, self.size)                          # Recreate
        return image

    def _ensure_staging(self, desc):
        if self._staging and self._staging_size == (desc.Width, desc.Height):
            return
        _release(self._staging)
        d = _TextureDesc(desc.Width, desc.Height, 1, 1, PIXEL_BGRA, 1, 0, 3, 0, 0x20000, 0)  # STAGING, READ
        self._staging = ctypes.c_void_p()
        _call(self.device, 5, [ctypes.POINTER(_TextureDesc), ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)],
              ctypes.byref(d), None, ctypes.byref(self._staging))                   # CreateTexture2D
        self._staging_size = (desc.Width, desc.Height)

    def close(self):
        _close(self.session)
        _release(self.session)
        _close(self.pool)
        _release(self.pool)
        _release(self._staging)
        for p in reversed(self._objs):
            _release(p)
        self._objs = []
        self.session = self.pool = self._staging = ctypes.c_void_p()


def supported():
    """Can this Windows capture this way? (10 1903+, a GPU that does D3D11)"""
    try:
        cls = _factory("Windows.Graphics.Capture.GraphicsCaptureItem", IID_ITEM_INTEROP)
        _release(cls)
        return True
    except OSError:
        return False
