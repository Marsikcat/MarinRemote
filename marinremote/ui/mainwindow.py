"""
The main window: this PC (its code, who is connected), connecting to another PC (a code,
contacts, PCs on the LAN), who may come in without asking, and the journal.
"""

import os
import threading

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QGuiApplication
from PySide6.QtWidgets import (QApplication, QButtonGroup, QCheckBox, QComboBox, QDialog, QFileDialog,
                               QFrame, QHBoxLayout, QInputDialog, QLineEdit, QMainWindow, QMenu,
                               QMessageBox, QPushButton, QScrollArea, QStackedWidget,
                               QSystemTrayIcon, QVBoxLayout, QWidget)

from .. import auth, updater
from ..codes import fingerprint, make_code, make_link, parse_code, pretty, relay_label
from ..config import DISCOVERY_PORT, INSTANCE, PEER_PORT, VERSION
from ..host.fs import drive_roots
from ..hotkeys import KillHotkey, Recorder, describe
from ..protocol import RIGHT_NAMES, RIGHTS, describe_rights
from ..system import autostart_enabled, is_elevated, open_firewall, set_autostart
from . import icons
from .approve import ApproveDialog
from .files_window import FilesWindow
from .journalview import JournalView
from .overlay import Indicators, control_button
from .pair import PairDialog
from .session_window import SessionWindow
from .theme import GREEN, YELLOW, mono_font, style_window
from .widgets import button, card, clear_layout, dot, hbox, label, note, switch_row

_jobs = set()
FILES_TIP = "Только файлы: без картинки экрана и управления — тот ПК увидит, что просят лишь файлы"


class _Job(QObject):
    done = Signal(object)


def in_thread(fn, on_done):
    """fn() on a worker thread, on_done(result or exception) back on the Qt thread."""
    job = _Job()
    _jobs.add(job)

    def finish(result):
        _jobs.discard(job)
        on_done(result)

    job.done.connect(finish)

    def run():
        try:
            result = fn()
        except Exception as e:            # handed to on_done
            result = e
        job.done.emit(result)

    threading.Thread(target=run, daemon=True).start()


class NodeSignals(QObject):
    changed = Signal(str)               # "addr" | "lan" (from the network threads)


class UpdateProgress(QObject):
    changed = Signal(str)               # what to show while the update downloads (from its thread)


def page(title, subtitle=""):
    """A scrollable page: (scroll area, its vertical layout)."""
    inner = QWidget()
    lay = QVBoxLayout(inner)
    lay.setContentsMargins(28, 24, 28, 24)
    lay.setSpacing(14)
    lay.setSizeConstraint(QVBoxLayout.SetMinimumSize)     # taller than the window: scroll, don't squeeze
    lay.addWidget(label(title, "h1"))
    if subtitle:
        lay.addWidget(label(subtitle, "muted", wrap=True))
    area = QScrollArea()
    area.setWidgetResizable(True)
    area.setWidget(inner)
    return area, lay


def row(*widgets, stretch_at=0):
    w = QFrame()
    w.setObjectName("Row")
    lay = QHBoxLayout(w)
    lay.setContentsMargins(8, 6, 8, 6)
    lay.setSpacing(10)
    for i, x in enumerate(widgets):
        lay.addWidget(x, 1 if i == stretch_at else 0)
    return w


