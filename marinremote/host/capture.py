"""
Encoder processes on the host's side: start one for a monitor at a quality, read its frames
off the loopback socket and hand them on, ask it for keyframes, stop it. One process serves
every viewer watching the same monitor at the same quality.
"""

import json
import os
import socket
import subprocess
import sys
import threading
import time
from collections import namedtuple
from fractions import Fraction

from ..config import FROZEN, ROOT
from ..protocol import FRAME, FRAME_KEY, MAX_FRAME
from ..winapi import tie_to_us

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# Remote desktop wants sharp text: the monitor's own resolution unless said otherwise.
QUALITY = {
    "auto": {"label": "Авто"},
    "smooth": {"label": "Плавно · 60 к/с", "fps": 60, "kbps": 20000, "h": None},
    "sharp": {"label": "Чётко · 30 к/с", "fps": 30, "kbps": 15000, "h": None},
    "low": {"label": "Экономно · 720p", "fps": 30, "kbps": 4000, "h": 720},
}
# "auto" starts at step 0 on a direct path, at step 2 through a relay, and steps down while
# the viewer keeps falling behind
AUTO_STEPS = [
    {"fps": 60, "kbps": 20000, "h": None},
    {"fps": 30, "kbps": 10000, "h": None},
    {"fps": 30, "kbps": 6000, "h": 1080},
    {"fps": 30, "kbps": 3000, "h": 720},
]

Frame = namedtuple("Frame", "data ts key")

_nvenc = None


def has_nvenc():
    """Is there an NVIDIA encoder? (opening it fails without a suitable GPU and driver)"""
    global _nvenc
    if _nvenc is None:
        try:
            import av
            ctx = av.CodecContext.create("h264_nvenc", "w")
            ctx.width, ctx.height, ctx.pix_fmt = 640, 480, "yuv420p"
            ctx.time_base = Fraction(1, 30)
            ctx.open()
            _nvenc = True
        except Exception:
            _nvenc = False
    return _nvenc


def worker_command(cfg):
    arg = json.dumps(cfg, ensure_ascii=False)
    if FROZEN:
        return [sys.executable, "--encoder", arg]
    exe = sys.executable
    if exe.lower().endswith("pythonw.exe"):             # stderr must be readable
        console = exe[:-len("pythonw.exe")] + "python.exe"
        exe = console if os.path.exists(console) else exe
    return [exe, str(ROOT / "app.py"), "--encoder", arg]


class Encoder:
    """One encoder process. on_frame(Frame) and on_state(state, info) are called on its
    reader threads: state "ok" (info: size, codec, capture) once it runs, "ended" (info: the
    error, or "" when stopped on purpose; exit code) when the process is gone."""

    def __init__(self, source, params, encoder, on_frame, on_state):
        self.source, self.params = source, dict(params)
        self.encoder = encoder              # "nvenc" | "x264"
        self.on_frame, self.on_state = on_frame, on_state
        self.proc = None
        self.conn = None
        self.running = False
        self.info = {}
        self.frames = 0
        self.bytes = 0
        self.stats = {}             # what the worker last said: encoded / repeats (skipped) since it began
        self._err = []
        self._lock = threading.Lock()
        self._last_key = 0.0

    def start(self):
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        listener.settimeout(20)
        cfg = {"source": self.source, "fps": self.params["fps"], "kbps": self.params["kbps"],
               "height": self.params.get("h"), "encoder": self.encoder, "gop": 10,
               "port": listener.getsockname()[1]}
        try:
            self.proc = subprocess.Popen(worker_command(cfg), stdin=subprocess.DEVNULL,
                                         stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                         creationflags=NO_WINDOW)
        except OSError as e:
            listener.close()
            self.on_state("ended", {"error": f"не удалось запустить кодировщик: {e}", "code": None})
            return
        tie_to_us(self.proc)
        self.running = True
        threading.Thread(target=self._read, args=(listener,), daemon=True, name="encoder-in").start()
        threading.Thread(target=self._watch, args=(self.proc,), daemon=True, name="encoder-watch").start()

    def request_key(self):
        """The next picture becomes a keyframe (any thread; a burst of requests is one)."""
        now = time.monotonic()
        with self._lock:
            if self.conn is None or now - self._last_key < 0.05:
                return
            self._last_key = now
            try:
                self.conn.send(b"K")
            except OSError:
                pass

    def _read(self, listener):
        try:
            conn, _ = listener.accept()
        except OSError:
            return                              # the worker never came: _watch says why
        finally:
            listener.close()
        conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        with self._lock:
            self.conn = conn
        head = bytearray(FRAME.size)
        try:
            while self.running:
                if not _recv_into(conn, head):
                    break
                n, ts, flags = FRAME.unpack(head)
                if not 0 < n <= MAX_FRAME:
                    break
                data = bytearray(n)
                if not _recv_into(conn, data):
                    break
                self.frames += 1
                self.bytes += n
                self.on_frame(Frame(bytes(data), ts, bool(flags & FRAME_KEY)))
        except OSError:
            pass
        conn.close()

    def _watch(self, proc):
        for raw in iter(proc.stderr.readline, b""):
            line = raw.decode("utf-8", errors="replace").strip()
            if line.startswith("OK "):
                w_h, codec, capture = (line[3:].split() + ["", "", ""])[:3]
                w, h = (int(x) for x in w_h.split("x"))
                self.info = {"w": w, "h": h, "codec": codec, "capture": capture,
                             "encoder": "NVENC" if "nvenc" in codec else "x264 (процессор)"}
                self.on_state("ok", dict(self.info))
            elif line.startswith("STAT "):
                self.stats = {k: int(v) for k, _, v in (part.partition("=") for part in line[5:].split())
                              if v.isdigit()}
            elif line.startswith("INFO"):
                print(f"encoder: {line}", flush=True)
            elif line:
                self._err = (self._err + [line])[-3:]
        proc.wait()
        was = self.running
        self.running = False
        with self._lock:
            conn, self.conn = self.conn, None
        if conn is not None:
            try:
                conn.close()
            except OSError:
                pass
        error = "" if not was else (" | ".join(self._err) or f"код {proc.returncode}")
        self.on_state("ended", {"error": error, "code": proc.returncode})

    def stop(self):
        """Ends the process (the worker sees its socket close and leaves); blocking up to 2 s."""
        self.running = False
        with self._lock:
            conn, self.conn = self.conn, None
        if conn is not None:
            try:
                conn.shutdown(socket.SHUT_RDWR)
                conn.close()
            except OSError:
                pass
        proc = self.proc
        if proc is not None and proc.poll() is None:
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()


def _recv_into(conn, buf):
    view = memoryview(buf)
    got = 0
    while got < len(buf):
        n = conn.recv_into(view[got:])
        if n == 0:
            return False
        got += n
    return True
