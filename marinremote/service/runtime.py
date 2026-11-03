"""SCM broker: authenticate a protected local client, launch its elevated input-only helper.

Never executes a client-supplied command or path and never launches a SYSTEM desktop process.
The logged-in administrator must have a linked elevated token. Standard users are refused.
"""

import logging
import os
from pathlib import Path
import secrets
import subprocess
import sys
import threading

import pywintypes
import servicemanager
import win32api
import win32con
import win32event
import win32process
import win32security
import win32service
import win32serviceutil

from . import AGENT_PREFIX, NAME, PIPE
from .ipc import create, executable, managed, peer_pid, win32process_name


def require_install():
    if not getattr(sys, "frozen", False):
        raise PermissionError("Служба устанавливается только из собранной программы.")
    expected = executable()
    if Path(sys.executable).resolve() != expected:
        raise PermissionError("Служба работает только из установленной в Program Files версии MarinRemote.")
    # Program Files itself is protected. A link below it must not redirect the broker to user code.
    for path in (expected.parent, expected.parent / "_internal", expected):
        if path.is_symlink() or (getattr(path.lstat(), "st_file_attributes", 0) & 0x400):
            raise PermissionError("Служба не работает из папки-ссылки.")
        sd = win32security.GetNamedSecurityInfo(str(path), win32security.SE_FILE_OBJECT,
                                               win32security.DACL_SECURITY_INFORMATION)
        dacl = sd.GetSecurityDescriptorDacl()
        if dacl is None:
            raise PermissionError("У папки службы отсутствуют ограничения доступа.")
        for i in range(dacl.GetAceCount()):
            ace = dacl.GetAce(i)
            (kind, flags), mask, sid = ace[:3]
            sid = win32security.ConvertSidToStringSid(sid)
            # Ignore inherit-only rules; executable code must be writable only by administrators,
            # SYSTEM or the Windows installer identity, never by the interactive user token.
            trusted = sid in ("S-1-5-18", "S-1-5-32-544") or sid.startswith("S-1-5-80-")
            writes = 0x10000000 | 0x40000000 | 0xD0116
            if kind == win32security.ACCESS_ALLOWED_ACE_TYPE and not flags & 0x8 and mask & writes and not trusted:
                raise PermissionError("Папка службы доступна для записи без повышения прав.")


def authenticate(pipe):
    """The observed process and the pipe's impersonated identity must be the same logon."""
    proc = win32api.OpenProcess(0x1000 | win32con.SYNCHRONIZE, False, peer_pid(pipe.handle))
    token = win32security.OpenProcessToken(proc, win32con.TOKEN_QUERY | win32con.TOKEN_DUPLICATE)
    try:
        expected = executable()
        if Path(win32process_name(proc)).resolve() != expected:
            raise PermissionError("only the protected installed MarinRemote may use the broker")
        user = win32security.GetTokenInformation(token, win32security.TokenUser)[0]
        sid = win32security.ConvertSidToStringSid(user)
        session = win32security.GetTokenInformation(token, win32security.TokenSessionId)
        auth_id = win32security.GetTokenInformation(token, win32security.TokenStatistics)["AuthenticationId"]
        win32security.ImpersonateNamedPipeClient(pipe.handle)
        try:
            with managed(win32security.OpenThreadToken(win32api.GetCurrentThread(), win32con.TOKEN_QUERY, True)) as peer:
                peer_user = win32security.GetTokenInformation(peer, win32security.TokenUser)[0]
                peer_auth = win32security.GetTokenInformation(peer, win32security.TokenStatistics)["AuthenticationId"]
                if sid != win32security.ConvertSidToStringSid(peer_user) or auth_id != peer_auth:
                    raise PermissionError("pipe identity differs from the observed process")
        finally:
            win32security.RevertToSelf()
        if not session:
            raise PermissionError("an interactive session is required")
        # A filtered administrator's linked token, NOT the service's SYSTEM token.
        if win32security.GetTokenInformation(token, win32security.TokenElevation):
            elevated = win32security.DuplicateTokenEx(token, win32security.SecurityImpersonation,
                                                      win32con.TOKEN_ALL_ACCESS, win32security.TokenPrimary)
        else:
            elevated = win32security.GetTokenInformation(token, win32security.TokenLinkedToken)
        try:
            if (not win32security.GetTokenInformation(elevated, win32security.TokenElevation)
                    or win32security.GetTokenInformation(elevated, win32security.TokenSessionId) != session
                    or sid != win32security.ConvertSidToStringSid(
                        win32security.GetTokenInformation(elevated, win32security.TokenUser)[0])):
                raise PermissionError("the linked token is not this user's elevated session")
        except Exception:
            elevated.Close()
            raise
        return proc, elevated, sid
    except Exception:
        proc.Close()
        raise
    finally:
        token.Close()


