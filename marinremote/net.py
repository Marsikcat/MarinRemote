"""
Networking on iroh (Rust QUIC): PCs are dialled by public key, not by IP.

- Identity is our ed25519 key; the QUIC/TLS handshake authenticates both ends and encrypts
  everything on the wire.
- iroh punches through NAT and, when it can't, relays the (still end-to-end encrypted)
  traffic through n0's public relays.
- On a LAN, PCs that want to be found announce themselves by UDP broadcast (Radmin VPN
  carries it too); a viewer then dials them straight away.
- Everything runs on one asyncio loop in a thread of its own: uniffi's event loop is one per
  process, so several endpoints (the tests run two) share it.
"""

import asyncio
import ipaddress
import json
import re
import socket
import subprocess
import threading
import time

import iroh

from .config import DISCOVERY_PORT, PEER_PORT
from .protocol import ALPN, APP, MAX_HEADER, MAX_LINE, PROTO, STREAM_MAGIC, parse_stream_header

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
IPV4 = r"(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})"
KEY_RE = re.compile(r"^[0-9a-f]{64}$")
LAN_FRESH = 15                      # seconds an announcement stays valid


class NetLoop:
    """The network thread: one asyncio loop for the whole process."""

    _inst = None
    _lock = threading.Lock()

    @classmethod
    def get(cls):
        with cls._lock:
            if cls._inst is None:
                cls._inst = cls()
            return cls._inst

    def __init__(self):
        self.loop = asyncio.new_event_loop()
        self._tasks = set()          # the loop keeps only weak references to tasks: we hold them
        ready = threading.Event()
        self.thread = threading.Thread(target=self._main, args=(ready,), daemon=True, name="iroh")
        self.thread.start()
        ready.wait()

    def _main(self, ready):
        asyncio.set_event_loop(self.loop)
        iroh.iroh_ffi.uniffi_set_event_loop(self.loop)
        self.loop.call_soon(ready.set)
        self.loop.run_forever()

    def spawn(self, coro):
        """create_task that cannot be garbage-collected halfway through (network thread only)."""
        task = self.loop.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    def submit(self, coro):
        """Run a coroutine on the network loop from any thread: a concurrent Future."""
        return asyncio.run_coroutine_threadsafe(coro, self.loop)

    def call(self, fn, *args):
        """fn(*args) on the network thread, soon (from any thread)."""
        self.loop.call_soon_threadsafe(fn, *args)


class LineReader:
    """JSON lines from a QUIC stream: next() is a line (bytes) or None when the stream ends."""

    def __init__(self, recv, limit=MAX_LINE):
        self.recv = recv
        self.limit = limit
        self.buf = bytearray()

    async def next(self):
        while True:
            i = self.buf.find(b"\n")
            if i >= 0:
                line = bytes(self.buf[:i])
                del self.buf[:i + 1]
                return line
            if len(self.buf) > self.limit:
                return None
            try:
                chunk = await self.recv.read(65536)
            except iroh.IrohError:
                return None
            if not chunk:
                return None
            self.buf += chunk


async def read_stream_header(rs):
    """The header of a one-way stream and the bytes that came after it: (dict, bytes).
    (None, b"") if it isn't one of ours."""
    buf = bytearray()
    while b"\n" not in buf:
        if len(buf) > MAX_HEADER:
            return None, b""
        chunk = await rs.read(MAX_HEADER)
        if not chunk:
            return None, b""
        buf += chunk
        if not buf[:len(STREAM_MAGIC)] == STREAM_MAGIC[:len(buf)]:
            return None, b""
    head, rest = bytes(buf).split(b"\n", 1)
    if not head.startswith(STREAM_MAGIC):
        return None, b""
    return parse_stream_header(head[len(STREAM_MAGIC):]), rest


class BufferedStream:
    """read_exact() over a QUIC stream in big chunks (one FFI call per chunk, not per frame)."""

    def __init__(self, rs, initial=b"", chunk=256 * 1024):
        self.rs = rs
        self.buf = bytearray(initial)
        self.chunk = chunk

    async def read_exact(self, n):
        while len(self.buf) < n:
            data = await self.rs.read(max(self.chunk, n - len(self.buf)))
            if not data:
                raise EOFError("stream ended")
            self.buf += data
        out = bytes(self.buf[:n])
        del self.buf[:n]
        return out


async def stop_stream(rs, code=1):
    """Tell the sender we won't read this stream."""
    try:
        await rs.stop(code)
    except Exception:
        pass


