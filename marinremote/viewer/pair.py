"""
Pairing two PCs of one person, from the side that dials: a hello that asks to pair (with a
commitment to a random number), the other PC's random number back, ours revealed — then both
show the same six digits, and the person there compares them and answers. See HostCore._ask.
"""

import asyncio
import hashlib
import secrets

from .. import protocol as P
from ..codes import pair_code
from ..config import VERSION
from ..net import LineReader

TIMEOUT = 80                 # the other PC's 60 s window, plus the dialling and some slack


async def request_pair(node, target, name, on_code=None):
    """Ask `target` ({id, relay, addrs}) to pair. on_code(code) is called (network thread) as soon
    as the code is known. Returns {"ok": True, "name", "rights", "relay", "addrs"} or
    {"ok": False, "reason": one of protocol.AUTH_REASONS or "lost", "retry_after"}."""
    conn = await node.connect(target["id"], target.get("relay"), target.get("addrs") or [], timeout=20)
    try:
        bi = await conn.open_bi()
        send = bi.send()
        reader = LineReader(bi.recv())
        mine = secrets.token_bytes(32)
        hello = P.make_hello(name, P.RIGHTS, VERSION,
                             pair={"commit": hashlib.sha256(mine).hexdigest(), "relay": node.addr["relay"],
                                   "addrs": node.addr["addrs"]})
        await send.write_all(P.line(hello))
        loop = asyncio.get_running_loop()
        deadline = loop.time() + TIMEOUT
        code = None
        while True:
            raw = await asyncio.wait_for(reader.next(), max(0.1, deadline - loop.time()))
            if raw is None:
                return {"ok": False, "reason": "lost"}
            msg = P.parse(raw)
            if msg is None:
                continue
            if msg["t"] == "pair.nonce" and code is None:
                theirs = msg.get("n")
                if not isinstance(theirs, str) or not P.HEX64.match(theirs):
                    return {"ok": False, "reason": "protocol"}
                await send.write_all(P.line({"t": "pair.reveal", "n": mine.hex()}))
                code = pair_code(node.id, target["id"], mine, bytes.fromhex(theirs))
                if on_code is not None:
                    on_code(code)
            elif msg["t"] == "pair" and msg.get("ok") and code is not None:
                addrs = msg.get("addrs") if isinstance(msg.get("addrs"), list) else []
                return {"ok": True, "name": P.clean_text(msg.get("name"), 64), "code": code,
                        "rights": P.clean_rights(msg.get("rights")),
                        "relay": P.clean_text(msg.get("relay"), 300) or None,
                        "addrs": [a for a in addrs[:8] if isinstance(a, str) and 0 < len(a) <= 64]}
            elif msg["t"] == "auth":
                if msg.get("ok"):                   # an older MarinRemote let us in as a viewer instead
                    return {"ok": False, "reason": "version"}
                return {"ok": False, "reason": str(msg.get("reason", "denied")),
                        "retry_after": msg.get("retry_after")}
    except asyncio.TimeoutError:
        return {"ok": False, "reason": "timeout"}
    finally:
        try:
            conn.close(P.CLOSE_NORMAL, b"bye")
        except Exception:
            pass
