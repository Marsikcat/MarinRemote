"""
Mouse and keyboard from a viewer, played on this PC through SendInput.

- Coordinates arrive as 0..65535 across the monitor being watched; they become pixels of that
  monitor, then MOUSEEVENTF_ABSOLUTE | VIRTUALDESK units of the whole virtual desktop.
- Keys arrive as scan codes (+ the extended flag), never characters: this PC's own layout
  decides what they type, exactly as with a keyboard plugged in here. The few keys whose scan
  codes Windows reports oddly (Pause, Num Lock, Ctrl+Break) go by virtual-key code.
- Moves come by datagram and may arrive late or out of order: only one newer than everything
  seen so far is played. Clicks, the wheel and keys come in order on a stream and carry the same
  sequence numbers, so a late datagram never drags the pointer back after a click.
- Everything held down is remembered per session and let go when the session ends, drops,
  loses the right to control, or the viewer's window loses focus.
- Nothing reaches MarinRemote's own windows: a remote click on them or a key while one of them
  is in front is dropped — otherwise a viewer could press «Разрешить» in someone else's request,
  remember itself or change the password.
"""

import ctypes
import ctypes.wintypes as wt
import os

from .. import winapi

INPUT_MOUSE, INPUT_KEYBOARD = 0, 1
MOVE, ABSOLUTE, VIRTUALDESK = 0x0001, 0x8000, 0x4000
WHEEL, HWHEEL = 0x0800, 0x1000
BUTTON_FLAGS = {  # name -> (down flag, up flag, mouseData)
    "left": (0x0002, 0x0004, 0),
    "right": (0x0008, 0x0010, 0),
    "middle": (0x0020, 0x0040, 0),
    "x1": (0x0080, 0x0100, 1),
    "x2": (0x0080, 0x0100, 2),
}
KEYUP, SCANCODE, EXTENDEDKEY = 0x0002, 0x0008, 0x0001
VK_ONLY = {0x03, 0x13, 0x90}            # Ctrl+Break, Pause, Num Lock


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wt.LONG), ("dy", wt.LONG), ("mouseData", wt.DWORD), ("dwFlags", wt.DWORD),
                ("time", wt.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wt.WORD), ("wScan", wt.WORD), ("dwFlags", wt.DWORD), ("time", wt.DWORD),
                ("dwExtraInfo", ctypes.c_size_t)]


class _HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", wt.DWORD), ("wParamL", wt.WORD), ("wParamH", wt.WORD)]


class _UNION(ctypes.Union):
    _fields_ = [("mi", _MOUSEINPUT), ("ki", _KEYBDINPUT), ("hi", _HARDWAREINPUT)]


class _INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", wt.DWORD), ("u", _UNION)]


# ── coordinates ────────────────────────────────────────────────────
def to_pixel(monitor, nx, ny):
    """0..65535 across a monitor → a pixel of the virtual desktop."""
    nx, ny = max(0, min(65535, int(nx))), max(0, min(65535, int(ny)))
    return (monitor["x"] + round(nx * (monitor["w"] - 1) / 65535),
            monitor["y"] + round(ny * (monitor["h"] - 1) / 65535))


def to_absolute(px, py, virtual):
    """A pixel → SendInput's ABSOLUTE | VIRTUALDESK units. Windows maps a unit u back to
    floor(u * width / 65536), so the smallest u that lands on the pixel is the ceiling."""
    vx, vy, vw, vh = virtual
    ax = ((px - vx) * 65536 + vw - 1) // vw
    ay = ((py - vy) * 65536 + vh - 1) // vh
    return max(0, min(65535, ax)), max(0, min(65535, ay))


def from_absolute(ax, ay, virtual):
    """What Windows does with them (for the tests)."""
    vx, vy, vw, vh = virtual
    return vx + ax * vw // 65536, vy + ay * vh // 65536


