"""Privileged-input protocol and real local pipes; no real service installation or SendInput."""

import os
from pathlib import Path
import queue
import secrets
import threading

import pytest

from marinremote.host.input import _probe
from marinremote.service import agent, client, ipc, runtime


class FakeInjector:
    def __init__(self):
        self.log = []

    def mouse(self, *args):
        self.log.append(("mouse", *args))

    def key(self, *args):
        self.log.append(("key", *args))


def key(down=True):
    return {"kind": "key", "scan": 42, "ext": False, "down": down, "vk": 160}


def mouse(flags=0):
    return {"kind": "mouse", "x": 100, "y": 200, "flags": flags, "data": 0, "move": True}


def test_agent_releases_only_what_it_pressed_and_never_accepts_commands(monkeypatch):
    monkeypatch.setattr(agent.winapi, "foreground_pid", lambda: 50)
    monkeypatch.setattr(agent.winapi, "pid_at", lambda x, y: 50)
    inj = FakeInjector()
    worker = agent.InputAgent(10, inj, secure=lambda: False)
    worker.handle(key())
    worker.handle(mouse(2))
    worker.handle({"kind": "release"})
    assert inj.log == [("key", 42, False, True, 160), ("mouse", 100, 200, 2, 0, True),
                       ("key", 42, False, False, 160), ("mouse", 0, 0, 4, 0, False)]
    worker.handle(key(False))
    assert len(inj.log) == 4
    for junk in ({"kind": "run", "path": "cmd.exe"}, {**key(), "cmd": "cmd.exe"},
                 {**key(), "scan": -1}, {**mouse(), "flags": 0x8000}, {**mouse(), "x": 10**20}):
        with pytest.raises(ValueError):
            worker.handle(junk)


def test_agent_protects_the_real_host_not_only_its_own_pid(monkeypatch):
    inj = FakeInjector()
    worker = agent.InputAgent(10, inj, secure=lambda: False)
    for pid in (10, os.getpid()):
        monkeypatch.setattr(agent.winapi, "foreground_pid", lambda pid=pid: pid)
        monkeypatch.setattr(agent.winapi, "pid_at", lambda x, y, pid=pid: pid)
        assert worker.handle(key())["reason"] == "self"
        assert worker.handle(mouse(2))["reason"] == "self"
    assert not inj.log


def test_elevated_helper_still_refuses_secure_desktop():
    inj = FakeInjector()
    worker = agent.InputAgent(10, inj, secure=lambda: True)
    assert worker.handle(key())["reason"] == "secure"
    assert worker.handle(mouse(2))["reason"] == "secure"
    assert not inj.log


def test_probe_only_suppresses_uipi_when_service_is_ready(monkeypatch):
    monkeypatch.setattr(agent.winapi, "secure_desktop", lambda: False)
    monkeypatch.setattr(agent.winapi, "foreground_pid", lambda: 42)
    monkeypatch.setattr(agent.winapi, "above_us", lambda pid: True)
    monkeypatch.setattr(agent.winapi, "process_name", lambda pid: "Taskmgr.exe")
    assert _probe(False) == ("uipi", "Taskmgr.exe")
    assert _probe(True) == (None, "")
    monkeypatch.setattr(agent.winapi, "secure_desktop", lambda: True)
    assert _probe(True) == ("secure", "")


def test_developer_or_portable_copy_cannot_install_or_use_service(monkeypatch):
    monkeypatch.setattr(client, "FROZEN", False)
    assert not client.eligible()
    monkeypatch.setattr(runtime.sys, "frozen", False, raising=False)
    with pytest.raises(PermissionError):
        runtime.require_install()


def test_input_queue_overflow_disconnects_instead_of_losing_a_key_up():
    inj = object.__new__(client.ServiceInjector)
    ch = type("C", (), {"active": True, "messages": queue.Queue(maxsize=1), "stop": threading.Event()})()
    inj._channel = ch
    assert inj._remote(key())
    assert inj._remote(key(False))
    assert ch.stop.is_set() and not ch.active
    inj.close_service()
    assert inj._channel is None


def test_real_pipe_roundtrip_peer_identity_and_timeout():
    import win32api
    import win32con
    import win32security
    with ipc.managed(win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32con.TOKEN_QUERY)) as token:
        sid = win32security.ConvertSidToStringSid(win32security.GetTokenInformation(token, win32security.TokenUser)[0])
    name = r"\\.\pipe\MarinRemote.Test." + secrets.token_hex(16)
    server = ipc.create(name, first=True, sid=sid)
    results, errors = [], []

    def serve():
        try:
            server.accept(timeout=3)
            results.append(ipc.peer_pid(server.handle))
            results.append(server.recv())
            server.send({"ok": True})
            with pytest.raises(TimeoutError):
                server.recv(timeout=0.1)
            # Cancellation must drain its OVERLAPPED; the same connection remains usable.
            server.send({"timed_out": True})
        except Exception as e:
            errors.append(e)
        finally:
            server.close()

    t = threading.Thread(target=serve)
    pipe = ipc.connect(name)
    t.start()                            # already connected before ConnectNamedPipe: exercise that race
    try:
        assert ipc.peer_pid(pipe.handle, server=True) == os.getpid()
        pipe.send({"kind": "hello", "version": 1})
        assert pipe.recv() == {"ok": True}
        assert pipe.recv() == {"timed_out": True}
    finally:
        pipe.close()
        t.join(5)
    assert not t.is_alive() and not errors
    assert results == [os.getpid(), {"kind": "hello", "version": 1}]


