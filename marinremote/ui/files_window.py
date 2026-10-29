"""
«Только файлы» — a connection to another PC for its files and nothing else. Only the "files"
right is asked for, so the other PC starts no screen capture and its question says just that;
nothing here decodes video or catches keys. The window is the two panels of «Файлы» with, on
top, whom it is with and how the line is. Until the line is first up (someone may have to click
«Разрешить» there) the middle says how things stand; after that the panels and the transfers
stay, and a strip over them tells about a drop or the end.
"""

import time

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import QFrame, QHBoxLayout, QStackedWidget, QVBoxLayout, QWidget

from ..codes import fingerprint
from ..viewer.files import FileClient
from ..viewer.session import ViewerSession
from . import icons
from .bridge import ViewerBridge
from .filemanager import FileManager
from .session_window import STATE_COLOR, STATE_TEXT, end_text, remember_contact, save_contact
from .theme import YELLOW, style_window
from .widgets import button, dot, label, note

WANT = ["files"]


class FilesWindow(QWidget):
    finished = Signal(str)              # the remote key, when the window closes

    def __init__(self, settings, node, target, password=None):
        super().__init__(None, Qt.Window)
        self.s, self.node = settings, node
        self.target = dict(target)
        self.key = target["id"]
        self._last_state = None
        self._had_files = False          # the panels are up to date with the line as it is
        self.setAttribute(Qt.WA_DeleteOnClose)
        self.setObjectName("Root")
        self.setWindowIcon(icons.app_icon())
        self.resize(1180, 800)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        bar = QFrame()
        bar.setObjectName("TopBar")
        b = QHBoxLayout(bar)
        b.setContentsMargins(14, 6, 10, 6)
        self.dot = dot(YELLOW, 10)
        b.addWidget(self.dot)
        self.title = label("", "h2")
        b.addWidget(self.title)
        self.sub = label("", "muted")
        b.addWidget(self.sub, 1)
        self.save_btn = button("Сохранить в контакты", "small", self._save_contact)
        self.retry_btn = button("Подключиться снова", "small", self._retry)
        self.leave_btn = button("Отключить", "danger", self.close)
        for w in (self.save_btn, self.retry_btn, self.leave_btn):
            b.addWidget(w)
        lay.addWidget(bar)
        self.warn = note("", "Warning")
        self.warn.layout().setContentsMargins(14, 7, 14, 7)
        self.warn.setStyleSheet("#Warning { border-radius: 0; }")
        self.warn.hide()
        lay.addWidget(self.warn)

        self.stack = QStackedWidget()
        status = QWidget()
        v = QVBoxLayout(status)
        v.setContentsMargins(40, 30, 40, 30)
        v.addStretch(1)
        self.big = label("", "h1", wrap=True)
        self.big.setAlignment(Qt.AlignCenter)
        v.addWidget(self.big)
        self.small = label("", "muted", wrap=True)
        self.small.setAlignment(Qt.AlignCenter)
        v.addWidget(self.small)
        v.addStretch(2)
        self.stack.addWidget(status)
        lay.addWidget(self.stack, 1)

        self.manager = None              # the panels, made once the line is first up
        self.session = None
        self.files = None
        self.timer = QTimer(self, interval=500, timeout=self._tick)
        self.timer.start()
        self._start(password)

    def showEvent(self, e):
        super().showEvent(e)
        style_window(self)

    # ── the session ─────────────────────────────────────────────────
    def _start(self, password=None):
        self.bridge = ViewerBridge()
        self.bridge.state.connect(self._on_state)
        self.session = ViewerSession(self.node, self.target, WANT, self.s["name"], self.bridge,
                                     password=password)
        self.files = FileClient(self.session, self.s.folder / "transfers.json")
        if self.manager is not None:
            self.manager.set_client(self.files)
        self.session.start()
        self._on_state(self.session)

    def _retry(self):
        if self.session is not None and self.session.state != "closed":
            return
        self._start()

    def _name(self):
        contact = (self.s["contacts"] or {}).get(self.key)
        return ((contact or {}).get("name") or self.session.host.get("name")
                or self.target.get("name") or "Удалённый ПК")

    def _on_state(self, session):
        if session is not self.session:
            return
        st = session.state
        changed, self._last_state = st != self._last_state, st
        name = self._name()
        self.title.setText(name)
        self.setWindowTitle(f"Файлы — {name} — MarinRemote")
        self.dot.setStyleSheet(f"background: {STATE_COLOR.get(st, YELLOW)}; border-radius: 5px;")
        self.retry_btn.setVisible(st == "closed")
        self.leave_btn.setText("Закрыть" if st == "closed" else "Отключить")
        self.save_btn.setVisible(st == "connected" and self.key not in (self.s["contacts"] or {}))
        allowed = st == "connected" and "files" in session.rights
        if st == "connected" and changed:
            remember_contact(self.s, self.key, session.host)
        if allowed and not self._had_files:     # first up, back after a drop, or the right given back
            if self.manager is None:
                self.manager = FileManager(self.files, name, session.host.get("ver", ""), parent=self,
                                           control=False)
                self.stack.addWidget(self.manager)
            else:
                self.manager.reload()
        self._had_files = allowed
        panels = self.manager is not None and (allowed or st != "connected")
        self.stack.setCurrentWidget(self.manager if panels else self.stack.widget(0))
        self._show_status()
        self._tick()

    def _status(self):
        """How the line stands: a headline and a sentence."""
        s = self.session
        st = s.state
        if st == "waiting":
            left = s.detail.get("timeout", 30) - (time.monotonic() - s.detail.get("since", 0))
            return STATE_TEXT[st], f"Осталось {max(0, int(left))} с. Там должны нажать «Разрешить»."
        if st == "closed":
            return STATE_TEXT[st], end_text(s.detail)
        if st == "connected" and "files" not in s.rights:
            return "Нет доступа к файлам", "Тот ПК закрыл доступ к файлам для этого сеанса."
        return STATE_TEXT.get(st, st), ""

    def _show_status(self):
        big, small = self._status()
        self.big.setText(big)
        self.small.setText(small)
        over_panels = self.manager is not None and self.stack.currentWidget() is self.manager
        if over_panels and self.session.state != "connected":
            self.warn.text.setText(f"<b>{big}</b>" + (f" — {small}" if small else ""))
            self.warn.show()
        else:
            self.warn.hide()

    def _tick(self):
        s = self.session
        if s is None:
            return
        fp = fingerprint(self.key)
        if s.state != "connected":
            self.sub.setText(f"{fp} · только файлы")
            if s.state == "waiting":
                self._show_status()
            return
        p = s.path()
        via = "через ретранслятор" if p.get("via_relay") else "напрямую"
        rtt = f" · {p['rtt']} мс" if p.get("rtt") is not None else ""
        self.sub.setText(f"{fp} · только файлы · {via}{rtt}")

    def _save_contact(self):
        if save_contact(self, self.s, self.key, self.session.host, self.target):
            self._on_state(self.session)

    def closeEvent(self, e):
        self.timer.stop()
        if self.manager is not None:
            self.manager.close()
        if self.session is not None:
            self.session.stop()
        self.finished.emit(self.key)
        super().closeEvent(e)