def launch_agent(token, pipe_name, host_pid):
    exe = str(executable())
    si = win32process.STARTUPINFO()
    si.lpDesktop = r"winsta0\default"
    si.dwFlags = win32con.STARTF_USESHOWWINDOW
    si.wShowWindow = win32con.SW_HIDE
    # Fixed CWD and a system-built environment: never inherit PYTHONPATH or DLL paths from the client.
    import win32profile
    env = win32profile.CreateEnvironmentBlock(token, False)
    for name in list(env):
        if name.upper().startswith(("PYTHON", "MARINREMOTE", "QT_")):
            del env[name]
    env["PATH"] = str(Path(os.environ["SystemRoot"]) / "System32")
    cmd = subprocess.list2cmdline([exe, "--input-agent", pipe_name, str(host_pid)])
    proc = thread = None
    try:
        proc, thread, pid, _ = win32process.CreateProcessAsUser(
            token, exe, cmd, None, None, False,
            win32con.CREATE_NO_WINDOW | win32con.CREATE_UNICODE_ENVIRONMENT | win32con.CREATE_SUSPENDED,
            env, str(executable().parent), si)
        win32process.ResumeThread(thread)
    except Exception:
        if proc:
            win32process.TerminateProcess(proc, 1)
            proc.Close()
        raise
    finally:
        if thread:
            thread.Close()
    return proc, pid


def serve_client(client, stop):
    host = token = agent_proc = agent = None
    stage = "hello"
    try:
        if client.recv(timeout=2) != {"kind": "hello", "version": 1}:
            raise ValueError("invalid hello")
        stage = "authenticate"
        host, token, sid = authenticate(client)
        pid = win32process.GetProcessId(host)
        name = AGENT_PREFIX + secrets.token_hex(16)
        agent = create(name, first=True, sid=sid)
        stage = "launch helper"
        agent_proc, helper_pid = launch_agent(token, name, pid)
        stage = "helper handshake"
        agent.accept(timeout=10, stop=stop)
        hello = agent.recv(timeout=2)
        if hello.get("kind") == "agent.error":
            raise RuntimeError(hello.get("error", "helper failed"))
        if peer_pid(agent.handle) != helper_pid or hello != {"kind": "agent"}:
            raise PermissionError("unexpected helper")
        client.send({"ok": True, "version": 1})
        while not stop.is_set() and win32event.WaitForSingleObject(host, 0) == win32event.WAIT_TIMEOUT:
            stage = "client input"
            msg = client.recv(timeout=3)
            stage = "helper input"
            agent.send(msg)
            client.send(agent.recv(timeout=1))
    except Exception as exc:
        logging.info("Input connection ended: %s", type(exc).__name__)
        servicemanager.LogInfoMsg(f"MarinRemote input connection ({stage}): {type(exc).__name__}: {exc}")
        try:
            client.send({"ok": False, "reason": "unavailable"}, timeout=0.2)
        except Exception:
            pass
    finally:
        if agent:
            agent.close()              # EOF: helper releases all keys before it exits
        if agent_proc:
            if win32event.WaitForSingleObject(agent_proc, 1500) == win32event.WAIT_TIMEOUT:
                win32process.TerminateProcess(agent_proc, 1)
            agent_proc.Close()
        # On service death, Windows closes its pipe and the helper releases input and exits on
        # EOF (or the 3 s watchdog). Job objects cannot span session 0 and the interactive session.
        if token:
            token.Close()
        if host:
            host.Close()
        client.close()


class InputBroker(win32serviceutil.ServiceFramework):
    _svc_name_ = NAME
    _svc_display_name_ = "MarinRemote — управление окнами администратора"
    _svc_description_ = "Передаёт разрешённый ввод помощнику в сеансе пользователя. Без сетевого доступа."

    def __init__(self, args):
        super().__init__(args)
        self.stop = threading.Event()

    def SvcStop(self):
        self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
        self.stop.set()

    def SvcDoRun(self):
        require_install()
        workers = []
        first = True
        while not self.stop.is_set():
            pipe = create(PIPE, first=first)
            try:
                pipe.accept(timeout=60, stop=self.stop)
            except TimeoutError:
                pipe.close()
                continue
            first = False
            workers = [w for w in workers if w.is_alive()]
            if len(workers) >= 8:
                pipe.close()
                continue
            t = threading.Thread(target=serve_client, args=(pipe, self.stop), daemon=True)
            workers.append(t)
            t.start()
        for w in workers:
            w.join(timeout=5)


def run():
    servicemanager.Initialize()
    servicemanager.PrepareToHostSingle(InputBroker)
    servicemanager.StartServiceCtrlDispatcher()


def install():
    require_install()
    try:
        win32serviceutil.InstallService("marinremote.service.runtime.InputBroker", NAME,
            InputBroker._svc_display_name_, startType=win32service.SERVICE_AUTO_START,
            exeName=str(executable()), exeArgs="--input-service", description=InputBroker._svc_description_)
    except pywintypes.error as e:
        if e.winerror != 1073:   # already exists
            raise
        win32serviceutil.ChangeServiceConfig("marinremote.service.runtime.InputBroker", NAME,
            exeName=str(executable()), exeArgs="--input-service", startType=win32service.SERVICE_AUTO_START)
    try:
        win32serviceutil.StartService(NAME)
    except pywintypes.error as e:
        if e.winerror != 1056:
            raise
    win32serviceutil.WaitForServiceStatus(NAME, win32service.SERVICE_RUNNING, 20)


def remove():
    require_install()
    try:
        win32serviceutil.StopService(NAME)
        win32serviceutil.WaitForServiceStatus(NAME, win32service.SERVICE_STOPPED, 20)
    except pywintypes.error as e:
        if e.winerror not in (1060, 1062):
            raise
    try:
        win32serviceutil.RemoveService(NAME)
    except pywintypes.error as e:
        if e.winerror != 1060:
            raise
