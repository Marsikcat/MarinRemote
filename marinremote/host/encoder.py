"""
The screen encoder, in a process of its own: `MarinRemote.exe --encoder <json>`
(from source: `python app.py --encoder <json>`).

FFmpeg's libraries do the work through PyAV, no ffmpeg.exe. DXGI desktop duplication
(ddagrab) hands D3D11 frames straight to NVENC on the GPU. x264, scaled pictures and monitors
ddagrab can't reach (another video adapter) go through system memory (gdigrab). The cursor is
not drawn into the picture: the viewer draws it.

Out, on a loopback TCP socket to the app — our own frames, one encoded picture each:
    [u32 length][u64 capture time, µs, QueryPerformanceCounter][u8 flags: 1 = keyframe][H.264 Annex-B]
SPS/PPS go in front of every keyframe, so any keyframe starts a decodable stream.
In, on the same socket: b"K" — make the next picture a keyframe.

Low latency: no B-frames, no lookahead, ultra-low-latency tuning, a VBV of ~100 ms, keyframes
only when asked for (plus one every `gop` seconds as a safety net). Rate control is capped VBR:
NVENC's CBR pads every frame to the full rate (20 Mbit/s for a still desktop), capped VBR
keeps the same ceiling — and the same latency bound — but a still screen costs almost nothing.

A still screen costs nothing at all, in fact: when nothing changed, ddagrab hands out the
previous picture again — the very same D3D11 texture — to keep its frame rate. Such a repeat is
neither encoded nor sent (the viewer keeps showing the last picture), unless a keyframe is
wanted: then it is encoded as one, so a keyframe never waits for the screen to change.
"STAT encoded=N repeats=M" on stderr every few seconds says how many were skipped.

The app ties this process to its own lifetime (a job object), so it never outlives MarinRemote.
Exit codes: 0 the app hung up, 1 an error (the last stderr lines say which), 3 the picture
changed size or format (start again with fresh settings), 4 desktop duplication was lost
(Windows took it back — start again; if that keeps happening, use GDI for this monitor).
"""

import ctypes
import json
import socket
import struct
import sys
import threading
import time
from fractions import Fraction

FRAME = struct.Struct("<IQB")
EXIT_FORMAT = 3
EXIT_LOST = 4
STAT_EVERY = 5


def _say(text):
    """A line for the app (stderr) — works in the windowed exe too, which has no sys.stderr."""
    data = (text.replace("\n", " ") + "\n").encode("utf-8", "replace")
    try:
        k32 = ctypes.windll.kernel32
        k32.GetStdHandle.restype = ctypes.c_void_p
        handle = k32.GetStdHandle(-12)
        written = ctypes.c_ulong()
        k32.WriteFile(ctypes.c_void_p(handle), data, len(data), ctypes.byref(written), None)
    except Exception:
        pass


