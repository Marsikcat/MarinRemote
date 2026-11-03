"""Local, bounded JSON messages over named pipes, with cancellable I/O and peer PID checks."""

import ctypes
from contextlib import contextmanager
import json
import struct
import time
from pathlib import Path

import pywintypes
import win32api
import win32con
import win32event
import win32file
import win32pipe
import win32security
import win32service

from . import MAX_MESSAGE, NAME


def executable():
    # Do not trust a client's environment or a path supplied in a protocol message.
    folder = ctypes.create_unicode_buffer(32768)
    if ctypes.windll.shell32.SHGetFolderPathW(None, 0x26, None, 0, folder) != 0:   # CSIDL_PROGRAM_FILES
        raise OSError("cannot locate Program Files")
    return Path(folder.value) / "MarinRemote" / "MarinRemote.exe"


def process_path(pid):
    with managed(win32api.OpenProcess(0x1000, False, pid)) as proc:
        return Path(win32process_name(proc))


def win32process_name(proc):
    ctypes.windll.kernel32.QueryFullProcessImageNameW.argtypes = [ctypes.c_void_p, ctypes.c_ulong,
        ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_ulong)]
    buf = ctypes.create_unicode_buffer(32768)
    size = ctypes.c_ulong(len(buf))
    if not ctypes.windll.kernel32.QueryFullProcessImageNameW(int(proc), 0, buf, ctypes.byref(size)):
        raise OSError("cannot inspect pipe peer")
    return buf.value


def peer_pid(handle, server=False):
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    fn = k32.GetNamedPipeServerProcessId if server else k32.GetNamedPipeClientProcessId
    fn.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
    fn.restype = ctypes.c_int
    pid = ctypes.c_ulong()
    if not fn(int(handle), ctypes.byref(pid)):
        raise ctypes.WinError(ctypes.get_last_error())
    return pid.value


def trusted_server(handle):
    """SCM, not a name alone, identifies the server. A user cannot impersonate the service."""
    with managed(win32service.OpenSCManager(None, None, win32service.SC_MANAGER_CONNECT)) as scm:
        access = win32service.SERVICE_QUERY_STATUS | win32service.SERVICE_QUERY_CONFIG
        with managed(win32service.OpenService(scm, NAME, access)) as svc:
            status = win32service.QueryServiceStatusEx(svc)
            config = win32service.QueryServiceConfig(svc)
    pid = peer_pid(handle, server=True)
    expected = f'"{executable()}" --input-service'
    if (status["CurrentState"] != win32service.SERVICE_RUNNING or status["ProcessId"] != pid
            or config[3].strip().casefold() != expected.casefold() or config[7].casefold() != "localsystem"):
        raise PermissionError("unexpected input service")


def create(name, first=False, sid=None):
    # Explicit DACL; network access is rejected by the kernel even for an otherwise allowed SID.
    sddl = "D:P(A;;GA;;;SY)(A;;GA;;;BA)"
    if sid:
        sddl += f"(A;;0x12019b;;;{sid})"  # read/write data, never FILE_CREATE_PIPE_INSTANCE
    else:
        sddl += "(A;;0x12019b;;;IU)"   # authentication still requires the linked admin token
    sa = pywintypes.SECURITY_ATTRIBUTES()
    sa.SECURITY_DESCRIPTOR = win32security.ConvertStringSecurityDescriptorToSecurityDescriptor(sddl, 1)
    flags = win32pipe.PIPE_ACCESS_DUPLEX | win32file.FILE_FLAG_OVERLAPPED
    if first:
        flags |= 0x00080000     # FILE_FLAG_FIRST_PIPE_INSTANCE: fail closed on pipe squatting
    h = win32pipe.CreateNamedPipe(name, flags, win32pipe.PIPE_TYPE_BYTE | 0x8, 16, 65536, 65536, 0, sa)
    return Pipe(h)


def connect(name):
    flags = win32file.FILE_FLAG_OVERLAPPED | 0x00100000 | 0x00010000  # SQOS: identification only
    h = win32file.CreateFile(name, win32con.GENERIC_READ | win32con.GENERIC_WRITE, 0, None,
                            win32con.OPEN_EXISTING, flags, None)
    return Pipe(h)


class Pipe:
    def __init__(self, handle):
        self.handle = handle

    def close(self):
        h, self.handle = self.handle, None
        if h is not None:
            h.Close()

    def _io(self, op, timeout, stop=None):
        ov = pywintypes.OVERLAPPED()
        ov.hEvent = win32event.CreateEvent(None, True, False, None)
        try:
            try:
                result = op(ov)
            except pywintypes.error as e:
                if e.winerror == 535:     # ERROR_PIPE_CONNECTED
                    return None
                raise
            if result == 535:           # pywin32 also returns this code without raising
                return None
            deadline = time.monotonic() + timeout
            while win32event.WaitForSingleObject(ov.hEvent, 50) == win32event.WAIT_TIMEOUT:
                if time.monotonic() >= deadline or (stop is not None and stop.is_set()):
                    cancel(self.handle, ov)
                    try:
                        win32file.GetOverlappedResult(self.handle, ov, True)
                    except pywintypes.error:
                        pass
                    raise TimeoutError("input pipe timed out")
            count = win32file.GetOverlappedResult(self.handle, ov, True)
            return result, count
        finally:
            ov.hEvent.Close()

    def accept(self, timeout=60, stop=None):
        self._io(lambda ov: win32pipe.ConnectNamedPipe(self.handle, ov), timeout, stop)

    def _read(self, n, deadline):
        out = bytearray()
        while len(out) < n:
            remaining = n - len(out)
            result, count = self._io(lambda ov, size=remaining: win32file.ReadFile(self.handle, size, ov),
                                     max(0.01, deadline - time.monotonic()))
            if not count:
                raise EOFError("input pipe closed")
            out += bytes(result[1][:count])
        return bytes(out)

    def recv(self, timeout=3):
        deadline = time.monotonic() + timeout
        size = struct.unpack("<I", self._read(4, deadline))[0]
        if not 0 < size <= MAX_MESSAGE:
            raise ValueError("input message too large")
        msg = json.loads(self._read(size, deadline))
        if not isinstance(msg, dict):
            raise ValueError("input message is not an object")
        return msg

    def send(self, msg, timeout=1):
        data = json.dumps(msg, ensure_ascii=True, separators=(",", ":")).encode()
        if not 0 < len(data) <= MAX_MESSAGE:
            raise ValueError("input message too large")
        data = struct.pack("<I", len(data)) + data
        _, count = self._io(lambda ov: win32file.WriteFile(self.handle, data, ov), timeout)
        if count != len(data):
            raise OSError("short input pipe write")


def cancel(handle, overlapped):
    # Each endpoint has one I/O operation at a time, cancelled from the thread which issued it.
    win32file.CancelIo(handle)


@contextmanager
def managed(handle):
    try:
        yield handle
    finally:
        handle.Close()

