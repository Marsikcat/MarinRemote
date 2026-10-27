"""
«Связать с …» on the PC that starts the pairing: dials the other PC, shows the code both screens
should have, and once the person here says the codes match and the other PC said yes, this PC
remembers that one (which may then come in without asking) and keeps it among its contacts. The
other side is HostCore._paired, with the same code in its confirmation window.
"""

import time

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QCheckBox, QDialog, QVBoxLayout

from ..codes import fingerprint
from ..protocol import AUTH_REASONS, RIGHT_NAMES, RIGHTS, wait_text
from ..viewer.pair import request_pair
from .theme import GREEN, mono_font, style_window
from .widgets import button, hbox, label

FAILED = {
    "lost": "Связь с тем ПК оборвалась.",
    "unreachable": "Не удалось связаться с тем ПК: он выключен или не в этой сети.",
    "timeout": "На том ПК не ответили.",
    "version": "На том ПК старая версия MarinRemote, она не умеет связываться — обновите её.",
}


class PairDialog(QDialog):
    got_code = Signal(str)
    got_answer = Signal(dict)

    def __init__(self, parent, settings, node, core, target):
        super().__init__(parent)
        self.s, self.node, self.core, self.target = settings, node, core, target
        self.name = target.get("name") or "ПК"
        self.answer = None              # the other PC's yes
        self.confirmed = None           # the rights this PC gives, once the person here said the codes match
        self.setAttribute(Qt.WA_DeleteOnClose)
        self.setWindowTitle(f"Связать с «{self.name}»")
        self.setMinimumWidth(500)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(22, 20, 22, 18)
        lay.setSpacing(10)
        lay.addWidget(label(f"Связать с «{self.name}»", "h2", wrap=True))
        lay.addWidget(label(f"Отпечаток {fingerprint(target['id'])}. Оба ПК запомнят друг друга: дальше "
                            f"подключение в один клик, без подтверждения и пароля. Связывайте только свои "
                            f"компьютеры.", "muted", wrap=True))
        self.code = label("· · ·")
        self.code.setFont(mono_font(26))
        self.code.setStyleSheet("font-size: 26pt; font-weight: 700;")
        self.code.setAlignment(Qt.AlignCenter)
        lay.addWidget(self.code)
        self.hint = label("Связываемся с тем ПК…", "muted", wrap=True)
        lay.addWidget(self.hint)

        lay.addWidget(label(f"«{self.name}» сможет на этом ПК:", "caption"))
        self.boxes = {}
        for r in RIGHTS:
            cb = QCheckBox(RIGHT_NAMES[r])
            cb.setChecked(True)
            self.boxes[r] = cb
            lay.addWidget(cb)
        self.boxes["view"].toggled.connect(lambda on: on or self.boxes["control"].setChecked(False))
        self.unattended = None
        if not self.s["unattended"]:
            self.unattended = QCheckBox("Пускать запомненные устройства без подтверждения (сейчас выключено)")
            lay.addWidget(self.unattended)

        self.status = label("", "muted", wrap=True)
        lay.addWidget(self.status)
        self.cancel_btn = button("Отмена", "secondary", self.close)
        self.ok_btn = button("Коды совпадают — связать", "success", self._confirm)
        self.ok_btn.setEnabled(False)
        self.ok_btn.setAutoDefault(False)
        lay.addWidget(hbox(None, self.cancel_btn, self.ok_btn))

        self.got_code.connect(self._code)
        self.got_answer.connect(self._answered)
        self.future = node.net.submit(request_pair(node, target, settings["name"], self._emit_code))
        self.future.add_done_callback(self._done)

    def showEvent(self, e):
        super().showEvent(e)
        style_window(self)

    # ── from the network thread ─────────────────────────────────────
    def _emit_code(self, code):
        try:
            self.got_code.emit(code)
        except RuntimeError:             # the window is gone
            pass

    def _done(self, fut):
        if fut.cancelled():
            return
        try:
            answer = fut.result()
        except Exception as e:
            answer = {"ok": False, "reason": "unreachable", "error": str(e)}
        try:
            self.got_answer.emit(answer)
        except RuntimeError:
            pass

    # ── here ────────────────────────────────────────────────────────
    def _code(self, code):
        self.code.setText(code)
        self.hint.setText(f"На «{self.name}» открылось окно с кодом. Сверьте: коды должны совпадать. "
                          f"Если совпадают — нажмите «Связать» на обоих ПК.")
        self.ok_btn.setEnabled(True)

    def _confirm(self):
        rights = [r for r, cb in self.boxes.items() if cb.isChecked()]
        if not rights:
            self.status.setText("Отметьте хотя бы одно право.")
            return
        self.confirmed = rights
        self.ok_btn.setEnabled(False)
        for cb in self.boxes.values():
            cb.setEnabled(False)
        if self.answer is not None:
            self._finish()
        else:
            self.status.setText(f"Ждём ответа на «{self.name}»…")

    def _answered(self, answer):
        if not answer.get("ok"):
            reason = answer.get("reason")
            text = FAILED.get(reason) or AUTH_REASONS.get(reason, "Тот ПК отказал.")
            self.status.setText(text.format(wait=wait_text(answer.get("retry_after") or 1)))
            self.hint.setText("")
            self.ok_btn.setEnabled(False)
            self.cancel_btn.setText("Закрыть")
            return
        self.answer = answer
        if self.confirmed is not None:
            self._finish()
        else:
            self.status.setText(f"«{self.name}» согласился. Если коды совпадают — нажмите «Связать».")

    def _finish(self):
        a, key = self.answer, self.target["id"]
        if self.unattended is not None and self.unattended.isChecked():
            self.s.set("unattended", True)
        contact = (self.s["contacts"] or {}).get(key) or {}
        name = contact.get("name") or a.get("name") or self.name
        now = time.strftime("%Y-%m-%d %H:%M")
        self.s.put("contacts", key, {**contact, "name": name, "host_name": a.get("name", ""),
                                     "relay": a.get("relay") or self.target.get("relay"),
                                     "addrs": a.get("addrs") or self.target.get("addrs") or [],
                                     "added": contact.get("added") or now, "last": now, "paired": now})
        self.core.trust(key, name, self.confirmed, "contact")
        self.core.journal.write("paired", key, name=a.get("name"), rights=self.confirmed)
        self.hint.setText("")
        self.status.setText(f"<b style='color:{GREEN}'>Готово: ПК связаны.</b> «{name}» в ваших контактах; "
                            f"подключение к нему и с него — без подтверждения.")
        self.status.setTextFormat(Qt.RichText)
        self.cancel_btn.setText("Готово")

    def closeEvent(self, e):
        if not self.future.done():
            self.future.cancel()            # the other PC's window closes by itself
        super().closeEvent(e)
