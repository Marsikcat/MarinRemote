"""
The picture for viewers, on the host's side.

Every viewer with the "view" right watches one monitor at one quality; one encoder process
serves everyone watching the same monitor at the same quality (capture.Encoder).

Frames go to a viewer on one-way QUIC streams — a *segment* per stream, each starting at a
keyframe. The viewer acknowledges what it has received (datagrams, ~20 a second). When frames
sit unacknowledged longer than max(150 ms, 2·RTT + 50 ms), or pile up here because QUIC won't
take them, the segment is reset — QUIC throws away whatever it still had queued, nothing stale
is retransmitted — and the next segment starts at a fresh keyframe. Whole frames are dropped,
never bytes, so the decoder never sees half a picture.

With quality "auto" a viewer that keeps falling behind (3 resets in 15 s) moves one step down
the AUTO_STEPS ladder, and one step back up after a quiet spell (no resets for UP_AFTER seconds),
up to the ceiling of its path: the top for a direct path, step 2 through a relay — so the picture
also gets better by itself once iroh moves a relayed connection onto a direct one. A step up that
soon ends in a step down doubles the wait before the next try (up to UP_WAIT_MAX), so the
quality doesn't swing up and down on a link that can't hold the higher step.

The pointer goes separately: its position by datagram (up to ~60 a second while it moves),
its shape on the control stream whenever it changes.
"""

import asyncio
import threading
import time
from collections import deque

from .. import protocol as P
from .. import winapi
from ..net import path_info
from . import cursor
from .capture import AUTO_STEPS, QUALITY, Encoder, has_nvenc
from .core import Service

LAG_MIN = 0.15
RESETS_TO_STEP_DOWN = 3
RESET_WINDOW = 15
RESTART_DELAYS = (0.5, 1, 2, 4, 5)
RELAY_CEILING = 2           # the best AUTO_STEPS rung through a relay
UP_WAIT_MAX = 300
PROBE_WINDOW = 30           # a step down this soon after a step up: that step up failed


def auto_step(step, ceiling, quiet_for, wait):
    """The rung "auto" should use now: one step up (towards the ceiling) after `wait` quiet seconds."""
    if step > ceiling and quiet_for >= wait:
        return step - 1
    return step


def _now_us():
    return time.perf_counter_ns() // 1000


async def _reset(stream):
    try:
        await stream.reset(0)
    except Exception:
        pass


class Segment:
    """One stream of frames to one viewer, from a keyframe on."""

    def __init__(self, watch, number, header):
        self.watch = watch
        self.number = number
        self.queue = deque()
        self.wake = asyncio.Event()
        self.sent = deque()          # (index, time written, keyframe?) not yet acknowledged
        self.index = 0
        self.stream = None
        self.dead = False
        self.task = watch.net.spawn(self._run(header))

    def push(self, frame):
        self.queue.append(frame)
        self.wake.set()

    async def _run(self, header):
        try:
            self.stream = await self.watch.s.conn.open_uni()
            await self.stream.set_priority(1)
            await self.stream.write_all(header)
            while True:
                if not self.queue:
                    self.wake.clear()
                    await self.wake.wait()
                    continue
                frame = self.queue.popleft()
                await self.stream.write_all(P.pack_frame(frame.data, frame.ts, P.FRAME_KEY if frame.key else 0))
                self.sent.append((self.index, time.monotonic(), frame.key))
                self.index += 1
                self.watch.bytes_out += len(frame.data)
        except asyncio.CancelledError:
            raise
        except Exception:
            if not self.dead:
                self.watch.restart()

    def abort(self):
        """Stop it here and tell QUIC to drop whatever of it is still queued."""
        if self.dead:
            return
        self.dead = True
        self.task.cancel()
        if self.stream is not None:
            self.watch.net.spawn(_reset(self.stream))


