"""Small pieces of Win32 shared by the host and the encoder process: monitors, DPI awareness,
tying child processes to our lifetime."""

import ctypes
import ctypes.wintypes as wt
import os

_job = None


class _BASIC_LIMITS(ctypes.Structure):
    _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", wt.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wt.DWORD),
                ("Affinity", ctypes.c_size_t), ("PriorityClass", wt.DWORD), ("SchedulingClass", wt.DWORD)]


class _EXTENDED_LIMITS(ctypes.Structure):
    _fields_ = [("BasicLimitInformation", _BASIC_LIMITS), ("IoInfo", ctypes.c_uint64 * 6),
                ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]


def tie_to_us(proc):
    """If we die (a crash, Task Manager) Windows ends the child too, instead of leaving it
    capturing the screen and loading the GPU until the next reboot."""
    global _job
    if os.name != "nt":
        return
    try:
        k32 = ctypes.windll.kernel32
        k32.CreateJobObjectW.restype = wt.HANDLE
        k32.AssignProcessToJobObject.argtypes = [wt.HANDLE, wt.HANDLE]
        if _job is None:
            job = k32.CreateJobObjectW(None, None)
            limits = _EXTENDED_LIMITS()
            limits.BasicLimitInformation.LimitFlags = 0x2000          # KILL_ON_JOB_CLOSE
            k32.SetInformationJobObject(wt.HANDLE(job), 9, ctypes.byref(limits), ctypes.sizeof(limits))
            _job = job
        k32.AssignProcessToJobObject(_job, int(proc._handle))
    except (OSError, AttributeError):
        pass


def dpi_aware():
    """Physical pixels everywhere (monitor rects, the cursor, SendInput): per-monitor v2."""
    try:
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    except (OSError, AttributeError):
        pass


# ── monitors ───────────────────────────────────────────────────────
class _MONITORINFOEXW(ctypes.Structure):
    _fields_ = [("cbSize", wt.DWORD), ("rcMonitor", wt.RECT), ("rcWork", wt.RECT),
                ("dwFlags", wt.DWORD), ("szDevice", wt.WCHAR * 32)]


class _DXGI_OUTPUT_DESC(ctypes.Structure):
    _fields_ = [("DeviceName", wt.WCHAR * 32), ("DesktopCoordinates", wt.RECT),
                ("AttachedToDesktop", wt.BOOL), ("Rotation", wt.UINT), ("Monitor", wt.HMONITOR)]


class _GUID(ctypes.Structure):
    _fields_ = [("a", ctypes.c_uint32), ("b", ctypes.c_uint16), ("c", ctypes.c_uint16),
                ("d", ctypes.c_ubyte * 8)]


def _vcall(obj, index, proto, *args):
    vtbl = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    return proto(vtbl[index])(obj, *args)


def dxgi_outputs():
    """{device name: output index} for the default adapter, in ddagrab's output_idx order."""
    HR = ctypes.c_long
    enum_p = ctypes.WINFUNCTYPE(HR, ctypes.c_void_p, ctypes.c_uint, ctypes.POINTER(ctypes.c_void_p))
    desc_p = ctypes.WINFUNCTYPE(HR, ctypes.c_void_p, ctypes.POINTER(_DXGI_OUTPUT_DESC))
    release_p = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)
    out = {}
    try:
        iid = _GUID(0x770aae78, 0xf26f, 0x4dba, (ctypes.c_ubyte * 8)(0xa8, 0x29, 0x25, 0x3c, 0x83, 0xd1, 0xb3, 0x87))
        factory = ctypes.c_void_p()
        if ctypes.windll.dxgi.CreateDXGIFactory1(ctypes.byref(iid), ctypes.byref(factory)) != 0:
            return out
        adapter = ctypes.c_void_p()
        if _vcall(factory, 7, enum_p, 0, ctypes.byref(adapter)) == 0:     # EnumAdapters(0)
            i = 0
            while True:
                output = ctypes.c_void_p()
                if _vcall(adapter, 7, enum_p, i, ctypes.byref(output)) != 0:  # EnumOutputs(i)
                    break
                d = _DXGI_OUTPUT_DESC()
                _vcall(output, 7, desc_p, ctypes.byref(d))                    # GetDesc
                _vcall(output, 2, release_p)
                out[d.DeviceName] = i
                i += 1
            _vcall(adapter, 2, release_p)
        _vcall(factory, 2, release_p)
    except (OSError, AttributeError, ValueError):
        pass
    return out


def monitors():
    """The displays, primary first then left to right, in physical pixels:
    [{id, dev, name, x, y, w, h, primary, dxgi}] — dxgi is ddagrab's output index or None
    (a display on another video adapter)."""
    user32 = ctypes.windll.user32
    found = []
    proto = ctypes.WINFUNCTYPE(wt.BOOL, wt.HMONITOR, wt.HDC, ctypes.POINTER(wt.RECT), wt.LPARAM)

    def cb(hmon, _hdc, _rect, _):
        info = _MONITORINFOEXW()
        info.cbSize = ctypes.sizeof(info)
        if user32.GetMonitorInfoW(hmon, ctypes.byref(info)):
            r = info.rcMonitor
            found.append({"dev": info.szDevice, "x": r.left, "y": r.top, "w": r.right - r.left,
                          "h": r.bottom - r.top, "primary": bool(info.dwFlags & 1)})
        return True

    user32.EnumDisplayMonitors(None, None, proto(cb), 0)
    outputs = dxgi_outputs()
    found.sort(key=lambda m: (not m["primary"], m["x"], m["y"]))
    for i, m in enumerate(found):
        m["id"] = i
        m["dxgi"] = outputs.get(m["dev"])
        m["name"] = f"Экран {i + 1}" + (" (основной)" if m["primary"] else "")
    return found


def virtual_screen():
    """The rect spanning every monitor: (x, y, w, h) in physical pixels."""
    user32 = ctypes.windll.user32
    return (user32.GetSystemMetrics(76), user32.GetSystemMetrics(77),
            user32.GetSystemMetrics(78), user32.GetSystemMetrics(79))
