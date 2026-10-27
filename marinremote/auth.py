"""
Who gets into this PC.

- Identity is settled by QUIC: the connection proves the viewer's ed25519 key. What's left is
  whether that key may come in, and with which rights.
- Remembered devices ("trusted") carry a mode chosen by the person at this PC:
  contact (straight in), password (in with the password, nobody needs to be here) or ask.
  Everyone else is asked about in a window on this PC.
- The password is stored as an argon2id string (salt and cost inside) and is worth nothing
  for a key that isn't remembered — guessing it from fresh keys gets nowhere.
- Failed passwords slow down per key and, past a limit, for everyone; the counters survive a
  restart. Confirmation windows are rationed so nobody can bury the screen in them.
- After a network drop the viewer may come back within a minute with a one-time token bound
  to its key, without a new confirmation. Disconnecting it on purpose voids the token.
"""

import hashlib
import json
import secrets
import time

import nacl.exceptions
import nacl.pwhash

from .config import write_json
from .protocol import clean_rights

OPS = nacl.pwhash.argon2id.OPSLIMIT_MODERATE      # 3 passes
MEM = nacl.pwhash.argon2id.MEMLIMIT_MODERATE      # 256 MiB: costly to guess on any hardware
MIN_PASSWORD = 8

MODES = {
    "contact": "Сразу, без подтверждения",
    "password": "По паролю",
    "ask": "Спрашивать каждый раз",
}


# ── password ───────────────────────────────────────────────────────
def hash_password(password, ops=OPS, mem=MEM):
    if len(password) < MIN_PASSWORD:
        raise ValueError(f"пароль короче {MIN_PASSWORD} символов")
    return nacl.pwhash.argon2id.str(password.encode("utf-8"), opslimit=ops, memlimit=mem).decode("ascii")


def verify_password(stored, password):
    if not stored or not password:
        return False
    try:
        return nacl.pwhash.verify(stored.encode("ascii"), password.encode("utf-8"))
    except (nacl.exceptions.CryptoError, ValueError, TypeError, UnicodeError):
        return False


# ── failed attempts ────────────────────────────────────────────────
class Limiter:
    """Failed logins: per key the first FREE are free, then each waits twice as long
    (2, 4, 8 … s, at most MAX_DELAY); more than GLOBAL_LIMIT failures in GLOBAL_WINDOW
    from anyone pauses logins for everyone for GLOBAL_PAUSE."""

    FREE = 3
    MAX_DELAY = 15 * 60
    GLOBAL_LIMIT = 20
    GLOBAL_WINDOW = 10 * 60
    GLOBAL_PAUSE = 5 * 60
    FORGET = 24 * 3600              # a key's count starts over after a quiet day

    def __init__(self, path=None, clock=time.time):
        self.path, self.clock = path, clock
        self.keys = {}              # key -> {"n": failures, "until": ts, "last": ts}
        self.recent = []            # times of recent failures, any key
        self.global_until = 0.0
        self._load()

    def _load(self):
        if not self.path:
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self.keys = {k: v for k, v in data.get("keys", {}).items() if isinstance(v, dict)}
            self.recent = [float(t) for t in data.get("recent", [])]
            self.global_until = float(data.get("global_until", 0))
        except (OSError, ValueError, TypeError, AttributeError):
            pass

    def _save(self):
        if self.path:
            write_json(self.path, {"keys": self.keys, "recent": self.recent,
                                   "global_until": self.global_until})

    def retry_after(self, key):
        """Seconds until this key may try again (0: now)."""
        now = self.clock()
        entry = self.keys.get(key) or {}
        return max(0.0, float(entry.get("until", 0)) - now, self.global_until - now)

    def failed(self, key):
        now = self.clock()
        entry = dict(self.keys.get(key) or {})
        if now - float(entry.get("last", now)) > self.FORGET:
            entry = {}
        n = int(entry.get("n", 0)) + 1
        entry.update(n=n, last=now,
                     until=now + min(2 ** (n - self.FREE + 1), self.MAX_DELAY) if n >= self.FREE else 0)
        self.keys[key] = entry
        self.recent = [t for t in self.recent if now - t < self.GLOBAL_WINDOW] + [now]
        if len(self.recent) > self.GLOBAL_LIMIT:
            self.global_until = now + self.GLOBAL_PAUSE
            self.recent = []
        self._prune(now)
        self._save()
        return self.retry_after(key)

    def succeeded(self, key):
        if self.keys.pop(key, None) is not None:
            self._save()

    def _prune(self, now):
        self.keys = {k: v for k, v in self.keys.items() if now - float(v.get("last", 0)) <= self.FORGET}


