"""The windows build offscreen, a question reaches its window and the answer gets back, and the
emergency hotkey fires once per press of exactly its combination."""
import time

import pytest
from PySide6.QtWidgets import QApplication

from marinremote.config import Settings
from marinremote.host.core import HostCore
from marinremote.journal import Journal
from marinremote.net import Node

REQUEST = {"id": 7, "key": "ab" * 32, "fp": "ABCD-EFGH", "name": "ноутбук", "want": ["view", "control"],
           "record": None, "remember": False, "password_given": False, "timeout": 30,
           "via_relay": False, "via": "127.0.0.1:1"}


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def pump(app, cond, timeout=5):
    deadline = time.time() + timeout
    while not cond():
        assert time.time() < deadline
        app.processEvents()
        time.sleep(0.01)


def test_main_window_and_the_question_round_trip(app, tmp_path):
    from marinremote.ui import theme
    from marinremote.ui.bridge import HostBridge, QtApprover
    from marinremote.ui.mainwindow import MainWindow
    theme.apply(app)
    s = Settings(tmp_path)
    node = Node(s["secret_key"], port=0, mode="lan", bind_host="127.0.0.1", lan=False)
    node.start()
    try:
        journal = Journal(tmp_path / "journal.jsonl")
        approver = QtApprover(node.net)
        bridge = HostBridge()
        core = HostCore(s, node, journal, approver, events=bridge)
        win = MainWindow(s, node, core, journal, approver, bridge)
        win.show()
        for i in range(4):
            win.nav.button(i).click()
        answer = node.net.submit(approver.ask(dict(REQUEST)))
        pump(app, lambda: 7 in win.dialogs)
        dlg = win.dialogs[7]
        dlg.boxes["control"].setChecked(False)
        dlg.remember.setChecked(True)
        dlg._allow()
        assert answer.result(5) == {"allow": True, "rights": ["view"], "block": False, "remember": "contact"}

        # withdrawn questions close their window unanswered
        second = node.net.submit(approver.ask(dict(REQUEST, id=8)))
        pump(app, lambda: 8 in win.dialogs)
        approver.cancel(8)
        pump(app, lambda: 8 not in win.dialogs)
        assert not second.done()
        second.cancel()

        # a journal entry shows up at the top of the table
        journal.write("kill_all", sessions=1)
        pump(app, lambda: win.table.item(0, 1) is not None and "разорваны" in win.table.item(0, 1).text())
        win.shutdown()
    finally:
        node.stop()


def test_kill_hotkey_needs_exactly_its_combination(app, tmp_path, monkeypatch):
    from marinremote import hotkeys
    down = set()
    monkeypatch.setattr(hotkeys, "_down", lambda vk: vk in down)
    hk = hotkeys.KillHotkey(Settings(tmp_path))
    fired = []
    hk.triggered.connect(lambda: fired.append(1))
    down |= {0x11, 0x10, 0x12, 0x7B}             # Ctrl+Shift+Alt+F12, held for a while
    hk._poll()
    hk._poll()
    assert fired == [1]
    down.clear()
    hk._poll()
    down |= {0x11, 0x10, 0x7B}                   # no Alt
    hk._poll()
    assert fired == [1]
