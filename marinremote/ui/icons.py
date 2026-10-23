"""The app icon, drawn rather than shipped: a screen with a pointer; a red dot while someone
is connected to this PC."""

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPainterPath, QPen, QPixmap

from .theme import ACCENT, RED

_cache = {}


def pixmap(size=64, active=False):
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    s = size / 64
    p.setPen(Qt.NoPen)
    p.setBrush(QColor(ACCENT))
    p.drawRoundedRect(QRectF(2 * s, 2 * s, 60 * s, 60 * s), 14 * s, 14 * s)
    # the screen
    p.setPen(QPen(QColor("white"), 4.5 * s, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
    p.setBrush(Qt.NoBrush)
    p.drawRoundedRect(QRectF(12 * s, 15 * s, 40 * s, 27 * s), 4 * s, 4 * s)
    p.drawLine(QPointF(24 * s, 50 * s), QPointF(40 * s, 50 * s))
    # the pointer
    arrow = QPainterPath()
    arrow.moveTo(30 * s, 22 * s)
    arrow.lineTo(30 * s, 38 * s)
    arrow.lineTo(34 * s, 34 * s)
    arrow.lineTo(38 * s, 40 * s)
    arrow.lineTo(40.5 * s, 38.5 * s)
    arrow.lineTo(36.5 * s, 32.5 * s)
    arrow.lineTo(42 * s, 32 * s)
    arrow.closeSubpath()
    p.setPen(QPen(QColor(ACCENT), 1.5 * s))
    p.setBrush(QColor("white"))
    p.drawPath(arrow)
    if active:
        p.setPen(QPen(QColor("white"), 3 * s))
        p.setBrush(QColor(RED))
        p.drawEllipse(QRectF(40 * s, 2 * s, 22 * s, 22 * s))
    p.end()
    return pm


def app_icon(active=False):
    key = ("app", active)
    if key not in _cache:
        icon = QIcon()
        for n in (16, 20, 24, 32, 48, 64, 256):
            icon.addPixmap(pixmap(n, active))
        _cache[key] = icon
    return _cache[key]
