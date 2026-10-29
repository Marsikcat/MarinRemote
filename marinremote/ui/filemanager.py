"""
Files between this PC and the remote one: two panels side by side (here | there), buttons to send
and fetch the selection, drag and drop between the panels and from Explorer, and the list of
transfers with their progress, speed and a way to cancel or continue them.
"""

import json
import os
import string
import time
from pathlib import Path

from PySide6.QtCore import QMimeData, QObject, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QDrag
from PySide6.QtWidgets import (QAbstractItemView, QComboBox, QHBoxLayout, QHeaderView, QInputDialog,
                               QLabel, QLineEdit, QMessageBox, QProgressBar, QSplitter, QTableWidget,
                               QTableWidgetItem, QVBoxLayout, QWidget)

from ..updater import parse_version
from ..viewer.files import human_speed, join
from . import icons
from .theme import style_window
from .widgets import button, label

REMOTE_MIME = "application/x-marinremote-remote"
STATE_TEXT = {"queued": "в очереди", "starting": "начинается", "running": "идёт", "finishing": "проверка SHA-256",
              "done": "готово", "error": "ошибка", "cancelled": "отменено", "interrupted": "прервано"}


def human_size(n):
    for unit in ("Б", "КБ", "МБ", "ГБ", "ТБ"):
        if n < 1024 or unit == "ТБ":
            return f"{n:.0f} {unit}" if unit == "Б" else f"{n:.1f} {unit}"
        n /= 1024


class _Replies(QObject):
    got = Signal(object, object)        # callback, reply — delivered on the Qt thread


class FileTable(QTableWidget):
    """Name / size / modified, folders first. Drags out its selection; takes drops."""

    dropped = Signal(object)            # QMimeData
    opened = Signal(dict)               # an entry was double-clicked

    def __init__(self, remote):
        super().__init__(0, 3)
        self.remote = remote
        self.entries = []
        self.setHorizontalHeaderLabels(["Имя", "Размер", "Изменён"])
        self.verticalHeader().hide()
        self.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.setShowGrid(False)
        hdr = self.horizontalHeader()
        hdr.setSectionResizeMode(0, QHeaderView.Stretch)
        hdr.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setDragDropMode(QAbstractItemView.DragDrop)
        self.cellDoubleClicked.connect(lambda r, _c: self.opened.emit(self.entries[r]))

    def fill(self, entries):
        self.entries = sorted(entries, key=lambda e: (not e["d"], e["n"].lower()))
        self.setRowCount(len(self.entries))
        for r, e in enumerate(self.entries):
            name = QTableWidgetItem(("📁  " if e["d"] else "      ") + e["n"])
            size = QTableWidgetItem("" if e["d"] else human_size(e.get("s", 0)))
            size.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            when = QTableWidgetItem(time.strftime("%d.%m.%Y %H:%M", time.localtime(e["m"])) if e.get("m") else "")
            for c, item in enumerate((name, size, when)):
                self.setItem(r, c, item)

    def selected(self):
        return [self.entries[i.row()] for i in self.selectionModel().selectedRows()]

    def startDrag(self, actions):
        chosen = self.selected()
        if not chosen:
            return
        mime = QMimeData()
        if self.remote:
            mime.setData(REMOTE_MIME, json.dumps(chosen).encode())
        else:
            mime.setUrls([QUrl.fromLocalFile(e["path"]) for e in chosen])
        drag = QDrag(self)
        drag.setMimeData(mime)
        drag.exec(Qt.CopyAction)

    def dragEnterEvent(self, e):
        md = e.mimeData()
        if (self.remote and md.hasUrls()) or (not self.remote and md.hasFormat(REMOTE_MIME)):
            e.acceptProposedAction()
        else:
            e.ignore()

    dragMoveEvent = dragEnterEvent

    def dropEvent(self, e):
        self.dropped.emit(e.mimeData())
        e.acceptProposedAction()


