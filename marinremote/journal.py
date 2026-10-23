"""
The session journal of this PC: who asked to connect, what was decided, sessions with their
rights, refused actions and every file that went either way. One JSON object per line;
journal.jsonl moves aside to journal.1.jsonl past LIMIT, so there are two files at most.
"""

import json
import threading
import time

from .codes import fingerprint

LIMIT = 5 * 1024 * 1024

EVENTS = {
    "request": "Запрос на подключение",
    "approved": "Подключение разрешено",
    "denied": "Подключение отклонено",
    "auth_failed": "Вход не удался",
    "session_start": "Сеанс начался",
    "session_end": "Сеанс закончился",
    "violation": "Действие без права — проигнорировано",
    "pre_auth": "Данные до входа — проигнорированы",
    "kill_all": "Все сеансы разорваны",
    "trusted": "Устройство запомнено",
    "untrusted": "Устройство забыто",
    "blocked": "Устройство заблокировано",
    "unblocked": "Устройство разблокировано",
    "password": "Пароль изменён",
    "rights": "Права сеанса изменены",
    "file_get": "Файл скачан с этого ПК",
    "file_put": "Файл загружен на этот ПК",
    "fs": "Действие с файлами",
}


class Journal:
    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()
        self.listeners = []         # callback(entry) — called on the writing thread

    def write(self, event, key=None, **fields):
        entry = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "event": event}
        if key:
            entry["key"] = key
            entry["fp"] = fingerprint(key)
        entry.update({k: v for k, v in fields.items() if v is not None})
        data = (json.dumps(entry, ensure_ascii=False) + "\n").encode("utf-8")
        with self.lock:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                if self.path.exists() and self.path.stat().st_size + len(data) > LIMIT:
                    self.path.replace(self.path.with_name("journal.1.jsonl"))
                with open(self.path, "ab") as f:
                    f.write(data)
            except OSError as e:
                print(f"journal: {e}", flush=True)
        for cb in list(self.listeners):
            try:
                cb(entry)
            except Exception:
                pass
        return entry

    def read(self, limit=1000):
        """The newest `limit` entries, oldest first."""
        lines = []
        with self.lock:
            for path in (self.path.with_name("journal.1.jsonl"), self.path):
                try:
                    lines += path.read_text(encoding="utf-8").splitlines()
                except OSError:
                    pass
        out = []
        for raw in lines[-limit:]:
            try:
                out.append(json.loads(raw))
            except ValueError:
                pass
        return out
