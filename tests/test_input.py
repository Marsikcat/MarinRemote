"""
Mouse and keyboard: coordinates land on the exact pixel of the right monitor (one of them left
of the primary, at negative X), events keep their order across the datagram and the stream,
whatever is held is let go, nothing reaches MarinRemote's own windows, and end to end over iroh
the host plays what the viewer sent — only with the right to control.
"""
import threading
import time
from types import SimpleNamespace

import pytest

from marinremote import protocol as P
from marinremote.config import Settings
from marinremote.host.core import Approver, HostCore
from marinremote.host.inject import (EXTENDEDKEY, InputState, from_absolute, to_absolute,
                                     to_pixel)
from marinremote.host.input import InputService
from marinremote.journal import Journal
from marinremote.net import Node
from marinremote.viewer.input import InputSender, qt_key
from marinremote.viewer.kbhook import KBDLLHOOKSTRUCT, translate
from marinremote.viewer.session import ViewerEvents, ViewerSession

PRIMARY = {"id": 0, "x": 0, "y": 0, "w": 1920, "h": 1080, "primary": True}
LEFT = {"id": 1, "x": -1920, "y": 0, "w": 1920, "h": 1080, "primary": False}
VIRTUAL = (-1920, 0, 3840, 1080)


class FakeInjector:
    def __init__(self):
        self.log = []
        self.ours_point = None             # (px, py) where "our window" is
        self.ours_front = False
        self.lock = threading.Lock()

    def mouse(self, px, py, flags=0, data=0, move=True):
        with self.lock:
            self.log.append(("mouse", px, py, flags, data, move))

    def key(self, scan, ext, down, vk):
        with self.lock:
            self.log.append(("key", scan, ext, down, vk))

    def ours_at(self, px, py):
        return self.ours_point == (px, py)

    def ours_in_front(self):
        return self.ours_front


# ── coordinates ────────────────────────────────────────────────────
def test_every_pixel_of_both_monitors_is_reachable_exactly():
    for px in range(-1920, 1920):
        ax, _ = to_absolute(px, 0, VIRTUAL)
        assert from_absolute(ax, 0, VIRTUAL)[0] == px
    for py in range(0, 1080):
        _, ay = to_absolute(0, py, VIRTUAL)
        assert from_absolute(0, ay, VIRTUAL)[1] == py


def test_normalised_corners_land_on_the_monitor_corners():
    assert to_pixel(LEFT, 0, 0) == (-1920, 0)
    assert to_pixel(LEFT, 65535, 65535) == (-1, 1079)
    assert to_pixel(PRIMARY, 0, 0) == (0, 0)
    assert to_pixel(PRIMARY, 65535, 65535) == (1919, 1079)
    assert to_pixel(PRIMARY, 32768, 32768) == (960, 540)
    assert to_pixel(PRIMARY, -5, 99999) == (0, 1079)                  # clamped


# ── order, held keys, our own windows ──────────────────────────────
@pytest.fixture
def state():
    inj = FakeInjector()
    blocked = []
    st = InputState(inj, lambda: PRIMARY, blocked.append)
    return st, inj, blocked


def test_a_late_move_never_undoes_a_click(state):
    st, inj, _ = state
    st.move(5, 0, 0)
    st.event({"m": "down", "b": "left", "x": 65535, "y": 65535, "s": 7})
    st.move(6, 100, 100)                       # sent before the click, came after it
    st.event({"m": "up", "b": "left", "x": 65535, "y": 65535, "s": 8})
    st.move(9, 32768, 32768)
    moves = [(e[1], e[2]) for e in inj.log if e[0] == "mouse" and e[3] == 0]
    assert moves == [(0, 0), (960, 540)]
    clicks = [e for e in inj.log if e[0] == "mouse" and e[3]]
    assert [(e[1], e[2], e[3]) for e in clicks] == [(1919, 1079, 0x0002), (1919, 1079, 0x0004)]


def test_only_what_was_pressed_is_let_go_and_everything_is_at_the_end(state):
    st, inj, _ = state
    st.event({"m": "up", "b": "right", "x": 1, "y": 1, "s": 1})       # never pressed: nothing
    st.event({"k": 0x1E, "e": 0, "d": 0, "vk": 0x41})                 # never pressed: nothing
    assert inj.log == []
    st.event({"k": 0x1D, "e": 1, "d": 1, "vk": 0xA3})                 # right Ctrl
    st.event({"k": 0x1E, "e": 0, "d": 1, "vk": 0x41})
    st.event({"m": "down", "b": "middle", "x": 10, "y": 10, "s": 2})
    inj.log.clear()
    st.release_all()
    keys = sorted((e[1], e[2], e[3]) for e in inj.log if e[0] == "key")
    assert keys == [(0x1D, True, False), (0x1E, False, False)]
    assert [e[3] for e in inj.log if e[0] == "mouse"] == [0x0040]       # middle up
    inj.log.clear()
    st.release_all()
    assert inj.log == []


