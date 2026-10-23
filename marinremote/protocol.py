"""
The MarinRemote wire protocol, version 1 — one QUIC connection per session (docs/PROTOCOL.md).

- The viewer dials the host by its public key and opens the control stream (bidirectional,
  JSON lines). The first thing on it is `hello`; until the host answers `auth ok` it reads
  nothing else, takes no datagrams and accepts no other stream.
- Every one-way stream starts with `MRS1` and a JSON line naming its kind and the session id
  from `auth ok` — a stream opened before the login can't know it and is refused.
- Who is on the other end is settled by QUIC itself: the TLS handshake proves the key.
"""

import json
import re

APP = "MarinRemote"
ALPN = b"marinremote/1"
PROTO = 1
MAX_LINE = 1 << 20                  # control messages (clipboard text included)
STREAM_MAGIC = b"MRS1"
MAX_HEADER = 4096

RIGHTS = ("view", "control", "files", "clipboard")
RIGHT_NAMES = {
    "view": "Просмотр экрана",
    "control": "Управление мышью и клавиатурой",
    "files": "Передача файлов",
    "clipboard": "Общий буфер обмена",
}
RIGHT_SHORT = {"view": "просмотр", "control": "управление", "files": "файлы", "clipboard": "буфер"}

# what each message from the viewer needs; anything else (except the free ones) is unknown
NEEDS = {
    "monitor": "view", "video.key": "view", "video.quality": "view",
    "clip": "clipboard",
    "fs.roots": "files", "fs.list": "files", "fs.mkdir": "files", "fs.rename": "files",
    "fs.delete": "files", "get": "files", "put": "files", "put.end": "files", "xfer.cancel": "files",
}
FREE = {"ping", "bye"}

# QUIC application close codes
CLOSE_NORMAL = 0        # the viewer left / the app is closing
CLOSE_AUTH = 1          # not let in
CLOSE_KICKED = 2        # the host's user disconnected this session
CLOSE_KILLED = 3        # the emergency hotkey / "disconnect everyone"
CLOSE_REPLACED = 4      # the same viewer came back on a new connection
CLOSE_PROTOCOL = 5      # nonsense on the wire
CLOSE_IDLE = 6          # nothing heard for too long

# why a login failed, as the viewer explains it (retry_after fills {wait})
AUTH_REASONS = {
    "denied": "На удалённом ПК отказали в подключении.",
    "timeout": "На удалённом ПК никто не ответил за 30 секунд.",
    "bad_password": "Неверный пароль.",
    "locked": "Слишком много неудачных попыток. Попробуйте через {wait}.",
    "cooldown": "Удалённый ПК недавно отказал этому устройству. Попробуйте через {wait}.",
    "blocked": "Это устройство заблокировано на удалённом ПК.",
    "off": "Удалённый ПК сейчас не принимает подключения.",
    "busy": "На удалённом ПК уже ждут ответа на другие запросы. Попробуйте чуть позже.",
    "version": "Версии MarinRemote несовместимы — обновите программу на обоих ПК.",
    "protocol": "Удалённый ПК не понял запрос.",
}

_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def line(obj):
    return (json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n").encode()


def parse(raw):
    """A message: a JSON object with a string "t" — anything else is None."""
    try:
        msg = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(msg, dict) or not isinstance(msg.get("t"), str):
        return None
    return msg


def clean_rights(value):
    """Known rights, in the canonical order, from whatever came in."""
    if not isinstance(value, (list, tuple, set)):
        return []
    return [r for r in RIGHTS if r in value]


def describe_rights(rights):
    return ", ".join(RIGHT_SHORT[r] for r in clean_rights(rights)) or "нет прав"


def clean_text(value, limit):
    if not isinstance(value, str):
        return ""
    return _CONTROL.sub("", value).strip()[:limit]


def check_hello(msg):
    """The fields of a hello we act on, normalised — ValueError("version" | "protocol") if it
    can't be one."""
    if not isinstance(msg, dict) or msg.get("t") != "hello" or msg.get("app") != APP:
        raise ValueError("protocol")
    if msg.get("proto") != PROTO:
        raise ValueError("version")
    password = msg.get("password")
    resume = msg.get("resume")
    return {
        "name": clean_text(msg.get("name"), 64) or "Без имени",
        "want": clean_rights(msg.get("want")),
        "password": password if isinstance(password, str) and 0 < len(password) <= 256 else None,
        "resume": resume if isinstance(resume, str) and 0 < len(resume) <= 128 else None,
        "remember": msg.get("remember") is True,
        "ver": clean_text(msg.get("ver"), 32),
    }


def make_hello(name, want, version, password=None, resume=None, remember=False):
    msg = {"t": "hello", "app": APP, "proto": PROTO, "ver": version, "name": name,
           "want": clean_rights(want)}
    if password:
        msg["password"] = password
    if resume:
        msg["resume"] = resume
    if remember:
        msg["remember"] = True
    return msg


def needed_right(msg_type):
    """The right a viewer message needs, "" for the free ones, None for unknown types."""
    if msg_type in FREE:
        return ""
    return NEEDS.get(msg_type)


# ── datagrams: [type][session tag: 4][payload] ─────────────────────
# The tag (from the session id) proves the datagram was sent after the login: QUIC may still
# hold ones that arrived earlier, and those can't know it.
def dgram_tag(sid):
    return bytes.fromhex(sid[:8])


def dgram(kind, sid, payload):
    return bytes([kind]) + dgram_tag(sid) + payload


# ── one-way streams ────────────────────────────────────────────────
def stream_header(kind, sid, **fields):
    return STREAM_MAGIC + line({"t": kind, "sid": sid, **fields})


def parse_stream_header(raw):
    """raw: the bytes after the magic, up to the newline. None if not a header."""
    msg = parse(raw)
    if msg is None or not isinstance(msg.get("sid"), str):
        return None
    return msg


def close_code(text):
    """The application code in iroh's close text ("closed by peer: kicked (code 2)"), or None
    when the connection died some other way (timeout, the network went away)."""
    m = re.search(r"\(code (\d+)\)\s*$", text or "")
    if not m or "by peer" not in (text or ""):
        return None
    return int(m.group(1))


def wait_text(seconds):
    s = max(1, int(round(seconds)))
    if s < 60:
        return f"{s} с"
    m = (s + 59) // 60
    return f"{m} мин"
