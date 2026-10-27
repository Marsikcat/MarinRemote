#!/usr/bin/env python3
"""
MarinRemote — remote access to a Windows PC (screen, mouse, keyboard, clipboard, files)
with no servers of its own: QUIC through iroh, dialled by the PC's public key.

    python app.py              open the app (this PC can be connected to; it can connect out)
    python app.py --autostart  launched by Windows at logon (starts in the tray)
    python app.py --connect <code or marinremote:// link>   connect to that PC (asks first)
    python app.py --encoder <json>   the screen encoder (started by the app itself)

MARINREMOTE_INSTANCE=1 (2, …) runs a test copy with its own ports and a data folder in %TEMP%.
"""

import os
import sys
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

# `--instance N` (what autostart of a test copy passes) means MARINREMOTE_INSTANCE=N; it has to be
# known before marinremote.config is imported
if "--instance" in sys.argv[1:-1]:
    os.environ["MARINREMOTE_INSTANCE"] = sys.argv[sys.argv.index("--instance") + 1]

REQUIRED = ("PySide6", "av", "numpy", "iroh", "nacl")


def _missing():
    import importlib.util
    return [m for m in REQUIRED if importlib.util.find_spec(m) is None]


def link_arg(args):
    """The code of `--connect <code>`, or a marinremote:// link passed as it is, or None."""
    if "--connect" in args[:-1]:
        return args[args.index("--connect") + 1]
    return next((a for a in args if a.lower().startswith("marinremote:")), None)


def _message(text, icon=0x10):
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, text, "MarinRemote", icon)
    except Exception:
        print(text)


def _setup_logging(folder):
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "app.log"
    try:
        if path.stat().st_size > 2 * 1024 * 1024:       # keep one old log, not years of them
            path.replace(folder / "app.old.log")
    except OSError:
        pass
    log = open(path, "a", encoding="utf-8", buffering=1)
    if sys.stdout is None or sys.stderr is None:   # pythonw has no console
        sys.stdout = sys.stderr = log
    log.write(f"\n[{time.strftime('%Y-%m-%d %H:%M:%S')}] start, pid {os.getpid()}\n")

    def hook(exc_type, exc, tb):
        log.write(f"\n[{time.strftime('%Y-%m-%d %H:%M:%S')}] "
                  + "".join(traceback.format_exception(exc_type, exc, tb)))
    sys.excepthook = hook


def main():
    missing = _missing()
    if missing:
        _message("Не установлены модули: " + ", ".join(missing) + "\n\nЗапустите setup.bat — он всё поставит.")
        return 1
    from marinremote.config import APP_NAME, DATA, INSTANCE, Settings
    _setup_logging(DATA)

    from PySide6.QtCore import Qt, QTimer
    from PySide6.QtGui import QGuiApplication
    from PySide6.QtNetwork import QLocalServer, QLocalSocket
    from PySide6.QtWidgets import QApplication

    args = sys.argv[1:]
    QGuiApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setQuitOnLastWindowClosed(False)

    # one copy per Windows user (and test instance): a second launch brings the window up — or
    # hands over the link it was opened with
    link = link_arg(args)
    key = f"{APP_NAME}-{os.environ.get('USERNAME', 'user')}-{INSTANCE}"
    sock = QLocalSocket()
    sock.connectToServer(key)
    if sock.waitForConnected(300):
        sock.write(b"connect " + link.encode() if link else b"show")
        sock.waitForBytesWritten(1000)
        sock.disconnectFromServer()
        return 0
    QLocalServer.removeServer(key)
    server = QLocalServer()
    server.listen(key)

    try:
        settings = Settings()
    except PermissionError as e:
        _message(f"Не получается прочитать настройки MarinRemote ({e}).\n\nПохоже, файл держит другая "
                 f"программа. Попробуйте ещё раз через минуту.")
        return 1
    if settings.identity_note == "unreadable":
        _message("Ключ этого устройства зашифрован для другой учётной записи Windows или другого ПК — "
                 "похоже, настройки скопировали сюда. Чужой ключ не используется: создан новый.\n\n"
                 "Код подключения этого ПК изменился. Устройства, которые помнили его, должны добавить "
                 "его заново.", 0x30)

    from marinremote.host.clipboard import ClipboardService
    from marinremote.host.core import HostCore
    from marinremote.host.files import FilesService
    from marinremote.host.input import InputService
    from marinremote.host.power import PowerService
    from marinremote.host.video import VideoService
    from marinremote.journal import Journal
    from marinremote.net import Node
    from marinremote.system import register_links
    from marinremote.ui import theme
    from marinremote.ui.bridge import HostBridge, QtApprover
    from marinremote.ui.clipboard import QtClipboard
    from marinremote.ui.mainwindow import MainWindow

    theme.apply(app)
    node = Node(settings.secret_key, mode=settings["network_mode"])
    try:
        node.start()
    except Exception as e:
        _message(f"Не удалось запустить сеть MarinRemote: {e}")
        return 1
    journal = Journal(settings.folder / "journal.jsonl")
    approver = QtApprover(node.net)
    bridge = HostBridge()
    core = HostCore(settings, node, journal, approver, events=bridge)
    video = VideoService(core)
    core.services += [video, InputService(core, video), FilesService(core), ClipboardService(core, QtClipboard()),
                      PowerService(core)]
    win = MainWindow(settings, node, core, journal, approver, bridge)

    def on_second_instance():
        conn = server.nextPendingConnection()
        if not conn:
            return
        got = bytearray()

        def read():
            got.extend(bytes(conn.readAll()))

        def done():
            read()
            text = got[:4096].decode("utf-8", "replace")
            if text.startswith("connect "):
                win.open_link(text[8:])
            else:
                win.show_window()
            conn.deleteLater()
        conn.readyRead.connect(read)
        conn.disconnected.connect(done)
    server.newConnection.connect(on_second_instance)

    def on_quit():
        win.shutdown()
        core.shutdown()
        node.stop()
    app.aboutToQuit.connect(on_quit)

    register_links()
    if "--autostart" not in args or not settings["start_minimized"]:
        win.show()
    if link:
        QTimer.singleShot(0, lambda: win.open_link(link))
    print(f"ready: {node.id} port {node.port} data {settings.folder}", flush=True)
    return app.exec()


if __name__ == "__main__":
    if len(sys.argv) > 2 and sys.argv[1] == "--encoder":
        # the screen encoder: a separate process of this same program, no window, no Qt
        from marinremote.host import encoder
        sys.exit(encoder.main(sys.argv[2]))
    sys.exit(main())