def path_info(conn):
    """The path in use: via_relay (bool), via (remote address), rtt (ms)."""
    try:
        for p in conn.paths():
            if p.is_selected:
                return {"via_relay": bool(p.is_relay), "via": str(p.remote_addr), "rtt": p.rtt_ms}
    except Exception:
        pass
    return {"via_relay": False, "via": "", "rtt": None}


def remote_key(conn):
    return conn.remote_id().to_bytes().hex()


# ── local network ───────────────────────────────────────────────────
def _run(cmd):
    try:
        out = subprocess.run(cmd, capture_output=True, timeout=5, creationflags=NO_WINDOW).stdout
        return out.decode("cp866", errors="replace")
    except (OSError, subprocess.SubprocessError):
        return ""


def interfaces():
    """[(ip, broadcast)] for every IPv4 adapter, Radmin VPN (26.x) first."""
    res = []
    text = _run(["ipconfig"])
    for m in re.finditer(r"IPv4[^:\n]*:\s*" + IPV4 + r"[^\n]*\n[^\n:]*:\s*" + IPV4, text):
        ip, mask = m.group(1), m.group(2)
        if ip.startswith(("127.", "169.254.")):
            continue
        try:
            bcast = str(ipaddress.IPv4Network(f"{ip}/{mask}", strict=False).broadcast_address)
        except ValueError:
            bcast = None
        res.append((ip, bcast))
    return sorted(res, key=lambda x: (not x[0].startswith("26."), x[0]))


def radmin_neighbours():
    return list(dict.fromkeys(re.findall(r"\b(26\.\d+\.\d+\.\d+)\b", _run(["arp", "-a"]))))


