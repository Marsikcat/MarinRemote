"""Password hash, failed-attempt limits, confirmation windows, resume tokens, the decision."""
import nacl.pwhash
import pytest

from marinremote.auth import (Limiter, PromptGate, ResumeTokens, decide, hash_password,
                              verify_password)
from marinremote.config import Settings
from marinremote.protocol import check_hello

FAST = {"ops": nacl.pwhash.argon2id.OPSLIMIT_MIN, "mem": nacl.pwhash.argon2id.MEMLIMIT_MIN}
KEY = "11" * 32
OTHER = "22" * 32


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


# ── password ───────────────────────────────────────────────────────
def test_password_hash_is_argon2id_with_its_own_salt():
    a, b = hash_password("correct horse", **FAST), hash_password("correct horse", **FAST)
    assert a.startswith("$argon2id$") and a != b            # a fresh salt every time
    assert verify_password(a, "correct horse") and verify_password(b, "correct horse")
    assert not verify_password(a, "correct horse!")
    assert not verify_password(a, "")
    assert not verify_password("", "correct horse")
    assert not verify_password("garbage", "correct horse")


def test_default_cost_is_moderate():
    stored = hash_password("long enough password")
    assert "m=262144" in stored and "t=3" in stored          # 256 MiB, 3 passes


def test_short_password_refused():
    with pytest.raises(ValueError):
        hash_password("short", **FAST)


# ── limiter ────────────────────────────────────────────────────────
def test_three_free_then_doubling(tmp_path):
    clock = Clock()
    lim = Limiter(tmp_path / "state.json", clock)
    assert lim.retry_after(KEY) == 0
    assert lim.failed(KEY) == 0 and lim.failed(KEY) == 0
    assert lim.failed(KEY) == 2                               # the third failure: wait 2 s
    clock.t += 2
    assert lim.retry_after(KEY) == 0
    assert lim.failed(KEY) == 4
    clock.t += 4
    assert lim.failed(KEY) == 8
    assert lim.retry_after(OTHER) == 0                        # other keys are not held up


def test_delay_is_capped(tmp_path):
    clock = Clock()
    lim = Limiter(tmp_path / "state.json", clock)
    lim.GLOBAL_LIMIT = 10_000
    for _ in range(30):
        clock.t += lim.retry_after(KEY)
        lim.failed(KEY)
    assert lim.retry_after(KEY) == Limiter.MAX_DELAY


def test_success_resets_the_key(tmp_path):
    clock = Clock()
    lim = Limiter(tmp_path / "state.json", clock)
    for _ in range(4):
        lim.failed(KEY)
    lim.succeeded(KEY)
    assert lim.retry_after(KEY) == 0
    assert lim.failed(KEY) == 0


def test_survives_a_restart(tmp_path):
    clock = Clock()
    lim = Limiter(tmp_path / "state.json", clock)
    for _ in range(5):
        lim.failed(KEY)
    wait = lim.retry_after(KEY)
    again = Limiter(tmp_path / "state.json", clock)
    assert wait > 0 and again.retry_after(KEY) == wait


def test_many_keys_trip_the_global_pause(tmp_path):
    clock = Clock()
    lim = Limiter(tmp_path / "state.json", clock)
    for i in range(Limiter.GLOBAL_LIMIT + 1):
        lim.failed(f"{i:064x}")
        clock.t += 1
    assert lim.retry_after(KEY) > Limiter.GLOBAL_PAUSE - 30   # even a key that never failed
    clock.t += Limiter.GLOBAL_PAUSE
    assert lim.retry_after(KEY) == 0


def test_a_quiet_day_forgets(tmp_path):
    clock = Clock()
    lim = Limiter(tmp_path / "state.json", clock)
    for _ in range(6):
        lim.failed(KEY)
    clock.t += Limiter.FORGET + 1
    assert lim.failed(KEY) == 0


# ── confirmation windows ───────────────────────────────────────────
def test_prompt_gate():
    clock = Clock()
    gate = PromptGate(clock)
    assert gate.check(KEY) is None
    gate.begin(KEY)
    assert gate.check(KEY) == ("busy", 0)                     # one window per key
    for i in range(2):
        gate.begin(f"{i:064x}")
    assert gate.check(OTHER) == ("busy", 0)                   # three at once at most
    gate.end(KEY, refused=True)
    assert gate.check(KEY)[0] == "cooldown"
    clock.t += PromptGate.COOLDOWN
    gate.end("0" * 64, refused=False)
    assert gate.check(KEY) is None


