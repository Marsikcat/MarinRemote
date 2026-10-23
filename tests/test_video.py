"""
The picture end to end: a host with the video service (a test pattern through x264 instead of
a real monitor) and a viewer that decodes it — over two real iroh endpoints on 127.0.0.1.
Plus the segment logic on its own: a viewer that stops acknowledging gets its segment reset
and the next one starts at a keyframe.
"""
import asyncio
import time

import pytest
from PySide6.QtCore import QCoreApplication

from marinremote import protocol as P
from marinremote.config import Settings
from marinremote.host.capture import Frame
from marinremote.host.core import Approver, HostCore
from marinremote.host.video import VideoService, Watch
from marinremote.journal import Journal
from marinremote.net import Node
from marinremote.viewer.decoder import Decoder
from marinremote.viewer.session import ViewerEvents, ViewerSession
from marinremote.viewer.video import VideoReceiver

MONITORS = [{"id": 0, "dev": "test0", "name": "Тест 1", "x": 0, "y": 0, "w": 640, "h": 360,
             "primary": True, "dxgi": None, "test": True},
            {"id": 1, "dev": "test1", "name": "Тест 2", "x": 640, "y": 0, "w": 320, "h": 240,
             "primary": False, "dxgi": None, "test": True}]


class Yes(Approver):
    async def ask(self, request):
        return {"allow": True, "rights": request["want"], "remember": None, "block": False}


class Messages(ViewerEvents):
    def __init__(self):
        self.msgs = []
        self.states = []

    def state_changed(self, s):
        self.states.append(s.state)

    def message(self, s, msg):
        self.msgs.append(msg)


def until(cond, timeout=20):
    deadline = time.time() + timeout
    while not cond():
        assert time.time() < deadline, "condition never came true"
        time.sleep(0.05)


def make_pair(tmp_path, receiver=VideoReceiver):
    QCoreApplication.instance() or QCoreApplication([])
    hs, vs = Settings(tmp_path / "h"), Settings(tmp_path / "v")
    host = Node(hs["secret_key"], port=0, mode="lan", bind_host="127.0.0.1", lan=False)
    viewer = Node(vs["secret_key"], port=0, mode="lan", bind_host="127.0.0.1", lan=False)
    host.start()
    viewer.start()
    core = HostCore(hs, host, Journal(tmp_path / "j.jsonl"), Yes())
    svc = VideoService(core, monitors=lambda: [dict(m) for m in MONITORS], encoder="x264")
    core.services.append(svc)
    events = Messages()
    s = ViewerSession(viewer, {"id": host.id, "relay": None, "addrs": [f"127.0.0.1:{host.port}"]},
                      ["view"], "зритель", events)
    dec = Decoder()
    rx = receiver(s, dec)
    s.start()

    def close():
        s.stop()
        time.sleep(0.5)
        dec.stop()
        viewer.stop()
        host.stop()
        until(lambda: not svc.entries, 5)                # the encoder process is gone
    return (s, rx, dec, events, svc), close


@pytest.fixture
def video_pair(tmp_path):
    pair, close = make_pair(tmp_path)
    yield pair
    close()


class SlowReceiver(VideoReceiver):
    """Reads its stream at a third of the frame rate: a viewer on a link too slow for the picture."""

    async def on_stream(self, header, rest, rs):
        if header.get("t") == "video":
            self.segments = getattr(self, "segments", 0) + 1
        return await super().on_stream(header, rest, _Slow(rs))


class _Slow:
    def __init__(self, rs):
        self.rs = rs

    async def read(self, n):
        await asyncio.sleep(0.1)
        return await self.rs.read(min(n, 2000))

    async def stop(self, code):
        await self.rs.stop(code)


def test_falling_behind_resets_the_segment_and_lowers_auto_quality(tmp_path):
    (s, rx, dec, events, svc), close = make_pair(tmp_path, SlowReceiver)
    try:
        until(lambda: getattr(rx, "segments", 0) >= 3, 30)          # reset, and a fresh segment again
        w = next(iter(svc.watches.values()))
        until(lambda: w.step >= 1, 30)                              # "auto" stepped down
        until(lambda: any(m["t"] == "video.state" and m.get("step", 0) >= 1 for m in events.msgs), 10)
    finally:
        close()