# ── the endpoint ────────────────────────────────────────────────────
class Node:
    """Our iroh endpoint. Incoming connections go to `on_incoming(conn)` (a coroutine function,
    run on the network loop); `connect()` dials out. Thread-safe where it says so."""

    def __init__(self, secret_hex, port=PEER_PORT, mode="internet", bind_host="0.0.0.0", lan=True,
                 discovery_port=DISCOVERY_PORT):
        self.net = NetLoop.get()
        self.secret = bytes.fromhex(secret_hex)
        self.id = iroh.SecretKey.from_bytes(self.secret).public().to_bytes().hex()
        self.port_wanted, self.mode, self.bind_host = port, mode, bind_host
        self.lan, self.discovery_port = lan, discovery_port
        self.ep = None
        self.port = None
        self.on_incoming = None
        self.addr = {"relay": None, "addrs": []}
        self.seen = {}               # LAN: key -> {ip, port, name, first, last}
        self.listeners = []          # callback(what) on any thread: "addr" | "lan"
        self.announce = lambda: None     # -> {"name": …} to be visible on the LAN, None to stay quiet
        self.running = False
        self._ifaces, self._neighbours = [], []
        self._lan_lock = threading.Lock()

    # ── lifecycle ───────────────────────────────────────────────────
    def start(self, timeout=20):
        """Blocking, from any thread but the network one."""
        self.net.submit(self._start()).result(timeout)
        if self.lan:
            threading.Thread(target=self._discover, daemon=True, name="discovery").start()

    async def _start(self):
        internet = self.mode == "internet"
        binds = [f"{self.bind_host}:{self.port_wanted}"] if self.port_wanted else []
        err = None
        for bind in binds + [f"{self.bind_host}:0"]:
            try:
                self.ep = await iroh.Endpoint.bind(iroh.EndpointOptions(
                    preset=iroh.preset_n0() if internet else iroh.preset_minimal(),
                    alpns=[ALPN], bind_addr=bind, secret_key=self.secret))
                break
            except iroh.IrohError as e:
                err = e
        if self.ep is None:
            raise OSError(f"не удалось запустить сеть: {err}")
        for sock in self.ep.bound_sockets():
            if not sock.startswith("["):
                self.port = int(sock.rsplit(":", 1)[1])
        self.running = True
        self._refresh_addr()
        self.net.spawn(self._accept_loop())
        self.net.spawn(self._maintain())

    def stop(self, timeout=4):
        if not self.running:
            return
        self.running = False
        try:
            self.net.submit(self._stop()).result(timeout)
        except Exception:
            pass

    async def _stop(self):
        if self.ep:
            try:
                await asyncio.wait_for(self.ep.close(), 3)
            except Exception:
                pass

    def _notify(self, what):
        for cb in list(self.listeners):
            try:
                cb(what)
            except Exception:
                pass

    def _refresh_addr(self):
        a = self.ep.addr()
        new = {"relay": a.relay_url(), "addrs": a.direct_addresses()[:8]}
        if new != self.addr:
            self.addr = new
            self._notify("addr")

    async def _maintain(self):
        while self.running:
            await asyncio.sleep(2)
            try:
                self._refresh_addr()
            except Exception:
                pass
            now = time.time()
            with self._lan_lock:
                stale = [k for k, v in self.seen.items() if now - v["last"] > LAN_FRESH]
                for k in stale:
                    del self.seen[k]
            if stale:
                self._notify("lan")

    # ── connections ─────────────────────────────────────────────────
    async def _accept_loop(self):
        while self.running:
            try:
                inc = await self.ep.accept_next()
            except Exception:
                break
            if inc is None:
                break
            self.net.spawn(self._incoming(inc))

    async def _incoming(self, inc):
        conn = None
        try:
            conn = await asyncio.wait_for((await inc.accept()).connect(), 15)
        except Exception:
            return
        if self.on_incoming is None:
            conn.close(1, b"not accepting")
            return
        try:
            await self.on_incoming(conn)
        except Exception:
            import traceback
            traceback.print_exc()
            try:
                conn.close(5, b"internal error")
            except Exception:
                pass

    async def connect(self, key, relay=None, addrs=(), timeout=20):
        """Dial a PC by its key (network thread). Hints: its relay, addresses; a LAN
        announcement from it is added by itself."""
        hints = list(addrs or [])
        with self._lan_lock:
            seen = self.seen.get(key)
        if seen and time.time() - seen["last"] < LAN_FRESH:
            hints.insert(0, f"{seen['ip']}:{seen['port']}")
        target = iroh.EndpointAddr(iroh.EndpointId.from_bytes(bytes.fromhex(key)), relay,
                                   list(dict.fromkeys(hints)))
        return await asyncio.wait_for(self.ep.connect(target, ALPN), timeout)

    def discovered(self):
        """PCs announcing themselves on the LAN right now: key -> {ip, port, name}."""
        now = time.time()
        with self._lan_lock:
            return {k: dict(v) for k, v in self.seen.items() if now - v["last"] < LAN_FRESH}

    # ── LAN discovery (plain UDP broadcast, works over Radmin VPN too) ──
    def _announcement(self):
        me = self.announce()
        if not me or self.port is None:
            return None
        return json.dumps({"app": APP, "proto": PROTO, "id": self.id, "port": self.port,
                           "name": str(me.get("name", ""))[:64]}, ensure_ascii=False).encode()

    def _discover(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        try:
            sock.bind(("0.0.0.0", self.discovery_port))
        except OSError as e:
            print(f"discovery: port {self.discovery_port} busy: {e}", flush=True)
            return
        sock.settimeout(0.5)
        next_announce = next_ifaces = 0.0
        while self.running:
            now = time.time()
            if now >= next_ifaces:
                self._ifaces = interfaces()
                self._neighbours = radmin_neighbours()
                next_ifaces = now + 20
            if now >= next_announce:
                self._announce(sock)
                next_announce = now + 2
            try:
                data, (ip, _) = sock.recvfrom(4096)
            except (socket.timeout, ConnectionResetError):
                continue
            except OSError:
                break
            self._on_announcement(sock, data, ip)
        sock.close()

    def _announce(self, sock, only=None):
        payload = self._announcement()
        if payload is None:
            return
        targets = [only] if only else (["255.255.255.255"] + [b for _, b in self._ifaces if b]
                                       + self._neighbours)
        for t in dict.fromkeys(targets):
            try:
                sock.sendto(payload, (t, self.discovery_port))
            except OSError:
                pass

    def _on_announcement(self, sock, data, ip):
        try:
            msg = json.loads(data)
        except ValueError:
            return
        if not isinstance(msg, dict):
            return
        key = str(msg.get("id", ""))
        if msg.get("app") != APP or msg.get("proto") != PROTO or key == self.id or not KEY_RE.match(key):
            return
        port = msg.get("port")
        if not isinstance(port, int) or not 0 < port < 65536:
            return
        now = time.time()
        name = str(msg.get("name", ""))[:64]
        with self._lan_lock:
            entry = self.seen.get(key)
            fresh = entry is None or now - entry["last"] > LAN_FRESH
            changed = fresh or entry["name"] != name or entry["ip"] != ip
            self.seen[key] = {"ip": ip, "port": port, "name": name,
                              "first": now if fresh else entry["first"], "last": now}
        if fresh:
            self._announce(sock, only=ip)   # answer directly so they find us without waiting
        if changed:
            self._notify("lan")
