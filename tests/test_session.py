"""
Login and rights end to end: two real iroh endpoints on 127.0.0.1 in this process, the host's
confirmation window replaced by a scripted answer.
"""
import asyncio
import json
import threading
import time

import nacl.pwhash
import pytest

from marinremote import protocol as P
from marinremote.auth import Limiter, hash_password
from marinremote.config import Settings
from marinremote.host.core import Approver, HostCore, Service
from marinremote.journal import Journal
from marinremote.net import LineReader, Node
from marinremote.viewer.session import ViewerEvents, ViewerSession

FAST = {"ops": nacl.pwhash.argon2id.OPSLIMIT_MIN, "mem": nacl.pwhash.argon2id.MEMLIMIT_MIN}
ALL = ["view", "control", "files", "clipboard"]


class ScriptedApprover(Approver):
    """Answers every question with `answer` (None: never answers)."""

    def __init__(self, answer=None):
        self.answer = answer
        self.asked = []
        self.cancelled = []

    async def ask(self, request):
        self.asked.append(request)
        if self.answer is None:
            await asyncio.sleep(3600)
        return self.answer

    def cancel(self, request_id):
        self.cancelled.append(request_id)


class Recorder(Service):
    def __init__(self):
        self.messages, self.streams, self.datagrams = [], [], []

    def on_message(self, s, msg):
        self.messages.append(msg)
        return True

    def on_datagram(self, s, data):
        self.datagrams.append(data)
        return True

    async def on_stream(self, s, header, rest, rs):
        self.streams.append(header)
        return True


class States(ViewerEvents):
    def __init__(self):
        self.seen = []
        self.msgs = []
        self.cv = threading.Condition()

    def message(self, session, msg):
        self.msgs.append(msg)

    def state_changed(self, session):
        with self.cv:
            self.seen.append((session.state, dict(session.detail)))
            self.cv.notify_all()

    def wait(self, session, state, timeout=15, after=0):
        """Until the session has been in `state` (counting from the `after`-th change)."""
        deadline = time.time() + timeout
        with self.cv:
            while not any(s == state for s, _ in self.seen[after:]):
                left = deadline - time.time()
                assert left > 0, f"never got to {state}: {self.seen}"
                self.cv.wait(left)
            return next(d for s, d in self.seen[after:] if s == state)


class Pair:
    def __init__(self, tmp_path, answer=None):
        self.hs = Settings(tmp_path / "host")
        self.vs = Settings(tmp_path / "viewer")
        self.host = Node(self.hs.secret_key, port=0, mode="lan", bind_host="127.0.0.1", lan=False)
        self.viewer = Node(self.vs.secret_key, port=0, mode="lan", bind_host="127.0.0.1", lan=False)
        self.host.start()
        self.viewer.start()
        self.journal = Journal(tmp_path / "host" / "journal.jsonl")
        self.approver = ScriptedApprover(answer)
        self.core = HostCore(self.hs, self.host, self.journal, self.approver,
                             limiter=Limiter(tmp_path / "host" / "auth_state.json"))
        self.svc = Recorder()
        self.core.services.append(self.svc)
        self.sessions = []

    @property
    def target(self):
        return {"id": self.host.id, "relay": None, "addrs": [f"127.0.0.1:{self.host.port}"]}

    def connect(self, want=ALL, password=None, token=None):
        events = States()
        s = ViewerSession(self.viewer, self.target, want, "ноутбук", events, password=password)
        s.token = token
        s.events_rec = events
        self.sessions.append(s)
        s.start()
        return s

    def events(self, name=None):
        out = self.journal.read()
        return [e for e in out if name is None or e["event"] == name]

    def on_loop(self, fn, *args):
        return self.host.net.submit(_call(fn, *args)).result(5)

    def close(self):
        for s in self.sessions:
            s.stop()
        time.sleep(0.4)
        self.viewer.stop()
        self.host.stop()


async def _call(fn, *args):
    return fn(*args)


def until(cond, timeout=10):
    deadline = time.time() + timeout
    while not cond():
        assert time.time() < deadline, "condition never came true"
        time.sleep(0.05)


@pytest.fixture
def pair(tmp_path):
    p = Pair(tmp_path)
    yield p
    p.close()


def approve(rights, remember=None):
    return {"allow": True, "rights": rights, "remember": remember, "block": False}