def test_keys_by_scan_code_and_the_odd_ones_by_vk(state):
    st, inj, _ = state
    st.event({"k": 0x45, "e": 1, "d": 1, "vk": 0x90})                 # Num Lock
    st.event({"k": 0x48, "e": 1, "d": 1, "vk": 0x26})                 # Up arrow
    assert inj.log == [("key", 0x45, True, True, 0x90), ("key", 0x48, True, True, 0x26)]


def test_the_wheel_and_side_buttons(state):
    st, inj, _ = state
    st.event({"m": "wheel", "dx": 0, "dy": -240, "x": 0, "y": 0, "s": 1})
    st.event({"m": "wheel", "dx": 120, "dy": 0, "s": 2})
    st.event({"m": "down", "b": "x2", "x": 0, "y": 0, "s": 3})
    assert [(e[3], e[4], e[5]) for e in inj.log] == [(0x0800, -240, True), (0x1000, 120, False),
                                                     (0x0080, 2, True)]


def test_nothing_reaches_our_own_windows(state):
    st, inj, blocked = state
    inj.ours_point = (960, 540)
    st.event({"m": "down", "b": "left", "x": 32768, "y": 32768, "s": 1})
    st.event({"m": "up", "b": "left", "x": 32768, "y": 32768, "s": 2})
    assert [e[3] for e in inj.log] == [0]       # the pointer went there; no click, no release
    inj.ours_front = True
    st.event({"k": 0x1E, "e": 0, "d": 1, "vk": 0x41})
    st.event({"k": 0x1E, "e": 0, "d": 0, "vk": 0x41})
    assert not [e for e in inj.log if e[0] == "key"]
    assert blocked == ["click", "key"]


@pytest.mark.parametrize("junk", [None, 5, "x", {}, {"m": "down"}, {"m": "down", "b": "evil"},
                                  {"k": "a", "d": 1}, {"k": 999, "d": 1}, {"k": 0, "vk": 0, "d": 1},
                                  {"m": "wheel", "dy": 10 ** 9}])
def test_junk_does_nothing(state, junk):
    st, inj, _ = state
    st.event(junk)
    assert inj.log == []


def test_a_click_with_broken_coordinates_clicks_where_the_pointer_is(state):
    st, inj, _ = state
    st.event({"m": "down", "b": "left", "x": "1", "y": 2})
    assert inj.log == [("mouse", 0, 0, 0x0002, 0, False)]


# ── the viewer's side: keys from Qt and from the hook ──────────────
def test_qt_keys_keep_the_extended_bit():
    ev = SimpleNamespace(nativeScanCode=lambda: 0x11D, nativeVirtualKey=lambda: 0xA3)
    assert qt_key(ev) == (0x1D, 1, 0xA3)
    assert qt_key(SimpleNamespace(nativeScanCode=lambda: 0, nativeVirtualKey=lambda: 0)) is None


def test_hook_skips_injected_keys_and_altgrs_fake_ctrl():
    kb = KBDLLHOOKSTRUCT(vkCode=0x5B, scanCode=0x5B, flags=0x01)
    assert translate(0x100, kb) == (0x5B, 1, True, 0x5B)              # Win down
    assert translate(0x105, kb) == (0x5B, 1, False, 0x5B)             # …and up (as a system key)
    assert translate(0x100, KBDLLHOOKSTRUCT(vkCode=0x41, scanCode=0x1E, flags=0x10)) is None
    assert translate(0x100, KBDLLHOOKSTRUCT(vkCode=0xA2, scanCode=0x21D, flags=0)) is None
    assert EXTENDEDKEY == 1


# ── end to end ─────────────────────────────────────────────────────
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


def setup(tmp_path, rights, probe=lambda: (None, "")):
    hs, vs = Settings(tmp_path / "h"), Settings(tmp_path / "v")
    host = Node(hs.secret_key, port=0, mode="lan", bind_host="127.0.0.1", lan=False)
    viewer = Node(vs.secret_key, port=0, mode="lan", bind_host="127.0.0.1", lan=False)
    host.start()
    viewer.start()
    journal = Journal(tmp_path / "j.jsonl")
    core = HostCore(hs, host, journal, Grant(rights))
    inj = FakeInjector()
    core.services.append(InputService(core, injector=inj, monitors=lambda: [LEFT, PRIMARY], probe=probe))
    msgs = Msgs()
    s = ViewerSession(viewer, {"id": host.id, "relay": None, "addrs": [f"127.0.0.1:{host.port}"]},
                      ["view", "control"], "зритель", msgs)
    sender = InputSender(s)
    s.start()
    until(lambda: s.state == "connected")

    def close():
        s.stop()
        time.sleep(0.4)
        viewer.stop()
        host.stop()
    return s, sender, inj, journal, msgs, core, close


