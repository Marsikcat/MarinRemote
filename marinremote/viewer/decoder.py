"""
The viewer's decoder: H.264 frames of the current segment in, the newest picture out — a
QImage at the size the window shows it. Decodes on a thread of its own, straight from
packets (no demuxer, nothing buffered). Every frame is decoded, since a P-frame needs the one
before it, but only the newest of a batch is converted for showing, so a decoder that fell
behind catches up instead of showing old pictures.
"""

import threading
import time
from collections import deque

from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QImage

BACKLOG = 90            # frames waiting: hopelessly behind, start over at a keyframe


class Decoder(QObject):
    frame_ready = Signal()          # a new picture: take it with take()
    cursor_moved = Signal()         # the remote pointer moved (any thread → the UI's)

    def __init__(self, ask_key=None):
        super().__init__()
        self.ask_key = ask_key or (lambda: None)     # called on the decoder thread
        self.target = (1280, 720)   # device pixels available for the picture (set by the UI)
        self.source_size = (0, 0)
        self.decoded = 0            # in total, for the frame rate
        self.shown_in_seg = 0       # pictures handed out in the current segment (for acks)
        self.decode_ms = 0.0
        self._q = deque()
        self._cv = threading.Condition()
        self._stopped = False
        self._lock = threading.Lock()
        self._picture = None        # (QImage, capture time µs)
        self._waiting = False
        self._seg = None
        self._last_ask = 0.0
        threading.Thread(target=self._run, daemon=True, name="video-decoder").start()

    # ── any thread ──────────────────────────────────────────────────
    def feed(self, seg, data, ts, key):
        with self._cv:
            if len(self._q) > BACKLOG:
                self._q.clear()
                self._seg = None                    # needs a keyframe now
                self._ask()
            self._q.append((seg, data, ts, key))
            self._cv.notify()

    def stop(self):
        with self._cv:
            self._stopped = True
            self._cv.notify_all()

    def take(self):
        """The newest picture (QImage, capture µs) or None; the next frame_ready comes after this."""
        with self._lock:
            self._waiting = False
            return self._picture

    # ── decoder thread ──────────────────────────────────────────────
    def _ask(self):
        now = time.monotonic()
        if now - self._last_ask > 0.5:
            self._last_ask = now
            self.ask_key()

    def _run(self):
        import av
        from av.codec.context import Flags
        from av.video.reformatter import VideoReformatter
        reformatter = VideoReformatter()
        ctx = None
        need_key = True
        while True:
            with self._cv:
                while not self._q and not self._stopped:
                    self._cv.wait()
                if self._stopped:
                    return
                batch = list(self._q)
                self._q.clear()
            for i, (seg, data, ts, key) in enumerate(batch):
                if seg != self._seg or ctx is None:
                    # a new segment: a fresh decoder, nothing carried over from the old picture
                    self._seg = seg
                    self.shown_in_seg = 0
                    ctx = av.CodecContext.create("h264", "r")
                    ctx.flags |= Flags.low_delay
                    need_key = True
                if need_key and not key:
                    continue
                need_key = False
                t0 = time.perf_counter()
                try:
                    frames = ctx.decode(av.Packet(data))
                except av.error.FFmpegError:
                    need_key = True
                    self._ask()
                    continue
                self.decode_ms = self.decode_ms * 0.9 + (time.perf_counter() - t0) * 100
                for frame in frames:
                    self.decoded += 1
                    if i == len(batch) - 1:
                        self._publish(frame, ts, reformatter)

    def _publish(self, frame, ts, reformatter):
        w, h = frame.width, frame.height
        self.source_size = (w, h)
        tw, th = self.target
        k = min(tw / w, th / h, 1.0)              # never bigger than the source: the UI upscales
        dw, dh = max(2, int(w * k) // 2 * 2), max(2, int(h * k) // 2 * 2)
        rgb = reformatter.reformat(frame, width=dw, height=dh, format="bgra",
                                   interpolation="AREA" if k < 1 else "POINT", threads=2)
        # straight from FFmpeg's buffer: rows can be padded for alignment (line_size > dw * 4)
        plane = rgb.planes[0]
        img = QImage(bytes(plane), dw, dh, plane.line_size, QImage.Format_RGB32).copy()
        with self._lock:
            self._picture = (img, ts)
            notify = not self._waiting
            self._waiting = True
        self.shown_in_seg += 1
        if notify:
            self.frame_ready.emit()
