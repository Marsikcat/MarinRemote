"""
The remote screen in the session window: the newest decoded picture, fitted into the widget
(the decoder already scales it down; the painter only scales up), the remote pointer on top,
and an overlay for messages and statistics.
"""

import time
from collections import deque

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QFont, QImage, QPainter
from PySide6.QtWidgets import QWidget

from ..host.cursor import unpacked
from ..protocol import CURSOR_HERE, CURSOR_VISIBLE


def now_us():
    return time.perf_counter_ns() // 1000


class VideoWidget(QWidget):
    def __init__(self, decoder, parent=None):
        super().__init__(parent)
        self.decoder = decoder
        self.rx = None                  # the session's VideoReceiver (pointer position, size)
        self.image = None
        self.ts = 0
        self.cursors = {}               # shape id -> (QImage, hotspot x, hotspot y)
        self.cursor_id = None
        self.draw_cursor = True         # off while the local pointer stands in for it (stage c)
        self.message = ""
        self.stats = ""
        self.show_stats = False
        self.clock_offset = 0           # host clock - ours, µs
        self.latencies = deque(maxlen=120)
        self.painted = 0
        decoder.frame_ready.connect(self.update)
        decoder.cursor_moved.connect(self.update)
        self.setAttribute(Qt.WA_OpaquePaintEvent)
        self.setMouseTracking(True)
        self.setMinimumSize(320, 180)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        dpr = self.devicePixelRatioF()
        self.decoder.target = (max(2, int(self.width() * dpr)), max(2, int(self.height() * dpr)))

    def source_size(self):
        size = self.decoder.source_size
        if not size[0] and self.rx is not None:
            size = self.rx.size
        return size

    def picture_rect(self):
        sw, sh = self.source_size()
        if not sw or not sh:
            return QRectF()
        k = min(self.width() / sw, self.height() / sh)
        w, h = sw * k, sh * k
        return QRectF((self.width() - w) / 2, (self.height() - h) / 2, w, h)

    def set_cursor(self, msg):
        if "bgra" in msg:
            shape = unpacked(msg)
            if shape is None:
                return
            w, h, hx, hy, data = shape
            img = QImage(data, w, h, w * 4, QImage.Format_ARGB32_Premultiplied).copy()
            self.cursors[msg["id"]] = (img, hx, hy)
        if msg.get("id") in self.cursors:
            self.cursor_id = msg["id"]
            self.update()

    def clear(self):
        self.image = None
        self.update()

    def paintEvent(self, _):
        pic = self.decoder.take()
        if pic is not None and pic[1] != self.ts:
            self.image, self.ts = pic
            self.painted += 1
            self.latencies.append((now_us() + self.clock_offset - self.ts) / 1000)
        p = QPainter(self)
        p.fillRect(self.rect(), QColor("#000000"))
        r = self.picture_rect()
        if self.image is not None and not r.isEmpty():
            dpr = self.devicePixelRatioF()
            p.setRenderHint(QPainter.SmoothPixmapTransform, r.width() * dpr > self.image.width() + 1)
            p.drawImage(r, self.image)
            self._paint_cursor(p, r)
        if self.message:
            self._box(p, self.message, center=True)
        if self.show_stats and self.stats:
            self._box(p, self.stats, center=False)

    def _paint_cursor(self, p, r):
        if not self.draw_cursor or self.rx is None or self.rx.cursor is None:
            return
        x, y, flags = self.rx.cursor
        shape = self.cursors.get(self.cursor_id)
        if shape is None or flags & (CURSOR_VISIBLE | CURSOR_HERE) != (CURSOR_VISIBLE | CURSOR_HERE):
            return
        img, hx, hy = shape
        sw = self.source_size()[0] or 1
        k = r.width() / sw                         # the pointer is as big as on the remote screen
        px = r.x() + x / 65535 * r.width() - hx * k
        py = r.y() + y / 65535 * r.height() - hy * k
        p.setRenderHint(QPainter.SmoothPixmapTransform, True)
        p.drawImage(QRectF(px, py, img.width() * k, img.height() * k), img)

    def _box(self, p, text, center):
        f = QFont(self.font())
        f.setPointSizeF(11 if center else 9)
        p.setFont(f)
        fm = p.fontMetrics()
        lines = text.split("\n")
        w = max(fm.horizontalAdvance(line) for line in lines) + 24
        h = fm.height() * len(lines) + 16
        if center:
            box = QRectF((self.width() - w) / 2, (self.height() - h) / 2, w, h)
        else:
            box = QRectF(10, 10, w, h)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(0, 0, 0, 170))
        p.drawRoundedRect(box, 8, 8)
        p.setPen(QColor("#ffffff"))
        p.drawText(box.adjusted(12, 8, -12, -8), (Qt.AlignCenter if center else Qt.AlignLeft) | Qt.AlignVCenter,
                   text)

    def latency(self):
        """Capture on the host → shown here, ms (median of the last ones), or None."""
        if not self.latencies:
            return None
        s = sorted(self.latencies)
        return s[len(s) // 2]
