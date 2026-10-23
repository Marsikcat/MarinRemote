"""
A connection to another PC as the viewer sees it: the remote screen in the middle; on top whom
it is with, the path (direct or relayed, round trip), which monitor and quality, statistics,
full screen and a way out. Before the picture comes (or without the right to see it) the middle
says how things stand.
"""

import time
from collections import deque

from PySide6.QtCore import QEvent, Qt, QTimer, Signal
from PySide6.QtWidgets import (QComboBox, QFrame, QHBoxLayout, QInputDialog, QStackedWidget,
                               QVBoxLayout, QWidget)

from ..codes import fingerprint
from ..host.capture import QUALITY
from ..protocol import AUTH_REASONS, describe_rights, wait_text
from ..viewer.decoder import Decoder
from ..viewer.session import END_TEXT, ViewerSession
from ..viewer.video import VideoReceiver
from . import icons
from .bridge import ViewerBridge
from .theme import GREEN, RED, YELLOW, style_window
from .videowidget import VideoWidget, now_us
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
VIDEO_REASONS = {
    "secure_desktop": "На удалённом ПК сейчас защищённый экран (запрос UAC, блокировка или экран входа).\n"
                      "Его нельзя показать в этой версии — картинка вернётся сама.",
    "no_monitor": "На удалённом ПК не найдено ни одного экрана.",
    "error": "Не удалось захватить экран удалённого ПК — пробуем снова…",
}


