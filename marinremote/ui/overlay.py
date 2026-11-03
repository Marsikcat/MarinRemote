"""
While someone is connected to this PC, a compact panel in the bottom-left corner shows who
is connected and offers a «Отключить» button. The tray icon also has a red dot.
"""

import time

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QFrame, QHBoxLayout, QVBoxLayout, QWidget

from ..protocol import describe_rights
from .theme import RED, T
from .widgets import button, clear_layout, dot, label


def control_button(s, set_rights):
    """«Забрать управление» / «Вернуть управление» for a session let in with control, else None."""
    if "control" not in s.get("granted", s["rights"]):
        return None
    if "control" in s["rights"]:
        rights = [r for r in s["rights"] if r != "control"]
        return button("Забрать управление", None, lambda: set_rights(s["sid"], rights),
                      "Мышь и клавиатура зрителя перестают действовать; смотреть он продолжает")
    return button("Вернуть управление", None, lambda: set_rights(s["sid"], s["rights"] + ["control"]))

class SessionPanel(QFrame):
    """Who is connected, shown in a corner; the panel can be dragged aside."""

    disconnect = Signal(str)        # sid
    disconnect_all = Signal()
    rights = Signal(str, list)      # sid, the rights it keeps

    def __init__(self):
        super().__init__(None, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setObjectName("Panel")
        self.setStyleSheet(
            f"#Panel {{ background: {T.c['float']}; border: 1px solid {T.c['border']}; "
            "border-radius: 10px; }}"
        )
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
        self._dragged = False

    def set_sessions(self, sessions):
        clear_layout(self.rows)
        self.all.setVisible(len(sessions) > 1)
        for s in sessions:
            since = time.strftime("%H:%M", time.localtime(s["started"])) if s.get("started") else ""
            text = label(f"{s['name']} · {s['fp']} · {describe_rights(s['rights'])} · с {since}")
            btn = button("Отключить", "danger", lambda _=False, sid=s["sid"]: self.disconnect.emit(sid))
            row = QHBoxLayout()
            row.addWidget(text, 1)
            control = control_button(s, self.rights.emit)
            if control is not None:
                row.addWidget(control)
            row.addWidget(btn)
            w = QWidget()
            w.setLayout(row)
            row.setContentsMargins(0, 0, 0, 0)
            self.rows.addWidget(w)
        self.adjustSize()
        if not self._dragged:
            screen = QGuiApplication.primaryScreen().availableGeometry()
            self.move(screen.left() + 12, screen.bottom() - self.height() - 12)

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self._drag = e.globalPosition().toPoint() - self.frameGeometry().topLeft()
            self._dragged = True

    def mouseMoveEvent(self, e):
        if self._drag is not None:
            self.move(e.globalPosition().toPoint() - self._drag)

    def mouseReleaseEvent(self, e):
        self._drag = None


class Indicators:
    """Shows or hides the frame and the panel for the current list of sessions."""

    def __init__(self, on_disconnect, on_disconnect_all, on_rights=None):
        self.panel = SessionPanel()
        self.panel.disconnect.connect(on_disconnect)
        self.panel.disconnect_all.connect(on_disconnect_all)
        if on_rights is not None:
            self.panel.rights.connect(on_rights)
        self.sessions = []
        # something else that wants to be on top (a full-screen game) must not bury them
        self.timer = QTimer(interval=3000, timeout=self._raise)

    def update(self, sessions):
        self.sessions = sessions
        if not sessions:
            self.panel.hide()
            self.timer.stop()
            return
        self.panel.set_sessions(sessions)
        self.panel.show()
        self.timer.start()

    def _raise(self):
        self.panel.raise_()

    def close(self):
        self.update([])
        self.panel.close()
