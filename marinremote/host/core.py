"""
This PC as a host: lets viewers in (or not), keeps their sessions and ends them — one by one
or all at once.

No Qt in here: the UI hears about things through `events` and answers confirmation requests
through `approver`. That keeps this part movable into a Windows service later (stage 2), with
the questions asked by a helper in the user's session instead of this process.

Everything except the thread-safe entry points marked as such runs on the network loop.
"""

import asyncio
import secrets
import time

from .. import protocol as P
from ..auth import Limiter, MODES, PromptGate, ResumeTokens, decide, verify_password
from ..config import VERSION
from ..net import path_info, stop_stream
from .session import HostSession


class Approver:
    """Asks the person at this PC. ask(request) returns
    {"allow": bool, "rights": [...], "remember": mode | None, "block": bool};
    cancel(request_id) withdraws a question nobody needs answered any more."""

    async def ask(self, request):
        raise NotImplementedError

    def cancel(self, request_id):
        pass


class Service:
    """A part of a session after the login (video, input, files…). The on_* methods return True
    when they handled the thing; the session has already checked the right it needs."""

    def session_started(self, s):
        pass

    def session_ended(self, s):
        pass

    def on_message(self, s, msg):
        return False

    def on_datagram(self, s, data):
        return False

    async def on_stream(self, s, header, rest, rs):
        return False


class HostEvents:
    """What the UI hears about (called on the network thread)."""

    def sessions_changed(self, sessions):
        pass

    def settings_changed(self):
        pass