def test_picture_arrives_and_follows_the_monitor(video_pair):
    s, rx, dec, events, svc = video_pair
    until(lambda: dec.decoded >= 20)
    img, ts = dec.take()
    assert (img.width(), img.height()) == (640, 360) and rx.size == (640, 360)
    mons = next(m for m in events.msgs if m["t"] == "monitors")
    assert [m["id"] for m in mons["list"]] == [0, 1] and mons["current"] == 0
    ok = next(m for m in events.msgs if m["t"] == "video.state" and m["state"] == "ok")
    assert ok["encoder"].startswith("x264") and (ok["w"], ok["h"]) == (640, 360)
    assert 0 < time.perf_counter_ns() // 1000 - ts < 2_000_000       # capture time is recent
    # the viewer acknowledges: the host knows what got through
    w = next(iter(svc.watches.values()))
    until(lambda: w.acks_seen and w.acked >= 5)

    # another monitor: a new encoder, a new segment, the new size
    seg = rx.seg
    s.post({"t": "monitor", "id": 1})
    until(lambda: rx.seg > seg and dec.source_size == (320, 240))
    until(lambda: len(svc.entries) == 1)                  # the first encoder was stopped

    # a keyframe on request
    frames = dec.decoded
    s.post({"t": "video.key"})
    until(lambda: dec.decoded > frames + 5)

    # the clock: the host answers with its time
    s.post({"t": "clock", "v": 123})
    until(lambda: any(m["t"] == "clock" and m["v"] == 123 and m["h"] > 0 for m in events.msgs))


def test_pause_stops_the_picture(video_pair):
    s, rx, dec, events, svc = video_pair
    until(lambda: dec.decoded >= 10)
    s.post({"t": "video.pause", "on": True})
    time.sleep(0.5)
    n = rx.frames
    time.sleep(0.7)
    assert rx.frames == n
    s.post({"t": "video.pause", "on": False})
    until(lambda: rx.frames > n + 5)


def test_pointer_position_comes_by_datagram(video_pair):
    s, rx, dec, events, svc = video_pair
    until(lambda: rx.cursor is not None)
    x, y, flags = rx.cursor
    assert 0 <= x <= 65535 and 0 <= y <= 65535
    shape = next((m for m in events.msgs if m["t"] == "cursor"), None)
    if shape is not None and "bgra" in shape:                     # if the pointer is showing at all
        from marinremote.host.cursor import unpacked
        assert unpacked(shape) is not None


# ── the segment logic, without a network ───────────────────────────
class FakeStream:
    def __init__(self, log):
        self.log = log
        self.written = []
        self.was_reset = False

    async def set_priority(self, p):
        pass

    async def write_all(self, data):
        self.written.append(data)

    async def reset(self, code):
        self.was_reset = True
        self.log.append("reset")


class FakeConn:
    def __init__(self):
        self.streams = []
        self.log = []

    async def open_uni(self):
        st = FakeStream(self.log)
        self.streams.append(st)
        return st

    def paths(self):
        return []


class FakeSession:
    sid = "0123456789abcdef"

    def __init__(self):
        self.conn = FakeConn()
        self.posted = []

    def post(self, msg):
        self.posted.append(msg)


class FakeService:
    def __init__(self, loop):
        self.net = type("N", (), {"spawn": staticmethod(lambda c: loop.create_task(c))})()
        self.keys = 0

    def ask_key(self, w):
        self.keys += 1

    def on_congestion(self, w, reason):
        pass


def test_a_viewer_that_falls_behind_gets_a_fresh_segment():
    async def scenario():
        loop = asyncio.get_running_loop()
        svc, s = FakeService(loop), FakeSession()
        w = Watch(svc, s)
        w.monitor, w.params, w.size = MONITORS[0], {"fps": 30}, (640, 360)
        frame = lambda key: Frame(b"\0\0\0\1" + (b"k" if key else b"p") * 100, 1, key)  # noqa: E731
        w.restart()
        w.push(frame(False))                       # no keyframe yet: dropped, a key is asked for
        await asyncio.sleep(0)
        assert w.seg is None
        w.push(frame(True))
        for _ in range(5):
            await asyncio.sleep(0.005)             # frames come a few ms apart, QUIC takes them
            w.push(frame(False))
        await asyncio.sleep(0.05)
        first = s.conn.streams[0]
        assert len(first.written) == 1 + 6         # header + frames
        assert first.written[0].startswith(P.STREAM_MAGIC) and b'"seg":1' in first.written[0]
        w.on_ack(1, 3, 3)                          # the viewer got three of them…
        assert w.seg.sent[0][0] == 3
        await asyncio.sleep(0.25)                  # …and then nothing for longer than the limit
        w.check()
        assert first.was_reset and w.waiting_key and w.seg is None
        keys = svc.keys
        w.push(frame(False))                       # still no keyframe: nothing goes out
        assert w.seg is None and svc.keys >= keys
        w.push(frame(True))
        await asyncio.sleep(0.05)
        second = s.conn.streams[1]
        assert b'"seg":2' in second.written[0] and len(second.written) == 2
        assert len(w.resets) == 1
        w.close()

    asyncio.run(scenario())


def test_a_jam_here_drops_whole_frames():
    async def scenario():
        loop = asyncio.get_running_loop()
        svc, s = FakeService(loop), FakeSession()
        w = Watch(svc, s)
        w.monitor, w.params, w.size = MONITORS[0], {"fps": 30}, (640, 360)
        w.restart()
        w.push(Frame(b"k", 1, True))
        for _ in range(10):                        # QUIC takes nothing (the task never runs)
            w.push(Frame(b"p", 1, False))
        assert w.waiting_key and w.seg is None     # reset after ~100 ms worth of frames
        w.close()

    asyncio.run(scenario())