def test_the_host_plays_what_the_viewer_sends(tmp_path):
    s, sender, inj, journal, msgs, core, close = setup(tmp_path, ["view", "control"])
    try:
        sender.move(32768, 32768)
        until(lambda: inj.log)
        sender.button("left", True, 0, 0)
        sender.button("left", False, 0, 0)
        sender.key(0x1E, 0, True, 0x41)
        sender.key(0x1E, 0, False, 0x41)
        sender.key(0x2A, 0, True, 0xA0)        # Shift held when the session goes away…
        until(lambda: len(inj.log) >= 6)
        assert inj.log[0] == ("mouse", 960, 540, 0, 0, True)          # the primary monitor
        assert [e[3] for e in inj.log[1:3]] == [0x0002, 0x0004]
        assert inj.log[3:6] == [("key", 0x1E, False, True, 0x41), ("key", 0x1E, False, False, 0x41),
                                ("key", 0x2A, False, True, 0xA0)]
        s.net.call(s._close_conn, s.conn, P.CLOSE_IDLE, "gone")    # …the network drops
        until(lambda: ("key", 0x2A, False, False, 0xA0) in inj.log)   # …and the host lets it go
    finally:
        close()


def test_without_the_right_nothing_is_played(tmp_path):
    s, sender, inj, journal, msgs, core, close = setup(tmp_path, ["view"])
    try:
        sender.move(100, 100)
        sender.key(0x1E, 0, True, 0x41)
        time.sleep(0.6)
        assert inj.log == []
        until(lambda: {e.get("what") for e in journal.read() if e["event"] == "violation"}
              >= {"datagram 1", "stream input"})
    finally:
        close()


def test_the_viewer_is_told_what_input_cant_reach(tmp_path):
    state = {"code": ("uipi", "Taskmgr.exe")}
    s, sender, inj, journal, msgs, core, close = setup(tmp_path, ["view", "control"],
                                                        probe=lambda: state["code"])
    try:
        until(lambda: any(m["t"] == "warn" and m["code"] == "uipi" and m["app"] == "Taskmgr.exe" for m in msgs.msgs))
        state["code"] = (None, "")
        until(lambda: msgs.msgs[-1] == {"t": "warn", "code": None, "app": ""})
    finally:
        close()


def test_release_all_from_the_viewer(tmp_path):
    s, sender, inj, journal, msgs, core, close = setup(tmp_path, ["view", "control"])
    try:
        sender.key(0x38, 0, True, 0x12)        # Alt held when the window lost focus
        sender.button("right", True, 10, 10)
        until(lambda: len(inj.log) == 2)
        sender.release_all()
        until(lambda: len(inj.log) == 4)
        ups = inj.log[2:]
        assert ("key", 0x38, False, False, 0x12) in ups
        assert any(e[0] == "mouse" and e[3] == 0x0010 for e in ups)   # right button up
        sender.release_all()                   # nothing held any more: nothing sent
        time.sleep(0.3)
        assert len(inj.log) == 4
    finally:
        close()


def test_input_stream_reopens_after_a_reconnect(tmp_path):
    s, sender, inj, journal, msgs, core, close = setup(tmp_path, ["view", "control"])
    try:
        sid = s.sid
        s.net.call(s._close_conn, s.conn, P.CLOSE_IDLE, "gone")
        until(lambda: s.state == "connected" and s.sid != sid)
        sender.key(0x1E, 0, True, 0x41)
        until(lambda: ("key", 0x1E, False, True, 0x41) in inj.log)
    finally:
        close()


def test_control_taken_back_and_given_back(tmp_path):
    s, sender, inj, journal, msgs, core, close = setup(tmp_path, ["view", "control"])
    try:
        sender.key(0x2A, 0, True, 0xA0)                    # Shift held when the host takes control back…
        until(lambda: len(inj.log) == 1)
        core.set_rights(s.sid, ["view"])
        until(lambda: s.rights == ["view"])
        until(lambda: ("key", 0x2A, False, False, 0xA0) in inj.log)     # …is let go
        n = len(inj.log)
        sender.move(100, 100)                              # sent before the viewer's window caught up:
        sender.key(0x1E, 0, True, 0x41)                    # dropped without a word
        time.sleep(0.6)
        assert len(inj.log) == n
        sid = s.sid                                        # a drop keeps what may be given back
        s.net.call(s._close_conn, s.conn, P.CLOSE_IDLE, "gone")
        until(lambda: s.state == "connected" and s.sid != sid)
        assert s.rights == ["view"]
        core.set_rights(s.sid, ["view", "control", "files"])          # never more than it got in with
        until(lambda: s.rights == ["view", "control"])
        sender.key(0x1F, 0, True, 0x53)
        until(lambda: ("key", 0x1F, False, True, 0x53) in inj.log)
        events = journal.read()
        assert not [e for e in events if e["event"] == "violation"]
        assert [e["rights"] for e in events if e["event"] == "rights"] == [["view"], ["view", "control"]]
        assert any(m["t"] == "rights" for m in msgs.msgs)
    finally:
        close()