# ── confirmation ───────────────────────────────────────────────────
def test_approved_with_fewer_rights(pair):
    pair.approver.answer = approve(["view", "control"])
    s = pair.connect()
    s.events_rec.wait(s, "waiting")                       # the viewer is told to wait
    s.events_rec.wait(s, "connected")
    assert s.rights == ["view", "control"] and s.host["name"] == pair.hs["name"]
    req = pair.approver.asked[0]
    assert req["key"] == pair.viewer.id and req["want"] == ALL and req["name"] == "ноутбук"
    assert len(pair.core.sessions) == 1
    assert [e["event"] for e in pair.events()] == ["request", "approved", "session_start"]


def test_denied(pair):
    pair.approver.answer = {"allow": False}
    s = pair.connect()
    d = s.events_rec.wait(s, "closed")
    assert d["reason"] == "refused" and d["auth"] == "denied"
    assert not pair.core.sessions


def test_nobody_answers_in_time(pair):
    pair.core.PROMPT_TIMEOUT = 0.5
    s = pair.connect()
    d = s.events_rec.wait(s, "closed")
    assert d["auth"] == "timeout" and pair.approver.cancelled == [1]
    assert pair.events("denied")[-1]["reason"] == "timeout"


def test_viewer_leaving_withdraws_the_question(pair):
    s = pair.connect()
    s.events_rec.wait(s, "waiting")
    s.stop()
    until(lambda: pair.approver.cancelled == [1])
    until(lambda: any(e["reason"] == "left" for e in pair.events("denied")))


def test_block_from_the_window(pair):
    pair.approver.answer = {"allow": False, "block": True}
    s = pair.connect()
    assert s.events_rec.wait(s, "closed")["auth"] == "blocked"
    assert pair.viewer.id in pair.hs["blocked"]
    s2 = pair.connect()
    assert s2.events_rec.wait(s2, "closed")["auth"] == "blocked"
    assert len(pair.approver.asked) == 1                  # not asked about again


def test_off_means_off(pair):
    pair.hs["allow_incoming"] = False
    s = pair.connect()
    assert s.events_rec.wait(s, "closed")["auth"] == "off"
    assert not pair.approver.asked


# ── nothing before the login ───────────────────────────────────────
def test_nothing_is_taken_before_the_hello(pair):
    pair.approver.answer = approve(ALL)

    async def sneaky():
        conn = await pair.viewer.connect(pair.host.id, None, [f"127.0.0.1:{pair.host.port}"])
        # a one-way stream and a datagram before even saying hello
        uni = await conn.open_uni()
        await uni.write_all(P.stream_header("input", "guess") + b'{"m":"down"}\n')
        conn.send_datagram(bytes([1]) + b"\0" * 8)
        bi = await conn.open_bi()
        send = bi.send()
        await send.write_all(P.line({"t": "clip", "text": "before login"}))
        await send.write_all(P.line(P.make_hello("x", ALL, "test")))
        reader = LineReader(bi.recv())
        while True:
            msg = json.loads(await reader.next())
            if msg["t"] == "auth":
                break
        assert msg["ok"]
        await send.write_all(P.line({"t": "clip", "text": "after login"}))
        await asyncio.sleep(1.0)
        conn.close(0, b"bye")
        return msg

    pair.host.net.submit(sneaky()).result(20)
    until(lambda: not pair.core.sessions)
    assert pair.svc.messages == [{"t": "clip", "text": "after login"}]
    assert pair.svc.streams == [] and pair.svc.datagrams == []
    pre = pair.events("pre_auth")
    assert pre and pre[0]["what"] == "clip"


# ── rights are checked on this side ────────────────────────────────
def test_actions_beyond_the_rights_are_ignored_and_logged(pair):
    pair.approver.answer = approve(["view"])
    s = pair.connect()
    s.events_rec.wait(s, "connected")
    s.post({"t": "clip", "text": "not allowed"})
    s.post({"t": "fs.list", "id": 1, "path": "home"})
    s.post({"t": "monitor", "id": 0})
    until(lambda: pair.svc.messages)
    time.sleep(0.3)
    assert pair.svc.messages == [{"t": "monitor", "id": 0}]
    assert {e["what"] for e in pair.events("violation")} == {"clip", "fs.list"}


# ── without anyone at the PC ───────────────────────────────────────
def test_remembered_as_contact_goes_straight_in(pair):
    pair.approver.answer = approve(["view", "control"], remember="contact")
    s = pair.connect()
    s.events_rec.wait(s, "connected")
    assert pair.hs["trusted"][pair.viewer.id]["mode"] == "contact"
    assert s.remembered == "contact"
    s.stop()
    until(lambda: not pair.core.sessions)
    s2 = pair.connect()
    s2.events_rec.wait(s2, "connected")
    assert s2.rights == ["view", "control"]
    assert len(pair.approver.asked) == 1                  # nobody was asked the second time
    assert pair.events("session_start")[-1]["how"] == "contact"


