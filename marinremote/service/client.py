"""The ordinary host's input backend. The service is used only for an authorized control session."""

from pathlib import Path
import queue
import sys
import threading

from ..config import FROZEN, INSTANCE
from ..host.inject import Injector
from . import NAME, PIPE


def eligible():
    if not FROZEN or INSTANCE:
        return False
    try:
        from .ipc import executable
        return Path(sys.executable).resolve() == executable().resolve()
    except (ImportError, OSError):
        return False


def status_text():
    try:
        import win32service
        from .ipc import managed
        with managed(win32service.OpenSCManager(None, None, win32service.SC_MANAGER_CONNECT)) as scm:
            with managed(win32service.OpenService(scm, NAME, win32service.SERVICE_QUERY_STATUS)) as svc:
                running = win32service.QueryServiceStatusEx(svc)["CurrentState"] == win32service.SERVICE_RUNNING
        if not eligible():
            return "Служба установлена. Запустите MarinRemote из Program Files, чтобы использовать её."
        return ("Служба запущена: ввод в окна администратора доступен для учётной записи администратора."
                if running else "Служба установлена, но остановлена. Запустите её в службах Windows.")
    except Exception:
        return ("Служба не установлена. Для управления Диспетчером задач установите версию «Admin» "
                "на этом ПК с подтверждением администратора.")


class Channel:
    """One worker owns its pipe. Queued keys stay ordered; broken connections are never replayed."""
    def __init__(self):
        self.stop = threading.Event()
        self.messages = queue.Queue(maxsize=256)
        self.active = False
        self.last_error = None
        self.thread = threading.Thread(target=self.run, daemon=True, name="privileged-input")
        self.thread.start()

    def run(self):
        from .ipc import connect, trusted_server
        while not self.stop.is_set():
            pipe = None
            try:
                # Discard the previous connection's backlog before becoming active again.
                while not self.messages.empty():
                    self.messages.get_nowait()
                pipe = connect(PIPE)
                trusted_server(pipe.handle)
                pipe.send({"kind": "hello", "version": 1})
                if pipe.recv(timeout=10) != {"ok": True, "version": 1}:
                    raise PermissionError("service refused the user")
                if self.stop.is_set():
                    break
                self.active = True
                self.last_error = None
                while not self.stop.is_set():
                    try:
                        msg = self.messages.get(timeout=0.5)
                    except queue.Empty:
                        msg = {"kind": "ping"}
                    pipe.send(msg, timeout=0.25)
                    pipe.recv(timeout=0.25)
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
                if error != self.last_error:
                    print(f"input service unavailable: {error}", flush=True)
                    self.last_error = error
            finally:
                self.active = False
                if pipe:
                    pipe.close()           # helper releases everything on EOF, crash or watchdog timeout
            self.stop.wait(3)


class ServiceInjector(Injector):
    def __init__(self):
        super().__init__()
        self._channel = None
        self._eligible = eligible()

    def service_active(self):
        if not self._eligible:
            return False
        if self._channel is None:
            self._channel = Channel()
        return self._channel.active

    def _remote(self, msg):
        ch = self._channel
        if ch is None or not ch.active:
            return False
        try:
            ch.messages.put_nowait(msg)
        except queue.Full:
            # Never lose a key-up silently while keeping a helper alive with the key held.
            ch.active = False
            ch.stop.set()
            self._channel = None
        return True

    def mouse(self, px, py, flags=0, data=0, move=True):
        if not self._remote({"kind": "mouse", "x": px, "y": py, "flags": flags, "data": data, "move": move}):
            super().mouse(px, py, flags, data, move)

    def key(self, scan, ext, down, vk):
        if not self._remote({"kind": "key", "scan": scan, "ext": bool(ext), "down": down, "vk": vk}):
            super().key(scan, ext, down, vk)

    def close_service(self):
        ch, self._channel = self._channel, None
        if ch:
            ch.active = False
            ch.stop.set()