# ── confirmation windows ───────────────────────────────────────────
class PromptGate:
    """At most one window per key and MAX_PENDING at once; a refused key waits COOLDOWN."""

    MAX_PENDING = 3
    COOLDOWN = 60

    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.pending = set()
        self.denied = {}            # key -> until

    def check(self, key):
        """None if a window may open now, else (reason, retry_after)."""
        now = self.clock()
        until = self.denied.get(key, 0)
        if until > now:
            return "cooldown", until - now
        if key in self.pending or len(self.pending) >= self.MAX_PENDING:
            return "busy", 0
        return None

    def begin(self, key):
        self.pending.add(key)

    def end(self, key, refused):
        self.pending.discard(key)
        if refused:
            self.denied[key] = self.clock() + self.COOLDOWN
        else:
            self.denied.pop(key, None)


# ── coming back after a drop ───────────────────────────────────────
class ResumeTokens:
    """One-time tokens: valid while their session lives (the viewer may be back on a new
    connection before this side noticed the old one died) and TTL seconds after it was lost.
    Only the sha256 of a token is kept."""

    TTL = 60

    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.items = {}             # sha256(token) -> {key, rights, granted, sid, until (None: session alive)}

    @staticmethod
    def _h(token):
        return hashlib.sha256(token.encode()).hexdigest()

    def issue(self, key, rights, sid, granted=None):
        """`granted`: what the session was let in with, if the host has taken some of it back."""
        self.revoke_session(sid)
        token = secrets.token_urlsafe(32)
        self.items[self._h(token)] = {"key": key, "rights": list(rights), "sid": sid, "until": None,
                                      "granted": list(rights if granted is None else granted)}
        return token

    def use(self, key, token):
        """The entry (consumed) if this key may come back with this token, else None."""
        self._prune()
        h = self._h(token)
        entry = self.items.get(h)
        if entry is None or entry["key"] != key:
            return None
        del self.items[h]
        return entry

    def session_lost(self, sid):
        for entry in self.items.values():
            if entry["sid"] == sid and entry["until"] is None:
                entry["until"] = self.clock() + self.TTL

    def set_rights(self, sid, rights):
        for entry in self.items.values():
            if entry["sid"] == sid:
                entry["rights"] = list(rights)

    def revoke_session(self, sid):
        self.items = {h: e for h, e in self.items.items() if e["sid"] != sid}

    def revoke_key(self, key):
        self.items = {h: e for h, e in self.items.items() if e["key"] != key}

    def revoke_all(self):
        self.items = {}

    def _prune(self):
        now = self.clock()
        self.items = {h: e for h, e in self.items.items() if e["until"] is None or e["until"] > now}


# ── the decision ───────────────────────────────────────────────────
def decide(settings, key, hello):
    """What to do with a viewer that said hello (tokens are checked before this):
    ("reject", reason) | ("contact", rights) | ("password", rights) | ("ask", record | None)."""
    if key in (settings["blocked"] or {}):
        return "reject", "blocked"
    if not settings["allow_incoming"]:
        return "reject", "off"
    record = (settings["trusted"] or {}).get(key)
    if record and settings["unattended"]:
        rights = [r for r in clean_rights(record.get("rights")) if r in hello["want"]]
        if rights and record.get("mode") == "contact":
            return "contact", rights
        if rights and record.get("mode") == "password" and settings["password_hash"] and hello["password"]:
            return "password", rights
    return "ask", record