def test_process_path_uses_a_real_process_handle():
    import sys
    assert ipc.process_path(os.getpid()).resolve() == Path(sys._base_executable).resolve()


def test_server_verification_uses_scm_even_when_system_process_cannot_be_opened(monkeypatch):
    class Handle:
        def Close(self):
            pass
    monkeypatch.setattr(ipc.win32service, "OpenSCManager", lambda *a: Handle())
    monkeypatch.setattr(ipc.win32service, "OpenService", lambda *a: Handle())
    status = {"CurrentState": ipc.win32service.SERVICE_RUNNING, "ProcessId": 123}
    config = [0, 0, 0, f'"{ipc.executable()}" --input-service', "", 0, None, "LocalSystem", ""]
    monkeypatch.setattr(ipc.win32service, "QueryServiceStatusEx", lambda h: status)
    monkeypatch.setattr(ipc.win32service, "QueryServiceConfig", lambda h: config)
    monkeypatch.setattr(ipc, "peer_pid", lambda *a, **kw: 123)
    monkeypatch.setattr(ipc, "process_path", lambda pid: pytest.fail("medium user cannot inspect SYSTEM process"))
    ipc.trusted_server(1)
    status["ProcessId"] = 99
    with pytest.raises(PermissionError):
        ipc.trusted_server(1)
    status["ProcessId"] = 123
    config[3] = r'"C:\Users\Public\MarinRemote.exe" --input-service'
    with pytest.raises(PermissionError):
        ipc.trusted_server(1)


def test_helper_launch_is_fixed_and_failed_resume_kills_suspended_process(monkeypatch):
    import win32profile
    class Handle:
        def __init__(self):
            self.closed = False
        def Close(self):
            self.closed = True
    proc, thread = Handle(), Handle()
    calls = []
    monkeypatch.setattr(win32profile, "CreateEnvironmentBlock", lambda *a: {"PATH":"untrusted", "PYTHONPATH":"x"})
    monkeypatch.setattr(runtime.win32process, "CreateProcessAsUser", lambda *a: (calls.append(a) or (proc, thread, 123, 456)))
    monkeypatch.setattr(runtime.win32process, "TerminateProcess", lambda *a: calls.append("terminated"))
    def resume_fails(*args):
        raise OSError("test resume failure")
    monkeypatch.setattr(runtime.win32process, "ResumeThread", resume_fails)
    with pytest.raises(OSError, match="resume failure"):
        runtime.launch_agent(1, r"\\.\pipe\MarinRemote.Agent.test", 99)
    assert calls[-1] == "terminated" and proc.closed and thread.closed
    args = calls[0]
    assert args[5] is False and args[9].lpDesktop == r"winsta0\default"
    assert "--input-agent" in args[2] and "PYTHONPATH" not in args[7]


def test_real_pipe_authentication_checks_the_observed_image_and_logon(monkeypatch):
    import win32api
    import win32con
    import win32security
    with ipc.managed(win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32con.TOKEN_QUERY)) as tok:
        sid = win32security.ConvertSidToStringSid(win32security.GetTokenInformation(tok, win32security.TokenUser)[0])
        try:
            linked = win32security.GetTokenInformation(tok, win32security.TokenLinkedToken)
        except Exception:
            pytest.skip("the test user has no linked administrator token")
        linked.Close()
    name = r"\\.\pipe\MarinRemote.Test." + secrets.token_hex(16)
    server = ipc.create(name, first=True, sid=sid)
    results, errors = [], []

    def serve():
        try:
            server.accept(timeout=3)
            server.recv()
            with pytest.raises(PermissionError, match="protected"):
                runtime.authenticate(server)   # a Python test process isn't the protected executable
            monkeypatch.setattr(runtime, "executable", lambda: ipc.process_path(os.getpid()))
            proc, token, user = runtime.authenticate(server)  # same logon & elevated token; never launch it
            try:
                results.append((user, win32security.GetTokenInformation(token, win32security.TokenElevation)))
            finally:
                proc.Close()
                token.Close()
            server.send({"ok": True})
        except Exception as e:
            errors.append(e)
        finally:
            server.close()

    t = threading.Thread(target=serve)
    t.start()
    pipe = ipc.connect(name)
    try:
        pipe.send({"kind": "hello"})
        try:
            reply = pipe.recv()
        except Exception:
            t.join(5)
            if errors:
                raise errors[0] from None
            raise
    finally:
        pipe.close()
        t.join(5)
    assert not errors and reply == {"ok": True} and results == [(sid, 1)]
