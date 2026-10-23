"""The encoder process: a test pattern in, our own frames out — keyframes when asked for,
every frame decodable as plain Annex-B, capture times in order."""
import os
import threading
import time

import av
import pytest

from marinremote.host.capture import Encoder, has_nvenc

TEST_SOURCE = {"kind": "test", "w": 640, "h": 360}


def run_encoder(encoder, seconds=2.5, key_at=1.2, source=TEST_SOURCE, params=None):
    frames, states = [], []
    got_ok = threading.Event()
    ended = threading.Event()

    def on_state(state, info):
        states.append((state, info))
        (got_ok if state == "ok" else ended).set()

    enc = Encoder(source, params or {"fps": 30, "kbps": 2000, "h": None}, encoder,
                  lambda f: frames.append((time.monotonic(), f)), on_state)
    enc.start()
    try:
        assert got_ok.wait(30), states
        t0 = time.monotonic()
        asked = None
        while time.monotonic() - t0 < seconds:
            if asked is None and time.monotonic() - t0 > key_at:
                asked = time.monotonic()
                enc.request_key()
            time.sleep(0.02)
    finally:
        enc.stop()
    assert ended.wait(5) and states[-1] == ("ended", {"error": "", "code": 0})
    return frames, asked, states[0][1]


def check_stream(frames, asked):
    assert frames[0][1].key                                   # starts decodable
    keys_after = [t for t, f in frames[1:] if f.key and t >= asked]
    assert keys_after and keys_after[0] - asked < 0.5         # the asked-for keyframe came quickly
    assert sum(f.key for _, f in frames) <= 3                 # and only when asked (+ the first)
    ts = [f.ts for _, f in frames]
    assert ts == sorted(ts)
    dec = av.CodecContext.create("h264", "r")
    decoded = 0
    for _, f in frames:
        for frame in dec.decode(av.Packet(f.data)):
            decoded += 1
            size = (frame.width, frame.height)
    return decoded, size


def test_x264_stream():
    frames, asked, info = run_encoder("x264")
    assert info["codec"] == "libx264" and info["capture"] == "test"
    decoded, size = check_stream(frames, asked)
    assert size == (640, 360)
    assert decoded >= len(frames) - 1 and len(frames) >= 50  # ~30 fps


@pytest.mark.skipif(not has_nvenc(), reason="no NVIDIA encoder here")
def test_nvenc_stream():
    frames, asked, info = run_encoder("nvenc")
    assert info["codec"] == "h264_nvenc"
    decoded, _ = check_stream(frames, asked)
    assert decoded >= len(frames) - 1


@pytest.mark.skipif(not os.environ.get("MARINREMOTE_REAL_SCREEN"),
                    reason="captures the real screen: set MARINREMOTE_REAL_SCREEN=1")
def test_real_monitor_capture():
    from marinremote.winapi import monitors
    m = monitors()[0]
    source = {"kind": "monitor", **{k: m[k] for k in ("x", "y", "w", "h", "dxgi")}}
    frames, asked, info = run_encoder("nvenc" if has_nvenc() else "x264", source=source,
                                      params={"fps": 60, "kbps": 20000, "h": None})
    assert info["capture"] == "ddagrab" and (info["w"], info["h"]) == (m["w"], m["h"])
    # a still screen sends next to nothing (repeats are skipped), but the asked-for keyframe comes
    check_stream(frames, asked)


def test_repeats_are_told_apart_by_their_texture():
    from types import SimpleNamespace

    from marinremote.host.encoder import Repeats

    def frame(fmt, ptr):
        return SimpleNamespace(format=SimpleNamespace(name=fmt), planes=[SimpleNamespace(buffer_ptr=ptr)])
    r = Repeats()
    assert not r(frame("d3d11", 100))            # the first one is news
    assert not r(frame("d3d11", 200))            # another texture: a new picture
    assert r(frame("d3d11", 200))                # the same texture again: the screen didn't change
    assert r(frame("d3d11", 200))
    assert not r(frame("d3d11", 100))
    assert not r(frame("bgra", 5)) and not r(frame("bgra", 5))   # system memory: never taken for repeats
