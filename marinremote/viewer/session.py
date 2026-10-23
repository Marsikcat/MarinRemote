"""
A connection from here to another PC: dial it, say hello, wait for the answer (someone may
have to click «Разрешить» there), keep the line alive and, if the network drops, come back
with the one-time resume token — no new confirmation needed for about a minute.

Runs on the network loop; `events` hears about states and messages on that thread.
"""

import asyncio
import time

from .. import protocol as P
from ..config import VERSION
from ..net import LineReader, path_info, read_stream_header, stop_stream

PING_EVERY = 5
IDLE_TIMEOUT = 20
AUTH_TIMEOUT = 45           # the host's 30 s confirmation window, plus slack
RECONNECT_FOR = 55          # the host keeps the token for 60 s
BACKOFF = (1, 2, 3, 5)

# why the host ended it, by close code
ENDED = {
    P.CLOSE_KICKED: "kicked",
    P.CLOSE_KILLED: "killed",
    P.CLOSE_REPLACED: "replaced",
    P.CLOSE_NORMAL: "host_closed",
    P.CLOSE_AUTH: "refused",
    P.CLOSE_PROTOCOL: "protocol",
}
END_TEXT = {
    "left": "Вы отключились.",
    "kicked": "Удалённый ПК завершил сеанс.",
    "killed": "На удалённом ПК нажали аварийное отключение.",
    "replaced": "Этот сеанс продолжился в другом подключении.",
    "host_closed": "MarinRemote на удалённом ПК закрыли.",
    "protocol": "Удалённый ПК прервал сеанс из-за ошибки протокола.",
    "lost": "Связь с удалённым ПК потеряна.",
    "unreachable": "Не удалось связаться с удалённым ПК: он выключен, не в сети или код устарел.",
}


class ViewerEvents:
    def state_changed(self, session):
        pass

    def message(self, session, msg):
        pass


