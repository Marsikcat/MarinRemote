"""
«… хочет подключиться» — the window that lets someone in. Nothing is pre-approved: the person
at this PC sees who it is (the name it gives itself and the fingerprint of its key), picks the
rights, may remember the device, and has 30 seconds; no answer is a «no».

The same window asks about pairing («… хочет связаться»): then it shows the code to compare with
the other PC, and «Связать» remembers that PC for good.
"""

import math
import time

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QDialog, QHBoxLayout,
                               QVBoxLayout)

from ..auth import MODES
from ..protocol import RIGHT_NAMES, RIGHTS
from . import icons
from .theme import mono_font, style_window
from .widgets import button, label, note


class ApproveDialog(QDialog):
    answered = Signal(int, dict)        # request id, decision

    def __init__(self, request, parent=None):
        super().__init__(parent, Qt.Dialog | Qt.WindowStaysOnTopHint | Qt.WindowTitleHint
                         | Qt.CustomizeWindowHint)
        self.request = request
        self.rid = request["id"]
        self.done_ = False
        self.deadline = time.monotonic() + request.get("timeout", 30)
        self.setAttribute(Qt.WA_DeleteOnClose)
        self.pair = request.get("pair")
        self.setWindowTitle("MarinRemote — " + ("связать ПК" if self.pair else "запрос на подключение"))
        self.setWindowIcon(icons.app_icon(True))
        self.setMinimumWidth(540)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(22, 20, 22, 18)
        lay.setSpacing(10)
        record = request.get("record") or {}
        title = label(f"«{request['name']}» хочет " + ("связаться с этим компьютером" if self.pair else
                                                         "подключиться к этому компьютеру"), "h2", wrap=True)
        lay.addWidget(title)
        who = [f"Устройство <b>{request['fp']}</b>"]
        if record:
            who.append(f"вы запомнили его как «{record.get('name', '')}»")
        else:
            who.append("подключается впервые")
        who.append("через ретранслятор" if request.get("via_relay") else
                   (f"напрямую, {request['via']}" if request.get("via") else "напрямую"))
        info = label(" · ".join(who), "muted", wrap=True)
        info.setTextFormat(Qt.RichText)
        lay.addWidget(info)
        if self.pair:
            code = label(self.pair)
            code.setFont(mono_font(26))
            code.setStyleSheet("font-size: 26pt; font-weight: 700;")
            code.setAlignment(Qt.AlignCenter)
            code.setTextInteractionFlags(Qt.TextSelectableByMouse)
            lay.addWidget(code)
            lay.addWidget(note(f"Сверьте код: на «{request['name']}» должен быть такой же. Если не совпадает — "
                               f"нажмите «Отклонить»: значит, в сети кто-то выдаёт себя за тот ПК."))
            lay.addWidget(label("Оба ПК запомнят друг друга: тот сможет подключаться сюда без подтверждения "
                                "с правами, отмеченными ниже, а этот — к нему в один клик из контактов. "
                                "Связывайте только свои компьютеры.", "hint", wrap=True))
        if request.get("password_given"):
            lay.addWidget(note("Указан пароль, но этому устройству вход по паролю не разрешён — "
                               "поэтому спрашиваем вас."))

        lay.addSpacing(4)
        lay.addWidget(label("Разрешить:", "caption"))
        self.boxes = {}
        want = request.get("want") or []
        for r in RIGHTS:
            cb = QCheckBox(RIGHT_NAMES[r])
            cb.setEnabled(r in want)
            cb.setChecked(r in want)
            if r not in want:
                cb.setToolTip("Устройство этого не просит")
            self.boxes[r] = cb
            lay.addWidget(cb)
        self.boxes["view"].toggled.connect(self._view_toggled)

        self.unattended = None
        if self.pair and not request.get("unattended", True):
            self.unattended = QCheckBox("Пускать запомненные устройства без подтверждения (сейчас выключено)")
            self.unattended.setToolTip("То же, что переключатель на странице «Доступ». Без него связанный ПК "
                                       "будет каждый раз спрашивать разрешения.")
            lay.addWidget(self.unattended)

        lay.addSpacing(4)
        self.remember = QCheckBox("Запомнить это устройство:")
        self.mode = QComboBox()
        for key in ("contact", "password", "ask"):
            self.mode.addItem(MODES[key].lower() if key != "contact" else
                              "дальше подключаться без подтверждения", key)
        self.mode.setEnabled(False)
        self.remember.toggled.connect(self.mode.setEnabled)
        row = QHBoxLayout()
        row.addWidget(self.remember)
        row.addWidget(self.mode, 1)
        if not self.pair:                        # pairing remembers it anyway
            lay.addLayout(row)
        else:
            self.remember.hide()
            self.mode.hide()
        if request.get("remember") and not self.pair:
            lay.addWidget(label("Устройство просит запомнить его, чтобы подключаться без подтверждения.",
                                "hint", wrap=True))

        lay.addSpacing(6)
        self.countdown = label("", "muted")
        lay.addWidget(self.countdown)
        buttons = QHBoxLayout()
        block = button("Заблокировать", "link", self._block,
                       tip="Отказать и больше не принимать запросы от этого устройства")
        buttons.addWidget(block)
        buttons.addStretch(1)
        deny = button("Отклонить", "secondary", self._deny)
        allow = button("Связать" if self.pair else "Разрешить", "success", self._allow)
        deny.setAutoDefault(False)
        allow.setAutoDefault(False)              # Enter must not let anyone in by accident
        buttons.addWidget(deny)
        buttons.addWidget(allow)
        lay.addLayout(buttons)

        self.timer = QTimer(self, interval=250, timeout=self._tick)
        self.timer.start()
        self._tick()

    def showEvent(self, e):
        super().showEvent(e)
        style_window(self)
        self.raise_()
        self.activateWindow()
        QApplication.alert(self, 0)
        QApplication.beep()

    def _view_toggled(self, on):
        """Control without seeing the screen makes no sense."""
        if not on:
            self.boxes["control"].setChecked(False)

    def _tick(self):
        left = self.deadline - time.monotonic()
        if left <= 0:
            self.countdown.setText("Время вышло — отказано.")
            self.timer.stop()
            return
        self.countdown.setText(f"Если не ответить, через {math.ceil(left)} с "
                               + ("связывание будет отменено." if self.pair else "подключение будет отклонено."))

    def _finish(self, decision):
        if self.done_:
            return
        self.done_ = True
        self.timer.stop()
        self.answered.emit(self.rid, decision)
        self.close()

    def _allow(self):
        rights = [r for r, cb in self.boxes.items() if cb.isChecked()]
        if not rights:
            self.countdown.setText("Отметьте хотя бы одно право — или нажмите «Отклонить».")
            return
        self._finish({"allow": True, "rights": rights, "block": False,
                      "remember": self.mode.currentData() if self.remember.isChecked() and not self.pair else None,
                      "unattended": self.unattended is not None and self.unattended.isChecked()})

    def _deny(self):
        self._finish({"allow": False})

    def _block(self):
        self._finish({"allow": False, "block": True})

    def withdraw(self):
        """The question is gone (timeout, the device left): close without answering."""
        self.done_ = True
        self.timer.stop()
        self.close()

    def reject(self):           # Esc / the window's close button
        self._deny()

    def closeEvent(self, e):
        if not self.done_:
            self._finish({"allow": False})
        super().closeEvent(e)