class HostCore:
    PROMPT_TIMEOUT = 30

    def __init__(self, settings, node, journal, approver, events=None, limiter=None, gate=None,
                 tokens=None):
        self.s = settings
        self.node = node
        self.net = node.net
        self.journal = journal
        self.approver = approver
        self.events = events or HostEvents()
        self.limiter = limiter or Limiter(settings.folder / "auth_state.json")
        self.gate = gate or PromptGate()
        self.tokens = tokens or ResumeTokens()
        self.sessions = {}           # sid -> HostSession
        self.services = []           # video, input, files…: on_message / on_datagram / on_stream / ended
        self._asks = {}              # request id -> the asking task (cancelled by kill_all)
        self._verify_lock = None
        self._next_request = 0
        node.on_incoming = self._incoming
        node.announce = lambda: ({"name": self.s["name"]}
                                 if self.s["lan_visible"] and self.s["allow_incoming"] else None)

    async def _incoming(self, conn):
        await HostSession(self, conn).run()

    # ── the login ───────────────────────────────────────────────────
    async def login(self, s, hello):
        """Let `s` in (True) or turn it away (False, the connection is closed)."""
        s.name = hello["name"]
        via = path_info(s.conn)
        if hello["resume"] and s.key not in (self.s["blocked"] or {}) and self.s["allow_incoming"]:
            entry = self.tokens.use(s.key, hello["resume"])
            if entry is not None:
                old = self.sessions.get(entry["sid"])
                if old is not None:
                    old.close(P.CLOSE_REPLACED, "replaced")
                rights = [r for r in entry["rights"] if r in hello["want"]]
                if rights:
                    return await self._admit(s, rights, "resume")
        kind, extra = decide(self.s, s.key, hello)
        if kind == "reject":
            self.journal.write("denied", s.key, name=s.name, reason=extra, via=via["via"])
            await s.refuse(extra)
            return False
        if kind == "contact":
            return await self._admit(s, extra, "contact")
        if kind == "password":
            return await self._password(s, hello, extra)
        return await self._ask(s, hello, extra)

    async def _password(self, s, hello, rights):
        wait = self.limiter.retry_after(s.key)
        if wait > 0:
            self.journal.write("auth_failed", s.key, name=s.name, reason="locked", wait=round(wait))
            await s.refuse("locked", wait)
            return False
        if self._verify_lock is None:
            self._verify_lock = asyncio.Lock()
        async with self._verify_lock:           # argon2id takes 256 MB: one at a time
            ok = await asyncio.to_thread(verify_password, self.s["password_hash"], hello["password"])
        if ok:
            self.limiter.succeeded(s.key)
            return await self._admit(s, rights, "password")
        wait = self.limiter.failed(s.key)
        self.journal.write("auth_failed", s.key, name=s.name, reason="bad_password")
        await s.refuse("bad_password", wait)
        return False

    async def _ask(self, s, hello, record):
        busy = self.gate.check(s.key)
        if busy is not None:
            reason, wait = busy
            self.journal.write("denied", s.key, name=s.name, reason=reason)
            await s.refuse(reason, wait)
            return False
        self._next_request += 1
        rid = self._next_request
        request = {"id": rid, "key": s.key, "fp": s.fp, "name": s.name, "want": hello["want"],
                   "record": dict(record) if record else None, "remember": hello["remember"],
                   "password_given": bool(hello["password"]), "timeout": self.PROMPT_TIMEOUT,
                   **path_info(s.conn)}
        self.journal.write("request", s.key, name=s.name, want=hello["want"], via=request["via"],
                           relay=request["via_relay"])
        self.gate.begin(s.key)
        refused = True
        try:
            await s.write({"t": "auth.wait", "timeout": self.PROMPT_TIMEOUT})
            ask = self.net.spawn(self.approver.ask(request))
            self._asks[rid] = ask
            gone = asyncio.ensure_future(s.conn.closed())
            done, _ = await asyncio.wait({ask, gone}, timeout=self.PROMPT_TIMEOUT,
                                         return_when=asyncio.FIRST_COMPLETED)
            if ask not in done:
                ask.cancel()
                self.approver.cancel(rid)
                if gone in done:
                    self.journal.write("denied", s.key, name=s.name, reason="left")
                    return False
                gone.cancel()
                self.journal.write("denied", s.key, name=s.name, reason="timeout")
                await s.refuse("timeout")
                return False
            gone.cancel()
            try:
                answer = ask.result() or {}
            except (asyncio.CancelledError, Exception):
                answer = {}
            rights = [r for r in P.clean_rights(answer.get("rights")) if r in hello["want"]]
            if answer.get("block"):
                self.block(s.key, s.name, kick=False)
                await s.refuse("blocked")
                return False
            if not answer.get("allow") or not rights:
                self.journal.write("denied", s.key, name=s.name, reason="denied")
                await s.refuse("denied")
                return False
            refused = False
            mode = answer.get("remember")
            if mode in MODES:
                self.remember(s.key, (record or {}).get("name") or s.name, rights, mode)
            self.journal.write("approved", s.key, name=s.name, rights=rights, remember=mode)
            return await self._admit(s, rights, "approved")
        finally:
            self._asks.pop(rid, None)
            self.gate.end(s.key, refused)

    async def _admit(self, s, rights, how):
        s.sid = secrets.token_hex(8)
        s.rights, s.how, s.started = list(rights), how, time.time()
        token = self.tokens.issue(s.key, s.rights, s.sid)
        record = (self.s["trusted"] or {}).get(s.key)
        await s.write({"t": "auth", "ok": True, "sid": s.sid, "rights": s.rights, "resume": token,
                       "remembered": record.get("mode") if record else None,
                       "host": {"name": self.s["name"], "ver": VERSION, "relay": self.node.addr["relay"],
                                "addrs": self.node.addr["addrs"]}})
        self.sessions[s.sid] = s
        info = path_info(s.conn)
        self.journal.write("session_start", s.key, name=s.name, sid=s.sid, rights=s.rights, how=how,
                           via=info["via"], relay=info["via_relay"])
        for svc in self.services:
            svc.session_started(s)
        self._changed()
        return True

    # ── during a session ────────────────────────────────────────────
    def on_message(self, s, msg):
        for svc in self.services:
            if svc.on_message(s, msg):
                return

    def on_datagram(self, s, data):
        for svc in self.services:
            if svc.on_datagram(s, data):
                return

    async def on_stream(self, s, header, rest, rs):
        for svc in self.services:
            if await svc.on_stream(s, header, rest, rs):
                return
        await stop_stream(rs)

    def session_closed(self, s, text):
        if self.sessions.get(s.sid) is not s:
            return                       # never got in
        del self.sessions[s.sid]
        code = P.close_code(text)
        end = s.end_reason or ("left" if code == P.CLOSE_NORMAL else "lost")
        if end in ("lost", "idle"):
            self.tokens.session_lost(s.sid)      # it may come back within a minute
        elif end != "replaced":
            self.tokens.revoke_session(s.sid)
        self.journal.write("session_end", s.key, name=s.name, sid=s.sid, reason=end,
                           seconds=round(time.time() - s.started),
                           violations=({k: v for k, v in s.violations.items() if v > 1} or None))
        for svc in self.services:
            svc.session_ended(s)
        self._changed()

    def _changed(self):
        self.events.sessions_changed([x.info() for x in self.sessions.values()])

    # ── remembered and blocked devices (network thread) ─────────────
    def remember(self, key, name, rights, mode):
        self.s.put("trusted", key, {"name": name, "rights": P.clean_rights(rights), "mode": mode,
                                    "added": time.strftime("%Y-%m-%d %H:%M")})
        self.journal.write("trusted", key, name=name, rights=rights, mode=mode)
        self.events.settings_changed()

    def block(self, key, name, kick=True):
        self.s.put("blocked", key, {"name": name, "when": time.strftime("%Y-%m-%d %H:%M")})
        self.s.put("trusted", key, None)
        self.tokens.revoke_key(key)
        self.journal.write("blocked", key, name=name)
        if kick:
            for s in list(self.sessions.values()):
                if s.key == key:
                    s.close(P.CLOSE_KICKED, "kicked")
        self.events.settings_changed()

    # ── thread-safe entry points for the UI ─────────────────────────
    def disconnect(self, sid):
        self.net.call(self._disconnect, sid)

    def _disconnect(self, sid):
        s = self.sessions.get(sid)
        if s is not None:
            self.tokens.revoke_session(sid)
            s.close(P.CLOSE_KICKED, "kicked")

    def kill_all(self, reason="killed"):
        """End every session and voids every token; pending questions are refused."""
        self.net.call(self._kill_all, reason)

    def _kill_all(self, reason):
        self.tokens.revoke_all()
        for task in list(self._asks.values()):
            task.cancel()
        n = len(self.sessions)
        for s in list(self.sessions.values()):
            s.close(P.CLOSE_KILLED if reason == "killed" else P.CLOSE_NORMAL, reason)
        if reason == "killed":
            self.journal.write("kill_all", sessions=n)

    def forget(self, key):
        self.net.call(self._forget, key)

    def _forget(self, key):
        record = (self.s["trusted"] or {}).get(key)
        self.s.put("trusted", key, None)
        self.journal.write("untrusted", key, name=(record or {}).get("name"))
        self.events.settings_changed()

    def block_key(self, key, name):
        self.net.call(self.block, key, name)

    def unblock(self, key):
        self.net.call(self._unblock, key)

    def _unblock(self, key):
        record = (self.s["blocked"] or {}).get(key)
        self.s.put("blocked", key, None)
        self.journal.write("unblocked", key, name=(record or {}).get("name"))
        self.events.settings_changed()

    def trust(self, key, name, rights, mode):
        self.net.call(self.remember, key, name, rights, mode)

    def shutdown(self, timeout=3):
        """Blocking: everyone is told this PC is going away."""
        try:
            self.net.submit(self._shutdown()).result(timeout)
        except Exception:
            pass

    async def _shutdown(self):
        self._kill_all("shutdown")
        await asyncio.sleep(0.2)
