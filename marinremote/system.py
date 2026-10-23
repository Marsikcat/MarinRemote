"""Windows integration: autostart, the firewall rule, key names, elevation."""

import ctypes
import subprocess
import sys
from pathlib import Path

from .config import APP_NAME, DISCOVERY_PORT, FROZEN, INSTANCE, PEER_PORT, ROOT

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_NAME = APP_NAME + (f"-{INSTANCE}" if INSTANCE else "")
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _launch_command():
    if FROZEN:
        return f'"{sys.executable}" --autostart'
    exe = Path(sys.executable)
    pyw = exe.with_name("pythonw.exe")
    return f'"{pyw if pyw.exists() else exe}" "{ROOT / "app.py"}" --autostart'


def autostart_enabled():
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            winreg.QueryValueEx(k, RUN_NAME)
        return True
    except OSError:
        return False


def set_autostart(enabled):
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        if enabled:
            winreg.SetValueEx(k, RUN_NAME, 0, winreg.REG_SZ, _launch_command())
        else:
            try:
                winreg.DeleteValue(k, RUN_NAME)
            except FileNotFoundError:
                pass


def open_firewall():
    """Inbound UDP for our ports. Windows shows its own UAC prompt; True if applied."""
    cmds = [f"netsh advfirewall firewall delete rule name='{APP_NAME} UDP' | Out-Null",
            f"netsh advfirewall firewall add rule name='{APP_NAME} UDP' dir=in action=allow "
            f"protocol=UDP localport={DISCOVERY_PORT},{PEER_PORT} profile=any | Out-Null"]
    inner = "; ".join(cmds).replace('"', '\\"')
    ps = (f"$p = Start-Process powershell -Verb RunAs -WindowStyle Hidden -Wait -PassThru "
          f"-ArgumentList '-NoProfile','-Command',\"{inner}\"; exit $p.ExitCode")
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True,
                           timeout=180, creationflags=NO_WINDOW)
        return r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def is_elevated():
    """Is this process running as administrator?"""
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except (OSError, AttributeError):
        return False


SPECIAL_KEYS = {
    0x04: "Средняя кнопка мыши", 0x05: "Кнопка мыши 4", 0x06: "Кнопка мыши 5",
    0xA0: "Левый Shift", 0xA1: "Правый Shift", 0xA2: "Левый Ctrl", 0xA3: "Правый Ctrl",
    0xA4: "Левый Alt", 0xA5: "Правый Alt", 0x5B: "Левый Win", 0x5C: "Правый Win",
    0x20: "Пробел", 0x08: "Backspace", 0x09: "Tab", 0x14: "Caps Lock", 0x13: "Pause",
    0x2C: "Print Screen", 0x91: "Scroll Lock", 0xC0: "Ё", 0x23: "End", 0x24: "Home",
    0x2D: "Insert", 0x2E: "Delete", 0x21: "Page Up", 0x22: "Page Down",
}


def key_name(vk):
    if vk in SPECIAL_KEYS:
        return SPECIAL_KEYS[vk]
    if 0x70 <= vk <= 0x87:
        return f"F{vk - 0x6F}"
    if 0x30 <= vk <= 0x39 or 0x41 <= vk <= 0x5A:
        return chr(vk)
    try:
        user32 = ctypes.windll.user32
        scan = user32.MapVirtualKeyW(vk, 0)
        extended = vk in (0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28, 0x2D, 0x2E, 0xA3, 0xA5)
        buf = ctypes.create_unicode_buffer(64)
        if user32.GetKeyNameTextW((scan << 16) | (extended << 24), buf, 64):
            return buf.value
    except (OSError, AttributeError):
        pass
    return f"Клавиша {vk}"