class FileManager(QWidget):
    """A window of its own — or, given a parent, the inside of one («Только файлы»). `control`: this
    session controls the other PC, which locks its access settings while it lasts."""

    def __init__(self, files, host_name, host_ver="", parent=None, control=True):
        super().__init__(parent, Qt.Widget if parent is not None else Qt.Window)
        self.files = files
        self.host_name, self.host_ver = host_name, host_ver
        self.control = control
        if parent is None:
            self.setAttribute(Qt.WA_DeleteOnClose)
        self.setObjectName("Root")
        self.setWindowTitle(f"Файлы — {host_name}")
        self.setWindowIcon(icons.app_icon())
        self.resize(1180, 760)
        self.replies = _Replies()
        self.replies.got.connect(lambda cb, reply: cb(reply))
        self.local_dir = str(_downloads())
        self.remote_dir = None
        self.roots = []

        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 12, 12, 12)
        split = QSplitter(Qt.Horizontal)

        # ── here ──
        left = QWidget()
        lv = QVBoxLayout(left)
        lv.setContentsMargins(0, 0, 0, 0)
        lv.addWidget(label("ЭТОТ ПК", "caption"))
        self.local_path = QLineEdit()
        self.local_path.returnPressed.connect(lambda: self.open_local(self.local_path.text()))
        lv.addWidget(_row(button("↑", "small", self.local_up, tip="На уровень выше"), self.local_path,
                          button("⟳", "small", lambda: self.open_local(self.local_dir), tip="Обновить")))
        self.local = FileTable(remote=False)
        self.local.opened.connect(self._local_opened)
        self.local.dropped.connect(self._dropped_here)
        lv.addWidget(self.local, 1)
        split.addWidget(left)

        # ── the middle ──
        mid = QWidget()
        mv = QVBoxLayout(mid)
        mv.addStretch(1)
        mv.addWidget(button("Отправить →", None, self.send_selected, tip="Выбранное здесь — в папку справа"))
        mv.addWidget(button("← Скачать", None, self.fetch_selected, tip="Выбранное там — в папку слева"))
        mv.addStretch(1)
        split.addWidget(mid)

        # ── there ──
        right = QWidget()
        rv = QVBoxLayout(right)
        rv.setContentsMargins(0, 0, 0, 0)
        rv.addWidget(label(host_name.upper(), "caption"))
        self.root_box = QComboBox()
        self.root_box.setMinimumWidth(190)
        self.root_box.activated.connect(lambda i: self.open_remote(self.root_box.itemData(i)))
        self.remote_path = QLabel("")
        self.remote_path.setProperty("role", "muted")
        rv.addWidget(_row(self.root_box, button("↑", "small", self.remote_up, tip="На уровень выше"),
                          self.remote_path, None,
                          button("⟳", "small", lambda: self.open_remote(self.remote_dir), tip="Обновить"),
                          button("Новая папка", "small", self.remote_mkdir),
                          button("Переименовать", "small", self.remote_rename),
                          button("Удалить", "small", self.remote_delete)))
        self.remote = FileTable(remote=True)
        self.remote.opened.connect(self._remote_opened)
        self.remote.dropped.connect(self._dropped_there)
        rv.addWidget(self.remote, 1)
        self.remote_hint = label("", "hint", wrap=True)
        self.remote_hint.hide()
        rv.addWidget(self.remote_hint)
        self.remote_error = label("", "error", wrap=True)
        rv.addWidget(self.remote_error)
        split.addWidget(right)
        split.setSizes([520, 120, 540])
        lay.addWidget(split, 3)

        # ── transfers ──
        lay.addWidget(_row(label("ПЕРЕДАЧИ", "caption"), None,
                           button("Убрать завершённые", "small", self.files.clear_finished)))
        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(["", "Файл", "Прогресс", "Скорость", "Состояние", ""])
        self.table.verticalHeader().hide()
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionMode(QAbstractItemView.NoSelection)
        hdr = self.table.horizontalHeader()
        for c, mode in enumerate((QHeaderView.ResizeToContents, QHeaderView.Stretch, QHeaderView.Fixed,
                                  QHeaderView.ResizeToContents, QHeaderView.ResizeToContents,
                                  QHeaderView.Fixed)):
            hdr.setSectionResizeMode(c, mode)
        hdr.resizeSection(2, 220)
        hdr.resizeSection(5, 130)
        lay.addWidget(self.table, 2)
        self._rows = {}
        self.timer = QTimer(self, interval=300, timeout=self.refresh_transfers)
        self.timer.start()

        self.open_local(self.local_dir)
        self.ask({"t": "fs.roots"}, self._got_roots)

    def showEvent(self, e):
        super().showEvent(e)
        style_window(self)

    # ── requests ────────────────────────────────────────────────────
    def ask(self, msg, callback):
        fut = self.files.request(msg)
        fut.add_done_callback(lambda f: self.replies.got.emit(callback, f.result()))

    def set_client(self, files):
        """The session started again: a new client, the same window. Its folders come with
        reload(), once it is connected — asked before that, nothing would answer."""
        self.files = files
        self._rows = {}
        self.table.setRowCount(0)

    def reload(self):
        """The line is (back) up: the other PC's folders again, where we were."""
        self.ask({"t": "fs.roots"}, self._got_roots)

    # ── here ────────────────────────────────────────────────────────
    def open_local(self, path):
        if not path:
            drives = [f"{d}:\\" for d in string.ascii_uppercase if os.path.exists(f"{d}:\\")]
            self.local_dir = ""
            self.local_path.setText("Компьютер")
            self.local.fill([{"n": d, "d": True, "s": 0, "m": 0, "path": d} for d in drives])
            return
        try:
            entries = []
            with os.scandir(path) as it:
                for e in it:
                    try:
                        st = e.stat()
                        entries.append({"n": e.name, "d": e.is_dir(), "s": st.st_size, "m": int(st.st_mtime),
                                        "path": e.path})
                    except OSError:
                        continue
        except OSError as e:
            QMessageBox.warning(self, "Файлы", f"Не открыть папку: {e}")
            return
        self.local_dir = path
        self.local_path.setText(path)
        self.local.fill(entries)

    def local_up(self):
        if not self.local_dir:
            return
        parent = os.path.dirname(self.local_dir.rstrip("\\/"))
        self.open_local("" if parent == self.local_dir.rstrip("\\/") or not parent else
                        (parent + "\\" if parent.endswith(":") else parent))

    def _local_opened(self, e):
        if e["d"]:
            self.open_local(e["path"])

    # ── there ───────────────────────────────────────────────────────
    def _got_roots(self, reply):
        self.root_box.clear()
        if not reply.get("ok"):
            self.remote_error.setText(reply.get("msg", ""))
            return
        self.roots = reply.get("roots") or []
        for r in self.roots:
            self.root_box.addItem(r["label"], r["id"])
        # 0.2.0 doesn't say it; its drives are roots named by a single letter
        self._explain_roots(bool(reply.get("all_drives", any(len(r["id"]) == 1 for r in self.roots))))
        if not self.roots:
            self.remote_error.setText("На удалённом ПК не открыто ни одной папки для файлов.")
            return
        self.open_remote(self.remote_dir or self.roots[0]["id"])

    def _explain_roots(self, all_drives):
        """Which folders the other PC opened is up to it alone: say where that is changed."""
        if all_drives:
            self.remote_hint.hide()
            return
        opened = ", ".join(f"«{r['label']}»" for r in self.roots) or "ничего"
        old = parse_version(self.host_ver or "0") < (0, 2, 0)
        self.remote_hint.setText(
            f"{self.host_name} открыл для файлов только {opened} — выше не подняться. Другие папки и диски "
            f"открывают на нём самом: «Доступ» → «Папки для файлов» → «Все диски этого ПК»"
            + (f" (там MarinRemote {self.host_ver or 'старой версии'}: сначала обновите его до 0.2.0)." if old
               else ". Пока к нему подключены с управлением, эти настройки там заблокированы — включите "
                    "заранее или отключитесь на минуту." if self.control else "."))
        self.remote_hint.show()

    def open_remote(self, path):
        if not path:
            return
        self.ask({"t": "fs.list", "path": path}, lambda reply, p=path: self._got_list(p, reply))

    def _got_list(self, path, reply):
        if not reply.get("ok"):
            self.remote_error.setText(reply.get("msg", "не удалось открыть папку"))
            return
        self.remote_error.setText("")
        self.remote_dir = path
        root_id, _, rest = path.partition("/")
        i = self.root_box.findData(root_id)
        if i >= 0:
            self.root_box.setCurrentIndex(i)
        self.remote_path.setText("/" + rest if rest else "/")
        self.remote.fill([dict(e, path=join(path, e["n"])) for e in reply.get("entries") or []])

    def remote_up(self):
        if self.remote_dir and "/" in self.remote_dir:
            self.open_remote(self.remote_dir.rsplit("/", 1)[0])

    def _remote_opened(self, e):
        if e["d"]:
            self.open_remote(e["path"])

    def remote_mkdir(self):
        if not self.remote_dir:
            return
        name, ok = QInputDialog.getText(self, "Новая папка", "Имя папки:")
        if ok and name.strip():
            self.ask({"t": "fs.mkdir", "path": join(self.remote_dir, name.strip())}, self._after_change)

    def remote_rename(self):
        chosen = self.remote.selected()
        if len(chosen) != 1:
            return
        name, ok = QInputDialog.getText(self, "Переименовать", "Новое имя:", text=chosen[0]["n"])
        if ok and name.strip() and name.strip() != chosen[0]["n"]:
            self.ask({"t": "fs.rename", "path": chosen[0]["path"], "name": name.strip()}, self._after_change)

    def remote_delete(self):
        chosen = self.remote.selected()
        if not chosen:
            return
        if QMessageBox.question(self, "Удалить", f"Отправить в корзину удалённого ПК: {len(chosen)} шт.?") \
                != QMessageBox.Yes:
            return
        for e in chosen:
            self.ask({"t": "fs.delete", "path": e["path"]}, self._after_change)

    def _after_change(self, reply):
        if not reply.get("ok"):
            QMessageBox.warning(self, "Файлы", reply.get("msg", "не получилось"))
        self.open_remote(self.remote_dir)

    # ── moving things ───────────────────────────────────────────────
    def send(self, paths):
        if not self.remote_dir:
            return
        for p in paths:
            if os.path.isdir(p):
                self.files.upload_tree(p, self.remote_dir)
            elif os.path.isfile(p):
                self.files.upload(p, self.remote_dir)
        QTimer.singleShot(1500, lambda: self.open_remote(self.remote_dir))

    def fetch(self, entries):
        if not self.local_dir:
            return
        for e in entries:
            if e["d"]:
                self.files.download_tree(e["path"], self.local_dir, e["n"])
            else:
                self.files.download(e["path"], e.get("s", 0), e.get("m", 0), self.local_dir, e["n"])

    def send_selected(self):
        self.send([e["path"] for e in self.local.selected()])

    def fetch_selected(self):
        self.fetch(self.remote.selected())

    def _dropped_there(self, mime):
        self.send([u.toLocalFile() for u in mime.urls() if u.isLocalFile()])

    def _dropped_here(self, mime):
        try:
            entries = json.loads(bytes(mime.data(REMOTE_MIME)).decode())
        except ValueError:
            return
        self.fetch(entries)

    # ── transfers ───────────────────────────────────────────────────
    def refresh_transfers(self):
        items = self.files.snapshot()
        refresh_local = False
        for t in items:
            row = self._rows.get(t.x)
            if row is None:
                row = self.table.rowCount()
                self.table.insertRow(row)
                self.table.setItem(row, 0, QTableWidgetItem("→" if t.kind == "put" else "←"))
                self.table.setItem(row, 1, QTableWidgetItem(t.name))
                bar = QProgressBar()
                bar.setRange(0, 1000)
                bar.setTextVisible(True)
                self.table.setCellWidget(row, 2, bar)
                self.table.setItem(row, 3, QTableWidgetItem(""))
                self.table.setItem(row, 4, QTableWidgetItem(""))
                act = button("", "small")
                act.clicked.connect(lambda _=False, x=t.x: self._act(x))
                self.table.setCellWidget(row, 5, act)
                self._rows[t.x] = row
            bar = self.table.cellWidget(row, 2)
            bar.setValue(int(1000 * t.done / t.size) if t.size else (1000 if t.state == "done" else 0))
            bar.setFormat(f"{human_size(t.done)} из {human_size(t.size)}")
            running = t.state == "running"
            self.table.item(row, 3).setText(human_speed(t.speed.value()) if running else "")
            text = STATE_TEXT.get(t.state, t.state) + (f": {t.error}" if t.error else "")
            if t.state == "done" and t.result_name and t.result_name != t.name:
                text += f" → {t.result_name}"
            item = self.table.item(row, 4)
            if item.text() != text:
                item.setText(text)
                if t.state == "done" and t.kind == "get":
                    refresh_local = True
            act = self.table.cellWidget(row, 5)
            if t.state in ("done", "cancelled"):
                if act is not None:
                    self.table.removeCellWidget(row, 5)
            elif act is not None:
                act.setText({"interrupted": "Продолжить", "error": "Повторить"}.get(t.state, "Отмена"))
        if len(items) < len(self._rows):            # finished ones were cleared
            self._rows = {}
            self.table.setRowCount(0)
        if refresh_local and self.local_dir:
            self.open_local(self.local_dir)

    def _act(self, x):
        t = next((t for t in self.files.snapshot() if t.x == x), None)
        if t is None:
            return
        if t.state in ("interrupted", "error"):
            self.files.resume(x)
        else:
            self.files.cancel(x)

    def closeEvent(self, e):
        self.timer.stop()
        super().closeEvent(e)


def _row(*widgets):
    lay = QHBoxLayout()
    lay.setSpacing(6)
    for w in widgets:
        if w is None:
            lay.addStretch(1)
        else:
            lay.addWidget(w, 1 if isinstance(w, QLineEdit) else 0)
    holder = QWidget()
    holder.setLayout(lay)
    lay.setContentsMargins(0, 0, 0, 0)
    return holder


def _downloads():
    d = Path.home() / "Downloads"
    return d if d.is_dir() else Path.home()
