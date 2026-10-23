"""
Connection codes and fingerprints.

A code is what you give someone so they can connect to this PC: compact binary in base58
(no look-alike characters, double-click selects all of it):

    mr-<base58( [version 1][public key: 32][relay label length][relay label][checksum: 2] )>

No IP addresses: iroh exchanges them itself through the relay while punching through NAT,
and on a LAN the broadcast finds the PC anyway. The checksum catches a code that was cut
short or mistyped before any connection is attempted.
"""

import hashlib
import re

PREFIX = "mr-"
VERSION = 1
B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
N0_RELAY = re.compile(r"^https://([a-z0-9-]+)\.relay\.n0\.iroh\.link\.?/?$")
HEX_KEY = re.compile(r"^[0-9a-f]{64}$")
FOREIGN = ("marin-", "moyd-")       # MarinCall invites


def b58encode(data):
    n = int.from_bytes(data, "big")
    out = ""
    while n:
        n, r = divmod(n, 58)
        out = B58[r] + out
    return "1" * (len(data) - len(data.lstrip(b"\0"))) + out


def b58decode(text):
    n = 0
    for ch in text:
        n = n * 58 + B58.index(ch)
    body = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return b"\0" * (len(text) - len(text.lstrip("1"))) + body


def _check(body):
    return hashlib.sha256(body).digest()[:2]


def relay_label(relay):
    """n0's relays shrink to their short name; any other relay is kept whole."""
    m = N0_RELAY.match(relay or "")
    return m.group(1) if m else (relay or "")


def relay_url(label):
    if not label:
        return None
    return label if "://" in label else f"https://{label}.relay.n0.iroh.link./"


def make_code(key_hex, relay=None):
    label = relay_label(relay).encode()
    body = bytes([VERSION]) + bytes.fromhex(key_hex) + bytes([len(label)]) + label
    return PREFIX + b58encode(body + _check(body))


def parse_code(code):
    """{"id": key hex, "relay": url | None} — or ValueError with a message for people."""
    code = "".join(str(code).split())
    if HEX_KEY.match(code.lower()):
        return {"id": code.lower(), "relay": None}
    if code.startswith(FOREIGN):
        raise ValueError("это приглашение MarinCall, а не код MarinRemote")
    if not code.startswith(PREFIX):
        raise ValueError("это не код подключения MarinRemote (он начинается с «mr-»)")
    try:
        raw = b58decode(code[len(PREFIX):])
        body, check = raw[:-2], raw[-2:]
        if len(body) < 34 or _check(body) != check:
            raise ValueError
        if body[0] != VERSION:
            raise LookupError
        key = body[1:33].hex()
        n = body[33]
        if len(body) != 34 + n:
            raise ValueError
        label = body[34:34 + n].decode()
    except LookupError:
        raise ValueError("код от более новой версии MarinRemote — обновите программу") from None
    except (ValueError, IndexError, UnicodeDecodeError):
        raise ValueError("код повреждён — скопируйте его целиком") from None
    return {"id": key, "relay": relay_url(label)}


CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"      # base32 without I, L, O, U


def fingerprint(key_hex):
    """A short form of a device's key to compare by eye or read out: «7K3M-Q9XA»."""
    n = int.from_bytes(hashlib.sha256(bytes.fromhex(key_hex)).digest()[:5], "big")
    text = "".join(CROCKFORD[(n >> (35 - 5 * i)) & 31] for i in range(8))
    return f"{text[:4]}-{text[4:]}"


def pretty(code, group=5):
    """The code with thin breaks every few characters, for reading it out (not for copying)."""
    body = code[len(PREFIX):] if code.startswith(PREFIX) else code
    return PREFIX + " ".join(body[i:i + group] for i in range(0, len(body), group))
