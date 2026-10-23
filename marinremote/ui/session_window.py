"""
A connection to another PC as the viewer sees it: whom it is with, how the login is going,
the path (direct or relayed, round trip) and a way out. The remote screen goes in the middle
(stage b); for now the middle says how things stand.
"""

import time

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import QFrame, QHBoxLayout, QInputDialog, QVBoxLayout, QWidget

from ..codes import fingerprint
from ..protocol import AUTH_REASONS, describe_rights, wait_text
from ..viewer.session import END_TEXT, ViewerSession
from . import icons
from .bridge import ViewerBridge
from .theme import GREEN, RED, YELLOW, style_window
from .widgets import button, dot, label, note

STATE_TEXT = {
    "idle": "Подготовка…",
    "connecting": "Подключение…",
    "waiting": "Ждём, пока на удалённом ПК разрешат подключение",
    "connected": "Подключено",
    "reconnecting": "Связь пропала — переподключаемся…",
    "closed": "Отключено",
}
STATE_COLOR = {"connected": GREEN, "closed": RED}


class SessionWindow(QWidget):
    finished = Signal(str)              # the remote key, when the window closes

    def __init__(self, settings, node, target, want, password=None):
        super().__init__(None, Qt.Window)
        self.s, self.node = settings, node
        self.target = dict(target)
        self.key = target["id"]
        self.want = list(want)
        self.setAttribute(Qt.WA_DeleteOnClose)
        self.setObjectName("Root")
        self.setWindowIcon(icons.app_icon())
        self.resize(900, 560)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        bar = QFrame()
        bar.setObjectName("TopBar")
        b = QHBoxLayout(bar)
        b.setContentsMargins(14, 8, 10, 8)
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

        body = QWidget()
        v = QVBoxLayout(body)
        v.setContentsMargins(40, 30, 40, 30)
        v.addStretch(1)
        self.big = label("", "h1", wrap=True)
        self.big.setAlignment(Qt.AlignCenter)
        v.addWidget(self.big)
        self.small = label("", "muted", wrap=True)
        self.small.setAlignment(Qt.AlignCenter)
        v.addWidget(self.small)
        self.info = note("", "Info")
        self.info.hide()
        v.addWidget(self.info)
        v.addStretch(2)
        lay.addWidget(body, 1)

        self.timer = QTimer(self, interval=500, timeout=self._tick)
        self.timer.start()
        self.session = None
        self._start(password)

    def showEvent(self, e):
        super().showEvent(e)
        style_window(self)

    # ── the session ─────────────────────────────────────────────────
    def _start(self, password=None):
        self.bridge = ViewerBridge()
        self.bridge.state.connect(self._on_state)
        self.session = ViewerSession(self.node, self.target, self.want, self.s["name"], self.bridge,
                                     password=password)
        self.session.start()
        self._on_state(self.session)

    def _retry(self):
        old = self.session
        if old is not None and old.state != "closed":
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
        self.title.setText(self._name())
        self.setWindowTitle(f"{self._name()} — MarinRemote")
        self.dot.setStyleSheet(f"background: {STATE_COLOR.get(st, YELLOW)}; border-radius: 5px;")
        self.retry_btn.setVisible(st == "closed")
        self.leave_btn.setText("Закрыть" if st == "closed" else "Отключить")
        self.save_btn.setVisible(st == "connected" and self.key not in (self.s["contacts"] or {}))
        self.big.setText(STATE_TEXT.get(st, st))
        self.info.hide()
        if st == "connected":
            self._remember_contact()
            self.small.setText(f"Права: {describe_rights(session.rights)}.\n"
                               "Изображение экрана появится на следующем этапе разработки.")
            if session.remembered == "contact":
                self.info.text.setText(f"«{self._name()}» запомнил это устройство: дальше подключение "
                                       f"без подтверждения на том ПК.")
                self.info.show()
        elif st == "closed":
            d = session.detail
            if d.get("reason") == "refused":
                text = AUTH_REASONS.get(d.get("auth"), "Подключение отклонено.")
                text = text.format(wait=wait_text(d.get("retry_after") or 1))
            else:
                text = END_TEXT.get(d.get("reason"), "Сеанс завершён.")
            self.small.setText(text)
        elif st != "waiting":
            self.small.setText("")
        self._tick()

    def _tick(self):
        session = self.session
        if session is None:
            return
        fp = fingerprint(self.key)
        if session.state == "waiting":
            left = session.detail.get("timeout", 30) - (time.monotonic() - session.detail.get("since", 0))
            self.small.setText(f"Осталось {max(0, int(left))} с. Там должны нажать «Разрешить».")
        if session.state == "connected":
            p = session.path()
            via = "через ретранслятор" if p.get("via_relay") else "напрямую"
            rtt = f" · {p['rtt']} мс" if p.get("rtt") is not None else ""
            self.sub.setText(f"{fp} · {via}{rtt}")
        else:
            self.sub.setText(fp)

    # ── contacts ────────────────────────────────────────────────────
    def _remember_contact(self):
        """A saved contact learns where the PC is now (its relay, addresses) and when we last met."""
        contact = (self.s["contacts"] or {}).get(self.key)
        host = self.session.host
        if contact is None:
            return
        self.s.put("contacts", self.key, {**contact, "host_name": host.get("name", ""),
                                          "relay": host.get("relay") or contact.get("relay"),
                                          "addrs": host.get("addrs") or contact.get("addrs") or [],
                                          "last": time.strftime("%Y-%m-%d %H:%M")})

    def _save_contact(self):
        host = self.session.host
        name, ok = QInputDialog.getText(self, "Сохранить в контакты", "Как назвать этот ПК:",
                                        text=host.get("name") or self.target.get("name") or "")
        if not ok:
            return
        self.s.put("contacts", self.key, {
            "name": name.strip() or host.get("name") or "ПК", "host_name": host.get("name", ""),
            "relay": host.get("relay") or self.target.get("relay"),
            "addrs": host.get("addrs") or self.target.get("addrs") or [],
            "added": time.strftime("%Y-%m-%d %H:%M"), "last": time.strftime("%Y-%m-%d %H:%M")})
        self._on_state(self.session)

    def closeEvent(self, e):
        self.timer.stop()
        if self.session is not None:
            self.session.stop()
        self.finished.emit(self.key)
        super().closeEvent(e)