# ── the real thing ─────────────────────────────────────────────────
class Injector:
    """SendInput, and the questions the input state asks about this PC's windows."""

    def __init__(self):
        self._send = ctypes.windll.user32.SendInput
        self._send.argtypes = [wt.UINT, ctypes.POINTER(_INPUT), ctypes.c_int]
        self.pid = os.getpid()

    def _go(self, *inputs):
        arr = (_INPUT * len(inputs))(*inputs)
        self._send(len(inputs), arr, ctypes.sizeof(_INPUT))

    def mouse(self, px, py, flags=0, data=0, move=True):
        """A move to (px, py) and/or button/wheel flags in one event."""
        i = _INPUT(type=INPUT_MOUSE)
        if move:
            ax, ay = to_absolute(px, py, winapi.virtual_screen())
            i.mi.dx, i.mi.dy = ax, ay
            flags |= MOVE | ABSOLUTE | VIRTUALDESK
        i.mi.dwFlags = flags
        i.mi.mouseData = data & 0xFFFFFFFF
        self._go(i)

    def key(self, scan, ext, down, vk):
        i = _INPUT(type=INPUT_KEYBOARD)
        if vk in VK_ONLY or not scan:
            i.ki.wVk = vk
            i.ki.dwFlags = (0 if down else KEYUP) | (EXTENDEDKEY if ext else 0)
        else:
            i.ki.wScan = scan
            i.ki.dwFlags = SCANCODE | (0 if down else KEYUP) | (EXTENDEDKEY if ext else 0)
        self._go(i)

    def ours_at(self, px, py):
        return winapi.pid_at(px, py) == self.pid

    def ours_in_front(self):
        return winapi.foreground_pid() == self.pid


# ── one session's input ────────────────────────────────────────────
class InputState:
    """What one viewer holds down, and the order of its events. `monitor()` gives the monitor
    its coordinates are relative to; `blocked(what)` is told when something was kept away
    from our own windows."""

    def __init__(self, injector, monitor, blocked=None):
        self.inj = injector
        self.monitor = monitor
        self.blocked = blocked or (lambda what: None)
        self.last_seq = -1
        self.keys = set()            # (scan, ext, vk) held down
        self.buttons = set()

    def _newer(self, seq):
        if not isinstance(seq, int):
            return True
        if seq <= self.last_seq and self.last_seq - seq < 1 << 30:     # (wraps after 2**32)
            return False
        self.last_seq = seq
        return True

    def move(self, seq, nx, ny):
        """A move by datagram: only the newest counts."""
        if self._newer(seq):
            px, py = to_pixel(self.monitor(), nx, ny)
            self.inj.mouse(px, py)

    def event(self, ev):
        """One event from the input stream (already parsed JSON). Junk is ignored."""
        if not isinstance(ev, dict):
            return
        if ev.get("rel") == "all":
            self.release_all()
            return
        if "k" in ev:
            return self._key(ev)
        m = ev.get("m")
        x, y = ev.get("x"), ev.get("y")
        has_pos = isinstance(x, int) and isinstance(y, int)
        if isinstance(ev.get("s"), int):
            self._newer(ev["s"])     # anything older than this event is stale now
        px, py = to_pixel(self.monitor(), x, y) if has_pos else (0, 0)
        if m in ("down", "up"):
            b = ev.get("b")
            if b not in BUTTON_FLAGS:
                return
            down_flag, up_flag, data = BUTTON_FLAGS[b]
            if m == "down":
                if has_pos and self.inj.ours_at(px, py):
                    self.blocked("click")
                    self.inj.mouse(px, py)           # the pointer may go there, the click may not
                    return
                self.buttons.add(b)
                self.inj.mouse(px, py, down_flag, data, move=has_pos)
            elif b in self.buttons:                  # only what we pressed is let go
                self.buttons.discard(b)
                self.inj.mouse(px, py, up_flag, data, move=has_pos)
        elif m == "wheel":
            if has_pos and self.inj.ours_at(px, py):
                self.blocked("wheel")
                return
            for flag, delta in ((WHEEL, ev.get("dy")), (HWHEEL, ev.get("dx"))):
                if isinstance(delta, int) and delta and abs(delta) <= 120 * 50:
                    self.inj.mouse(px, py, flag, delta, move=has_pos)

    def _key(self, ev):
        scan, ext, down, vk = ev.get("k"), ev.get("e", 0), ev.get("d"), ev.get("vk", 0)
        if not (isinstance(scan, int) and 0 <= scan <= 0xFF and isinstance(vk, int) and 0 <= vk <= 0xFF):
            return
        if not scan and not vk:
            return
        key = (scan, 1 if ext else 0, vk)
        if down:
            if self.inj.ours_in_front():
                self.blocked("key")
                return
            self.keys.add(key)
            self.inj.key(scan, bool(ext), True, vk)
        elif key in self.keys:
            self.keys.discard(key)
            self.inj.key(scan, bool(ext), False, vk)

    def release_all(self):
        for scan, ext, vk in list(self.keys):
            self.inj.key(scan, bool(ext), False, vk)
        self.keys.clear()
        for b in list(self.buttons):
            _, up_flag, data = BUTTON_FLAGS[b]
            self.inj.mouse(0, 0, up_flag, data, move=False)
        self.buttons.clear()
