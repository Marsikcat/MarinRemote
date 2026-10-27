"""
The journal of this PC, readable: who connected when and with which rights, what was refused,
every file that went either way, and what was blocked — newest on top, with a filter by kind
and a search over names, fingerprints and paths.
"""

import os
import subprocess


from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QAbstractItemView, QComboBox, QHeaderView, QLineEdit, QTableWidget,
                               QTableWidgetItem, QVBoxLayout, QWidget)

from ..journal import EVENTS
from ..protocol import describe_rights
from .theme import RED, YELLOW
from .widgets import button, hbox, label

KINDS = {
    "Все события": None,
    "Подключения": {"request", "approved", "denied", "auth_failed", "session_start", "session_end"},
    "Файлы": {"file_get", "file_put", "fs"},
    "Защита": {"violation", "pre_auth", "self_input", "kill_all", "blocked", "auth_failed"},
    "Настройки доступа": {"trusted", "untrusted", "blocked", "unblocked", "password", "roots"},
}
PROBLEMS = {"violation", "pre_auth", "self_input", "auth_failed", "kill_all"}
NOTABLE = {"denied", "blocked"}

REASONS = {
    "denied": "отклонено", "timeout": "никто не ответил", "left": "устройство ушло само",
    "bad_password": "неверный пароль", "locked": "слишком много попыток", "cooldown": "недавно отказано",
    "blocked": "устройство заблокировано", "off": "подключения выключены", "busy": "много запросов сразу",
    "version": "несовместимая версия", "protocol": "ошибка протокола", "kicked": "отключено здесь",
    "killed": "аварийное отключение", "replaced": "продолжено в новом подключении", "lost": "связь потеряна",
    "idle": "долгая тишина", "shutdown": "программа закрыта",
}
HOW = {"approved": "по подтверждению", "contact": "запомненное, без подтверждения", "password": "по паролю",
       "resume": "повторный вход после обрыва"}
RESULTS = {"ok": "готово", "interrupted": "прервано", "cancelled": "отменено", "hash": "не совпал SHA-256",
           "bad_path": "путь вне разрешённых папок", "denied": "нет доступа", "not_found": "не найдено",
           "changed": "файл изменился", "space": "нет места", "io": "ошибка диска"}
MODES = {"contact": "сразу", "password": "по паролю", "ask": "спрашивать"}


def human_size(n):
    for unit in ("Б", "КБ", "МБ", "ГБ", "ТБ"):
        if n < 1024 or unit == "ТБ":
            return f"{n:.0f} {unit}" if unit == "Б" else f"{n:.1f} {unit}"
        n /= 1024


def duration(seconds):
    s = int(seconds)
    if s < 60:
        return f"{s} с"
    if s < 3600:
        return f"{s // 60} мин {s % 60} с"
    return f"{s // 3600} ч {s % 3600 // 60} мин"


def details(e):
    """The interesting fields of an entry as one line for people."""
    out = []
    if e.get("rights"):
        out.append(describe_rights(e["rights"]))
    if e.get("how"):
        out.append(HOW.get(e["how"], e["how"]))
    if e.get("reason"):
        out.append(REASONS.get(e["reason"], e["reason"]))
    if e.get("action"):
        out.append({"mkdir": "новая папка", "rename": "переименование", "delete": "в корзину",
                    "add": "добавлена", "remove": "убрана", "set": "задан", "cleared": "убран",
                    "lock": "заблокировать экран", "restart": "перезагрузить", "shutdown": "выключить",
                    "cancel": "отменено " + ("на этом ПК" if e.get("by") == "host" else "зрителем")
                    }.get(e["action"], e["action"]))
    if e.get("path"):
        out.append(str(e["path"]) + (f" → {e['to']}" if e.get("to") else ""))
    if isinstance(e.get("size"), int):
        out.append(human_size(e["size"]))
    if e.get("offset"):
        out.append(f"продолжено с {human_size(e['offset'])}")
    if e.get("result"):
        out.append(RESULTS.get(e["result"], e["result"]))
    if e.get("msg"):
        out.append(str(e["msg"]))
    if e.get("what"):
        out.append(f"что: {e['what']}")
    if e.get("mode"):
        out.append(f"вход: {MODES.get(e['mode'], e['mode'])}")
    if isinstance(e.get("seconds"), int):
        out.append(f"длился {duration(e['seconds'])}")
    if e.get("sessions") is not None:
        out.append(f"сеансов: {e['sessions']}")
    if e.get("via"):
        out.append(("через ретранслятор " if e.get("relay") else "") + str(e["via"]))
    if e.get("sha256"):
        out.append(f"SHA-256 {e['sha256'][:16]}…")
    return " · ".join(out)


class JournalView(QWidget):
    def __init__(self, journal):
        super().__init__()
        self.journal = journal
        self.entries = []
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.kind = QComboBox()
        for name in KINDS:
            self.kind.addItem(name)
        self.kind.currentIndexChanged.connect(self._refill)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Поиск: устройство, отпечаток, путь…")
        self.search.textChanged.connect(self._refill)
        lay.addWidget(hbox(self.kind, self.search, button("Открыть папку журнала", "small", self._open_folder)))
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["Время", "Событие", "Устройство", "Подробности"])
        self.table.verticalHeader().hide()
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setWordWrap(False)
        hdr = self.table.horizontalHeader()
        for c in range(3):
            hdr.setSectionResizeMode(c, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(3, QHeaderView.Stretch)
        self.table.setMinimumHeight(420)
        lay.addWidget(self.table, 1)
        self.count = label("", "hint")
        lay.addWidget(self.count)
        self.entries = list(reversed(self.journal.read(2000)))
        self._refill()

    def add(self, entry):
        """A new entry (Qt thread)."""
        self.entries.insert(0, entry)
        del self.entries[5000:]
        if self._matches(entry):
            self._insert(0, entry)
            self._count()

    def _matches(self, e):
        kinds = KINDS[self.kind.currentText()]
        if kinds is not None and e.get("event") not in kinds:
            return False
        q = self.search.text().strip().lower()
        if q:
            hay = " ".join(str(e.get(k, "")) for k in ("name", "fp", "key", "path", "to", "what")).lower()
            return q in hay
        return True

    def _refill(self):
        self.table.setRowCount(0)
        rows = [e for e in self.entries if self._matches(e)][:2000]
        self.table.setRowCount(len(rows))
        for i, e in enumerate(rows):
            self._fill(i, e)
        self._count()

    def _insert(self, row, e):
        self.table.insertRow(row)
        self._fill(row, e)

    def _fill(self, row, e):
        who = f"{e.get('name', '')} {e.get('fp', '')}".strip()
        cells = (e.get("ts", ""), EVENTS.get(e.get("event"), e.get("event", "")), who, details(e))
        color = (QColor(RED) if e.get("event") in PROBLEMS or e.get("result") in ("hash", "bad_path")
                 else QColor(YELLOW) if e.get("event") in NOTABLE else None)
        for c, text in enumerate(cells):
            item = QTableWidgetItem(str(text))
            if color is not None and c == 1:
                item.setForeground(color)
            if c == 3:
                item.setToolTip(str(text))
            self.table.setItem(row, c, item)

    def _count(self):
        self.count.setText(f"Показано: {self.table.rowCount()} из {len(self.entries)}. Файл: {self.journal.path}")

    def _open_folder(self):
        path = str(self.journal.path)
        if os.path.exists(path):
            subprocess.Popen(["explorer", "/select,", path])
        else:
            os.startfile(os.path.dirname(path))