class SessionWindow(QWidget):
    finished = Signal(str)              # the remote key, when the window closes

    def __init__(self, settings, node, target, want, password=None):
        super().__init__(None, Qt.Window)
        self.s, self.node = settings, node
        self.target = dict(target)
        self.key = target["id"]
        self.want = list(want)
        self.video_state = {}
        self.clock_samples = deque(maxlen=8)
        self._stats_mark = (time.monotonic(), 0, 0, 0)
        self.setAttribute(Qt.WA_DeleteOnClose)
        self.setObjectName("Root")
        self.setWindowIcon(icons.app_icon())
        self.resize(1280, 780)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        self.bar = QFrame()
        self.bar.setObjectName("TopBar")
        b = QHBoxLayout(self.bar)
        b.setContentsMargins(14, 6, 10, 6)
        self.dot = dot(YELLOW, 10)
        b.addWidget(self.dot)
        self.title = label("", "h2")
        b.addWidget(self.title)
        self.sub = label("", "muted")
        b.addWidget(self.sub, 1)
        self.monitor_box = QComboBox()
        self.monitor_box.setToolTip("Какой экран удалённого ПК показывать")
        self.monitor_box.activated.connect(self._monitor_chosen)
        self.quality_box = QComboBox()
        self.quality_box.setToolTip("Качество картинки: «Авто» подбирает его под связь")
        for key, q in QUALITY.items():
            self.quality_box.addItem(q["label"], key)
        self.quality_box.activated.connect(self._quality_chosen)
        self.stats_btn = button("Статистика", "small", self._toggle_stats)
        self.stats_btn.setCheckable(True)
        self.full_btn = button("На весь экран", "small", self._toggle_full, tip="F11 — на весь экран и обратно")
        self.save_btn = button("Сохранить в контакты", "small", self._save_contact)
        self.retry_btn = button("Подключиться снова", "small", self._retry)
        self.leave_btn = button("Отключить", "danger", self.close)
        for w in (self.monitor_box, self.quality_box, self.stats_btn, self.full_btn, self.save_btn,
                  self.retry_btn, self.leave_btn):
            b.addWidget(w)
        lay.addWidget(self.bar)

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
        self.info = note("", "Info")
        self.info.hide()
        v.addWidget(self.info)
        v.addStretch(2)
        self.stack.addWidget(status)
        self.decoder = Decoder()
        self.video = VideoWidget(self.decoder)
        self.video.installEventFilter(self)
        self.stack.addWidget(self.video)
        lay.addWidget(self.stack, 1)

        self.timer = QTimer(self, interval=500, timeout=self._tick)
        self.timer.start()
        self.clock_timer = QTimer(self, interval=2000, timeout=self._ask_clock)
        self.session = None
        self._start(password)

    def showEvent(self, e):
        super().showEvent(e)
        style_window(self)

    # ── the session ─────────────────────────────────────────────────
    def _start(self, password=None):
        self.bridge = ViewerBridge()
        self.bridge.state.connect(self._on_state)
        self.bridge.received.connect(self._on_message)
        self.session = ViewerSession(self.node, self.target, self.want, self.s["name"], self.bridge,
                                     password=password)
        self.rx = VideoReceiver(self.session, self.decoder)
        self.video.rx = self.rx
        self.video.clear()
        self.video_state = {}
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
        watching = st in ("connected", "reconnecting") and "view" in session.rights
        for w in (self.quality_box, self.stats_btn, self.full_btn):
            w.setVisible(watching)
        self.monitor_box.setVisible(watching and self.monitor_box.count() > 1)
        self.big.setText(STATE_TEXT.get(st, st))
        self.info.hide()
        if st == "connected":
            self._remember_contact()
            self.clock_samples.clear()
            self.clock_timer.start()
            self._ask_clock()
            self.small.setText(f"Права: {describe_rights(session.rights)}.")
            if session.remembered == "contact":
                self.info.text.setText(f"«{self._name()}» запомнил это устройство: дальше подключение "
                                       f"без подтверждения на том ПК.")
                self.info.show()
        elif st == "closed":
            self.clock_timer.stop()
            d = session.detail
            if d.get("reason") == "refused":
                text = AUTH_REASONS.get(d.get("auth"), "Подключение отклонено.")
                text = text.format(wait=wait_text(d.get("retry_after") or 1))
            else:
                text = END_TEXT.get(d.get("reason"), "Сеанс завершён.")
            self.small.setText(text)
        elif st != "waiting":
            self.small.setText("")
        if watching:
            self.stack.setCurrentWidget(self.video)
            if st == "reconnecting":
                self.video.message = "Связь пропала — переподключаемся…"
            elif not self.video_state:
                self.video.message = "Запуск захвата экрана…"
            self.video.update()
        else:
            if self.isFullScreen():
                self._toggle_full()
            self.stack.setCurrentIndex(0)
        self._tick()

    def _on_message(self, session, msg):
        if session is not self.session:
            return
        t = msg["t"]
        if t == "monitors":
            self.monitor_box.clear()
            for m in msg.get("list") or []:
                self.monitor_box.addItem(f"{m['name']} · {m['w']}×{m['h']}", m["id"])
            i = self.monitor_box.findData(msg.get("current"))
            self.monitor_box.setCurrentIndex(max(0, i))
            self.monitor_box.setVisible(self.monitor_box.count() > 1)
        elif t == "video.state":
            self.video_state = msg
            state = msg.get("state")
            if state == "ok":
                self.video.message = ""
                i = self.quality_box.findData(msg.get("quality"))
                if i >= 0:
                    self.quality_box.setCurrentIndex(i)
            elif state == "starting":
                self.video.message = "Запуск захвата экрана…"
            elif state == "unavailable":
                self.video.message = VIDEO_REASONS.get(msg.get("reason"), VIDEO_REASONS["error"])
            self.video.update()
        elif t == "cursor":
            self.video.set_cursor(msg)
        elif t == "clock":
            self._got_clock(msg)

    # ── the picture ─────────────────────────────────────────────────
    def _monitor_chosen(self, index):
        self.session.post({"t": "monitor", "id": self.monitor_box.itemData(index)})

    def _quality_chosen(self, index):
        self.session.post({"t": "video.quality", "preset": self.quality_box.itemData(index)})

    def _toggle_stats(self):
        self.video.show_stats = self.stats_btn.isChecked()
        self._tick()
        self.video.update()

    def _toggle_full(self):
        if self.isFullScreen():
            self.showNormal()
            self.bar.show()
            self.full_btn.setText("На весь экран")
        else:
            self.showFullScreen()
            self.bar.hide()
            self.full_btn.setText("Выйти из полноэкранного")

    def keyPressEvent(self, e):
        if e.key() == Qt.Key_F11 or (e.key() == Qt.Key_Escape and self.isFullScreen()):
            self._toggle_full()
            return
        super().keyPressEvent(e)

    def eventFilter(self, obj, e):
        # full screen: the bar comes back while the pointer is at the top edge
        if obj is self.video and e.type() == QEvent.MouseMove and self.isFullScreen():
            near = e.position().y() < 4 or (self.bar.isVisible() and e.position().y() < self.bar.height() + 40)
            if near != self.bar.isVisible():
                self.bar.setVisible(near)
        return super().eventFilter(obj, e)

    def changeEvent(self, e):
        # nobody sees a minimised window: no picture until it comes back
        if e.type() == QEvent.WindowStateChange and self.session is not None and self.session.state == "connected":
            self.session.post({"t": "video.pause", "on": bool(self.windowState() & Qt.WindowMinimized)})
        super().changeEvent(e)

    def _ask_clock(self):
        if self.session is not None and self.session.state == "connected":
            self.session.post({"t": "clock", "v": now_us()})

    def _got_clock(self, msg):
        """The host's clock relative to ours, from the reply with the shortest round trip."""
        v, h = msg.get("v"), msg.get("h")
        if not isinstance(v, int) or not isinstance(h, int):
            return
        now = now_us()
        rtt = now - v
        self.clock_samples.append((rtt, h - v - rtt // 2))
        self.video.clock_offset = min(self.clock_samples)[1]

    def _tick(self):
        session = self.session
        if session is None:
            return
        fp = fingerprint(self.key)
        if session.state == "waiting":
            left = session.detail.get("timeout", 30) - (time.monotonic() - session.detail.get("since", 0))
            self.small.setText(f"Осталось {max(0, int(left))} с. Там должны нажать «Разрешить».")
        if session.state != "connected":
            self.sub.setText(fp)
            return
        p = session.path()
        via = "через ретранслятор" if p.get("via_relay") else "напрямую"
        rtt = f" · {p['rtt']} мс" if p.get("rtt") is not None else ""
        self.sub.setText(f"{fp} · {via}{rtt}")
        # statistics, over the last second or so
        now = time.monotonic()
        t0, frames0, bytes0, painted0 = self._stats_mark
        if now - t0 >= 1.0:
            dt = now - t0
            fps = (self.video.painted - painted0) / dt
            mbit = (self.rx.bytes - bytes0) * 8 / dt / 1e6
            self._stats_mark = (now, self.rx.frames, self.rx.bytes, self.video.painted)
            vs = self.video_state
            w, h = self.video.source_size()
            lat = self.video.latency()
            lines = [f"{w}×{h} · {fps:.0f} к/с · {mbit:.1f} Мбит/с",
                     f"RTT {p.get('rtt', '?')} мс · {via}"
                     + (f" · задержка ≈{lat:.0f} мс" if lat is not None else ""),
                     f"{vs.get('encoder', '?')} · {vs.get('capture', '?')} · декодер {self.decoder.decode_ms:.1f} мс"]
            if vs.get("quality") == "auto" and vs.get("step") is not None:
                lines.append(f"авто: {vs.get('fps')} к/с, до {vs.get('kbps', 0) // 1000} Мбит/с")
            self.video.stats = "\n".join(lines)
            if self.video.show_stats:
                self.video.update()

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
        self.clock_timer.stop()
        if self.session is not None:
            self.session.stop()
        self.decoder.stop()
        self.finished.emit(self.key)
        super().closeEvent(e)
