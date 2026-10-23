#!/usr/bin/env python3
"""
Pictures of the app's windows without touching the screen: everything is drawn offscreen
(QT_QPA_PLATFORM=offscreen) into PNG files, with a throwaway data folder.

    python tools\\screens.py [out_dir]
"""

import os
import sys
import tempfile
import time
from pathlib import Path

os.environ["QT_QPA_PLATFORM"] = "offscreen"
os.environ.setdefault("QT_QPA_FONTDIR", os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts"))
os.environ["MARINREMOTE_DATA"] = tempfile.mkdtemp(prefix="marinremote-screens-")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from PySide6.QtWidgets import QApplication  # noqa: E402

from marinremote.config import Settings  # noqa: E402
from marinremote.host.core import HostCore  # noqa: E402
from marinremote.journal import Journal  # noqa: E402
from marinremote.net import Node  # noqa: E402
from marinremote.ui import theme  # noqa: E402
from marinremote.ui.approve import ApproveDialog  # noqa: E402
from marinremote.ui.bridge import HostBridge, QtApprover  # noqa: E402
from marinremote.ui.mainwindow import MainWindow  # noqa: E402
from marinremote.ui.overlay import SessionPanel  # noqa: E402
from marinremote.ui.session_window import SessionWindow  # noqa: E402


def shot(widget, path):
    QApplication.processEvents()
    widget.grab().save(str(path))
    print(path)


def main(out):
    out.mkdir(parents=True, exist_ok=True)
    app = QApplication(sys.argv)
    theme.apply(app)
    s = Settings()
    s["contacts"] = {"cd" * 32: {"name": "Домашний ПК", "relay": None, "addrs": []}}
    s["trusted"] = {"ef" * 32: {"name": "Ноутбук", "rights": ["view", "control"], "mode": "contact"}}
    node = Node(s["secret_key"], port=0, mode="lan", bind_host="127.0.0.1", lan=False)
    node.start()
    journal = Journal(s.folder / "journal.jsonl")
    journal.write("request", "ef" * 32, name="Ноутбук", want=["view", "control"])
    journal.write("approved", "ef" * 32, name="Ноутбук", rights=["view", "control"])
    approver = QtApprover(node.net)
    bridge = HostBridge()
    core = HostCore(s, node, journal, approver, events=bridge)
    win = MainWindow(s, node, core, journal, approver, bridge)
    win.resize(1000, 760)
    win.show()
    sessions = [{"sid": "a1", "key": "ef" * 32, "fp": "7K3M-Q9XA", "name": "Ноутбук",
                 "rights": ["view", "control"], "started": time.time(), "via_relay": False}]
    win._on_sessions(sessions)
    for i, name in enumerate(("host", "connect", "access", "journal")):
        win.nav.button(i).click()
        shot(win, out / f"main-{name}.png")
    dlg = ApproveDialog({"id": 1, "key": "ab" * 32, "fp": "50B8-DJPM", "name": "Ноутбук Маши",
                         "want": ["view", "control", "files", "clipboard"], "record": None,
                         "remember": True, "password_given": False, "timeout": 30,
                         "via_relay": False, "via": "192.168.1.20:8895"})
    dlg.show()
    shot(dlg, out / "approve.png")
    panel = SessionPanel()
    panel.set_sessions(sessions)
    panel.show()
    shot(panel, out / "panel.png")

    viewer = SessionWindow(s, node, {"id": "cd" * 32, "relay": None, "addrs": [], "name": "Домашний ПК"},
                           ["view", "control"])
    viewer.show()
    sess = viewer.session
    sess.stop()
    time.sleep(0.3)
    for name, state, detail in (("waiting", "waiting", {"timeout": 30, "since": time.monotonic()}),
                                ("refused", "closed", {"reason": "refused", "auth": "locked", "retry_after": 16}),
                                ("connected", "connected", {})):
        sess.state, sess.detail = state, detail
        if state == "connected":
            sess.rights, sess.remembered, sess.host = ["view", "control"], "contact", {"name": "Домашний ПК"}
        viewer._on_state(sess)
        shot(viewer, out / f"session-{name}.png")
    win.indicators.close()
    node.stop()


if __name__ == "__main__":
    main(Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "build" / "screens")