# ── resume tokens ──────────────────────────────────────────────────
def test_token_is_one_time_and_bound_to_its_key():
    tokens = ResumeTokens(Clock())
    t = tokens.issue(KEY, ["view"], "s1")
    assert tokens.use(OTHER, t) is None
    assert tokens.use(KEY, t)["rights"] == ["view"]
    assert tokens.use(KEY, t) is None


def test_token_lives_a_minute_after_the_drop():
    clock = Clock()
    tokens = ResumeTokens(clock)
    t = tokens.issue(KEY, ["view"], "s1")
    clock.t += 3600                       # the session is alive: still good
    tokens.session_lost("s1")
    clock.t += ResumeTokens.TTL - 1
    assert tokens.use(KEY, t) is not None
    t2 = tokens.issue(KEY, ["view"], "s2")
    tokens.session_lost("s2")
    clock.t += ResumeTokens.TTL + 1
    assert tokens.use(KEY, t2) is None


def test_revoking_tokens():
    tokens = ResumeTokens(Clock())
    a, b = tokens.issue(KEY, ["view"], "s1"), tokens.issue(OTHER, ["view"], "s2")
    tokens.revoke_session("s1")
    assert tokens.use(KEY, a) is None and tokens.use(OTHER, b) is not None
    c = tokens.issue(KEY, ["view"], "s3")
    tokens.revoke_all()
    assert tokens.use(KEY, c) is None


# ── the decision ───────────────────────────────────────────────────
def hello(**kw):
    msg = {"t": "hello", "app": "MarinRemote", "proto": 1, "name": "laptop",
           "want": ["view", "control", "files", "clipboard"], **kw}
    return check_hello(msg)


@pytest.fixture
def settings(tmp_path):
    return Settings(tmp_path)


def test_unknown_device_is_asked_about(settings):
    assert decide(settings, KEY, hello()) == ("ask", None)
    assert decide(settings, KEY, hello(password="whatever123"))[0] == "ask"   # no record: no password path


def test_blocked_and_off(settings):
    settings["blocked"] = {KEY: {"name": "x"}}
    assert decide(settings, KEY, hello()) == ("reject", "blocked")
    settings["blocked"] = {}
    settings["allow_incoming"] = False
    assert decide(settings, KEY, hello()) == ("reject", "off")


def test_contact_goes_straight_in_with_its_rights(settings):
    settings["trusted"] = {KEY: {"name": "ноутбук", "rights": ["view", "control"], "mode": "contact"}}
    assert decide(settings, KEY, hello()) == ("contact", ["view", "control"])
    assert decide(settings, KEY, hello(want=["view"])) == ("contact", ["view"])
    assert decide(settings, KEY, hello(want=["files"]))[0] == "ask"       # nothing in common
    settings["unattended"] = False
    assert decide(settings, KEY, hello())[0] == "ask"


def test_password_mode_needs_password_hash_and_password(settings):
    settings["trusted"] = {KEY: {"name": "x", "rights": ["view"], "mode": "password"}}
    assert decide(settings, KEY, hello(password="secret-pass"))[0] == "ask"   # no password set
    settings["password_hash"] = hash_password("secret-pass", **FAST)
    assert decide(settings, KEY, hello())[0] == "ask"                         # none given
    assert decide(settings, KEY, hello(password="secret-pass")) == ("password", ["view"])


def test_ask_mode_always_asks(settings):
    settings["trusted"] = {KEY: {"name": "x", "rights": ["view"], "mode": "ask"}}
    kind, record = decide(settings, KEY, hello())
    assert kind == "ask" and record["name"] == "x"


def test_hello_is_normalised():
    h = hello(name="  ноут\x00бук\n ", want=["files", "nonsense", "view"], password="", resume="t" * 500)
    assert h["name"] == "ноутбук" and h["want"] == ["view", "files"]
    assert h["password"] is None and h["resume"] is None
    with pytest.raises(ValueError, match="version"):
        check_hello({"t": "hello", "app": "MarinRemote", "proto": 99})
    with pytest.raises(ValueError, match="protocol"):
        check_hello({"t": "hello", "app": "MarinCall", "proto": 1})
