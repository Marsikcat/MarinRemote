"""
A connection to another PC as the viewer sees it: the remote screen in the middle; on top whom
it is with, the path (direct or relayed, round trip), which monitor and quality, control on or
off, statistics, full screen and a way out. Before the picture comes (or without the right to
see it) the middle says how things stand.

With control on, the mouse over the picture and the keys while this window is in front go to
the remote PC — the system keys too (Win, Alt+Tab…) through the keyboard hook. Ctrl+Alt+Enter
stays here: it switches full screen.
"""

import time
from collections import deque

import ctypes
import os

from PySide6.QtCore import QEvent, Qt, QTimer, Signal
from PySide6.QtWidgets import (QComboBox, QFrame, QHBoxLayout, QInputDialog, QMenu, QMessageBox,
                               QStackedWidget, QVBoxLayout, QWidget)

from ..codes import fingerprint
from ..host.clipboard import MAX_TEXT as MAX_CLIP
from ..host.capture import QUALITY
from ..host.power import DELAY as POWER_DELAY
from ..protocol import AUTH_REASONS, describe_rights, wait_text
from ..viewer.decoder import Decoder
from ..viewer.files import FileClient, join
from ..viewer.input import InputSender
from ..viewer.kbhook import KeyboardHook
from ..viewer.session import END_TEXT, ViewerSession
from ..viewer.video import VideoReceiver
from . import icons
from .bridge import ViewerBridge
from .clipboard import QtClipboard
from .filemanager import FileManager
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
POWER_ASK = {
    "lock": ("Заблокировать экран", "Заблокировать экран «{name}»?\n\nРазблокировать его отсюда в этой версии "
             "нельзя: экран блокировки защищён, войти сможет только человек у того ПК. Картинка и управление "
             "вернутся после входа."),
    "restart": ("Перезагрузить", f"Перезагрузить «{{name}}»?\n\nЧерез {POWER_DELAY} с ПК перезагрузится; "
                f"несохранённое в его программах может пропасть. Сеанс оборвётся — подключиться снова можно, "
                f"когда ПК загрузится и MarinRemote на нём запустится."),
    "shutdown": ("Выключить", f"Выключить «{{name}}»?\n\nЧерез {POWER_DELAY} с ПК выключится; несохранённое "
                 f"в его программах может пропасть. Включить его обратно отсюда не получится."),
}
POWER_DONE = {"restart": "перезагрузится", "shutdown": "выключится"}
VIDEO_REASONS = {
    "secure_desktop": "На удалённом ПК сейчас защищённый экран (запрос UAC, блокировка или экран входа).\n"
                      "Его нельзя показать в этой версии — картинка вернётся сама.",
    "no_monitor": "На удалённом ПК не найдено ни одного экрана.",
    "error": "Не удалось захватить экран удалённого ПК — пробуем снова…",
}
WARNINGS = {
    "uipi": "На удалённом ПК впереди окно, запущенное от имени администратора{app}. Управлять им "
            "MarinRemote не может — это ограничение Windows; картинку при этом видно.",
    "secure": "На удалённом ПК открыт защищённый экран (запрос UAC, Ctrl+Alt+Del или блокировка) — "
              "мышь и клавиатура туда не доходят.",
    "self": "Окна MarinRemote на удалённом ПК нельзя нажимать удалённо — это защита от того, "
            "чтобы зритель сам себе что-то разрешил.",
}


