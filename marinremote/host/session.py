"""
One viewer connected to this PC: its login, then its control messages, one-way streams and
datagrams — each checked against the rights this session holds. Runs on the network loop.
"""

import asyncio

from .. import protocol as P
from ..codes import fingerprint
from ..net import LineReader, path_info, read_stream_header, remote_key, stop_stream

HELLO_TIMEOUT = 10      # the hello must come this soon after connecting
IDLE_TIMEOUT = 20       # nothing at all from the viewer (it pings every 5 s): it is gone
PING_EVERY = 5

# what a one-way stream from the viewer needs
STREAM_NEEDS = {"input": "control", "file": "files"}
# what a datagram (by its first byte) needs
DGRAM_NEEDS = {P.DG_MOUSE: "control", P.DG_VIDEO_ACK: "view"}


class HostSession:
    def __init__(self, core, conn):
        self.core, self.conn = core, conn
        self.key = remote_key(conn)
        self.fp = fingerprint(self.key)
        self.sid = None
        self.rights = []
        self.name = ""
        self.how = ""                # how it got in: approved | contact | password | resume
        self.started = None
        self.end_reason = None       # set when this side ends it: kicked | killed | replaced | idle…
        self.violations = {}         # what it tried without the right -> count
        self.send = None
        self.queue = asyncio.Queue()
        self.tasks = []
        self._pre_auth_logged = False

    def info(self):
        return {"sid": self.sid, "key": self.key, "fp": self.fp, "name": self.name,
                "rights": list(self.rights), "started": self.started, "how": self.how,
                **path_info(self.conn)}

    def has(self, right):
        return right in self.rights

    # ── the connection's life ───────────────────────────────────────
    async def run(self):
        try:
            bi = await asyncio.wait_for(self.conn.accept_bi(), HELLO_TIMEOUT)
            self.send = bi.send()
            await self.send.set_priority(3)
            reader = LineReader(bi.recv())
            hello = await self._read_hello(reader)
            if hello is None:
                return
            if not await self.core.login(self, hello):
                return
        except Exception:
            self.close(P.CLOSE_PROTOCOL, "protocol")
            return
        self.tasks = [self.core.net.spawn(c) for c in (
            self._read_lines(reader), self._write_lines(), self._read_datagrams(), self._accept_streams())]
        try:
            text = await self.conn.closed()
        except Exception as e:
            text = str(e)
        for t in self.tasks:
            t.cancel()
        self.core.session_closed(self, text)

    async def _read_hello(self, reader):
        """Up to the hello: anything before it is ignored (and noted in the journal)."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + HELLO_TIMEOUT
        while True:
            raw = await asyncio.wait_for(reader.next(), max(0.1, deadline - loop.time()))
            if raw is None:
                self.close(P.CLOSE_PROTOCOL, "protocol")
                return None
            msg = P.parse(raw)
            if msg is not None and msg["t"] == "hello":
                try:
                    return P.check_hello(msg)
                except ValueError as e:
                    await self.refuse(str(e))
                    return None
            self.pre_auth(msg["t"] if msg else "?")

    def pre_auth(self, what):
        if not self._pre_auth_logged:
            self._pre_auth_logged = True
            self.core.journal.write("pre_auth", self.key, what=what)

    async def write(self, obj):
        """Straight onto the control stream (during the login, before the writer runs)."""
        await self.send.write_all(P.line(obj))

    def post(self, obj):
        """Queue a message for the viewer (network thread)."""
        self.queue.put_nowait(obj)

    async def refuse(self, reason, wait=0):
        msg = {"t": "auth", "ok": False, "reason": reason}
        if wait:
            msg["retry_after"] = round(wait)
        try:
            await self.write(msg)
            await self.send.finish()
            # the viewer closes on its own once it has read the answer
            await asyncio.wait_for(self.conn.closed(), 3)
        except Exception:
            pass
        self.close(P.CLOSE_AUTH, reason)

    def close(self, code, reason):
        if self.end_reason is None:
            self.end_reason = reason
        try:
            self.conn.close(code, reason.encode())
        except Exception:
            pass

    # ── after the login ─────────────────────────────────────────────
    async def _write_lines(self):
        while True:
            try:
                obj = await asyncio.wait_for(self.queue.get(), PING_EVERY)
            except asyncio.TimeoutError:
                obj = {"t": "ping"}
            if obj is None:
                break
            try:
                await self.write(obj)
            except Exception:
                break

    async def _read_lines(self, reader):
        while True:
            try:
                raw = await asyncio.wait_for(reader.next(), IDLE_TIMEOUT)
            except asyncio.TimeoutError:
                self.close(P.CLOSE_IDLE, "idle")
                return
            if raw is None:
                break
            msg = P.parse(raw)
            if msg is not None:
                self._dispatch(msg)
        # usually the connection itself is going (and this task is cancelled with it); a viewer
        # that ended its control stream but stays connected is talking nonsense
        await asyncio.sleep(1)
        self.close(P.CLOSE_PROTOCOL, "protocol")

    def _dispatch(self, msg):
        t = msg["t"]
        if t == "bye":
            self.close(P.CLOSE_NORMAL, "left")
            return
        need = P.needed_right(t)
        if need is None or t == "ping":
            return                       # unknown: a newer viewer — ignored quietly
        if need and need not in self.rights:
            self.violation(t)
            return
        self.core.on_message(self, msg)

    def violation(self, what):
        """Something beyond this session's rights: ignored. The first of each kind goes into the
        journal at once, the count when the session ends."""
        n = self.violations.get(what, 0) + 1
        self.violations[what] = n
        if n == 1:
            self.core.journal.write("violation", self.key, name=self.name, sid=self.sid, what=what,
                                    rights=self.rights)

    async def _read_datagrams(self):
        while True:
            try:
                data = await self.conn.read_datagram()
            except Exception:
                break
            if len(data) < 5 or data[1:5] != P.dgram_tag(self.sid):
                self.pre_auth("datagram")           # sent before the login
                continue
            need = DGRAM_NEEDS.get(data[0])
            if need is None:
                continue
            if need not in self.rights:
                self.violation(f"datagram {data[0]}")
                continue
            self.core.on_datagram(self, data)

    async def _accept_streams(self):
        while True:
            try:
                rs = await self.conn.accept_uni()
            except Exception:
                break
            self.core.net.spawn(self._stream(rs))

    async def _stream(self, rs):
        try:
            header, rest = await asyncio.wait_for(read_stream_header(rs), 10)
        except Exception:
            header, rest = None, b""
        if header is None or header.get("sid") != self.sid:
            # not ours, or opened before the login (it can't know the session id)
            self.pre_auth("stream")
            await stop_stream(rs)
            return
        need = STREAM_NEEDS.get(header["t"])
        if need is None:
            await stop_stream(rs)
            return
        if need not in self.rights:
            self.violation(f"stream {header['t']}")
            await stop_stream(rs)
            return
        await self.core.on_stream(self, header, rest, rs)
