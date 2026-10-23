"""
Keys Windows would keep to itself — Win, Alt+Tab, Alt+Esc, Ctrl+Esc, Alt+F4 — go to the remote
PC while its session window is in front: a low-level keyboard hook (WH_KEYBOARD_LL) takes every
key first and swallows it.

The hook lives on a thread of its own with its own message loop: Windows silently removes a
low-level hook whose thread doesn't answer in time, and Qt's thread is busy painting video.
The callback only hands the key over and returns.

Injected keys (LLKHF_INJECTED) are never taken: they are some program's, and on a PC that is
both viewer and host they would go round in circles. Ctrl+Alt+Del and Win+L can't be taken by
anyone but Windows.
"""

import ctypes
import ctypes.wintypes as wt
import threading

WH_KEYBOARD_LL = 13
WM_KEYDOWN, WM_KEYUP, WM_SYSKEYDOWN, WM_SYSKEYUP = 0x100, 0x101, 0x104, 0x105
LLKHF_EXTENDED, LLKHF_INJECTED = 0x01, 0x10
LRESULT = ctypes.c_ssize_t


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("vkCode", wt.DWORD), ("scanCode", wt.DWORD), ("flags", wt.DWORD), ("time", wt.DWORD),
                ("dwExtraInfo", ctypes.c_size_t)]


HOOKPROC = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_int, wt.WPARAM, wt.LPARAM)


def translate(wparam, kb):
    """(scan, extended, down, vk) of a hook event, or None for one we must leave alone."""
    if kb.flags & LLKHF_INJECTED:
        return None
    if kb.scanCode & 0x200:          # the Ctrl that AltGr fakes: the host's AltGr makes its own
        return None
    down = wparam in (WM_KEYDOWN, WM_SYSKEYDOWN)
    return kb.scanCode & 0xFF, 1 if kb.flags & LLKHF_EXTENDED else 0, down, kb.vkCode & 0xFF


class KeyboardHook:
    """One hook for the process. register(hwnd, handler): while that window is in front,
    handler(scan, ext, down, vk) gets every key and returns True to swallow it."""

    _inst = None
    _lock = threading.Lock()

    @classmethod
    def get(cls):
        with cls._lock:
            if cls._inst is None:
                cls._inst = cls()
            return cls._inst

    def __init__(self):
        self.targets = {}
        self._proc = HOOKPROC(self._callback)       # kept: Windows calls it for as long as we live
        self._thread = None
        self.installed = False

    def register(self, hwnd, handler):
        self.targets = {**self.targets, int(hwnd): handler}
        if self._thread is None:
            ready = threading.Event()
            self._thread = threading.Thread(target=self._loop, args=(ready,), daemon=True, name="keyboard-hook")
            self._thread.start()
            ready.wait(2)

    def unregister(self, hwnd):
        targets = dict(self.targets)
        targets.pop(int(hwnd), None)
        self.targets = targets

    def _loop(self, ready):
        user32, k32 = ctypes.windll.user32, ctypes.windll.kernel32
        user32.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC, wt.HINSTANCE, wt.DWORD]
        user32.SetWindowsHookExW.restype = wt.HHOOK
        user32.CallNextHookEx.argtypes = [wt.HHOOK, ctypes.c_int, wt.WPARAM, wt.LPARAM]
        user32.CallNextHookEx.restype = LRESULT
        user32.GetForegroundWindow.restype = wt.HWND
        k32.GetModuleHandleW.restype = wt.HMODULE
        self._next = user32.CallNextHookEx
        self._foreground = user32.GetForegroundWindow
        hook = user32.SetWindowsHookExW(WH_KEYBOARD_LL, self._proc, k32.GetModuleHandleW(None), 0)
        self.installed = bool(hook)
        ready.set()
        msg = wt.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

    def _callback(self, code, wparam, lparam):
        try:
            if code == 0 and self.targets:
                handler = self.targets.get(int(self._foreground() or 0))
                if handler is not None:
                    ev = translate(wparam, ctypes.cast(lparam, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents)
                    if ev is not None and handler(*ev):
                        return 1
        except Exception:
            pass
        return self._next(None, code, wparam, lparam)