class SessionWindow(QWidget):
    finished = Signal(str)              # the remote key, when the window closes
    full_requested = Signal()           # Ctrl+Alt+Enter caught by the keyboard hook

    def __init__(self, settings, node, target, want, password=None):
        super().__init__(None, Qt.Window)
        self.s, self.node = settings, node
        self.target = dict(target)
        self.key = target["id"]
        self.want = list(want)
        self.video_state = {}
        self._had_control = False        # to tell the viewer when the host takes control back or returns it
        self._last_state = None
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
        self.control_btn = button("Управление", "small", self._apply_control,
                                  tip="Мышь и клавиатура управляют удалённым ПК")
        self.control_btn.setCheckable(True)
        self.control_btn.setChecked(True)
        self.keys_btn = button("Системные клавиши", "small", self._apply_control,
                               tip="Win, Alt+Tab, Alt+F4 и другие уходят на удалённый ПК, пока это окно впереди.\n"
                                   "Чтобы вернуть их своему ПК, щёлкните вне окна или выключите это.")
        self.keys_btn.setCheckable(True)
        self.keys_btn.setChecked(True)
        self.clip_btn = button("Буфер обмена", "small", tip="Скопированный здесь текст вставляется там, и наоборот.\n"
                                                           "Уходит только то, что скопировано во время сеанса.")
        self.clip_btn.setCheckable(True)
        self.clip_btn.setChecked(True)
        self.power_btn = button("Действия", "small", tip="Заблокировать, перезагрузить или выключить удалённый ПК")
        self.power_menu = QMenu(self)
        for action, (text, _) in POWER_ASK.items():
            self.power_menu.addAction(text + ("…" if action != "lock" else ""),
                                      lambda a=action: self._power(a))
        self.power_menu.addSeparator()
        self.power_cancel = self.power_menu.addAction("Отменить перезагрузку или выключение",
                                                      lambda: self.session.post({"t": "power", "action": "cancel"}))
        self.power_cancel.setEnabled(False)
        self.power_btn.setMenu(self.power_menu)
        self.files_btn = button("Файлы", "small", self.open_files,
                                tip="Файлы этого и удалённого ПК; файлы можно и просто перетащить на картинку")
        self.stats_btn = button("Статистика", "small", self._toggle_stats)
        self.stats_btn.setCheckable(True)
        self.full_btn = button("На весь экран", "small", self._toggle_full,
                               tip="Ctrl+Alt+Enter — на весь экран и обратно")
        self.save_btn = button("Сохранить в контакты", "small", self._save_contact)
        self.retry_btn = button("Подключиться снова", "small", self._retry)
        self.leave_btn = button("Отключить", "danger", self.close)
        for w in (self.monitor_box, self.quality_box, self.control_btn, self.keys_btn, self.clip_btn,
                  self.files_btn, self.power_btn, self.stats_btn, self.full_btn, self.save_btn, self.retry_btn, self.leave_btn):
            w.setFocusPolicy(Qt.NoFocus)        # keys stay with the picture (and the remote PC)
            b.addWidget(w)
        lay.addWidget(self.bar)
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
        self.full_requested.connect(self._toggle_full)
        self.session = None
        self.input = None
        self.files = None
        self.file_manager = None
        self.board = QtClipboard()
        self.board.on_change = self._local_clip
        self._hooked = None
        self.setAcceptDrops(True)
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
        self.input = InputSender(self.session)
        self.files = FileClient(self.session, self.s.folder / "transfers.json")
        if self.file_manager is not None:
            self.file_manager.set_client(self.files)
        self.video.rx = self.rx
        self.video.input = self.input
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
        if st != self._last_state:
            self._last_state = st
            self._had_control = "control" in session.rights
            if st == "connected" and self.file_manager is not None:
                self.file_manager.reload()
        self.title.setText(self._name())
        self.setWindowTitle(f"{self._name()} — MarinRemote")
        self.dot.setStyleSheet(f"background: {STATE_COLOR.get(st, YELLOW)}; border-radius: 5px;")
        self.retry_btn.setVisible(st == "closed")
        self.leave_btn.setText("Закрыть" if st == "closed" else "Отключить")
        self.save_btn.setVisible(st == "connected" and self.key not in (self.s["contacts"] or {}))
        watching = st in ("connected", "reconnecting") and "view" in session.rights
        for w in (self.quality_box, self.stats_btn, self.full_btn):
            w.setVisible(watching)
        for w in (self.control_btn, self.keys_btn):
            w.setVisible(watching and "control" in session.rights)
        self.files_btn.setVisible(st in ("connected", "reconnecting") and "files" in session.rights)
        self.power_btn.setVisible(st == "connected" and "control" in session.rights)
        self.clip_btn.setVisible(st in ("connected", "reconnecting") and "clipboard" in session.rights)
        self.monitor_box.setVisible(watching and self.monitor_box.count() > 1)
        if st != "connected":
            self.warn.hide()
        self._apply_control()
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
            self.small.setText(end_text(session.detail))
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
        elif t == "clip":
            text = msg.get("text")
            if self.clip_btn.isChecked() and isinstance(text, str) and 0 < len(text) <= MAX_CLIP:
                self.board.set_text(text)
        elif t == "warn":
            code = msg.get("code")
            text = WARNINGS.get(code)
            if text:
                app = msg.get("app") or ""
                self.warn.text.setText(text.format(app=f" ({app})" if app else ""))
            self.warn.setVisible(bool(text))
            if code == "self":                       # a moment's notice, not a state
                QTimer.singleShot(4000, self._hide_self_warning)
        elif t == "clock":
            self._got_clock(msg)
        elif t == "rights":
            had, has = self._had_control, "control" in session.rights
            if had and not has:
                self._note("Тот ПК забрал управление: вы смотрите, мышь и клавиатура туда не идут.")
            elif has and not had:
                self._note("Тот ПК вернул управление.")
            else:
                self._note(f"Тот ПК изменил права: {describe_rights(session.rights)}.")
        elif t == "power.ok":
            action = msg.get("action")
            if action == "lock":
                self._note(f"Экран «{self._name()}» заблокирован.")
            elif action in POWER_DONE:
                self.power_cancel.setEnabled(True)
                QTimer.singleShot((POWER_DELAY + 10) * 1000, self, lambda: self.power_cancel.setEnabled(False))
                self._note(f"«{self._name()}» {POWER_DONE[action]} через {msg.get('delay', POWER_DELAY)} с. "
                           f"Передумали — «Действия» → «Отменить».")
        elif t == "power.error":
            self._note(f"Не получилось: {msg.get('msg') or 'ошибка на том ПК'}.")
        elif t == "power.cancelled":
            self.power_cancel.setEnabled(False)
            what = {"restart": "Перезагрузка", "shutdown": "Выключение"}.get(msg.get("action"), "Действие")
            self._note(f"{what} отменено" + (" на том ПК." if msg.get("by") == "host" else "."))
        self._had_control = "control" in session.rights

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
        if not self.video.control and (e.key() == Qt.Key_F11 or (e.key() == Qt.Key_Escape and self.isFullScreen())):
            self._toggle_full()
            return
        super().keyPressEvent(e)

    # ── control ─────────────────────────────────────────────────────
    def _apply_control(self):
        s = self.session
        on = (s is not None and s.state == "connected" and "control" in s.rights
              and self.control_btn.isChecked())
        self.keys_btn.setEnabled(self.control_btn.isChecked())
        self.video.set_control(on)
        hook = on and self.keys_btn.isChecked()
        if hook:
            kb = KeyboardHook.get()
            hwnd = int(self.winId())
            if self._hooked != hwnd:
                kb.register(hwnd, self._hook_key)
                self._hooked = hwnd
            self.video.keys_by_hook = kb.installed
        else:
            if self._hooked is not None:
                KeyboardHook.get().unregister(self._hooked)
                self._hooked = None
            self.video.keys_by_hook = False
        if on:
            self.video.setFocus()

    def _hook_key(self, scan, ext, down, vk):
        """A key while this window is in front (the hook's thread): it goes to the remote PC."""
        if vk == 0x0D and down and _held(0x11) and _held(0x12):        # Ctrl+Alt+Enter: ours
            self.full_requested.emit()
            return True
        if self.input is not None:
            self.input.key(scan, ext, down, vk)
        return True

    # ── the clipboard ───────────────────────────────────────────────
    def _local_clip(self, text):
        s = self.session
        if (s is not None and s.state == "connected" and "clipboard" in s.rights and self.clip_btn.isChecked()
                and len(text) <= MAX_CLIP):
            s.post({"t": "clip", "text": text})

    # ── files ───────────────────────────────────────────────────────
    def open_files(self):
        if self.file_manager is None:
            self.file_manager = FileManager(self.files, self._name(), self.session.host.get("ver", ""),
                                            control="control" in self.session.rights)
            self.file_manager.destroyed.connect(self._files_closed)
        self.file_manager.showNormal()
        self.file_manager.raise_()
        self.file_manager.activateWindow()

    def _files_closed(self):
        self.file_manager = None

    def dragEnterEvent(self, e):
        s = self.session
        if e.mimeData().hasUrls() and s is not None and s.state == "connected" and "files" in s.rights:
            e.acceptProposedAction()
        else:
            e.ignore()

    def dropEvent(self, e):
        """Files dropped on the session go to the folder open on the right of «Файлы», or else
        into the remote PC's Downloads."""
        paths = [u.toLocalFile() for u in e.mimeData().urls() if u.isLocalFile()]
        if not paths:
            return
        e.acceptProposedAction()
        fm = self.file_manager
        if fm is not None and fm.remote_dir:
            fm.send(paths)
            self.open_files()
            return
        self.open_files()

        def got_roots(reply):
            roots = reply.get("roots") or []
            if not reply.get("ok") or not roots:
                self._note("На удалённом ПК не открыто ни одной папки для файлов.")
                return
            root = roots[0]["id"]

            def got_downloads(r):
                folder = join(root, "Downloads") if r.get("ok") else root
                for p in paths:
                    (self.files.upload_tree if os.path.isdir(p) else self.files.upload)(p, folder)
                self._note(f"Отправляю на удалённый ПК, в «{folder.split('/', 1)[-1] if '/' in folder else 'корень'}»: "
                           f"{len(paths)} шт.")
            self.file_manager.ask({"t": "fs.list", "path": join(root, "Downloads")}, got_downloads)
        self.file_manager.ask({"t": "fs.roots"}, got_roots)

    def _note(self, text):
        self.warn.text.setText(text)
        self.warn.show()
        QTimer.singleShot(5000, lambda: self.warn.text.text() == text and self.warn.hide())

    def _power(self, action):
        title, text = POWER_ASK[action]
        box = QMessageBox(QMessageBox.Warning, title, text.format(name=self._name()),
                          QMessageBox.Cancel, self)
        ok = box.addButton(title, QMessageBox.AcceptRole)
        box.setDefaultButton(QMessageBox.Cancel)
        box.exec()
        s = self.session
        if box.clickedButton() is ok and s is not None and s.state == "connected" and "control" in s.rights:
            s.post({"t": "power", "action": action})

    def _hide_self_warning(self):
        if self.warn.text.text() == WARNINGS["self"]:
            self.warn.hide()

    def eventFilter(self, obj, e):
        if (obj is self.video and e.type() == QEvent.KeyPress and e.key() in (Qt.Key_Return, Qt.Key_Enter)
                and e.modifiers() & Qt.ControlModifier and e.modifiers() & Qt.AltModifier):
            self._toggle_full()
            return True
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
        # the keys held when focus left would never be let go: let go of everything now
        if e.type() == QEvent.ActivationChange and not self.isActiveWindow() and self.input is not None:
            self.input.release_all()
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
        remember_contact(self.s, self.key, self.session.host)

    def _save_contact(self):
        if save_contact(self, self.s, self.key, self.session.host, self.target):
            self._on_state(self.session)

    def closeEvent(self, e):
        self.timer.stop()
        self.clock_timer.stop()
        if self._hooked is not None:
            KeyboardHook.get().unregister(self._hooked)
            self._hooked = None
        if self.input is not None:
            self.input.release_all()
        if self.file_manager is not None:
            self.file_manager.close()
        if self.session is not None:
            self.session.stop()
        self.decoder.stop()
        self.finished.emit(self.key)
        super().closeEvent(e)