class MainWindow(QMainWindow):
    def __init__(self, settings, node, core, journal, approver, bridge):
        super().__init__()
        self.s, self.node, self.core, self.journal = settings, node, core, journal
        self.approver, self.bridge = approver, bridge
        self.sessions = []                # this PC's sessions (dicts from the core)
        self.dialogs = {}                 # request id -> ApproveDialog
        self.viewers = {}                 # remote key -> SessionWindow
        self.file_windows = {}            # remote key -> FilesWindow («Только файлы»)
        self._tray_told = False
        self._tray_click = None           # what a click on the tray's next message opens
        self.update_info = None
        self._update_told = None
        self.setWindowTitle("MarinRemote" + (f" — экземпляр {INSTANCE}" if INSTANCE else ""))
        self.setWindowIcon(icons.app_icon())
        self.resize(980, 700)

        root = QWidget()
        root.setObjectName("Root")
        h = QHBoxLayout(root)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(0)
        side = QFrame()
        side.setObjectName("Sidebar")
        side.setFixedWidth(210)
        sv = QVBoxLayout(side)
        sv.setContentsMargins(10, 16, 10, 12)
        sv.setSpacing(4)
        sv.addWidget(label("  MarinRemote", "h2"))
        sv.addSpacing(10)
        self.stack = QStackedWidget()
        self.nav = QButtonGroup(self)
        for i, (text, build) in enumerate((("Этот ПК", self._page_host), ("Подключиться", self._page_connect),
                                           ("Доступ", self._page_access), ("Журнал", self._page_journal),
                                           ("Настройки", self._page_settings))):
            b = QPushButton(text)
            b.setObjectName("NavButton")
            b.setCheckable(True)
            b.setCursor(Qt.PointingHandCursor)
            self.nav.addButton(b, i)
            sv.addWidget(b)
            self.stack.addWidget(build())
        self.nav.idClicked.connect(self.stack.setCurrentIndex)
        self.nav.button(0).setChecked(True)
        sv.addStretch(1)
        sv.addWidget(label(f"  версия {VERSION}", "hint"))
        h.addWidget(side)
        h.addWidget(self.stack, 1)
        self.setCentralWidget(root)

        # everything that happens elsewhere
        approver.asked.connect(self._ask)
        approver.withdrawn.connect(self._withdraw)
        bridge.sessions.connect(self._on_sessions)
        bridge.settings.connect(self._refresh_access)
        bridge.settings.connect(self._refresh_contacts)
        bridge.journal.connect(self._journal_add)
        bridge.power.connect(self._on_power)
        self.power_box = None
        journal.listeners.append(bridge.journal_entry)
        self.node_signals = NodeSignals()
        self.node_signals.changed.connect(self._on_node)
        node.listeners.append(self.node_signals.changed.emit)

        self.indicators = Indicators(core.disconnect, lambda: core.kill_all(), core.set_rights)
        self._make_tray()
        self.hotkey = KillHotkey(settings)
        self.hotkey.triggered.connect(self._kill_all)
        self.hotkey.start()

        self._refresh_code()
        self._refresh_contacts()
        self._refresh_access()
        self._on_sessions([])
        self.lan_timer = QTimer(self, interval=5000, timeout=self._refresh_contacts)
        self.lan_timer.start()

    def showEvent(self, e):
        super().showEvent(e)
        style_window(self)

    # ═════════════════════════ this PC ═════════════════════════════
    def _page_host(self):
        area, lay = page("Этот ПК", "Чтобы к этому компьютеру подключились, передайте код. "
                                    "Каждое подключение вы подтверждаете здесь, если не разрешили "
                                    "устройству входить без подтверждения.")
        c, cl = card()
        cl.addWidget(label("КОД ДЛЯ ПОДКЛЮЧЕНИЯ", "caption"))
        self.code_label = label("", "code", wrap=True, selectable=True)
        self.code_label.setFont(mono_font(12))
        cl.addWidget(self.code_label)
        self.copy_btn = button("Копировать код", None, self._copy_code)
        self.link_btn = button("Ссылка", "small", self._copy_link,
                               tip="Ссылка marinremote://… — открывается щелчком там, где MarinRemote установлен")
        self.name_label = label("", "muted")
        cl.addWidget(hbox(self.copy_btn, self.link_btn, self.name_label, None,
                          button("Переименовать", "small", self._rename)))
        self.net_dot = dot(YELLOW)
        self.net_label = label("", "muted")
        cl.addWidget(hbox(self.net_dot, self.net_label, None))
        lay.addWidget(c)

        c, cl = card()
        self.allow_row = switch_row("Разрешить подключаться к этому ПК",
                                    "Если выключить, все запросы будут отклоняться сразу.",
                                    self.s["allow_incoming"], lambda on: self._set("allow_incoming", on))
        cl.addWidget(self.allow_row)
        self.lan_row = switch_row("Виден в локальной сети",
                                  "Другие ПК в вашей сети (и в Radmin VPN) увидят этот ПК в списке без кода.",
                                  self.s["lan_visible"], lambda on: self._set("lan_visible", on))
        cl.addWidget(self.lan_row)
        lay.addWidget(c)

        c, cl = card()
        cl.addWidget(label("СЕЙЧАС ПОДКЛЮЧЕНЫ", "caption"))
        self.sessions_box = QVBoxLayout()
        self.sessions_box.setSpacing(2)
        cl.addLayout(self.sessions_box)
        self.kill_hint = label("", "hint", wrap=True)
        cl.addWidget(self.kill_hint)
        lay.addWidget(c)

        if not is_elevated():
            lay.addWidget(note("MarinRemote работает с правами обычного пользователя. Окнами программ, "
                               "запущенных от имени администратора, запросом UAC и экраном входа "
                               "удалённо управлять нельзя — это ограничение Windows."))
        lay.addStretch(1)
        return area

    # ── updates ─────────────────────────────────────────────────────
    def _check_updates(self, quiet=True):
        """quiet: the automatic check — says something only when there is a new version."""
        if quiet and not self.s["check_updates"]:
            return
        if not quiet:
            self.update_check_btn.setEnabled(False)
            self.update_label.setText("Проверяем…")

        def done(info):
            self.update_check_btn.setEnabled(True)
            if isinstance(info, Exception):
                if not quiet:
                    self.update_label.setText(f"Не удалось проверить: {info}")
                return
            self._show_update(info, quiet)
        in_thread(updater.check, done)

    def _show_update(self, info, quiet):
        self.update_info = info
        if not info["available"]:
            self.update_label.setText(f"Установлена версия {VERSION} — это последняя.")
            self.update_btn.hide()
            return
        link = f"<a href='{info['url']}'>что нового</a>"
        if updater.can_install():
            self.update_label.setText(f"Вышла версия <b>{info['latest']}</b> ({link}). Установлена {VERSION}.")
            self.update_btn.setText(f"Установить {info['latest']}")
        else:
            self.update_label.setText(f"Вышла версия <b>{info['latest']}</b> ({link}). Эта копия запущена не "
                                      f"из установленной программы — обновите её сами.")
            self.update_btn.setText("Открыть страницу выпуска")
        self.update_btn.setEnabled(True)
        self.update_btn.show()
        if quiet and self._update_told != info["latest"]:
            self._update_told = info["latest"]
            self._tray_click = self.show_settings
            self.tray.showMessage("MarinRemote", f"Вышла версия {info['latest']}. Установить — в «Настройках».",
                                  icons.app_icon(), 8000)

    def _install_update(self):
        info = self.update_info
        if not updater.can_install():
            os.startfile(info["url"])
            return
        if self.sessions and QMessageBox.question(
                self, "Обновление", f"К этому ПК сейчас подключены ({', '.join(s['name'] for s in self.sessions)}). "
                                    f"MarinRemote перезапустится, и сеансы прервутся. Продолжить?") != QMessageBox.Yes:
            return
        self.update_btn.setEnabled(False)
        self.update_check_btn.setEnabled(False)
        self.update_label.setText(f"Скачиваем {info['latest']}…")
        progress = UpdateProgress()
        progress.changed.connect(lambda text: self.update_label.setText(text))

        def fetch():
            def tick(done, total):
                if total:
                    progress.changed.emit(f"Скачиваем {info['latest']}… {done * 100 // total}%")
            return updater.download(info, tick)

        def done(result):
            if isinstance(result, Exception):
                self.update_label.setText(f"Обновление не установлено: {result}")
                self.update_btn.setEnabled(True)
                self.update_check_btn.setEnabled(True)
                return
            self.update_label.setText("Подпись проверена. Устанавливаем — MarinRemote перезапустится…")
            if updater.install():
                QTimer.singleShot(300, QApplication.quit)
            else:
                self.update_label.setText("Скачанный установщик изменился, пока ждал, — установка отменена.")
                self.update_btn.setEnabled(True)
        self._progress = progress
        in_thread(fetch, done)

    def _open_firewall(self):
        self.firewall_btn.setEnabled(False)
        self.firewall_note.setText("Ждём подтверждения Windows…")

        def done(ok):
            self.firewall_btn.setEnabled(True)
            self.firewall_note.setText("Порты открыты." if ok is True else
                                       "Не получилось — права администратора не дали или окно закрыли.")
        in_thread(open_firewall, done)

    def _code(self):
        return make_code(self.node.id, self.node.addr.get("relay"))

    def _refresh_code(self):
        self.code_label.setText(pretty(self._code(), 6))
        self.name_label.setText(f"{self.s['name']} · отпечаток {fingerprint(self.node.id)}")
        relay = self.node.addr.get("relay")
        if self.node.mode != "internet":
            color, text = YELLOW, f"Только локальная сеть · UDP {self.node.port}"
        elif relay:
            color, text = GREEN, f"В сети · ретранслятор {relay_label(relay)} · UDP {self.node.port}"
        else:
            color, text = YELLOW, "Подключение к ретранслятору… (в локальной сети уже можно подключаться)"
        self.net_dot.setStyleSheet(f"background: {color}; border-radius: 5px;")
        self.net_label.setText(text)

    def _copy_code(self):
        QGuiApplication.clipboard().setText(self._code())
        self.copy_btn.setText("Скопировано ✓")
        QTimer.singleShot(1800, lambda: self.copy_btn.setText("Копировать код"))

    def _copy_link(self):
        QGuiApplication.clipboard().setText(make_link(self.node.id, self.node.addr.get("relay")))
        self.link_btn.setText("Скопировано ✓")
        QTimer.singleShot(1800, lambda: self.link_btn.setText("Ссылка"))

    def open_link(self, text):
        """A marinremote:// link (or `--connect <code>`) from outside: asks, then connects."""
        self.show_window()
        try:
            target = parse_code(text)
        except ValueError as e:
            QMessageBox.warning(self, "Ссылка MarinRemote", f"По этой ссылке не подключиться: {e}.")
            return
        if target["id"] == self.node.id:
            QMessageBox.information(self, "Ссылка MarinRemote", "Это ссылка на этот же компьютер.")
            return
        contact = (self.s["contacts"] or {}).get(target["id"])
        want = [r for r, cb in self.want_boxes.items() if cb.isChecked()]
        who = (f"к «{contact.get('name') or 'ПК'}» из ваших контактов" if contact
               else "к ПК, которого нет в ваших контактах")
        if QMessageBox.question(
                self, "Ссылка MarinRemote",
                f"Подключиться {who}?\nОтпечаток: {fingerprint(target['id'])}\nЗапросить: "
                f"{describe_rights(want) if want else 'ничего не отмечено'}\n\n"
                f"Ссылку открыли из браузера или другой программы. Подключайтесь, только если вы её ждали: "
                f"тот ПК увидит имя этого, а с буфером обмена — и скопированный во время сеанса текст."
                ) != QMessageBox.Yes:
            return
        if contact:
            target["name"] = contact.get("name")
            target["addrs"] = contact.get("addrs") or []
        self.nav.button(self.CONNECT_PAGE).click()
        self.open_session(target)

    def _rename(self):
        name, ok = QInputDialog.getText(self, "Имя этого ПК", "Так этот ПК увидят другие:", text=self.s["name"])
        if ok and name.strip():
            self.s.set("name", name.strip()[:64])
            self._refresh_code()

    def _set(self, key, value):
        self.s.set(key, value)

    def _on_node(self, what):
        if what == "addr":
            self._refresh_code()
        else:
            self._refresh_contacts()

    def _on_sessions(self, sessions):
        self.sessions = sessions
        clear_layout(self.sessions_box)
        if not sessions:
            self.sessions_box.addWidget(label("Никто не подключён.", "muted"))
        for s in sessions:
            text = label(f"<b>{s['name']}</b> · {s['fp']} · {describe_rights(s['rights'])}"
                         f" · {'через ретранслятор' if s.get('via_relay') else 'напрямую'}")
            buttons = [b for b in (control_button(s, self.core.set_rights),
                                   button("Отключить", "danger", lambda _=False, sid=s["sid"]: self.core.disconnect(sid)))
                       if b is not None]
            self.sessions_box.addWidget(row(text, *buttons))
        self.kill_hint.setText(f"Разорвать все сеансы сразу, где бы ни был фокус: "
                               f"<b>{describe(self.s['kill_hotkey'])}</b>.")
        self.kill_hint.setTextFormat(Qt.RichText)
        self.indicators.update(sessions)
        active = bool(sessions)
        self.tray.setIcon(icons.app_icon(active))
        self.setWindowIcon(icons.app_icon(active))
        self.tray.setToolTip("MarinRemote — " + (f"подключено: {', '.join(s['name'] for s in sessions)}"
                                                 if active else "никто не подключён"))
        self.kill_action.setEnabled(active)
        self._lock_access(any("control" in s["rights"] for s in sessions))

    def _on_power(self, info):
        action, who = info.get("action"), f"«{info.get('name')}»"
        if info.get("state") == "cancelled":
            if self.power_box is not None:
                self.power_box.close()
            what = {"restart": "Перезагрузка", "shutdown": "Выключение"}.get(action, "Действие")
            self.tray.showMessage("MarinRemote", f"{what} отменено.", icons.app_icon(True), 4000)
            return
        if action == "lock":
            self.tray.showMessage("MarinRemote", f"{who} заблокировал экран этого ПК.", icons.app_icon(True), 5000)
            return
        what = {"restart": "перезагрузить", "shutdown": "выключить"}.get(action, action)
        if self.power_box is not None:
            self.power_box.close()
        box = QMessageBox(QMessageBox.Warning, "MarinRemote",
                          f"{who} ({info.get('fp')}) попросил {what} этот ПК — это случится через "
                          f"{info.get('delay')} с.\n\nЕсли это не вы, отмените и отключите его.")
        box.setWindowFlag(Qt.WindowStaysOnTopHint)
        stop = box.addButton("Отменить", QMessageBox.RejectRole)
        box.addButton("Пусть", QMessageBox.AcceptRole)
        box.setDefaultButton(stop)
        box.setAttribute(Qt.WA_DeleteOnClose)
        stop.clicked.connect(lambda: self.core.cancel_power())
        box.destroyed.connect(lambda _=None, b=box: self.power_box is b and setattr(self, "power_box", None))
        QTimer.singleShot(int(info.get("delay") or 30) * 1000, box, box.close)
        self.power_box = box
        box.show()
        box.raise_()

    def _kill_all(self):
        if self.sessions:
            self.core.kill_all()
            self.tray.showMessage("MarinRemote", "Все сеансы разорваны аварийной клавишей.",
                                  icons.app_icon(), 3000)

    # ═════════════════════════ connecting out ══════════════════════
    def _page_connect(self):
        area, lay = page("Подключиться к другому ПК")
        c, cl = card()
        cl.addWidget(label("КОД ДРУГОГО ПК", "caption"))
        self.code_edit = QLineEdit()
        self.code_edit.setPlaceholderText("Вставьте код: mr-…")
        self.code_edit.setFont(mono_font(11))
        self.code_edit.returnPressed.connect(self._connect_code)
        cl.addWidget(hbox(self.code_edit, button("Подключиться", None, lambda: self._connect_code()),
                          button("Только файлы", "secondary", lambda: self._connect_code(files=True), tip=FILES_TIP)))
        self.code_error = label("", "error", wrap=True)
        self.code_error.hide()
        cl.addWidget(self.code_error)
        cl.addWidget(label("ЧТО ЗАПРОСИТЬ", "caption"))
        self.want_boxes = {}
        boxes = QHBoxLayout()
        for r in RIGHTS:
            cb = QCheckBox(RIGHT_NAMES[r])
            cb.setChecked(r in self.s["want_rights"])
            cb.toggled.connect(self._want_changed)
            self.want_boxes[r] = cb
            boxes.addWidget(cb)
        boxes.addStretch(1)
        cl.addLayout(boxes)
        self.password_edit = QLineEdit()
        self.password_edit.setEchoMode(QLineEdit.Password)
        self.password_edit.setPlaceholderText("Пароль — только если тот ПК пускает вас по паролю")
        cl.addWidget(self.password_edit)
        lay.addWidget(c)

        c, cl = card()
        cl.addWidget(label("КОНТАКТЫ", "caption"))
        self.contacts_box = QVBoxLayout()
        self.contacts_box.setSpacing(2)
        cl.addLayout(self.contacts_box)
        lay.addWidget(c)

        c, cl = card()
        cl.addWidget(label("В ЛОКАЛЬНОЙ СЕТИ", "caption"))
        self.lan_box = QVBoxLayout()
        self.lan_box.setSpacing(2)
        cl.addLayout(self.lan_box)
        lay.addWidget(c)
        lay.addStretch(1)
        return area

    def _want_changed(self):
        self.s.set("want_rights", [r for r, cb in self.want_boxes.items() if cb.isChecked()])

    def _connect_code(self, files=False):
        self.code_error.hide()
        try:
            target = parse_code(self.code_edit.text())
        except ValueError as e:
            self.code_error.setText(str(e).capitalize() + ".")
            self.code_error.show()
            return
        if target["id"] == self.node.id:
            self.code_error.setText("Это код этого же компьютера.")
            self.code_error.show()
            return
        contact = (self.s["contacts"] or {}).get(target["id"])
        if contact:
            target["name"] = contact.get("name")
            target["addrs"] = contact.get("addrs") or []
        self.open_session(target, self.password_edit.text(), files=files)
        self.code_edit.clear()
        self.password_edit.clear()

    def open_session(self, target, password=None, files=False):
        if files:
            return self.open_files(target, password)
        want = [r for r, cb in self.want_boxes.items() if cb.isChecked()]
        if not want:
            self.code_error.setText("Отметьте, что запросить: хотя бы просмотр экрана.")
            self.code_error.show()
            return
        win = self.viewers.get(target["id"])
        if win is not None:
            win.showNormal()
            win.raise_()
            win.activateWindow()
            return
        target.setdefault("addrs", [])
        win = SessionWindow(self.s, self.node, target, want, password=password or None)
        win.finished.connect(self._viewer_closed)
        self.viewers[target["id"]] = win
        win.show()

    def _viewer_closed(self, key):
        self.viewers.pop(key, None)
        self._refresh_contacts()

    def open_files(self, target, password=None):
        """«Только файлы» with that PC — or «Файлы» of the session already open with it, which needs
        no second connection and no second confirmation there."""
        key = target["id"]
        view = self.viewers.get(key)
        if view is not None and view.session.state == "connected" and "files" in view.session.rights:
            view.open_files()
            return
        win = self.file_windows.get(key)
        if win is None:
            target.setdefault("addrs", [])
            win = FilesWindow(self.s, self.node, target, password=password or None)
            win.finished.connect(self._files_closed)
            self.file_windows[key] = win
        win.showNormal()
        win.raise_()
        win.activateWindow()

    def _files_closed(self, key):
        self.file_windows.pop(key, None)
        self._refresh_contacts()

    def _refresh_contacts(self):
        lan = self.node.discovered()
        contacts = dict(self.s["contacts"] or {})
        clear_layout(self.contacts_box)
        if not contacts:
            self.contacts_box.addWidget(label("Пока пусто. После подключения нажмите «Сохранить в контакты» — "
                                              "дальше подключение в один клик.", "muted", wrap=True))
        for key, c in sorted(contacts.items(), key=lambda kv: kv[1].get("name", "").lower()):
            here = key in lan
            text = label(f"<b>{c.get('name', 'ПК')}</b> · {fingerprint(key)}"
                         + (" · в локальной сети" if here else ""))
            more = button("⋯", "small", tip="Ещё")
            more.clicked.connect(lambda _=False, k=key, b=more: self._contact_menu(k, b))
            files = button("Файлы", "small", lambda _=False, k=key: self._connect_contact(k, files=True),
                           tip=FILES_TIP)
            go = button("Подключиться", "small", lambda _=False, k=key: self._connect_contact(k))
            self.contacts_box.addWidget(row(dot(GREEN if here else "#80848e", 8), text, files, go, more,
                                            stretch_at=1))
        clear_layout(self.lan_box)
        others = {k: v for k, v in lan.items() if k not in contacts}
        if not others:
            self.lan_box.addWidget(label("Никого не видно. Здесь появляются ПК, на которых запущен "
                                         "MarinRemote и включено «Виден в локальной сети».", "muted", wrap=True))
        for key, v in sorted(others.items(), key=lambda kv: kv[1]["name"].lower()):
            text = label(f"<b>{v['name'] or 'ПК'}</b> · {fingerprint(key)} · {v['ip']}")
            go = button("Подключиться", "small",
                        lambda _=False, k=key, n=v["name"]: self.open_session({"id": k, "relay": None, "name": n}))
            link = button("Связать", "small", lambda _=False, k=key, n=v["name"]: self._pair(k, n),
                          tip="Свой ПК: оба запомнят друг друга и дальше будут подключаться без подтверждения")
            files = button("Файлы", "small",
                           lambda _=False, k=key, n=v["name"]: self.open_files({"id": k, "relay": None, "name": n}),
                           tip=FILES_TIP)
            self.lan_box.addWidget(row(dot(GREEN, 8), text, link, files, go, stretch_at=1))

    def _connect_contact(self, key, files=False):
        c = (self.s["contacts"] or {}).get(key) or {}
        self.open_session({"id": key, "relay": c.get("relay"), "addrs": c.get("addrs") or [],
                           "name": c.get("name")}, self.password_edit.text(), files=files)
        self.password_edit.clear()

    def _pair(self, key, name):
        lan = self.node.discovered().get(key) or {}
        contact = (self.s["contacts"] or {}).get(key) or {}
        target = {"id": key, "relay": contact.get("relay"), "addrs": contact.get("addrs") or [],
                  "name": name or lan.get("name") or "ПК"}
        dlg = PairDialog(self, self.s, self.node, self.core, target)
        dlg.finished.connect(lambda _=0: self._refresh_contacts())
        dlg.show()

    def _contact_menu(self, key, anchor):
        menu = QMenu(self)
        rename = menu.addAction("Переименовать")
        code = menu.addAction("Скопировать код")
        link = menu.addAction("Скопировать ссылку")
        pair = menu.addAction("Связать (оба ПК запомнят друг друга)")
        pair.setEnabled(key in self.node.discovered())
        if not pair.isEnabled():
            pair.setText(pair.text() + " — только когда ПК в этой сети")
        menu.addSeparator()
        remove = menu.addAction("Удалить из контактов")
        act = menu.exec(anchor.mapToGlobal(anchor.rect().bottomLeft()))
        c = dict((self.s["contacts"] or {}).get(key) or {})
        if act is rename:
            name, ok = QInputDialog.getText(self, "Переименовать", "Имя контакта:", text=c.get("name", ""))
            if ok and name.strip():
                c["name"] = name.strip()[:64]
                self.s.put("contacts", key, c)
        elif act is code:
            QGuiApplication.clipboard().setText(make_code(key, c.get("relay")))
        elif act is link:
            QGuiApplication.clipboard().setText(make_link(key, c.get("relay")))
        elif act is pair:
            self._pair(key, c.get("name"))
        elif act is remove:
            self.s.put("contacts", key, None)
        self._refresh_contacts()

    # ═════════════════════════ who may come in ═════════════════════
    def _page_access(self):
        area, lay = page("Доступ к этому ПК", "Кого пускать без вашего подтверждения. Остальных вы "
                                              "впускаете вручную в окне запроса.")
        self.lock_note = note("Пока к этому ПК подключены с правом управления, настройки доступа "
                              "заблокированы — их нельзя поменять удалённо.", "Danger")
        lay.addWidget(self.lock_note)
        self.access_widgets = []

        c, cl = card()
        self.unattended_row = switch_row(
            "Пускать запомненные устройства без подтверждения",
            "Устройства с режимом «сразу» входят без окна запроса, с режимом «по паролю» — по паролю. "
            "Если выключить, каждое подключение придётся подтверждать здесь.",
            self.s["unattended"], lambda on: self._set("unattended", on))
        cl.addWidget(self.unattended_row)
        self.access_widgets.append(self.unattended_row)
        lay.addWidget(c)

        c, cl = card()
        cl.addWidget(label("ПАРОЛЬ ДЛЯ ВХОДА БЕЗ ПОДТВЕРЖДЕНИЯ", "caption"))
        self.password_state = label("", "muted", wrap=True)
        cl.addWidget(self.password_state)
        self.pw_set = button("Задать пароль", "small", self._set_password)
        self.pw_clear = button("Убрать пароль", "small", self._clear_password)
        cl.addWidget(hbox(self.pw_set, self.pw_clear, None))
        self.access_widgets += [self.pw_set, self.pw_clear]
        lay.addWidget(c)

        c, cl = card()
        cl.addWidget(label("ЗАПОМНЕННЫЕ УСТРОЙСТВА", "caption"))
        self.trusted_box = QVBoxLayout()
        self.trusted_box.setSpacing(2)
        cl.addLayout(self.trusted_box)
        add = button("Добавить устройство по коду", "small", self._add_device)
        cl.addWidget(hbox(add, None))
        cl.addWidget(label("Код устройства — на его странице «Этот ПК». Так можно заранее разрешить "
                           "своему ноутбуку входить сюда, даже если у этого ПК никого не будет.",
                           "hint", wrap=True))
        self.access_widgets.append(add)
        lay.addWidget(c)

        c, cl = card()
        cl.addWidget(label("ПАПКИ ДЛЯ ФАЙЛОВ", "caption"))
        self.roots_box = QVBoxLayout()
        self.roots_box.setSpacing(2)
        cl.addLayout(self.roots_box)
        add_root = button("Добавить папку или диск", "small", self._add_root)
        cl.addWidget(hbox(add_root, None))
        cl.addWidget(label("Устройства с правом «файлы» видят только эти папки — и ничего выше или рядом "
                           "с ними, даже по ярлыкам и ссылкам.", "hint", wrap=True))
        self.drives_row = switch_row(
            "Все диски этого ПК",
            "C:, D:, флешки, сетевые диски — сразу все, и новые тоже, как только появятся. Файлы Windows "
            "и чужих учётных записей всё равно защищены: программа работает с вашими правами.",
            self.s["files_all_drives"], self._set_all_drives)
        cl.addWidget(self.drives_row)
        self.access_widgets += [add_root, self.drives_row]
        lay.addWidget(c)

        c, cl = card()
        cl.addWidget(label("ЗАБЛОКИРОВАННЫЕ", "caption"))
        self.blocked_box = QVBoxLayout()
        self.blocked_box.setSpacing(2)
        cl.addLayout(self.blocked_box)
        lay.addWidget(c)

        c, cl = card()
        cl.addWidget(label("АВАРИЙНОЕ ОТКЛЮЧЕНИЕ", "caption"))
        self.hotkey_label = label("", None)
        self.hotkey_btn = button("Изменить", "small", self._change_hotkey)
        cl.addWidget(hbox(self.hotkey_label, None, self.hotkey_btn))
        cl.addWidget(label("Нажмите эту комбинацию в любой момент — все сеансы оборвутся, "
                           "а повторный вход потребует нового подтверждения.", "hint", wrap=True))
        self.access_widgets.append(self.hotkey_btn)
        lay.addWidget(c)
        lay.addStretch(1)
        self.recorder = Recorder(self)
        self.recorder.captured.connect(self._hotkey_captured)
        return area

    def _refresh_access(self):
        self.unattended_row.switch.setChecked(self.s["unattended"])
        has_pw = bool(self.s["password_hash"])
        self.password_state.setText(
            ("Пароль задан. " if has_pw else "Пароль не задан. ")
            + "Он действует только для устройств с режимом «по паролю» в списке ниже — "
              "чужое устройство даже с верным паролем не войдёт.")
        self.pw_set.setText("Сменить пароль" if has_pw else "Задать пароль")
        self.pw_clear.setVisible(has_pw)

        clear_layout(self.trusted_box)
        self.access_widgets_dynamic = []
        trusted = dict(self.s["trusted"] or {})
        if not trusted:
            self.trusted_box.addWidget(label("Нет. Отметьте «Запомнить это устройство» в окне запроса "
                                             "или добавьте устройство по коду.", "muted", wrap=True))
        for key, rec in sorted(trusted.items(), key=lambda kv: kv[1].get("name", "").lower()):
            mode = QComboBox()
            for m, text in auth.MODES.items():
                mode.addItem(text, m)
            mode.setCurrentIndex(max(0, mode.findData(rec.get("mode"))))
            mode.currentIndexChanged.connect(
                lambda _=0, k=key, r=rec, cb=mode: self.core.trust(k, r.get("name", ""), r.get("rights", []),
                                                                  cb.currentData()))
            text = label(f"<b>{rec.get('name', '')}</b> · {fingerprint(key)} · {describe_rights(rec.get('rights'))}")
            forget = button("Забыть", "small", lambda _=False, k=key: self.core.forget(k))
            self.trusted_box.addWidget(row(text, mode, forget))
            self.access_widgets_dynamic += [mode, forget]

        clear_layout(self.roots_box)
        roots = list(self.s["roots"] or [])
        self.drives_row.switch.setChecked(self.s["files_all_drives"])
        if self.s["files_all_drives"]:
            self.roots_box.addWidget(label("<b>Все диски</b> · " + ", ".join(d["id"] + ":" for d in drive_roots())))
        if not roots and not self.s["files_all_drives"]:
            self.roots_box.addWidget(label("Нет — файлы этого ПК удалённо недоступны.", "muted"))
        for r in roots:
            text = label(f"<b>{r.get('label', '')}</b> · {r.get('path', '')}")
            remove = button("Убрать", "small", lambda _=False, rid=r["id"]: self._remove_root(rid))
            self.roots_box.addWidget(row(text, remove))
            self.access_widgets_dynamic.append(remove)

        clear_layout(self.blocked_box)
        blocked = dict(self.s["blocked"] or {})
        if not blocked:
            self.blocked_box.addWidget(label("Нет.", "muted"))
        for key, rec in blocked.items():
            text = label(f"<b>{rec.get('name', '')}</b> · {fingerprint(key)} · с {rec.get('when', '')}")
            unblock = button("Разблокировать", "small", lambda _=False, k=key: self.core.unblock(k))
            self.blocked_box.addWidget(row(text, unblock))
        self.hotkey_label.setText(describe(self.s["kill_hotkey"]))
        self._lock_access(any("control" in s["rights"] for s in self.sessions))

    def _add_root(self):
        path = QFileDialog.getExistingDirectory(self, "Папка, к которой будет доступ")
        if not path:
            return
        path = os.path.normpath(path)
        roots = list(self.s["roots"] or [])
        if any(os.path.normcase(r["path"]) == os.path.normcase(path) for r in roots):
            return
        used = {r["id"] for r in roots}
        rid = next(f"r{i}" for i in range(1, 1000) if f"r{i}" not in used)
        label_text = os.path.basename(path.rstrip("\\")) or path
        roots.append({"id": rid, "path": path, "label": label_text})
        self.s.set("roots", roots)
        self.journal.write("roots", action="add", path=path)
        self._refresh_access()

    def _set_all_drives(self, on):
        if on == bool(self.s["files_all_drives"]):
            return
        self.s.set("files_all_drives", on)
        self.journal.write("roots", action="all_drives_on" if on else "all_drives_off")
        self._refresh_access()

    def _remove_root(self, rid):
        roots = list(self.s["roots"] or [])
        gone = next((r for r in roots if r["id"] == rid), None)
        self.s.set("roots", [r for r in roots if r["id"] != rid])
        if gone:
            self.journal.write("roots", action="remove", path=gone.get("path"))
        self._refresh_access()

    def _lock_access(self, locked):
        self.lock_note.setVisible(locked)
        for w in self.access_widgets + getattr(self, "access_widgets_dynamic", []):
            try:
                w.setEnabled(not locked)
            except RuntimeError:          # a row that was rebuilt meanwhile
                pass

    def _set_password(self):
        dlg = PasswordDialog(self)
        if dlg.exec() != QDialog.Accepted:
            return
        password = dlg.password()
        self.pw_set.setEnabled(False)
        self.password_state.setText("Сохраняю…")

        def done(result):
            self.pw_set.setEnabled(True)
            if isinstance(result, Exception):
                QMessageBox.warning(self, "Пароль", str(result))
            else:
                self.s.set("password_hash", result)
                self.journal.write("password", action="set")
            self._refresh_access()

        in_thread(lambda: auth.hash_password(password), done)

    def _clear_password(self):
        if QMessageBox.question(self, "Пароль", "Убрать пароль? Устройства с режимом «по паролю» "
                                "смогут подключаться только с подтверждением.") == QMessageBox.Yes:
            self.s.set("password_hash", "")
            self.journal.write("password", action="cleared")
            self._refresh_access()

    def _add_device(self):
        dlg = AddDeviceDialog(self, self.node.id)
        if dlg.exec() == QDialog.Accepted:
            key, name, rights, mode = dlg.result_
            self.core.trust(key, name, rights, mode)

    def _change_hotkey(self):
        self.hotkey_label.setText("Нажмите новую комбинацию (с Ctrl, Alt, Shift или Win)… Esc — отмена")
        self.hotkey.blockSignals(True)
        self.recorder.start()

    def _hotkey_captured(self, binding):
        self.hotkey.blockSignals(False)
        if binding:
            self.s.set("kill_hotkey", binding)
        self._refresh_access()
        self._on_sessions(self.sessions)

    # ═════════════════════════ settings ════════════════════════════
    CONNECT_PAGE, SETTINGS_PAGE = 1, 4

    def _page_settings(self):
        area, lay = page("Настройки")
        c, cl = card()
        cl.addWidget(label("ЗАПУСК", "caption"))
        self.autostart_row = switch_row(
            "Запускать вместе с Windows",
            "MarinRemote стартует при входе в Windows — к этому ПК можно подключиться, даже если вы "
            "сами программу не открывали. То же есть в меню значка в трее.",
            autostart_enabled(), self._apply_autostart)
        cl.addWidget(self.autostart_row)
        cl.addWidget(switch_row("При автозапуске — сразу в трей",
                                "Окно не открывается: программа тихо ждёт подключений, значок — в трее.",
                                self.s["start_minimized"], lambda on: self._set("start_minimized", on)))
        cl.addWidget(switch_row("Закрытие окна сворачивает в трей",
                                "Иначе закрытие окна выходит из программы и обрывает подключения.",
                                self.s["close_to_tray"], lambda on: self._set("close_to_tray", on)))
        lay.addWidget(c)

        c, cl = card()
        cl.addWidget(label("СЕТЬ", "caption"))
        self.firewall_btn = button("Открыть порты в брандмауэре", "small", self._open_firewall,
                                   tip=f"Входящие UDP {DISCOVERY_PORT} и {PEER_PORT} — Windows спросит права администратора")
        self.firewall_note = label(f"Входящие UDP {PEER_PORT} и {DISCOVERY_PORT}. Нужно только для прямых "
                                   "подключений в локальной сети, если брандмауэр их не пропустил — через "
                                   "интернет всё работает и так.", "hint", wrap=True)
        cl.addWidget(hbox(self.firewall_btn, None))
        cl.addWidget(self.firewall_note)
        lay.addWidget(c)

        c, cl = card()
        cl.addWidget(label("ОБНОВЛЕНИЯ", "caption"))
        self.update_label = label(f"Установлена версия {VERSION}.", wrap=True)
        self.update_label.setTextFormat(Qt.RichText)
        self.update_label.setOpenExternalLinks(True)
        cl.addWidget(self.update_label)
        self.update_check_btn = button("Проверить", "small", lambda: self._check_updates(quiet=False))
        self.update_btn = button("", None, self._install_update)
        self.update_btn.hide()
        cl.addWidget(hbox(self.update_check_btn, self.update_btn, None))
        cl.addWidget(switch_row("Проверять обновления сами",
                                "При запуске и дважды в день MarinRemote спрашивает GitHub, нет ли новой "
                                "версии, и говорит о ней. Ставится она только по вашей кнопке и только с "
                                "подписью MarinRemote.", self.s["check_updates"],
                                lambda on: self._set("check_updates", on)))
        lay.addWidget(c)

        c, cl = card()
        cl.addWidget(label("О ПРОГРАММЕ", "caption"))
        cl.addWidget(label(f"MarinRemote {VERSION}" + (f" · тестовый экземпляр {INSTANCE}" if INSTANCE else "")))
        cl.addWidget(label(f"Данные: {self.s.folder}", "hint", wrap=True, selectable=True))
        cl.addWidget(hbox(button("Открыть папку данных", "small", lambda: os.startfile(self.s.folder)), None))
        lay.addWidget(c)
        lay.addStretch(1)
        return area

    def _apply_autostart(self, on):
        """The switch in Settings and the check mark in the tray menu are one setting: the Run key."""
        if on != autostart_enabled():
            try:
                set_autostart(on)
            except OSError as e:
                QMessageBox.warning(self, "Автозапуск", f"Не получилось: {e}")
        self._sync_autostart()

    def _sync_autostart(self):
        on = autostart_enabled()                  # the installer can set it too: always ask Windows
        self.autostart_row.switch.setChecked(on)
        if hasattr(self, "autostart_action"):
            self.autostart_action.setChecked(on)

    def show_settings(self):
        self.show_window()
        self.nav.button(self.SETTINGS_PAGE).click()

    # ═════════════════════════ journal ═════════════════════════════
    def _page_journal(self):
        area, lay = page("Журнал", "Кто и когда подключался к этому ПК, с какими правами, что было "
                                   "отклонено, какие файлы передавались и что было заблокировано.")
        self.journal_view = JournalView(self.journal)
        lay.addWidget(self.journal_view, 1)
        return area

    def _journal_add(self, e):
        self.journal_view.add(e)

    # ═════════════════════════ confirmation ════════════════════════
    def _ask(self, request):
        dlg = ApproveDialog(request)
        dlg.answered.connect(self._answered)
        self.dialogs[request["id"]] = dlg
        dlg.show()

    def _answered(self, rid, decision):
        self.dialogs.pop(rid, None)
        self.approver.answer(rid, decision)

    def _withdraw(self, rid):
        dlg = self.dialogs.pop(rid, None)
        if dlg is not None:
            dlg.withdraw()

    # ═════════════════════════ tray & closing ══════════════════════
    def _make_tray(self):
        self.tray = QSystemTrayIcon(icons.app_icon(), self)
        menu = QMenu()
        show = QAction("Открыть MarinRemote", menu)
        show.triggered.connect(self.show_window)
        self.kill_action = QAction("Отключить все сеансы", menu)
        self.kill_action.triggered.connect(lambda: self.core.kill_all())
        self.autostart_action = QAction("Запускать вместе с Windows", menu, checkable=True)
        self.autostart_action.setChecked(autostart_enabled())
        self.autostart_action.toggled.connect(self._apply_autostart)
        settings = QAction("Настройки…", menu)
        settings.triggered.connect(self.show_settings)
        quit_ = QAction("Выход", menu)
        quit_.triggered.connect(QApplication.quit)
        for a in (show, self.kill_action):
            menu.addAction(a)
        menu.addSeparator()
        menu.addAction(self.autostart_action)
        menu.addAction(settings)
        menu.addSeparator()
        menu.addAction(quit_)
        menu.aboutToShow.connect(self._sync_autostart)
        self.tray.setContextMenu(menu)
        self.tray_menu = menu
        self.tray.messageClicked.connect(self._tray_message_clicked)
        self.tray.activated.connect(lambda reason: self.show_window()
                                    if reason in (QSystemTrayIcon.Trigger, QSystemTrayIcon.DoubleClick) else None)
        self.tray.show()

    def _tray_message_clicked(self):
        action, self._tray_click = self._tray_click, None
        (action or self.show_window)()

    def start_update_checks(self):
        """At start (a little later, the network first) and twice a day."""
        QTimer.singleShot(20_000, self._check_updates)
        self.update_timer = QTimer(self, interval=12 * 3600 * 1000, timeout=self._check_updates)
        self.update_timer.start()

    def show_window(self):
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def closeEvent(self, e):
        if self.s["close_to_tray"] and QSystemTrayIcon.isSystemTrayAvailable():
            e.ignore()
            self.hide()
            if not self._tray_told:
                self._tray_told = True
                self.tray.showMessage("MarinRemote работает в трее",
                                      "К этому ПК по-прежнему можно подключиться. Выход — в меню значка.",
                                      icons.app_icon(), 4000)
            return
        QApplication.quit()

    def shutdown(self):
        for win in list(self.viewers.values()) + list(self.file_windows.values()):
            win.close()
        self.indicators.close()
        self.tray.hide()


