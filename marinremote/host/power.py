"""
Lock, restart or shut down this PC at a viewer's word (the session has already checked the
"control" right: whoever may type here could do the same from the Start menu). A restart or a
shutdown waits DELAY seconds with Windows' own notice on screen; the person here can call it off,
and so can the viewer. Every request goes into the journal.
"""

import asyncio
import ctypes
import os
import subprocess
import time

from .core import Service

DELAY = 30
ACTIONS = ("lock", "restart", "shutdown")


class PowerService(Service):
    def __init__(self, core, run=None):
        self.core, self.net = core, core.net
        self.run = run or system_power      # run(action, comment); raises OSError with a message
        self.pending = None                 # the restart or shutdown under way: {action, sid, key, name, until}

    def on_message(self, s, msg):
        if msg["t"] != "power":
            return False
        action = msg.get("action")
        if action in ACTIONS:
            self.net.spawn(self._do(s, action))
        elif action == "cancel":
            self.net.spawn(self.cancel_pending("viewer"))
        return True

    async def _do(self, s, action):
        if action != "lock" and self._pending() is not None:
            s.post({"t": "power.error", "action": action, "msg": "перезагрузка или выключение уже назначены"})
            return
        try:
            await asyncio.to_thread(self.run, action, f"MarinRemote: {s.name}")
        except Exception as e:
            self.core.journal.write("power", s.key, name=s.name, action=action, result="error", msg=str(e))
            s.post({"t": "power.error", "action": action, "msg": str(e)})
            return
        delay = 0 if action == "lock" else DELAY
        if delay:
            self.pending = {"action": action, "sid": s.sid, "key": s.key, "name": s.name,
                            "until": time.monotonic() + delay}
        self.core.journal.write("power", s.key, name=s.name, action=action, result="ok")
        s.post({"t": "power.ok", "action": action, "delay": delay})
        self.core.events.power_changed({"action": action, "state": "scheduled", "name": s.name, "fp": s.fp,
                                        "delay": delay})

    def _pending(self):
        p = self.pending
        if p is not None and time.monotonic() > p["until"] + 10:
            self.pending = p = None          # it happened (or someone called it off outside MarinRemote)
        return p

    async def cancel_pending(self, by):
        """Call off the restart or shutdown under way; `by` is "viewer" or "host"."""
        p = self._pending()
        if p is None:
            return
        self.pending = None
        try:
            await asyncio.to_thread(self.run, "cancel", "")
        except Exception:
            pass                             # nothing was scheduled any more: the same result
        self.core.journal.write("power", p["key"], name=p["name"], action="cancel", by=by, result="ok")
        for s in list(self.core.sessions.values()):
            if s.has("control"):
                s.post({"t": "power.cancelled", "action": p["action"], "by": by})
        self.core.events.power_changed({"action": p["action"], "state": "cancelled", "name": p["name"],
                                        "by": by})


def system_power(action, comment):
    if action == "lock":
        if not ctypes.windll.user32.LockWorkStation():
            raise OSError("Windows не дала заблокировать экран")
        return
    exe = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "shutdown.exe")
    args = {"restart": ["/r", "/t", str(DELAY), "/c", comment[:500]],
            "shutdown": ["/s", "/t", str(DELAY), "/c", comment[:500]],
            "cancel": ["/a"]}[action]
    r = subprocess.run([exe, *args], capture_output=True, timeout=15,
                       creationflags=subprocess.CREATE_NO_WINDOW)
    if r.returncode:
        text = (r.stderr or r.stdout).decode("oem", "replace").strip()
        raise OSError(text or f"shutdown.exe: код {r.returncode}")
