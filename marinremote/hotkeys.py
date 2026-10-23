"""
The emergency hotkey: one combination that ends every session at once, wherever the focus is.

Polls the keyboard state (GetAsyncKeyState) like MarinCall's hotkeys: nothing to register
that another program could already hold, and it works over full-screen games too.
A binding is {"vk": int, "mods": ["ctrl", "shift", "alt", "win"]}.
"""

import ctypes

from PySide6.QtCore import QObject, QTimer, Signal

from .system import key_name

_user32 = ctypes.windll.user32 if hasattr(ctypes, "windll") else None

MOD_KEYS = {"ctrl": (0x11,), "shift": (0x10,), "alt": (0x12,), "win": (0x5B, 0x5C)}
MOD_ORDER = ("ctrl", "shift", "alt", "win")
MODIFIER_VK = {0x10: "shift", 0x11: "ctrl", 0x12: "alt", 0x5B: "win", 0x5C: "win",
               0xA0: "shift", 0xA1: "shift", 0xA2: "ctrl", 0xA3: "ctrl", 0xA4: "alt", 0xA5: "alt"}
IGNORED_VK = {0x01, 0x02, 0x0D}       # left/right click, Enter (confirms dialogs)


def _down(vk):
    return bool(_user32 and _user32.GetAsyncKeyState(int(vk)) & 0x8000)


def mods_down():
    return {name for name, vks in MOD_KEYS.items() if any(_down(v) for v in vks)}


def describe(binding):
    if not binding:
        return "Не назначено"
    parts = [m.capitalize() if m != "win" else "Win" for m in MOD_ORDER if m in (binding.get("mods") or [])]
    return " + ".join(parts + [key_name(binding["vk"])])


class KillHotkey(QObject):
    """Fires `triggered` once per press of the bound combination."""

    triggered = Signal()

    def __init__(self, settings):
        super().__init__()
        self.s = settings
        self._prev = False
        self._timer = QTimer(self, interval=40, timeout=self._poll)

    def start(self):
        self._timer.start()

    def _poll(self):
        b = self.s["kill_hotkey"]
        if not b:
            return
        now = _down(b["vk"]) and mods_down() == set(b.get("mods") or [])
        if now and not self._prev:
            self.triggered.emit()
        self._prev = now


class Recorder(QObject):
    """Captures the next combination (with at least one modifier); Esc cancels."""

    captured = Signal(object)     # binding dict, or None when cancelled

    def __init__(self, parent=None):
        super().__init__(parent)
        self._timer = QTimer(self, interval=20, timeout=self._poll)
        self._ticks = 0
        self._armed = False

    def start(self):
        self._ticks = 0
        self._armed = False       # wait until everything (incl. the click) is released
        self._timer.start()

    def stop(self):
        self._timer.stop()

    def _finish(self, binding):
        self._timer.stop()
        self.captured.emit(binding)

    def _poll(self):
        self._ticks += 1
        if self._ticks > 500:                      # 10 s
            return self._finish(None)
        pressed = [vk for vk in range(0x01, 0xFF) if vk not in IGNORED_VK and _down(vk)]
        if not self._armed:
            self._armed = not pressed and not _down(0x01)
            return
        keys = [vk for vk in pressed if vk not in MODIFIER_VK]
        if 0x1B in keys:                           # Esc
            return self._finish(None)
        mods = sorted({MODIFIER_VK[v] for v in pressed if v in MODIFIER_VK}, key=MOD_ORDER.index)
        if keys and mods:
            return self._finish({"vk": keys[0], "mods": mods})