def _even(n):
    return max(2, int(n) // 2 * 2)


def _now_us():
    return time.perf_counter_ns() // 1000


class FormatChanged(Exception):
    pass


class CaptureLost(Exception):
    pass


class Repeats:
    """Tells a repeat of the previous picture from a new one: D3D11 frames that share the previous
    frame's texture. The previous frame is held on to until the next one has come, so its texture
    can't be handed out again for a new picture in between. Frames in system memory (GDI, x264,
    tests) are never taken for repeats."""

    def __init__(self):
        self.prev = None
        self.prev_tex = None

    def __call__(self, frame):
        tex = frame.planes[0].buffer_ptr if frame.format.name == "d3d11" and frame.planes else None
        repeat = tex is not None and tex == self.prev_tex
        self.prev, self.prev_tex = frame, tex
        return repeat


class Worker:
    def __init__(self, cfg):
        self.cfg = cfg
        self.fps = int(cfg["fps"])
        self.key_wanted = threading.Event()
        self.stopping = threading.Event()
        self.capture = ""

    # ── the app's side of the socket ────────────────────────────────
    def _commands(self, sock):
        while not self.stopping.is_set():
            try:
                data = sock.recv(64)
            except OSError:
                break
            if not data:
                break
            if b"K" in data:
                self.key_wanted.set()
        self.stopping.set()

    # ── the picture ─────────────────────────────────────────────────
    def _open(self):
        import av
        src = self.cfg["source"]
        fps = self.fps
        target = self.cfg.get("height")
        nvenc = self.cfg["encoder"] == "nvenc"
        if src["kind"] == "test":                     # tests: a moving pattern, no real screen
            self.capture = "test"
            return av.open(f"testsrc2=s={src['w']}x{src['h']}:r={fps}", format="lavfi")
        if src.get("dxgi") is not None:
            graph = f"ddagrab=output_idx={src['dxgi']}:framerate={fps}:draw_mouse=0"
            scale = target if target and src["h"] > target else None
            if scale or not nvenc:                    # otherwise the frames never leave the GPU
                graph += ",hwdownload,format=bgra"
                if scale:
                    graph += f",scale=-2:{scale}:flags=bicubic"
                if not nvenc:
                    graph += ",format=yuv420p"
            for _ in range(3):                        # Windows may refuse a duplication for a moment
                try:
                    vin = av.open(graph, format="lavfi")
                    self.capture = "ddagrab"
                    return vin
                except av.error.FFmpegError as e:
                    error = e
                    time.sleep(0.4)
            _say(f"INFO ddagrab недоступен ({error}) — захват через GDI")
        self.capture = "gdigrab"
        return av.open("desktop", format="gdigrab",
                       options={"framerate": str(fps), "offset_x": str(src["x"]), "offset_y": str(src["y"]),
                                "video_size": f"{src['w']}x{src['h']}", "draw_mouse": "0"})

    def _codec(self, frame):
        import av
        nvenc = self.cfg["encoder"] == "nvenc"
        kbps = int(self.cfg["kbps"])
        fmt = frame.format.name
        w, h = frame.width, frame.height
        target = self.cfg.get("height")
        if fmt != "d3d11" and target and h > target:
            w, h = w * target / h, target
        self.size = (_even(w), _even(h))
        ctx = av.CodecContext.create("h264_nvenc" if nvenc else "libx264", "w")
        ctx.width, ctx.height = self.size
        ctx.time_base = Fraction(1, self.fps)
        ctx.framerate = Fraction(self.fps, 1)
        ctx.max_b_frames = 0
        ctx.gop_size = max(self.fps, int(self.cfg.get("gop", 10) * self.fps))
        common = {"maxrate": f"{kbps}k", "bufsize": f"{max(kbps // 10, 200)}k", "forced-idr": "1"}
        if nvenc:
            # NVENC takes D3D11 surfaces and converts BGRA itself
            ctx.pix_fmt = fmt if fmt in ("d3d11", "bgra", "bgr0") else "yuv420p"
            ctx.bit_rate = kbps * 1000
            ctx.options = {"preset": "p2", "tune": "ull", "rc": "vbr", "zerolatency": "1", "delay": "0",
                           "no-scenecut": "1", "rc-lookahead": "0", **common}
        else:
            ctx.pix_fmt = "yuv420p"
            ctx.options = {"preset": "superfast" if self.fps >= 50 else "veryfast", "tune": "zerolatency",
                           "crf": "23", "sc_threshold": "0", **common}
        self.fmt = fmt
        return ctx

    @staticmethod
    def nvenc_free():
        """Can we get an NVENC session right now? (consumer GPUs allow only a few at once)"""
        import av
        try:
            ctx = av.CodecContext.create("h264_nvenc", "w")
            ctx.width, ctx.height, ctx.pix_fmt = 256, 256, "yuv420p"
            ctx.time_base = Fraction(1, 30)
            ctx.open()
            return True
        except Exception:
            return False

    def run(self):
        import av
        from av.video.frame import PictureType
        if self.cfg["encoder"] == "nvenc" and not self.nvenc_free():
            self.cfg["encoder"] = "x264"           # the video card is busy: the processor encodes
            _say("INFO NVENC занят — кодирует процессор")
        sock = socket.create_connection(("127.0.0.1", self.cfg["port"]), timeout=10)
        sock.settimeout(None)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        threading.Thread(target=self._commands, args=(sock,), daemon=True, name="commands").start()
        vin = self._open()
        frames = vin.decode(video=0)
        first = next(frames)
        ctx = self._codec(first)
        reformat = None
        if first.format.name not in ("d3d11", ctx.pix_fmt) or (first.width, first.height) != self.size:
            from av.video.reformatter import VideoReformatter
            reformat = VideoReformatter()
        _say(f"OK {self.size[0]}x{self.size[1]} {ctx.name} {self.capture}")
        paced = self.capture == "test"
        t0 = time.perf_counter()
        i = 0
        frame = first
        captured = {}
        repeats = Repeats()
        encoded = skipped = 0
        stat_at = time.monotonic() + STAT_EVERY
        while not self.stopping.is_set():
            if i:
                try:
                    frame = next(frames)
                except av.error.FFmpegError as e:
                    if self.capture == "ddagrab":
                        raise CaptureLost(str(e)) from None
                    raise
                if paced:
                    time.sleep(max(0.0, t0 + i / self.fps - time.perf_counter()))
                if frame.format.name != self.fmt or (frame.format.name == "d3d11"
                                                     and (frame.width, frame.height) != self.size):
                    raise FormatChanged(f"{frame.width}x{frame.height} {frame.format.name}")
            if time.monotonic() >= stat_at:
                _say(f"STAT encoded={encoded} repeats={skipped}")
                stat_at = time.monotonic() + STAT_EVERY
            if repeats(frame) and not self.key_wanted.is_set():
                skipped += 1                          # the screen hasn't changed: nothing to send
                i += 1
                continue
            encoded += 1
            captured[i] = _now_us()
            if reformat is not None:
                frame = reformat.reformat(frame, width=self.size[0], height=self.size[1],
                                          format=ctx.pix_fmt, interpolation="BICUBIC", threads=2)
            frame.pts = i
            frame.time_base = ctx.time_base
            if i == 0 or self.key_wanted.is_set():
                self.key_wanted.clear()
                frame.pict_type = PictureType.I
            else:
                frame.pict_type = PictureType.NONE
            for p in ctx.encode(frame):
                ts = captured.pop(p.pts, None) or _now_us()
                data = bytes(p)
                sock.sendall(FRAME.pack(len(data), ts, 1 if p.is_keyframe else 0) + data)
            if len(captured) > 8:                    # never let it grow if an encoder holds frames
                for k in sorted(captured)[:-8]:
                    captured.pop(k, None)
            i += 1


def main(arg):
    try:
        from ..winapi import dpi_aware
        dpi_aware()
        Worker(json.loads(arg)).run()
    except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
        return 0                                       # the app stopped watching
    except FormatChanged as e:
        _say(f"FORMAT {e}")
        return EXIT_FORMAT
    except CaptureLost as e:
        _say(f"LOST {e}")
        return EXIT_LOST
    except StopIteration:
        _say("захват экрана прекратился")
        return 1
    except Exception as e:
        _say(f"{type(e).__name__}: {e}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
