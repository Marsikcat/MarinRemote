"""The windows build offscreen, a question reaches its window and the answer gets back, autostart
is one setting whether switched in Settings or in the tray, and the emergency hotkey fires once
per press of exactly its combination."""
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


@pytest.fixture
def autostart(monkeypatch):
    """The Run key in the registry, faked: tests never touch the real one."""
    from marinremote.ui import mainwindow
    state = {"on": False, "writes": []}

    def set_autostart(on):
        state["on"] = on
        state["writes"].append(on)
    monkeypatch.setattr(mainwindow, "autostart_enabled", lambda: state["on"])
    monkeypatch.setattr(mainwindow, "set_autostart", set_autostart)
    return state


@pytest.fixture
def window(app, tmp_path, autostart):
    from marinremote.ui import theme
    from marinremote.ui.bridge import HostBridge, QtApprover
    from marinremote.ui.mainwindow import MainWindow
    theme.apply(app)
    s = Settings(tmp_path)
    node = Node(s.secret_key, port=0, mode="lan", bind_host="127.0.0.1", lan=False)
    node.start()
    journal = Journal(tmp_path / "journal.jsonl")
    approver = QtApprover(node.net)
    bridge = HostBridge()
    core = HostCore(s, node, journal, approver, events=bridge)
    win = MainWindow(s, node, core, journal, approver, bridge)
    win.show()
    yield win, node, approver, journal
    win.shutdown()
    node.stop()


def pump(app, cond, timeout=5):
    deadline = time.time() + timeout
    while not cond():
        assert time.time() < deadline
        app.processEvents()
        time.sleep(0.01)


def test_main_window_and_the_question_round_trip(app, window):
    win, node, approver, journal = window
    for b in win.nav.buttons():
        b.click()
    answer = node.net.submit(approver.ask(dict(REQUEST)))
    pump(app, lambda: 7 in win.dialogs)
    dlg = win.dialogs[7]
    dlg.boxes["control"].setChecked(False)
    dlg.remember.setChecked(True)
    dlg._allow()
    assert answer.result(5) == {"allow": True, "rights": ["view"], "block": False, "remember": "contact",
                                "unattended": False}

    # withdrawn questions close their window unanswered
    second = node.net.submit(approver.ask(dict(REQUEST, id=8)))
    pump(app, lambda: 8 in win.dialogs)
    approver.cancel(8)
    pump(app, lambda: 8 not in win.dialogs)
    assert not second.done()
    second.cancel()

    # a journal entry shows up at the top of the table
    journal.write("kill_all", sessions=1)
    table = win.journal_view.table
    pump(app, lambda: table.item(0, 1) is not None and "разорваны" in table.item(0, 1).text())


def test_autostart_in_settings_and_in_the_tray_is_one_setting(app, window, autostart):
    win = window[0]
    switch, action = win.autostart_row.switch, win.autostart_action
    assert not switch.isChecked() and not action.isChecked()
    action.trigger()                                   # ticked in the tray menu…
    assert autostart["on"] and switch.isChecked()      # …and the switch in Settings follows
    switch.click()                                     # off in Settings…
    assert not autostart["on"] and not action.isChecked()
    assert autostart["writes"] == [True, False]        # one write each, no ping-pong
    autostart["on"] = True                             # the installer set it meanwhile
    win.tray_menu.aboutToShow.emit()                   # opening the tray menu asks Windows again
    assert action.isChecked() and switch.isChecked()
    assert autostart["writes"] == [True, False]
    win.show_settings()
    assert win.stack.currentIndex() == win.SETTINGS_PAGE


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


def test_a_test_copys_autostart_names_its_instance(monkeypatch):
    from marinremote import system
    monkeypatch.setattr(system, "INSTANCE", 0)
    assert system._launch_command().endswith("--autostart")
    monkeypatch.setattr(system, "INSTANCE", 2)
    assert system._launch_command().endswith("--autostart --instance 2")