def _held(vk):
    return bool(ctypes.windll.user32.GetAsyncKeyState(vk) & 0x8000)


def end_text(detail):
    """Why a session is over, in words (the detail of its "closed" state)."""
    if detail.get("reason") == "refused":
        text = AUTH_REASONS.get(detail.get("auth"), "Подключение отклонено.")
        return text.format(wait=wait_text(detail.get("retry_after") or 1))
    return END_TEXT.get(detail.get("reason"), "Сеанс завершён.")


def remember_contact(settings, key, host):
    """A saved contact learns where the PC is now (its relay, addresses) and when we last met."""
    contact = (settings["contacts"] or {}).get(key)
    if contact is None:
        return
    settings.put("contacts", key, {**contact, "host_name": host.get("name", ""),
                                   "relay": host.get("relay") or contact.get("relay"),
                                   "addrs": host.get("addrs") or contact.get("addrs") or [],
                                   "last": time.strftime("%Y-%m-%d %H:%M")})


def save_contact(parent, settings, key, host, target):
    """«Сохранить в контакты»: asks what to call the PC; True once it is saved."""
    name, ok = QInputDialog.getText(parent, "Сохранить в контакты", "Как назвать этот ПК:",
                                    text=host.get("name") or target.get("name") or "")
    if not ok:
        return False
    settings.put("contacts", key, {
        "name": name.strip() or host.get("name") or "ПК", "host_name": host.get("name", ""),
        "relay": host.get("relay") or target.get("relay"),
        "addrs": host.get("addrs") or target.get("addrs") or [],
        "added": time.strftime("%Y-%m-%d %H:%M"), "last": time.strftime("%Y-%m-%d %H:%M")})
    return True
