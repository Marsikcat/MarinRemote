"""Small reusable widgets painted to match the theme."""

from PySide6.QtCore import Property, QEasingCurve, QPropertyAnimation, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import (QAbstractButton, QFrame, QHBoxLayout, QLabel, QPushButton,
                               QVBoxLayout, QWidget)

from .theme import T


def label(text="", role=None, wrap=False, selectable=False):
    lb = QLabel(text)
    if role:
        lb.setProperty("role", role)
    lb.setWordWrap(wrap)
    if selectable:
        lb.setTextInteractionFlags(Qt.TextSelectableByMouse)
    return lb


def button(text, kind=None, on_click=None, tip=""):
    b = QPushButton(text)
    b.setCursor(Qt.PointingHandCursor)
    if kind:
        b.setProperty("kind", kind)
    if on_click:
        b.clicked.connect(on_click)
    if tip:
        b.setToolTip(tip)
    return b


def hbox(*widgets, spacing=8, margins=(0, 0, 0, 0)):
    w = QWidget()
    lay = QHBoxLayout(w)
    lay.setContentsMargins(*margins)
    lay.setSpacing(spacing)
    for x in widgets:
        if x is None:
            lay.addStretch(1)
        elif isinstance(x, int):
            lay.addSpacing(x)
        else:
            lay.addWidget(x)
    return w


def card(name="Card", margins=(16, 14, 16, 14), spacing=10):
    """A rounded panel; returns (frame, its vertical layout)."""
    f = QFrame()
    f.setObjectName(name)
    lay = QVBoxLayout(f)
    lay.setContentsMargins(*margins)
    lay.setSpacing(spacing)
    return f, lay


def note(text, kind="Warning"):
    """A coloured strip with a sentence in it (Warning / Danger / Info)."""
    f, lay = card(kind, (12, 9, 12, 9))
    lb = label(text, wrap=True)
    lb.setTextFormat(Qt.RichText)
    lay.addWidget(lb)
    f.text = lb
    return f


def clear_layout(lay):
    while lay.count():
        item = lay.takeAt(0)
        w = item.widget()
        if w is not None:
            w.setParent(None)
            w.deleteLater()


def dot(color, size=10):
    lb = QLabel()
    lb.setFixedSize(size, size)
    lb.setStyleSheet(f"background: {color}; border-radius: {size // 2}px;")
    return lb


class Switch(QAbstractButton):
    """iOS/Discord-style toggle."""

    def __init__(self, checked=False, parent=None):
        super().__init__(parent)
        self.setCheckable(True)
        self.setChecked(checked)
        self.setCursor(Qt.PointingHandCursor)
        self.setFixedSize(42, 24)
        self._pos = 1.0 if checked else 0.0
        self._anim = QPropertyAnimation(self, b"knob", self, duration=140,
                                        easingCurve=QEasingCurve.OutCubic)
        self.toggled.connect(self._animate)

    def _animate(self, on):
        self._anim.stop()
        self._anim.setEndValue(1.0 if on else 0.0)
        self._anim.start()

    def get_knob(self):
        return self._pos

    def set_knob(self, v):
        self._pos = v
        self.update()

    knob = Property(float, get_knob, set_knob)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        off, on = QColor(T.c["muted"]), QColor(T.c["green"])
        t = self._pos
        bg = QColor(round(off.red() + (on.red() - off.red()) * t),
                    round(off.green() + (on.green() - off.green()) * t),
                    round(off.blue() + (on.blue() - off.blue()) * t))
        if not self.isEnabled():
            bg.setAlpha(110)
        p.setPen(Qt.NoPen)
        p.setBrush(bg)
        p.drawRoundedRect(QRectF(0, 0, 42, 24), 12, 12)
        p.setBrush(QColor("white"))
        p.drawEllipse(QRectF(3 + t * 18, 3, 18, 18))

    def sizeHint(self):
        return QSize(42, 24)


def switch_row(title, hint, checked, on_toggle):
    """A setting: a title and a hint on the left, a switch on the right."""
    w = QWidget()
    lay = QHBoxLayout(w)
    lay.setContentsMargins(0, 0, 0, 0)
    text = QVBoxLayout()
    text.setSpacing(2)
    text.addWidget(label(title))
    if hint:
        text.addWidget(label(hint, "hint", wrap=True))
    lay.addLayout(text, 1)
    sw = Switch(checked)
    sw.toggled.connect(on_toggle)
    lay.addWidget(sw, 0, Qt.AlignTop)
    w.switch = sw
    return w