def test_take_control_back_button(app):
    from marinremote.ui.overlay import SessionPanel, control_button
    calls = []
    s = {"sid": "a1", "name": "ноут", "fp": "ABCD-EFGH", "rights": ["view", "control", "files"],
         "granted": ["view", "control", "files"], "started": time.time()}
    b = control_button(s, lambda sid, rights: calls.append((sid, rights)))
    assert b.text() == "Забрать управление"
    b.click()
    assert calls[-1] == ("a1", ["view", "files"])
    s["rights"] = ["view", "files"]
    b = control_button(s, lambda sid, rights: calls.append((sid, rights)))
    assert b.text() == "Вернуть управление"
    b.click()
    assert calls[-1] == ("a1", ["view", "files", "control"])
    assert control_button({**s, "granted": ["view", "files"]}, None) is None    # never had it
    panel = SessionPanel()
    panel.rights.connect(lambda sid, rights: calls.append((sid, rights)))
    panel.set_sessions([s])
    [btn] = [w for w in panel.findChildren(type(b)) if w.text() == "Вернуть управление"]
    btn.click()
    assert calls[-1] == ("a1", ["view", "files", "control"])
    panel.close()


def test_pairing_from_both_windows(app, window, tmp_path, autostart):
    """«Связать» on one PC, the code in both windows, «Связать» on both: each keeps the other."""
    from marinremote.ui.bridge import HostBridge, QtApprover
    from marinremote.ui.mainwindow import MainWindow
    from marinremote.ui.pair import PairDialog
    win_b, node_b, approver_b, _ = window
    s = Settings(tmp_path / "a")
    s.set("name", "ноутбук")
    node_a = Node(s.secret_key, port=0, mode="lan", bind_host="127.0.0.1", lan=False)
    node_a.start()
    bridge = HostBridge()
    approver = QtApprover(node_a.net)
    core_a = HostCore(s, node_a, Journal(tmp_path / "a" / "journal.jsonl"), approver, events=bridge)
    win_a = MainWindow(s, node_a, core_a, core_a.journal, approver, bridge)
    try:
        dlg = PairDialog(win_a, s, node_a, core_a, {"id": node_b.id, "relay": None, "name": "стол",
                                                     "addrs": [f"127.0.0.1:{node_b.port}"]})
        dlg.show()
        pump(app, lambda: win_b.dialogs and dlg.ok_btn.isEnabled())
        [there] = win_b.dialogs.values()
        assert there.pair == dlg.code.text() and "связаться" in there.windowTitle() + there.findChildren(
            type(dlg.code))[0].text()
        dlg.boxes["files"].setChecked(False)
        dlg._confirm()                                   # the codes match, here…
        there.boxes["clipboard"].setChecked(False)
        there._allow()                                   # …and there
        pump(app, lambda: "Готово" in dlg.status.text(), timeout=10)
        b = win_b.s
        assert b["trusted"][node_a.id]["rights"] == ["view", "control", "files"]
        assert b["contacts"][node_a.id]["name"] == "ноутбук"
        assert s["trusted"][node_b.id] == {**s["trusted"][node_b.id], "mode": "contact",
                                           "rights": ["view", "control", "clipboard"]}
        assert s["contacts"][node_b.id]["paired"]
        dlg.close()
    finally:
        win_a.shutdown()
        node_a.stop()


def test_settings_tell_about_a_new_version(app, window):
    win, *_ = window
    info = {"current": "0.2.0", "latest": "0.3.0", "available": True, "url": "https://example/tag",
            "title": "MarinRemote 0.3.0", "notes": "", "date": "", "download": "", "signature": ""}
    win._show_update(info, quiet=False)
    assert "0.3.0" in win.update_label.text() and not win.update_btn.isHidden()
    assert win.update_btn.text() == "Открыть страницу выпуска"         # not an installed copy
    win._show_update(dict(info, available=False, latest="0.2.0"), quiet=False)
    assert "последняя" in win.update_label.text() and win.update_btn.isHidden()
    win.drives_row.switch.setChecked(True)                             # «Все диски этого ПК»
    assert win.s["files_all_drives"] is True
    win.drives_row.switch.setChecked(False)
    assert [e["action"] for e in win.journal.read() if e["event"] == "roots"] == ["all_drives_on", "all_drives_off"]


