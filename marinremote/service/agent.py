"""An elevated helper on WinSta0\\Default. Only mouse/keyboard; no network, files or commands."""

import os

from .. import winapi
from ..host.inject import BUTTON_FLAGS, HWHEEL, WHEEL, Injector
from . import AGENT_PREFIX


def integer(value, lo, hi):
    return type(value) is int and lo <= value <= hi


def validate(msg):
    if msg.get("kind") == "key":
        return (set(msg) <= {"kind", "scan", "ext", "down", "vk"}
                and integer(msg.get("scan"), 0, 255) and integer(msg.get("vk"), 0, 255)
                and bool(msg["scan"] or msg["vk"]) and type(msg.get("ext")) is bool
                and type(msg.get("down")) is bool)
    if msg.get("kind") == "mouse":
        allowed = {0, WHEEL, HWHEEL} | {p for flags in BUTTON_FLAGS.values() for p in flags[:2]}
        return (set(msg) <= {"kind", "x", "y", "flags", "data", "move"}
                and integer(msg.get("x"), -131072, 131072) and integer(msg.get("y"), -131072, 131072)
                and integer(msg.get("flags"), 0, 65535) and msg["flags"] in allowed
                and integer(msg.get("data"), -6000, 6000) and type(msg.get("move")) is bool)
    return False


class CheckedInjector(Injector):
    def _go(self, *inputs):
        if super()._go(*inputs) != len(inputs):
            raise OSError("Windows rejected privileged input")


class InputAgent:
    def __init__(self, host_pid, injector=None, secure=None):
        self.host_pid = host_pid
        self.inj = injector or CheckedInjector()
        self.secure = secure or winapi.secure_desktop
        self.keys, self.buttons = set(), set()

    def handle(self, msg):
        if msg.get("kind") == "ping":
            secure = self.secure()
            if secure:
                self.release_all()
            return {"ok": True, "secure": secure}
        if msg.get("kind") == "release":
            self.release_all()
            return {"ok": True}
        if not validate(msg):
            raise ValueError("invalid input")
        if self.secure():
            self.release_all()
            return {"ok": False, "reason": "secure"}
        if msg["kind"] == "key":
            key = (msg["scan"], msg["ext"], msg["vk"])
            if msg["down"]:
                if winapi.foreground_pid() in (self.host_pid, os.getpid()):
                    return {"ok": False, "reason": "self"}
                self.keys.add(key)
            elif key not in self.keys:
                return {"ok": True}
            else:
                self.keys.discard(key)
            self.inj.key(msg["scan"], msg["ext"], msg["down"], msg["vk"])
        else:
            flags, data = msg["flags"], msg["data"]
            down = next((name for name, (d, _, _) in BUTTON_FLAGS.items() if flags == d), None)
            up = next((name for name, (_, u, _) in BUTTON_FLAGS.items() if flags == u), None)
            if flags and (down or flags in (WHEEL, HWHEEL)):
                # Also check keyboard target for clicks without an explicit position.
                pid = (winapi.pid_at(msg["x"], msg["y"]) if msg["move"] else winapi.foreground_pid())
                if pid in (self.host_pid, os.getpid()):
                    return {"ok": False, "reason": "self"}
            if down:
                self.buttons.add((up_flag(flags), data))
            if up:
                if (flags, data) not in self.buttons:
                    return {"ok": True}
                self.buttons.discard((flags, data))
            self.inj.mouse(msg["x"], msg["y"], flags, data, msg["move"])
        return {"ok": True}

    def release_all(self):
        for scan, ext, vk in self.keys:
            self.inj.key(scan, ext, False, vk)
        for flag, data in self.buttons:
            self.inj.mouse(0, 0, flag, data, False)
        self.keys.clear()
        self.buttons.clear()


def up_flag(flag):
    return next(u for d, u, _ in BUTTON_FLAGS.values() if d == flag)


def run(name, host_pid):
    from .ipc import connect, trusted_server
    if not name.startswith(AGENT_PREFIX) or len(name) != len(AGENT_PREFIX) + 32:
        raise ValueError("invalid agent pipe")
    winapi.dpi_aware()
    pipe = connect(name)
    agent = InputAgent(host_pid)
    try:
        trusted_server(pipe.handle)
        pipe.send({"kind": "agent"})
        while True:
            pipe.send(agent.handle(pipe.recv(timeout=3)))
    except Exception as exc:
        try:
            pipe.send({"kind": "agent.error", "error": f"{type(exc).__name__}: {exc}"})
        except Exception:
            pass
        raise
    finally:
        try:
            agent.release_all()
        finally:
            pipe.close()
