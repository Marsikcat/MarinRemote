"""
Mouse and keyboard from the viewer's window to the host.

Moves go by datagram — lost or late is fine, the newest wins (the host plays only a sequence
number newer than any it has seen). Clicks, the wheel and keys go on one reliable, ordered
stream with the same sequence numbers, so a late move never undoes a click.
Keys travel as scan codes (+ the extended flag) and the virtual-key code as a hint, never as
characters: the host's own layout decides what they type.

Thread-safe: the UI and the keyboard hook call in from their threads.
"""

import asyncio
import threading

from .. import protocol as P

BUTTONS = ("left", "right", "middle", "x1", "x2")


class InputSender:
    def __init__(self, session):
        self.session = session
        self.net = session.net
        self._lock = threading.Lock()
        self._seq = 0
        self._queue = None
        self._task = None
        self.keys = set()            # (scan, ext, vk) we have pressed on the host
        self.buttons = set()
        session.handlers.append(self)

    # ── session handler (network thread) ────────────────────────────
    def connected(self, session):
        if self._task is not None:
            self._task.cancel()
        self._queue = asyncio.Queue()
        self._task = self.net.spawn(self._writer(session.conn, session.sid, self._queue))
        with self._lock:
            self.keys.clear()
            self.buttons.clear()

    async def on_stream(self, header, rest, rs):
        return False

    def on_datagram(self, data):
        return False

    async def _writer(self, conn, sid, queue):
        """One input stream for the session, opened again if it breaks."""
        while True:
            try:
                stream = await conn.open_uni()
                await stream.set_priority(3)
                await stream.write_all(P.stream_header("input", sid))
                while True:
                    batch = [await queue.get()]
                    while not queue.empty():
                        batch.append(queue.get_nowait())
                    await stream.write_all(b"".join(P.line(e) for e in batch))
            except asyncio.CancelledError:
                raise
            except Exception:
                if self.session.conn is not conn:
                    return
                await asyncio.sleep(0.2)

    def _put(self, ev):
        if self._queue is not None:
            self._queue.put_nowait(ev)

    # ── any thread ──────────────────────────────────────────────────
    def _next(self):
        with self._lock:
            self._seq = (self._seq + 1) & 0xFFFFFFFF
            return self._seq

    def _send(self, ev):
        self.net.call(self._put, ev)

    def move(self, x, y):
        sid = self.session.sid
        if sid and self.session.state == "connected":
            self.net.call(self.session.send_datagram, P.pack_mouse(sid, self._next(), x, y))

    def button(self, name, down, x, y):
        if name not in BUTTONS:
            return
        with self._lock:
            (self.buttons.add if down else self.buttons.discard)(name)
        self._send({"m": "down" if down else "up", "b": name, "x": x, "y": y, "s": self._next()})

    def wheel(self, dx, dy, x, y):
        if dx or dy:
            self._send({"m": "wheel", "dx": int(dx), "dy": int(dy), "x": x, "y": y, "s": self._next()})

    def key(self, scan, ext, down, vk):
        k = (scan & 0xFF, 1 if ext else 0, vk & 0xFF)
        with self._lock:
            (self.keys.add if down else self.keys.discard)(k)
        self._send({"k": k[0], "e": k[1], "d": 1 if down else 0, "vk": k[2]})

    def release_all(self):
        """Let go of everything (the window lost focus: its key-ups will never come)."""
        with self._lock:
            held = bool(self.keys or self.buttons)
            self.keys.clear()
            self.buttons.clear()
        if held:
            self._send({"rel": "all"})


def qt_key(event):
    """(scan, extended, vk) of a Qt key event on Windows, or None. Qt keeps the extended bit
    as bit 8 of the native scan code."""
    sc, vk = event.nativeScanCode(), event.nativeVirtualKey()
    scan, ext = sc & 0xFF, (sc >> 8) & 1
    if not scan and not vk:
        return None
    return scan, ext, vk & 0xFF