class PasswordDialog(QDialog):
    def __init__(self, parent):
        super().__init__(parent)
        self.setWindowTitle("Пароль для входа без подтверждения")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 18, 20, 16)
        lay.addWidget(label(f"Не короче {auth.MIN_PASSWORD} символов. Его хранит только этот ПК — "
                            "в виде хэша argon2id, сам пароль нигде не записывается.", "muted", wrap=True))
        self.a, self.b = QLineEdit(), QLineEdit()
        for e, text in ((self.a, "Пароль"), (self.b, "Ещё раз")):
            e.setEchoMode(QLineEdit.Password)
            e.setPlaceholderText(text)
            lay.addWidget(e)
        self.err = label("", "error", wrap=True)
        lay.addWidget(self.err)
        lay.addWidget(hbox(None, button("Отмена", "secondary", self.reject), button("Сохранить", None, self._ok)))

    def password(self):
        return self.a.text()

    def _ok(self):
        if len(self.a.text()) < auth.MIN_PASSWORD:
            self.err.setText(f"Пароль короче {auth.MIN_PASSWORD} символов.")
        elif self.a.text() != self.b.text():
            self.err.setText("Пароли не совпадают.")
        else:
            self.accept()


class AddDeviceDialog(QDialog):
    def __init__(self, parent, my_id):
        super().__init__(parent)
        self.my_id = my_id
        self.result_ = None
        self.setWindowTitle("Добавить устройство")
        self.setMinimumWidth(480)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 18, 20, 16)
        self.code = QLineEdit()
        self.code.setPlaceholderText("Код устройства: mr-…")
        self.code.setFont(mono_font(10))
        self.name = QLineEdit()
        self.name.setPlaceholderText("Как его назвать, например «Мой ноутбук»")
        lay.addWidget(self.code)
        lay.addWidget(self.name)
        self.mode = QComboBox()
        for m, text in auth.MODES.items():
            self.mode.addItem(text, m)
        lay.addWidget(hbox(label("Входить:"), self.mode, None))
        self.boxes = {}
        for r in RIGHTS:
            cb = QCheckBox(RIGHT_NAMES[r])
            cb.setChecked(True)
            self.boxes[r] = cb
            lay.addWidget(cb)
        self.err = label("", "error", wrap=True)
        lay.addWidget(self.err)
        lay.addWidget(hbox(None, button("Отмена", "secondary", self.reject), button("Добавить", None, self._ok)))

    def _ok(self):
        try:
            target = parse_code(self.code.text())
        except ValueError as e:
            self.err.setText(str(e).capitalize() + ".")
            return
        if target["id"] == self.my_id:
            self.err.setText("Это код этого же компьютера.")
            return
        rights = [r for r, cb in self.boxes.items() if cb.isChecked()]
        if not rights:
            self.err.setText("Отметьте хотя бы одно право.")
            return
        self.result_ = (target["id"], self.name.text().strip()[:64] or fingerprint(target["id"]), rights,
                        self.mode.currentData())
        self.accept()