class Watch:
    """One viewer's picture: which monitor, which quality, the current segment."""

    def __init__(self, svc, s):
        self.svc, self.s, self.net = svc, s, svc.net
        self.monitor = None
        self.quality = "auto"
        self.step = None             # the AUTO_STEPS rung while quality is "auto"
        self.step_changed = time.monotonic()
        self.stepped_up = 0.0
        self.up_wait = svc.UP_AFTER  # quiet seconds before the next step up
        self.params = None
        self.enc_key = None
        self.seg = None
        self.seg_no = 0
        self.waiting_key = True
        self.waiting_since = 0.0
        self.paused = False
        self.acked = -1
        self.acks_seen = False
        self.shown = 0
        self.resets = deque()
        self.last_reset = 0.0
        self.bytes_out = 0
        self.dropped = 0
        self.size = (0, 0)
        self.cursor_ids = set()      # shapes this viewer already has
        self.cursor_shape = None
        self.cursor_last = (None, 0.0)
        self.cursor_seq = 0
        self.ticker = self.net.spawn(self._tick())

    # ── frames in ───────────────────────────────────────────────────
    def push(self, frame):
        if self.paused:
            return
        if self.waiting_key:
            if not frame.key:
                if time.monotonic() - self.waiting_since > 0.3:
                    self.svc.ask_key(self)
                    self.waiting_since = time.monotonic()
                return
            self.waiting_key = False
            self._new_segment()
        seg = self.seg
        if seg is None:
            return
        if len(seg.queue) >= max(3, self.params["fps"] // 10):     # ~100 ms not taken by QUIC
            self.congested("queue")
            return
        seg.push(frame)

    def _new_segment(self):
        if self.seg is not None:
            self.seg.abort()
        self.seg_no += 1
        self.acked = -1
        header = P.stream_header("video", self.s.sid, seg=self.seg_no, mon=self.monitor["id"],
                                 w=self.size[0], h=self.size[1], codec="h264")
        self.seg = Segment(self, self.seg_no, header)

    def restart(self):
        """Drop the current segment; the next one starts at a keyframe."""
        if self.seg is not None:
            self.dropped += len(self.seg.queue)
            self.seg.abort()
            self.seg = None
        self.waiting_key = True
        self.waiting_since = time.monotonic()
        self.svc.ask_key(self)

    # ── the viewer's acknowledgements ───────────────────────────────
    def on_ack(self, seg_no, received, shown):
        if seg_no != self.seg_no or self.seg is None:
            return
        self.acks_seen = True
        self.shown = shown
        self.acked = max(self.acked, received - 1)
        sent = self.seg.sent
        while sent and sent[0][0] <= self.acked:
            sent.popleft()
        self.check()

    def threshold(self):
        rtt = path_info(self.s.conn).get("rtt") or 0
        return max(LAG_MIN, 2 * rtt / 1000 + 0.05)

    def check(self):
        seg = self.seg
        if seg is None or self.paused or not self.acks_seen or not seg.sent:
            return
        _, written, key = seg.sent[0]
        limit = self.threshold() * (4 if key else 1)      # a keyframe is several frames' worth
        if time.monotonic() - written > limit:
            self.congested("lag")

    def congested(self, reason):
        now = time.monotonic()
        counts = now - self.last_reset > 0.3              # one jam, one count
        self.last_reset = now
        self.restart()
        if counts:
            self.resets.append(now)
            while self.resets and now - self.resets[0] > RESET_WINDOW:
                self.resets.popleft()
            self.svc.on_congestion(self, reason)

    async def _tick(self):
        while True:
            await asyncio.sleep(0.05)
            self.check()
            if self.waiting_key and not self.paused and time.monotonic() - self.waiting_since > 0.5:
                self.svc.ask_key(self)                    # the encoder may have missed the first ask
                self.waiting_since = time.monotonic()

    def set_paused(self, on):
        if on == self.paused:
            return
        self.paused = on
        if on and self.seg is not None:
            self.seg.abort()
            self.seg = None
        self.waiting_key = True
        self.waiting_since = time.monotonic()
        if not on:
            self.svc.ask_key(self)

    # ── the pointer ─────────────────────────────────────────────────
    def send_cursor(self, shape, visible, x, y):
        if self.paused or self.monitor is None:
            return
        if shape is not None and shape["id"] != self.cursor_shape:
            self.cursor_shape = shape["id"]
            if shape["id"] in self.cursor_ids:
                self.s.post({"t": "cursor", "id": shape["id"]})
            else:
                self.cursor_ids.add(shape["id"])
                self.s.post({"t": "cursor", **cursor.packed(shape)})
        m = self.monitor
        here = m["x"] <= x < m["x"] + m["w"] and m["y"] <= y < m["y"] + m["h"]
        state = (P.norm(x, m["x"], m["w"]), P.norm(y, m["y"], m["h"]),
                 (P.CURSOR_VISIBLE if visible else 0) | (P.CURSOR_HERE if here else 0))
        last, when = self.cursor_last
        now = time.monotonic()
        if state == last and now - when < 0.5:
            return
        self.cursor_last = (state, now)
        self.cursor_seq += 1
        try:
            self.s.conn.send_datagram(P.pack_cursor(self.s.sid, self.cursor_seq, *state))
        except Exception:
            pass

    def close(self):
        self.ticker.cancel()
        if self.seg is not None:
            self.seg.abort()
            self.seg = None


class _Entry:
    """An encoder process and the viewers it serves."""

    def __init__(self, key, monitor, params, encoder):
        self.key, self.monitor, self.params, self.encoder = key, monitor, params, encoder
        self.watches = set()
        self.enc = None
        self.info = None
        self.failures = 0
        self.restart_handle = None
        self.lost = []               # when desktop duplication was lost lately
        self.no_dda = False          # it keeps being lost on this monitor: GDI instead


class VideoService(Service):
    UP_AFTER = 20                    # quiet seconds before "auto" tries one step up

    def __init__(self, core, monitors=None, encoder="auto"):
        self.core, self.net = core, core.net
        self.monitors_fn = monitors or winapi.monitors
        self.monitors = []
        self.encoder_pref = encoder
        self.watches = {}            # sid -> Watch
        self.entries = {}            # (monitor dev, fps, kbps, h) -> _Entry
        self._background = None

    # ── sessions ────────────────────────────────────────────────────
    def session_started(self, s):
        if not s.has("view"):
            return
        w = Watch(self, s)
        self.watches[s.sid] = w
        self._refresh_monitors()
        w.monitor = next((m for m in self.monitors if m["primary"]), self.monitors[0] if self.monitors else None)
        if w.monitor is None:
            s.post({"t": "video.state", "state": "unavailable", "reason": "no_monitor"})
            return
        self._send_monitors(w)
        self._attach(w)
        if self._background is None:
            self._background = [self.net.spawn(self._cursor_loop()), self.net.spawn(self._monitor_loop()),
                                self.net.spawn(self._quality_loop())]

    def session_ended(self, s):
        w = self.watches.pop(s.sid, None)
        if w is None:
            return
        self._detach(w)
        w.close()
        if not self.watches and self._background:
            for t in self._background:
                t.cancel()
            self._background = None

    def on_message(self, s, msg):
        w = self.watches.get(s.sid)
        t = msg["t"]
        if w is None or not (t.startswith("video.") or t in ("monitor", "clock")):
            return False
        if t == "clock":
            s.post({"t": "clock", "v": msg.get("v"), "h": _now_us()})
        elif t == "monitor":
            m = next((m for m in self.monitors if m["id"] == msg.get("id")), None)
            if m is not None and m is not w.monitor:
                w.monitor = m
                w.cursor_last = (None, 0.0)
                self._send_monitors(w)
                self._attach(w)
        elif t == "video.quality":
            if msg.get("preset") in QUALITY:
                w.quality = msg["preset"]
                w.step = None
                w.step_changed, w.up_wait = time.monotonic(), self.UP_AFTER
                self._attach(w)
        elif t == "video.key":
            self.ask_key(w)
        elif t == "video.pause":
            w.set_paused(bool(msg.get("on")))
        return True

    def on_datagram(self, s, data):
        if data[0] != P.DG_VIDEO_ACK:
            return False
        w = self.watches.get(s.sid)
        ack = P.unpack_ack(data)
        if w is not None and ack is not None:
            w.on_ack(*ack)
        return True

    # ── encoders ────────────────────────────────────────────────────
    def _params(self, w):
        if w.quality != "auto":
            return dict(QUALITY[w.quality])
        if w.step is None:
            w.step = 2 if path_info(w.s.conn)["via_relay"] else 0
        return dict(AUTO_STEPS[w.step])

    def _attach(self, w):
        params = self._params(w)
        m = w.monitor
        key = (m["dev"], params["fps"], params["kbps"], params.get("h"))
        if key == w.enc_key:
            return
        self._detach(w)
        w.params = params
        w.enc_key = key
        entry = self.entries.get(key)
        if entry is None:
            enc = self.encoder_pref
            if enc == "auto":
                enc = "nvenc" if has_nvenc() else "x264"
            entry = self.entries[key] = _Entry(key, m, params, enc)
            self._start(entry)
        entry.watches.add(w)
        w.restart()
        if entry.info:
            self._post_state(w, entry)
        else:
            w.s.post({"t": "video.state", "state": "starting", "mon": m["id"]})

    def _detach(self, w):
        entry = self.entries.get(w.enc_key)
        w.enc_key = None
        if w.seg is not None:
            w.seg.abort()
            w.seg = None
        if entry is None:
            return
        entry.watches.discard(w)
        if not entry.watches:
            del self.entries[entry.key]
            if entry.restart_handle is not None:
                entry.restart_handle.cancel()
            if entry.enc is not None:
                enc, entry.enc = entry.enc, None
                threading.Thread(target=enc.stop, daemon=True).start()   # may wait for the process

    def _source(self, entry):
        m = next((x for x in self.monitors if x["dev"] == entry.monitor["dev"]), entry.monitor)
        entry.monitor = m
        if m.get("test"):
            return {"kind": "test", "w": m["w"], "h": m["h"]}
        return {"kind": "monitor", "x": m["x"], "y": m["y"], "w": m["w"], "h": m["h"],
                "dxgi": None if entry.no_dda else m.get("dxgi")}

    def _start(self, entry):
        entry.restart_handle = None
        key = entry.key
        enc = Encoder(self._source(entry), entry.params, entry.encoder,
                      lambda f: self.net.call(self._deliver, key, f),
                      lambda st, info: self.net.call(self._encoder_state, key, enc, st, info))
        entry.enc = enc
        entry.info = None
        enc.start()

    def _deliver(self, key, frame):
        entry = self.entries.get(key)
        if entry is None:
            return
        for w in list(entry.watches):
            w.push(frame)

    def _encoder_state(self, key, enc, state, info):
        entry = self.entries.get(key)
        if entry is None or entry.enc is not enc:
            return
        if state == "ok":
            entry.info = info
            entry.failures = 0
            for w in entry.watches:
                w.size = (info["w"], info["h"])
                w.restart()
                self._post_state(w, entry)
            return
        # the process is gone: tell the viewers why, try again shortly
        entry.enc = None
        entry.info = None
        error = info.get("error") or ""
        if info.get("code") == 3:                  # the picture changed size: right away
            self._refresh_monitors()
            delay = 0.1
        elif info.get("code") == 4:                # Windows took the desktop duplication back
            now = time.monotonic()
            entry.lost = [t for t in entry.lost if now - t < 10] + [now]
            if len(entry.lost) >= 2 and not entry.no_dda:
                entry.no_dda = True                # e.g. a monitor on another video adapter
                print(f"encoder: desktop duplication keeps failing on {entry.monitor['dev']} — GDI", flush=True)
            delay = 0.2
        else:
            entry.failures += 1
            delay = RESTART_DELAYS[min(entry.failures - 1, len(RESTART_DELAYS) - 1)]
            if entry.encoder == "nvenc" and entry.failures >= 2:
                entry.encoder = "x264"             # NVENC keeps failing: the processor encodes
            reason = "secure_desktop" if any(k in error for k in ("80070005", "E_ACCESSDENIED", "Access", "8007")) \
                else "error"
            for w in entry.watches:
                w.s.post({"t": "video.state", "state": "unavailable", "reason": reason, "text": error[-300:]})
            print(f"encoder ended: {error}", flush=True)
        entry.restart_handle = self.net.loop.call_later(delay, self._start, entry)

    def ask_key(self, w):
        entry = self.entries.get(w.enc_key)
        if entry is not None and entry.enc is not None:
            entry.enc.request_key()

    def on_congestion(self, w, reason):
        if w.quality == "auto" and len(w.resets) >= RESETS_TO_STEP_DOWN and w.step < len(AUTO_STEPS) - 1:
            now = time.monotonic()
            if now - w.stepped_up < PROBE_WINDOW:      # the last step up didn't hold: wait longer next time
                w.up_wait = min(w.up_wait * 2, UP_WAIT_MAX)
            w.step += 1
            w.step_changed = now
            w.resets.clear()
            self._attach(w)

    async def _quality_loop(self):
        """"auto" climbs back up a step at a time while the viewer keeps up."""
        while True:
            await asyncio.sleep(1)
            now = time.monotonic()
            for w in list(self.watches.values()):
                if w.quality != "auto" or w.step is None or w.paused:
                    continue
                if w.stepped_up and now - w.stepped_up > 2 * PROBE_WINDOW:
                    w.up_wait = self.UP_AFTER                  # that step held: back to the short wait
                ceiling = RELAY_CEILING if path_info(w.s.conn)["via_relay"] else 0
                quiet = now - max(w.step_changed, w.last_reset)
                step = auto_step(w.step, ceiling, quiet, w.up_wait)
                if step != w.step:
                    w.step, w.step_changed, w.stepped_up = step, now, now
                    w.resets.clear()
                    self._attach(w)

    def _post_state(self, w, entry):
        info = entry.info
        w.size = (info["w"], info["h"])
        w.s.post({"t": "video.state", "state": "ok", "mon": w.monitor["id"], "w": info["w"], "h": info["h"],
                  "fps": entry.params["fps"], "kbps": entry.params["kbps"], "encoder": info["encoder"],
                  "capture": info["capture"], "quality": w.quality, "step": w.step})

    # ── monitors ────────────────────────────────────────────────────
    def _refresh_monitors(self):
        try:
            found = self.monitors_fn()
        except Exception:
            found = self.monitors
        changed = [(m["dev"], m["x"], m["y"], m["w"], m["h"]) for m in found] != \
                  [(m["dev"], m["x"], m["y"], m["w"], m["h"]) for m in self.monitors]
        self.monitors = found
        return changed

    def _send_monitors(self, w):
        w.s.post({"t": "monitors", "current": w.monitor["id"] if w.monitor else None,
                  "list": [{k: m[k] for k in ("id", "name", "x", "y", "w", "h", "primary")} for m in self.monitors]})

    async def _monitor_loop(self):
        while True:
            await asyncio.sleep(3)
            if not self._refresh_monitors():
                continue
            for w in list(self.watches.values()):
                same = next((m for m in self.monitors if w.monitor and m["dev"] == w.monitor["dev"]), None)
                w.monitor = same or next((m for m in self.monitors if m["primary"]), None)
                if w.monitor is None:
                    continue
                self._send_monitors(w)
                if same is None:
                    self._attach(w)

    async def _cursor_loop(self):
        handle, shape = None, None
        while True:
            await asyncio.sleep(1 / 60)
            visible, x, y, h = cursor.current()
            if h != handle:
                handle = h
                try:
                    shape = cursor.shape(h) if h else None
                except Exception:
                    shape = None
            for w in list(self.watches.values()):
                w.send_cursor(shape, visible, x, y)