def test_password_login(pair):
    pair.hs["password_hash"] = hash_password("very secret", **FAST)
    pair.hs["trusted"] = {pair.viewer.id: {"name": "ноут", "rights": ["view", "files"], "mode": "password"}}
    s = pair.connect(password="very secret")
    s.events_rec.wait(s, "connected")
    assert s.rights == ["view", "files"] and not pair.approver.asked
    assert s.password is None                             # not kept after use


def test_wrong_password_slows_down(pair):
    pair.hs["password_hash"] = hash_password("very secret", **FAST)
    pair.hs["trusted"] = {pair.viewer.id: {"name": "ноут", "rights": ["view"], "mode": "password"}}
    reasons = []
    for _ in range(4):
        s = pair.connect(password="wrong one")
        d = s.events_rec.wait(s, "closed")
        reasons.append((d["auth"], d["retry_after"]))
    assert reasons[0] == ("bad_password", None) and reasons[2] == ("bad_password", 2)
    assert reasons[3][0] == "locked"
    assert not pair.approver.asked
    s = pair.connect(password="very secret")              # even the right one waits now
    assert s.events_rec.wait(s, "closed")["auth"] == "locked"


def test_password_from_an_unknown_device_is_ignored(pair):
    pair.hs["password_hash"] = hash_password("very secret", **FAST)
    s = pair.connect(password="very secret")
    s.events_rec.wait(s, "waiting")                       # still needs a person to confirm
    assert pair.approver.asked and pair.approver.asked[0]["password_given"]


# ── coming back after a drop ───────────────────────────────────────
def test_reconnects_after_a_drop_without_asking_again(pair):
    pair.approver.answer = approve(["view", "control"])
    s = pair.connect()
    s.events_rec.wait(s, "connected")
    n = len(s.events_rec.seen)
    first_sid = s.sid
    # the network "drops": the connection dies without a goodbye
    pair.viewer.net.call(s._close_conn, s.conn, P.CLOSE_IDLE, "idle")
    s.events_rec.wait(s, "reconnecting", after=n)
    s.events_rec.wait(s, "connected", after=n)
    assert s.sid != first_sid and s.rights == ["view", "control"]
    assert len(pair.approver.asked) == 1
    assert pair.events("session_start")[-1]["how"] == "resume"


def test_leaving_on_purpose_voids_the_token(pair):
    pair.approver.answer = approve(["view"])
    s = pair.connect()
    s.events_rec.wait(s, "connected")
    token = s.token
    s.stop()
    until(lambda: not pair.core.sessions)
    assert pair.events("session_end")[-1]["reason"] == "left"
    pair.approver.answer = None
    s2 = pair.connect(token=token)
    s2.events_rec.wait(s2, "waiting")                     # back to asking
    assert len(pair.approver.asked) == 2


def test_kill_all(pair):
    pair.approver.answer = approve(["view"])
    s = pair.connect()
    s.events_rec.wait(s, "connected")
    token = s.token
    pair.core.kill_all()
    assert s.events_rec.wait(s, "closed")["reason"] == "killed"
    assert pair.events("kill_all") and not pair.core.tokens.items
    s2 = pair.connect(token=token)
    s2.events_rec.wait(s2, "waiting")


def test_disconnect_one_session(pair):
    pair.approver.answer = approve(["view"])
    s = pair.connect()
    s.events_rec.wait(s, "connected")
    pair.core.disconnect(s.sid)
    assert s.events_rec.wait(s, "closed")["reason"] == "kicked"
    assert pair.events("session_end")[-1]["reason"] == "kicked"


