"""
The remote screen in the session window: the newest decoded picture, fitted into the widget
(the decoder already scales it down; the painter only scales up), the remote pointer on top,
and an overlay for messages and statistics.

With control on, the mouse and the keys over it go to the remote PC. The pointer then is our
own — no delay at all — wearing the remote pointer's shape; the drawn one is hidden.
"""

import time
from collections import deque

from PySide6.QtCore import QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QCursor, QFont, QImage, QPainter, QPixmap
from PySide6.QtWidgets import QWidget

from ..host.cursor import unpacked
from ..protocol import CURSOR_HERE, CURSOR_VISIBLE
from ..viewer.input import qt_key

QT_BUTTONS = {Qt.LeftButton: "left", Qt.RightButton: "right", Qt.MiddleButton: "middle",
              Qt.BackButton: "x1", Qt.ForwardButton: "x2"}
MOVE_EVERY_MS = 4


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
        self.input = None               # the session's InputSender
        self.control = False            # mouse and keys go to the remote PC
        self.keys_by_hook = False       # the keyboard hook takes the keys (system keys too)
        self._pending_move = None
        self._last_move = 0.0
        self._move_timer = QTimer(self, singleShot=True, timeout=self._flush_move)
        self._cursor_key = None
        decoder.frame_ready.connect(self.update)
        decoder.cursor_moved.connect(self._cursor_moved)
        self.setAttribute(Qt.WA_OpaquePaintEvent)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.StrongFocus)
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
            self._apply_cursor()
            self.update()

    def _cursor_moved(self):
        self._apply_cursor()
        self.update()

    def _apply_cursor(self):
        """Controlling: our own pointer, in the remote one's shape (none while the remote hides it)."""
        if not self.control:
            key = None
        else:
            flags = self.rx.cursor[2] if self.rx is not None and self.rx.cursor else CURSOR_VISIBLE
            key = (self.cursor_id, bool(flags & CURSOR_VISIBLE))
        if key == self._cursor_key:
            return
        self._cursor_key = key
        if key is None:
            self.unsetCursor()
        elif not key[1]:
            self.setCursor(Qt.BlankCursor)
        elif key[0] in self.cursors:
            img, hx, hy = self.cursors[key[0]]
            self.setCursor(QCursor(QPixmap.fromImage(img), hx, hy))
        else:
            self.setCursor(Qt.ArrowCursor)

    def set_control(self, on):
        if not on and self.input is not None:
            self.input.release_all()
        self.control = on
        self.draw_cursor = not on
        self._apply_cursor()
        self.update()

    # ── mouse and keys out ──────────────────────────────────────────
    def _norm(self, pos, clamp):
        """A point of the widget → 0..65535 across the remote monitor; None if it's off the
        picture (unless clamped onto its edge, as while dragging)."""
        r = self.picture_rect()
        if r.isEmpty():
            return None
        x, y = (pos.x() - r.x()) / r.width(), (pos.y() - r.y()) / r.height()
        if not clamp and not (0 <= x <= 1 and 0 <= y <= 1):
            return None
        return round(max(0.0, min(1.0, x)) * 65535), round(max(0.0, min(1.0, y)) * 65535)

    def _active(self):
        return self.control and self.input is not None

    def mouseMoveEvent(self, e):
        super().mouseMoveEvent(e)
        if not self._active():
            return
        p = self._norm(e.position(), clamp=bool(e.buttons()))
        if p is None:
            return
        self._pending_move = p
        if (time.monotonic() - self._last_move) * 1000 >= MOVE_EVERY_MS:
            self._flush_move()
        elif not self._move_timer.isActive():
            self._move_timer.start(MOVE_EVERY_MS)

    def _flush_move(self):
        if self._pending_move is not None and self._active():
            self.input.move(*self._pending_move)
            self._last_move = time.monotonic()
        self._pending_move = None

    def _button(self, e, down):
        name = QT_BUTTONS.get(e.button())
        p = self._norm(e.position(), clamp=not down)
        if name is None or p is None:
            return
        self._move_timer.stop()
        self._pending_move = None
        self.input.button(name, down, *p)

    def mousePressEvent(self, e):
        if self._active():
            self.setFocus()
            self._button(e, True)
        else:
            super().mousePressEvent(e)

    def mouseDoubleClickEvent(self, e):
        # Windows makes the double click itself from two clicks close in time
        if self._active():
            self._button(e, True)
        else:
            super().mouseDoubleClickEvent(e)

    def mouseReleaseEvent(self, e):
        if self._active():
            self._button(e, False)
        else:
            super().mouseReleaseEvent(e)

    def wheelEvent(self, e):
        if not self._active():
            return super().wheelEvent(e)
        p = self._norm(e.position(), clamp=True)
        d = e.angleDelta()
        if p is not None:
            self.input.wheel(d.x(), d.y(), *p)

    def keyPressEvent(self, e):
        if self._active() and not self.keys_by_hook:
            k = qt_key(e)
            if k is not None:
                self.input.key(k[0], k[1], True, k[2])
                return
        super().keyPressEvent(e)

    def keyReleaseEvent(self, e):
        if self._active() and not self.keys_by_hook:
            k = qt_key(e)
            if k is not None:
                self.input.key(k[0], k[1], False, k[2])
                return
        super().keyReleaseEvent(e)

    def focusNextPrevChild(self, forward):
        return False if self._active() else super().focusNextPrevChild(forward)   # Tab is for the remote PC

    def leaveEvent(self, e):
        self._flush_move()
        super().leaveEvent(e)

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