class ViewerSession:
    def __init__(self, node, target, want, my_name, events=None, password=None, remember=False):
        self.node, self.net = node, node.net
        self.target = dict(target)           # {id, relay, addrs, name}
        self.want = P.clean_rights(want)
        self.my_name = my_name
        self.events = events or ViewerEvents()
        self.password = password or None
        self.remember = remember
        self.state = "idle"     # connecting | waiting | connected | reconnecting | closed
        self.detail = {}        # for the state: wait timeout, close reason, retry_after…
        self.conn = None
        self.sid = None
        self.rights = []
        self.host = {}
        self.remembered = None  # the host's mode for this device, if it remembers us
        self.token = None
        self.queue = None
        self.stopping = False
        self.connected_at = None
        # parts of the session (the picture…): on_stream(header, rest, rs) -> bool (async),
        # on_datagram(data) -> bool, connected(session) — all on the network thread
        self.handlers = []

    # ── thread-safe ─────────────────────────────────────────────────
    def start(self):
        self.net.call(self._start)

    def stop(self):
        self.stopping = True
        self.net.call(self._leave)

    def post(self, obj):
        self.net.call(self._post, obj)

    def path(self):
        return path_info(self.conn) if self.conn is not None and self.state == "connected" else {}

    # ── network thread ──────────────────────────────────────────────
    def _start(self):
        self._task = self.net.spawn(self._run())

    def _post(self, obj):
        if self.queue is not None:
            self.queue.put_nowait(obj)

    def _leave(self):
        if self.state == "closed":
            return
        if self.queue is not None:
            self.queue.put_nowait({"t": "bye"})
            self.queue.put_nowait(None)
        conn = self.conn
        if conn is not None:
            # a moment for the bye to go out; the close carries "bye" anyway
            self.net.loop.call_later(0.3, self._close_conn, conn, P.CLOSE_NORMAL, "bye")
        else:                                # still dialling: give up on it
            self._task.cancel()
            self._set("closed", reason="left")

    @staticmethod
    def _close_conn(conn, code, reason):
        try:
            conn.close(code, reason.encode())
        except Exception:
            pass

    def _set(self, state, **detail):
        self.state, self.detail = state, detail
        self.events.state_changed(self)

    def _retry(self, lost_at):
        return lost_at is not None and not self.stopping and time.monotonic() - lost_at < RECONNECT_FOR

    async def _run(self):
        lost_at, attempt = None, 0
        while True:
            self._set("reconnecting" if lost_at else "connecting")
            try:
                conn = await self.node.connect(self.target["id"], self.target.get("relay"),
                                               self.target.get("addrs") or [], timeout=20)
            except Exception as e:
                if self._retry(lost_at):
                    await asyncio.sleep(BACKOFF[min(attempt, len(BACKOFF) - 1)])
                    attempt += 1
                    continue
                return self._set("closed", reason="lost" if lost_at else "unreachable", error=str(e))
            if self.stopping:
                self._close_conn(conn, P.CLOSE_NORMAL, "bye")
                return self._set("closed", reason="left")
            result = await self._session(conn)
            self.conn = None
            if self.stopping:
                return self._set("closed", reason="left")
            if result[0] == "refused":
                return self._set("closed", reason="refused", auth=result[1], retry_after=result[2])
            if result[0] == "lost" and self.token:
                if result[1] or lost_at is None:      # it was up this time: a fresh minute
                    lost_at, attempt = time.monotonic(), 0
                if self._retry(lost_at):
                    await asyncio.sleep(BACKOFF[min(attempt, len(BACKOFF) - 1)])
                    attempt += 1
                    continue
            return self._set("closed", reason=result[1] if result[0] == "ended" else "lost")

    async def _session(self, conn):
        """One connection: ("refused", reason, retry_after) | ("ended", why) | ("lost", was_up)."""
        self.conn = conn
        try:
            bi = await conn.open_bi()
            send = bi.send()
            await send.set_priority(3)
            reader = LineReader(bi.recv())
            hello = P.make_hello(self.my_name, self.want, VERSION, resume=self.token,
                                 password=None if self.token else self.password,
                                 remember=self.remember)
            await send.write_all(P.line(hello))
            loop = asyncio.get_running_loop()
            deadline = loop.time() + AUTH_TIMEOUT
            while True:
                raw = await asyncio.wait_for(reader.next(), max(0.1, deadline - loop.time()))
                if raw is None:
                    return await self._ended(conn, False)
                msg = P.parse(raw)
                if msg is None:
                    continue
                if msg["t"] == "auth.wait":
                    self._set("waiting", timeout=msg.get("timeout", 30), since=time.monotonic())
                elif msg["t"] == "auth":
                    break
        except Exception:
            self._close_conn(conn, P.CLOSE_NORMAL, "error")
            return "lost", False
        if not msg.get("ok"):
            self.token = None
            self._close_conn(conn, P.CLOSE_NORMAL, "refused")
            return "refused", str(msg.get("reason", "denied")), msg.get("retry_after")
        self.password = None                 # used once; reconnecting goes by the token
        self.sid = msg.get("sid")
        self.rights = P.clean_rights(msg.get("rights"))
        self.token = msg.get("resume")
        self.remembered = msg.get("remembered")
        self.host = msg.get("host") if isinstance(msg.get("host"), dict) else {}
        self.connected_at = time.monotonic()
        self.queue = asyncio.Queue()
        self._set("connected")
        for h in self.handlers:
            h.connected(self)
        tasks = [self.net.spawn(c) for c in (self._read(reader, conn), self._write(send),
                                             self._accept_streams(conn), self._read_datagrams(conn))]
        result = await self._ended(conn, True)
        for t in tasks:
            t.cancel()
        self.queue = None
        return result

    async def _ended(self, conn, was_up):
        try:
            text = await conn.closed()
        except Exception as e:
            text = str(e)
        if self.stopping:
            return "ended", "left"
        code = P.close_code(text)
        if code is None or code == P.CLOSE_IDLE:
            return "lost", was_up
        return "ended", ENDED.get(code, "host_closed")

    async def _read(self, reader, conn):
        while True:
            try:
                raw = await asyncio.wait_for(reader.next(), IDLE_TIMEOUT)
            except asyncio.TimeoutError:
                self._close_conn(conn, P.CLOSE_IDLE, "idle")
                return
            if raw is None:
                return
            msg = P.parse(raw)
            if msg is None or msg["t"] == "ping":
                continue
            if msg["t"] == "rights":
                self.rights = P.clean_rights(msg.get("rights"))
                self.events.state_changed(self)
            if not any(getattr(h, "on_message", None) and h.on_message(msg) for h in self.handlers):
                self.events.message(self, msg)

    async def _accept_streams(self, conn):
        while True:
            try:
                rs = await conn.accept_uni()
            except Exception:
                return
            self.net.spawn(self._stream(rs))

    async def _stream(self, rs):
        try:
            header, rest = await asyncio.wait_for(read_stream_header(rs), 10)
        except Exception:
            header, rest = None, b""
        if header is not None and header.get("sid") == self.sid:
            for h in self.handlers:
                if await h.on_stream(header, rest, rs):
                    return
        await stop_stream(rs)

    async def _read_datagrams(self, conn):
        while True:
            try:
                data = await conn.read_datagram()
            except Exception:
                return
            if len(data) < 5 or data[1:5] != P.dgram_tag(self.sid):
                continue
            for h in self.handlers:
                if h.on_datagram(data):
                    break

    def send_datagram(self, data):
        """Network thread; lost on the way is fine."""
        conn = self.conn
        if conn is not None:
            try:
                conn.send_datagram(data)
            except Exception:
                pass

    async def _write(self, send):
        while True:
            try:
                obj = await asyncio.wait_for(self.queue.get(), PING_EVERY)
            except asyncio.TimeoutError:
                obj = {"t": "ping"}
            if obj is None:
                return
            try:
                await send.write_all(P.line(obj))
            except Exception:
                return
