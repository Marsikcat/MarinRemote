"""
The viewer's mouse and keyboard, on the host's side (the session has already checked the
"control" right for every stream and datagram that gets here).

Also watches what injected input can't reach and tells viewers with control about it: a window
of an administrator in front (Windows' UIPI drops our input to it without a word) or a secure
desktop (UAC prompt, Ctrl+Alt+Del, the lock screen).
"""

import asyncio
import json
import time

from .. import protocol as P
from .. import winapi
from ..net import LineReader
from .core import Service
from .inject import InputState

GUARD_EVERY = 0.5


class InputService(Service):
    def __init__(self, core, video=None, injector=None, monitors=None, probe=None):
        self.core, self.net = core, core.net
        self.video = video                # whose monitor a session's coordinates are relative to
        if injector is None:
            from .inject import Injector
            injector = Injector()
        self.inj = injector
        self.monitors_fn = monitors or winapi.monitors
        self.probe = probe or _probe      # () -> (code, app): what our input can't reach right now
        self.states = {}                  # sid -> InputState
        self.sessions = {}
        self.warned = {}                  # sid -> the last warning sent
        self.blocked_logged = {}          # sid -> kinds already in the journal
        self._primary = (None, 0.0)
        self._guard = None

    # ── sessions ────────────────────────────────────────────────────
    def session_started(self, s):
        if not s.has("control"):
            return
        self.sessions[s.sid] = s
        self.states[s.sid] = InputState(self.inj, lambda: self._monitor(s), lambda what: self._blocked(s, what))
        if self._guard is None:
            self._guard = self.net.spawn(self._guard_loop())

    def session_ended(self, s):
        st = self.states.pop(s.sid, None)
        self.sessions.pop(s.sid, None)
        self.warned.pop(s.sid, None)
        self.blocked_logged.pop(s.sid, None)
        if st is not None:
            st.release_all()              # nothing stays pressed after a session
        if not self.states and self._guard is not None:
            self._guard.cancel()
            self._guard = None

    def on_datagram(self, s, data):
        if data[0] != P.DG_MOUSE:
            return False
        st = self.states.get(s.sid)
        m = P.unpack_mouse(data)
        if st is not None and m is not None:
            st.move(*m)
        return True

    async def on_stream(self, s, header, rest, rs):
        if header.get("t") != "input":
            return False
        reader = LineReader(rs, limit=4096)
        reader.buf += rest
        while True:
            raw = await reader.next()
            if raw is None:
                break
            st = self.states.get(s.sid)
            if st is None:
                break
            try:
                st.event(json.loads(raw))
            except ValueError:
                continue
        return True

    # ── helpers ─────────────────────────────────────────────────────
    def _monitor(self, s):
        w = self.video.watches.get(s.sid) if self.video is not None else None
        if w is not None and w.monitor is not None:
            return w.monitor
        primary, when = self._primary
        if primary is None or time.monotonic() - when > 3:
            found = self.monitors_fn()
            primary = next((m for m in found if m["primary"]), found[0] if found else
                           {"x": 0, "y": 0, "w": 1920, "h": 1080})
            self._primary = (primary, time.monotonic())
        return primary

    def _blocked(self, s, what):
        """Input aimed at MarinRemote's own windows was kept away from them."""
        seen = self.blocked_logged.setdefault(s.sid, set())
        if what not in seen:
            seen.add(what)
            self.core.journal.write("self_input", s.key, name=s.name, sid=s.sid, what=what)
        s.post({"t": "warn", "code": "self"})

    async def _guard_loop(self):
        while True:
            try:
                code, app = await asyncio.to_thread(self.probe)
            except Exception:
                code, app = None, ""
            for sid, s in list(self.sessions.items()):
                if self.warned.get(sid, "") != code:
                    self.warned[sid] = code
                    s.post({"t": "warn", "code": code, "app": app})
            await asyncio.sleep(GUARD_EVERY)


def _probe():
    if winapi.secure_desktop():
        return "secure", ""
    pid = winapi.foreground_pid()
    if winapi.above_us(pid):
        return "uipi", winapi.process_name(pid)
    return None, ""
