"""
While someone is connected to this PC it must be obvious: a red frame around the screen, a
panel on top of every window with who it is and a «Отключить» button, and the tray icon with
a red dot. The frame lets clicks through and is left out of the picture the viewer gets
(WDA_EXCLUDEFROMCAPTURE), so it never gets in anybody's way.
"""

import ctypes
import time

from PySide6.QtCore import QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QGuiApplication, QPainter, QPen
from PySide6.QtWidgets import QFrame, QHBoxLayout, QVBoxLayout, QWidget

from ..protocol import describe_rights
from .theme import RED, T
from .widgets import button, clear_layout, dot, label

WDA_EXCLUDEFROMCAPTURE = 0x11


def exclude_from_capture(widget):
    try:
        ctypes.windll.user32.SetWindowDisplayAffinity(int(widget.winId()), WDA_EXCLUDEFROMCAPTURE)
    except (OSError, AttributeError):
        pass


class Border(QWidget):
    """A frame along the edges of one screen; clicks go through it."""

    WIDTH = 4

    def __init__(self, screen):
        super().__init__(None, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool
                         | Qt.WindowTransparentForInput | Qt.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setScreen(screen)
        self.setGeometry(screen.geometry())

    def paintEvent(self, _):
        p = QPainter(self)
        w = self.WIDTH
        p.setPen(QPen(QColor(RED), w))
        p.setBrush(Qt.NoBrush)
        p.drawRect(QRectF(w / 2, w / 2, self.width() - w, self.height() - w))

    def showEvent(self, e):
        super().showEvent(e)
        exclude_from_capture(self)


class SessionPanel(QFrame):
    """Who is connected, with a button to end it. Stays on top; can be dragged aside."""

    disconnect = Signal(str)        # sid
    disconnect_all = Signal()

    def __init__(self):
        super().__init__(None, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setObjectName("Panel")
        self.setStyleSheet(f"#Panel {{ background: {T.c['float']}; border: 2px solid {RED}; border-radius: 10px; }}")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 8, 10, 8)
        lay.setSpacing(4)
        head = QHBoxLayout()
        head.addWidget(dot(RED, 10))
        head.addWidget(label("<b>Удалённый доступ к этому ПК</b>"))
        head.addStretch(1)
        self.all = button("Отключить всех", "danger", self.disconnect_all.emit)
        head.addWidget(self.all)
        lay.addLayout(head)
        self.rows = QVBoxLayout()
        self.rows.setSpacing(2)
        lay.addLayout(self.rows)
        self._drag = None
        self._placed = False

    def set_sessions(self, sessions):
        clear_layout(self.rows)
        self.all.setVisible(len(sessions) > 1)
        for s in sessions:
            since = time.strftime("%H:%M", time.localtime(s["started"])) if s.get("started") else ""
            text = label(f"{s['name']} · {s['fp']} · {describe_rights(s['rights'])} · с {since}")
            btn = button("Отключить", "danger", lambda _=False, sid=s["sid"]: self.disconnect.emit(sid))
            row = QHBoxLayout()
            row.addWidget(text, 1)
            row.addWidget(btn)
            w = QWidget()
            w.setLayout(row)
            row.setContentsMargins(0, 0, 0, 0)
            self.rows.addWidget(w)
        self.adjustSize()
        if not self._placed:
            screen = QGuiApplication.primaryScreen().availableGeometry()
            self.move(screen.center().x() - self.width() // 2, screen.top() + 12)
            self._placed = True

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self._drag = e.globalPosition().toPoint() - self.frameGeometry().topLeft()

    def mouseMoveEvent(self, e):
        if self._drag is not None:
            self.move(e.globalPosition().toPoint() - self._drag)

    def mouseReleaseEvent(self, e):
        self._drag = None


class Indicators:
    """Shows or hides the frame and the panel for the current list of sessions."""

    def __init__(self, on_disconnect, on_disconnect_all):
        self.borders = []
        self.panel = SessionPanel()
        self.panel.disconnect.connect(on_disconnect)
        self.panel.disconnect_all.connect(on_disconnect_all)
        self.sessions = []
        # something else that wants to be on top (a full-screen game) must not bury them
        self.timer = QTimer(interval=3000, timeout=self._raise)

    def update(self, sessions):
        self.sessions = sessions
        if not sessions:
            for b in self.borders:
                b.close()
            self.borders = []
            self.panel.hide()
            self.timer.stop()
            return
        if not self.borders:
            self.borders = [Border(s) for s in QGuiApplication.screens()]
            for b in self.borders:
                b.show()
        self.panel.set_sessions(sessions)
        self.panel.show()
        self.timer.start()

    def _raise(self):
        for w in self.borders + [self.panel]:
            w.raise_()

    def close(self):
        self.update([])
        self.panel.close()