def test_files_only_question_says_so(app):
    from PySide6.QtWidgets import QLabel
    from marinremote.ui.approve import ApproveDialog
    dlg = ApproveDialog(dict(REQUEST, want=["files"]))
    texts = " ".join(lb.text() for lb in dlg.findChildren(QLabel))
    assert "хочет обменяться файлами" in texts and "экран этого ПК он не увидит" in texts
    assert [r for r, cb in dlg.boxes.items() if cb.isEnabled()] == ["files"]
    dlg.withdraw()
    full = ApproveDialog(dict(REQUEST))
    assert "хочет подключиться" in " ".join(lb.text() for lb in full.findChildren(QLabel))
    full.withdraw()


def test_files_only_from_a_contact(app, window, tmp_path):
    """«Файлы» at a contact: one right asked for, the panels straight away; when the other PC ends it
    the panels stay with a strip saying so, and «Подключиться снова» brings its folders back."""
    from PySide6.QtWidgets import QPushButton
    from marinremote.host.core import Approver
    from marinremote.host.files import FilesService

    class Grant(Approver):
        def __init__(self):
            self.asked = []

        async def ask(self, request):
            self.asked.append(request)
            return {"allow": True, "rights": list(request["want"]), "remember": None, "block": False}

    win, node, _, _ = window
    hs = Settings(tmp_path / "host")
    shared = tmp_path / "shared"
    shared.mkdir()
    (shared / "a.txt").write_text("привет", encoding="utf-8")
    hs["roots"] = [{"id": "home", "path": str(shared), "label": "Общая"}]
    host = Node(hs.secret_key, port=0, mode="lan", bind_host="127.0.0.1", lan=False)
    host.start()
    grant = Grant()
    core = HostCore(hs, host, Journal(tmp_path / "host.jsonl"), grant)
    core.services.append(FilesService(core))
    try:
        win.s.put("contacts", host.id, {"name": "хост", "relay": None, "addrs": [f"127.0.0.1:{host.port}"]})
        win._refresh_contacts()
        [files] = [b for b in win.findChildren(QPushButton) if b.text() == "Файлы"]
        files.click()
        fw = win.file_windows[host.id]
        pump(app, lambda: fw.manager is not None and fw.manager.remote.rowCount() == 1, timeout=15)
        assert grant.asked[0]["want"] == ["files"]
        [s] = core.sessions.values()
        assert s.rights == ["files"] and fw.manager.remote.entries[0]["n"] == "a.txt"
        assert host.id not in win.viewers

        core.disconnect(s.sid)
        pump(app, lambda: not fw.retry_btn.isHidden())         # the window has heard it is over
        assert fw.stack.currentWidget() is fw.manager and not fw.warn.isHidden()
        assert "завершил" in fw.warn.text.text()
        fw.manager.root_box.clear()
        fw.retry_btn.click()
        pump(app, lambda: fw.session.state == "connected" and fw.manager.root_box.count() == 1, timeout=15)
        assert fw.warn.isHidden() and len(grant.asked) == 2

        win.open_files({"id": host.id, "relay": None})          # again: the same window
        assert list(win.file_windows.values()) == [fw]
        fw.close()
        pump(app, lambda: not win.file_windows)
    finally:
        host.stop()


def test_file_manager_says_where_other_folders_are_opened(app):
    from concurrent.futures import Future
    from marinremote.ui.filemanager import FileManager

    class Files:
        def __init__(self, all_drives):
            self.all_drives = all_drives

        def request(self, msg):
            f = Future()
            f.set_result({"ok": True, "roots": [{"id": "home", "label": "Папка пользователя"}],
                          "all_drives": self.all_drives} if msg["t"] == "fs.roots" else {"ok": True, "entries": []})
            return f

        def snapshot(self):
            return []

        def clear_finished(self):
            pass

    for ver, all_drives, says in (("0.1.0", None, "обновите его до 0.2.0"), ("0.2.0", False, "заблокированы"),
                                  ("0.2.0", True, None)):
        fm = FileManager(Files(all_drives), "DESKTOP-NFT63EP", ver)
        pump(app, lambda fm=fm: fm.root_box.count() == 1)
        if says is None:
            assert fm.remote_hint.isHidden()
        else:
            assert not fm.remote_hint.isHidden() and "Все диски этого ПК" in fm.remote_hint.text()
            assert says in fm.remote_hint.text()
        fm.close()
