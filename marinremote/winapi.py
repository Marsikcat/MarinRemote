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


# ── windows, processes and what input can reach ────────────────────
def _window_prototypes():
    try:
        user32, k32, advapi = ctypes.windll.user32, ctypes.windll.kernel32, ctypes.windll.advapi32
    except AttributeError:
        return
    user32.GetForegroundWindow.restype = wt.HWND
    user32.WindowFromPoint.argtypes = [wt.POINT]
    user32.WindowFromPoint.restype = wt.HWND
    user32.GetAncestor.argtypes = [wt.HWND, wt.UINT]
    user32.GetAncestor.restype = wt.HWND
    user32.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
    user32.OpenInputDesktop.restype = wt.HANDLE
    user32.GetUserObjectInformationW.argtypes = [wt.HANDLE, ctypes.c_int, ctypes.c_void_p, wt.DWORD,
                                                 ctypes.POINTER(wt.DWORD)]
    user32.CloseDesktop.argtypes = [wt.HANDLE]
    k32.OpenProcess.restype = wt.HANDLE
    k32.CloseHandle.argtypes = [wt.HANDLE]
    k32.GetCurrentProcess.restype = wt.HANDLE
    k32.QueryFullProcessImageNameW.argtypes = [wt.HANDLE, wt.DWORD, wt.LPWSTR, ctypes.POINTER(wt.DWORD)]
    advapi.OpenProcessToken.argtypes = [wt.HANDLE, wt.DWORD, ctypes.POINTER(wt.HANDLE)]
    advapi.GetTokenInformation.argtypes = [wt.HANDLE, ctypes.c_int, ctypes.c_void_p, wt.DWORD,
                                           ctypes.POINTER(wt.DWORD)]
    advapi.GetSidSubAuthorityCount.argtypes = [ctypes.c_void_p]
    advapi.GetSidSubAuthorityCount.restype = ctypes.POINTER(ctypes.c_ubyte)
    advapi.GetSidSubAuthority.argtypes = [ctypes.c_void_p, wt.DWORD]
    advapi.GetSidSubAuthority.restype = ctypes.POINTER(wt.DWORD)


_window_prototypes()


def window_pid(hwnd):
    if not hwnd:
        return 0
    pid = wt.DWORD()
    ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


def foreground_pid():
    return window_pid(ctypes.windll.user32.GetForegroundWindow())


def pid_at(x, y):
    """The process owning the top-level window under a point of the virtual desktop."""
    user32 = ctypes.windll.user32
    hwnd = user32.WindowFromPoint(wt.POINT(int(x), int(y)))
    return window_pid(user32.GetAncestor(hwnd, 2) if hwnd else None)       # GA_ROOT


def _integrity(process):
    """The integrity level RID of a process handle (0x2000 medium, 0x3000 high, 0x4000 system),
    None if its token can't be read."""
    advapi, k32 = ctypes.windll.advapi32, ctypes.windll.kernel32
    token = wt.HANDLE()
    if not advapi.OpenProcessToken(process, 0x0008, ctypes.byref(token)):       # TOKEN_QUERY
        return None
    try:
        need = wt.DWORD()
        advapi.GetTokenInformation(token, 25, None, 0, ctypes.byref(need))       # TokenIntegrityLevel
        buf = ctypes.create_string_buffer(need.value or 64)
        if not advapi.GetTokenInformation(token, 25, buf, len(buf), ctypes.byref(need)):
            return None
        sid = ctypes.c_void_p.from_buffer(buf).value                              # TOKEN_MANDATORY_LABEL.Sid
        count = advapi.GetSidSubAuthorityCount(sid)[0]
        return advapi.GetSidSubAuthority(sid, count - 1)[0]
    finally:
        k32.CloseHandle(token)


_own_integrity = None


def process_name(pid):
    k32 = ctypes.windll.kernel32
    h = k32.OpenProcess(0x1000, False, pid)                                      # QUERY_LIMITED_INFORMATION
    if not h:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(520)
        size = wt.DWORD(len(buf))
        if k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return os.path.basename(buf.value)
        return ""
    finally:
        k32.CloseHandle(h)


def above_us(pid):
    """Does the process run at a higher integrity level than ours (an administrator's window:
    Windows drops our injected input to it — UIPI)?"""
    global _own_integrity
    k32 = ctypes.windll.kernel32
    if _own_integrity is None:
        _own_integrity = _integrity(k32.GetCurrentProcess()) or 0x2000
    if not pid or pid == os.getpid():
        return False
    h = k32.OpenProcess(0x1000, False, pid)
    if not h:
        return True                     # not even that much access: it is protected from us
    try:
        level = _integrity(h)
    finally:
        k32.CloseHandle(h)
    return level is None or level > _own_integrity


def secure_desktop():
    """Is input going to another desktop than ours (UAC prompt, Ctrl+Alt+Del screen, the lock
    screen)? Nothing we inject reaches it, nothing we capture shows it."""
    user32 = ctypes.windll.user32
    h = user32.OpenInputDesktop(0, False, 0x0001)                                # DESKTOP_READOBJECTS
    if not h:
        return True
    try:
        buf = ctypes.create_unicode_buffer(64)
        need = wt.DWORD()
        if not user32.GetUserObjectInformationW(h, 2, buf, ctypes.sizeof(buf), ctypes.byref(need)):   # UOI_NAME
            return False
        return buf.value.lower() != "default"
    finally:
        user32.CloseDesktop(h)
