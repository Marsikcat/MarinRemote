"""
The clipboard: text goes both ways only with the right, nothing echoes back, nothing copied before
the session leaves, and a password manager's private copies stay here.
"""
import time

import pytest
from PySide6.QtCore import QMimeData
from PySide6.QtGui import QGuiApplication

from marinremote.config import Settings
from marinremote.host.clipboard import MAX_TEXT, ClipboardService
from marinremote.host.core import Approver, HostCore
from marinremote.journal import Journal
from marinremote.net import Node
from marinremote.ui.clipboard import QtClipboard
from marinremote.viewer.session import ViewerEvents, ViewerSession


class FakeBoard:
    def __init__(self):
        self.on_change = None
        self.set = []

    def set_text(self, text):
        self.set.append(text)


class Grant(Approver):
    def __init__(self, rights):
        self.rights = rights

    async def ask(self, request):
        return {"allow": True, "rights": self.rights, "remember": None, "block": False}


class Msgs(ViewerEvents):
    def __init__(self):
        self.msgs = []

    def message(self, s, msg):
        self.msgs.append(msg)


def until(cond, timeout=10):
    deadline = time.time() + timeout
    while not cond():
        assert time.time() < deadline, "condition never came true"
        time.sleep(0.02)


@pytest.fixture
def make(tmp_path):
    made = []

    def _make(rights):
        hs, vs = Settings(tmp_path / f"h{len(made)}"), Settings(tmp_path / f"v{len(made)}")
        host = Node(hs["secret_key"], port=0, mode="lan", bind_host="127.0.0.1", lan=False)
        viewer = Node(vs["secret_key"], port=0, mode="lan", bind_host="127.0.0.1", lan=False)
        host.start()
        viewer.start()
        journal = Journal(tmp_path / f"j{len(made)}.jsonl")
        core = HostCore(hs, host, journal, Grant(rights))
        board = FakeBoard()
        core.services.append(ClipboardService(core, board))
        msgs = Msgs()
        s = ViewerSession(viewer, {"id": host.id, "relay": None, "addrs": [f"127.0.0.1:{host.port}"]},
                          ["view", "clipboard"], "зритель", msgs)
        s.start()
        until(lambda: s.state == "connected")
        made.append((s, host, viewer))
        return s, board, msgs, journal

    yield _make
    for s, _, _ in made:
        s.stop()
    time.sleep(0.4)
    for _, host, viewer in made:
        viewer.stop()
        host.stop()


def test_text_goes_both_ways(make):
    s, board, msgs, _ = make(["view", "clipboard"])
    s.post({"t": "clip", "text": "скопировано там"})
    until(lambda: board.set == ["скопировано там"])
    board.on_change("скопировано здесь")
    until(lambda: {"t": "clip", "text": "скопировано здесь"} in msgs.msgs)


def test_too_much_or_not_text_is_ignored(make):
    s, board, msgs, _ = make(["view", "clipboard"])
    s.post({"t": "clip", "text": "x" * (MAX_TEXT + 1)})
    s.post({"t": "clip", "text": 42})
    s.post({"t": "clip", "text": "ok"})
    until(lambda: board.set == ["ok"])


def test_without_the_right_nothing_moves(make):
    s, board, msgs, journal = make(["view"])
    s.post({"t": "clip", "text": "secret"})
    board.on_change("host secret")
    time.sleep(0.6)
    assert board.set == [] and not [m for m in msgs.msgs if m["t"] == "clip"]
    assert any(e["event"] == "violation" and e["what"] == "clip" for e in journal.read())


# ── the local clipboard: no echo, changes only, private copies kept here ──
@pytest.fixture(scope="module")
def qapp():
    return QGuiApplication.instance() or QGuiApplication([])


def pump(qapp, seconds=0.2):
    end = time.time() + seconds
    while time.time() < end:
        qapp.processEvents()
        time.sleep(0.01)


def test_local_clipboard_no_echo_and_only_changes(qapp):
    QGuiApplication.clipboard().setText("было до сеанса")
    pump(qapp)
    board = QtClipboard()
    seen = []
    board.on_change = seen.append
    pump(qapp)
    assert seen == []                              # what was there before is not sent
    board.set_text("пришло с того ПК")
    pump(qapp)
    assert QGuiApplication.clipboard().text() == "пришло с того ПК" and seen == []   # not echoed back
    QGuiApplication.clipboard().setText("скопировал здесь")
    pump(qapp)
    assert seen == ["скопировал здесь"]


def test_password_managers_private_copies_stay_here(qapp):
    board = QtClipboard()
    seen = []
    board.on_change = seen.append
    md = QMimeData()
    md.setText("hunter2")
    md.setData('application/x-qt-windows-mime;value="ExcludeClipboardContentFromMonitorProcessing"', b"\0")
    QGuiApplication.clipboard().setMimeData(md)
    pump(qapp)
    md = QMimeData()
    md.setText("hunter3")
    md.setData('application/x-qt-windows-mime;value="CanIncludeInClipboardHistory"', b"\0\0\0\0")
    QGuiApplication.clipboard().setMimeData(md)
    pump(qapp)
    assert seen == []
    QGuiApplication.clipboard().setText("обычный текст")
    pump(qapp)
    assert seen == ["обычный текст"]