def test_power_needs_control_and_can_be_called_off(pair):
    from marinremote.host.power import DELAY, PowerService
    ran, told = [], []

    def run(action, comment):
        if action == "shutdown" and len(ran) > 4:
            raise OSError("Отказано в доступе.")
        ran.append((action, comment))
    pair.core.events.power_changed = told.append
    pair.core.services.insert(0, PowerService(pair.core, run=run))
    pair.approver.answer = approve(["view"])
    s = pair.connect()
    s.events_rec.wait(s, "connected")
    s.post({"t": "power", "action": "shutdown"})          # watching only: not a word reaches Windows
    until(lambda: any(e.get("what") == "power" for e in pair.events("violation")))
    assert ran == []
    s.stop()

    pair.approver.answer = approve(["view", "control"])
    s = pair.connect()
    s.events_rec.wait(s, "connected")
    msgs = s.events_rec.msgs
    s.post({"t": "power", "action": "restart"})
    until(lambda: {"t": "power.ok", "action": "restart", "delay": DELAY} in msgs)
    assert ran == [("restart", "MarinRemote: ноутбук")]
    assert told[-1]["state"] == "scheduled" and told[-1]["action"] == "restart"
    s.post({"t": "power", "action": "shutdown"})          # one at a time
    until(lambda: any(m["t"] == "power.error" for m in msgs))
    s.post({"t": "power", "action": "cancel"})
    until(lambda: {"t": "power.cancelled", "action": "restart", "by": "viewer"} in msgs)
    assert ran[-1] == ("cancel", "")
    s.post({"t": "power", "action": "shutdown"})
    until(lambda: ran[-1][0] == "shutdown")
    pair.core.cancel_power()                              # the person at the host says no
    until(lambda: {"t": "power.cancelled", "action": "shutdown", "by": "host"} in msgs)
    assert told[-1] == {"action": "shutdown", "state": "cancelled", "name": "ноутбук", "by": "host"}
    s.post({"t": "power", "action": "lock"})
    until(lambda: {"t": "power.ok", "action": "lock", "delay": 0} in msgs)
    s.post({"t": "power", "action": "shutdown"})          # Windows says no: the viewer hears why
    until(lambda: {"t": "power.error", "action": "shutdown", "msg": "Отказано в доступе."} in msgs)
    assert [(e["action"], e["result"]) for e in pair.events("power")] == [
        ("restart", "ok"), ("cancel", "ok"), ("shutdown", "ok"), ("cancel", "ok"), ("lock", "ok"),
        ("shutdown", "error")]


def test_pairing_shows_one_code_on_both_and_the_host_remembers(pair):
    import re
    from marinremote.viewer.pair import request_pair
    codes = []
    pair.approver.answer = {"allow": True, "rights": ["view", "control"], "remember": None, "block": False}
    result = pair.viewer.net.submit(request_pair(pair.viewer, pair.target, "ноутбук", codes.append)).result(20)
    assert result["ok"] and result["rights"] == ["view", "control"]
    asked = pair.approver.asked[-1]
    assert re.match(r"^\d{3} \d{3}$", codes[0]) and asked["pair"] == codes[0] == result["code"]
    assert asked["timeout"] == HostCore.PAIR_TIMEOUT and asked["want"] == list(P.RIGHTS)
    key = pair.viewer.id
    assert pair.hs["trusted"][key]["mode"] == "contact" and pair.hs["trusted"][key]["rights"] == ["view", "control"]
    contact = pair.hs["contacts"][key]
    assert contact["name"] == "ноутбук" and contact["paired"]
    assert [e["rights"] for e in pair.events("paired")] == [["view", "control"]]
    assert not pair.core.sessions                      # pairing is not a session
    s = pair.connect(want=["view", "control"])         # and from now on: straight in
    s.events_rec.wait(s, "connected")
    assert len(pair.approver.asked) == 1


def test_pairing_refused_leaves_nothing_behind(pair):
    from marinremote.viewer.pair import request_pair
    pair.approver.answer = {"allow": False}
    result = pair.viewer.net.submit(request_pair(pair.viewer, pair.target, "ноутбук")).result(20)
    assert result == {"ok": False, "reason": "denied", "retry_after": None}
    assert not (pair.hs["trusted"] or {}) and not (pair.hs["contacts"] or {})


def test_pairing_with_a_number_other_than_the_promised_one_is_cut_short(pair, monkeypatch):
    import hashlib
    from types import SimpleNamespace
    from marinremote.viewer import pair as viewer_pair
    # the dialler commits to one number and reveals another (what someone in between would need)
    monkeypatch.setattr(viewer_pair, "hashlib", SimpleNamespace(sha256=lambda b: hashlib.sha256(b"x" + b)))
    pair.approver.answer = {"allow": True, "rights": ["view"], "remember": None, "block": False}
    result = pair.viewer.net.submit(viewer_pair.request_pair(pair.viewer, pair.target, "ноутбук")).result(20)
    assert result["ok"] is False and result["reason"] == "protocol"
    assert pair.approver.asked == []
