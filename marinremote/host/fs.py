"""
The host's files as a viewer may reach them: only inside the folders («roots») the person at
this PC allowed, and only through virtual paths — `<root id>/<relative/path>`. A viewer never
sees or sends an absolute path of this PC.

Every path is checked twice:
1. by its text — no "..", ".", empty parts, drive letters, UNC or \\\\?\\ prefixes, device names
   (CON, NUL, COM1…), alternate data streams (":"), characters Windows forbids, trailing dots
   or spaces;
2. by what it really is — the file or folder is opened and the handle's final path
   (GetFinalPathNameByHandle) must lie inside the root's own final path. That catches symbolic
   links and junctions leading out, and there is no gap between checking and using: what was
   checked is the very handle that is then read or written.
"""

import ctypes
import ctypes.wintypes as wt
import msvcrt
import os
import re
import stat as statmod
from pathlib import Path

MAX_PATH_TEXT = 4096
MAX_ENTRIES = 20000
DEVICE = re.compile(r"^(con|prn|aux|nul|conin\$|conout\$|com[0-9¹²³]|lpt[0-9¹²³])(\..*)?$", re.I)
FORBIDDEN = re.compile(r'[<>:"|?*\\/\x00-\x1f]')

GENERIC_READ, GENERIC_WRITE = 0x80000000, 0x40000000
FILE_READ_ATTRIBUTES = 0x80
SHARE_ALL = 0x1 | 0x2 | 0x4
OPEN_EXISTING, OPEN_ALWAYS = 3, 4
FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
FILE_FLAG_SEQUENTIAL_SCAN = 0x08000000
FILE_ATTRIBUTE_HIDDEN = 0x2
INVALID_HANDLE = ctypes.c_void_p(-1).value

_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_k32.CreateFileW.argtypes = [wt.LPCWSTR, wt.DWORD, wt.DWORD, ctypes.c_void_p, wt.DWORD, wt.DWORD, wt.HANDLE]
_k32.CreateFileW.restype = wt.HANDLE
_k32.GetFinalPathNameByHandleW.argtypes = [wt.HANDLE, wt.LPWSTR, wt.DWORD, wt.DWORD]
_k32.CloseHandle.argtypes = [wt.HANDLE]


class FsError(Exception):
    """code: denied | bad_path | not_found | exists | io | not_dir | is_dir"""

    def __init__(self, code, message=""):
        super().__init__(message or code)
        self.code = code
        self.message = message or code


# ── text ───────────────────────────────────────────────────────────
def check_name(name):
    """One file or folder name, as Windows allows it; FsError otherwise."""
    if not isinstance(name, str) or not name or len(name) > 255:
        raise FsError("bad_path", "недопустимое имя")
    if name in (".", "..") or FORBIDDEN.search(name) or name[-1] in ". " or DEVICE.match(name):
        raise FsError("bad_path", f"недопустимое имя «{name}»")
    return name


def split_virtual(vpath):
    """'home/Documents/a.txt' → ('home', ['Documents', 'a.txt']). Both slashes are accepted,
    nothing else that could leave the root."""
    if not isinstance(vpath, str) or not vpath or len(vpath) > MAX_PATH_TEXT:
        raise FsError("bad_path", "недопустимый путь")
    if vpath.startswith(("/", "\\")):
        raise FsError("bad_path", "абсолютные пути не принимаются")
    parts = re.split(r"[\\/]", vpath)
    if parts and parts[-1] == "":                 # "home/Documents/" — a folder, fine
        parts.pop()
    if not parts:
        raise FsError("bad_path", "недопустимый путь")
    root, rest = parts[0], parts[1:]
    if not root or FORBIDDEN.search(root):
        raise FsError("bad_path", "недопустимый корень")
    for p in rest:
        check_name(p)
    return root, rest


def join_virtual(*parts):
    return "/".join(p.strip("/") for p in parts if p)


# ── handles and final paths ────────────────────────────────────────
def _open(path, access, disposition, flags=0, attrs=0):
    h = _k32.CreateFileW(str(path), access, SHARE_ALL, None, disposition, flags | attrs, None)
    if not h or h == INVALID_HANDLE:
        err = ctypes.get_last_error()
        if err in (2, 3):
            raise FsError("not_found", "не найдено")
        if err == 5:
            raise FsError("denied", "нет доступа")
        if err == 32:
            raise FsError("io", "файл занят другой программой")
        raise FsError("io", f"ошибка Windows {err}")
    return h


def final_path(handle):
    buf = ctypes.create_unicode_buffer(1024)
    n = _k32.GetFinalPathNameByHandleW(handle, buf, 1024, 0)
    if n > 1024:
        buf = ctypes.create_unicode_buffer(n + 1)
        n = _k32.GetFinalPathNameByHandleW(handle, buf, n + 1, 0)
    if not n:
        raise FsError("io", "не удалось узнать настоящий путь")
    text = buf.value
    if text.startswith("\\\\?\\UNC\\"):
        return "\\\\" + text[8:]
    if text.startswith("\\\\?\\"):
        return text[4:]
    return text


def final_path_of(path):
    h = _open(path, FILE_READ_ATTRIBUTES, OPEN_EXISTING, FILE_FLAG_BACKUP_SEMANTICS)
    try:
        return final_path(h)
    finally:
        _k32.CloseHandle(h)


def inside(path, root):
    """Is a final path the root itself or below it? (case-insensitive, as Windows is)"""
    p, r = os.path.normcase(path.rstrip("\\")), os.path.normcase(root.rstrip("\\"))
    return p == r or p.startswith(r + "\\")


# ── the roots ──────────────────────────────────────────────────────
class Roots:
    def __init__(self, roots):
        self.items = {}               # id -> {"id", "label", "path", "final"}
        for r in roots or []:
            try:
                final = final_path_of(r["path"])
            except (FsError, KeyError, TypeError):
                continue              # a folder that is gone: simply not offered
            self.items[str(r["id"])] = {"id": str(r["id"]), "label": r.get("label") or r["path"],
                                        "path": final, "final": final}

    def listing(self):
        return [{"id": r["id"], "label": r["label"]} for r in self.items.values()]

    def _root(self, root_id):
        r = self.items.get(root_id)
        if r is None:
            raise FsError("bad_path", "нет такой папки среди разрешённых")
        return r

    def locate(self, vpath):
        """(root, absolute path by text) — not checked against links yet."""
        root_id, parts = split_virtual(vpath)
        r = self._root(root_id)
        return r, Path(r["final"], *parts)

    def checked(self, path, root):
        """The final path of an existing file or folder, if it is inside the root."""
        final = final_path_of(path)
        if not inside(final, root["final"]):
            raise FsError("bad_path", "путь ведёт за пределы разрешённой папки")
        return final

    def resolve(self, vpath):
        """An existing file or folder → its checked final path."""
        r, path = self.locate(vpath)
        return self.checked(path, r)

    def resolve_new(self, vpath):
        """A path to be created: its folder must exist and be inside; the name must be valid.
        → (checked final folder, name, root)."""
        root_id, parts = split_virtual(vpath)
        r = self._root(root_id)
        if not parts:
            raise FsError("bad_path", "нельзя создать сам корень")
        folder = self.checked(Path(r["final"], *parts[:-1]), r)
        if not os.path.isdir(folder):
            raise FsError("not_dir", "это не папка")
        return folder, parts[-1], r

    # ── opening, with the check on the very handle ──────────────────
    def open_read(self, vpath):
        """A file opened for reading (Python file object) and its final path."""
        r, path = self.locate(vpath)
        h = _open(path, GENERIC_READ, OPEN_EXISTING, FILE_FLAG_SEQUENTIAL_SCAN)
        return _adopt(h, r, "rb")

    def open_write(self, folder, name, root, hidden=False):
        """A file in a checked folder, created if missing, opened for read+write."""
        check_name(name)
        h = _open(Path(folder, name), GENERIC_READ | GENERIC_WRITE, OPEN_ALWAYS,
                  attrs=FILE_ATTRIBUTE_HIDDEN if hidden else 0)
        return _adopt(h, root, "r+b")

    # ── folders ─────────────────────────────────────────────────────
    def list(self, vpath):
        final = self.resolve(vpath)
        if not os.path.isdir(final):
            raise FsError("not_dir", "это не папка")
        out = []
        try:
            with os.scandir(final) as it:
                for e in it:
                    if len(out) >= MAX_ENTRIES:
                        break
                    try:
                        st = e.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    attrs = getattr(st, "st_file_attributes", 0)
                    item = {"n": e.name, "d": e.is_dir(), "s": 0 if e.is_dir() else st.st_size,
                            "m": int(st.st_mtime)}
                    if attrs & (statmod.FILE_ATTRIBUTE_HIDDEN | statmod.FILE_ATTRIBUTE_SYSTEM):
                        item["h"] = True
                    if attrs & statmod.FILE_ATTRIBUTE_REPARSE_POINT:
                        item["l"] = True
                    out.append(item)
        except PermissionError:
            raise FsError("denied", "нет доступа к папке") from None
        return out

    def mkdir(self, vpath):
        folder, name, _ = self.resolve_new(vpath)
        target = os.path.join(folder, check_name(name))
        try:
            os.mkdir(target)
        except FileExistsError:
            if not os.path.isdir(target):
                raise FsError("exists", "уже есть файл с таким именем") from None
        except PermissionError:
            raise FsError("denied", "нет доступа") from None
        except OSError as e:
            raise FsError("io", str(e)) from None

    def rename(self, vpath, new_name):
        r, _ = self.locate(vpath)
        if not split_virtual(vpath)[1]:
            raise FsError("bad_path", "корень не переименовывается")
        final = self.resolve(vpath)
        target = os.path.join(os.path.dirname(final), check_name(new_name))
        if not inside(os.path.dirname(target), r["final"]):
            raise FsError("bad_path", "путь ведёт за пределы разрешённой папки")
        if os.path.exists(target) and os.path.normcase(target) != os.path.normcase(final):
            raise FsError("exists", "такое имя уже есть")
        try:
            os.rename(final, target)
        except PermissionError:
            raise FsError("denied", "нет доступа") from None
        except OSError as e:
            raise FsError("io", str(e)) from None

    def delete(self, vpath):
        """Into the Recycle Bin, never for good."""
        if not split_virtual(vpath)[1]:
            raise FsError("bad_path", "корень не удаляется")
        recycle(self.resolve(vpath))


def _adopt(handle, root, mode):
    """Check a fresh handle's final path, then hand it to Python as a file object."""
    try:
        final = final_path(handle)
        if not inside(final, root["final"]):
            raise FsError("bad_path", "путь ведёт за пределы разрешённой папки")
        fd = msvcrt.open_osfhandle(handle, os.O_BINARY | (os.O_RDONLY if mode == "rb" else os.O_RDWR))
    except BaseException:
        _k32.CloseHandle(handle)
        raise
    return os.fdopen(fd, mode), final


class _SHFILEOPSTRUCTW(ctypes.Structure):
    _fields_ = [("hwnd", wt.HWND), ("wFunc", wt.UINT), ("pFrom", wt.LPCWSTR), ("pTo", wt.LPCWSTR),
                ("fFlags", ctypes.c_uint16), ("fAnyOperationsAborted", wt.BOOL),
                ("hNameMappings", ctypes.c_void_p), ("lpszProgressTitle", wt.LPCWSTR)]


def recycle(path):
    FO_DELETE = 3
    FOF_SILENT, FOF_NOCONFIRMATION, FOF_ALLOWUNDO, FOF_NOERRORUI = 0x4, 0x10, 0x40, 0x400
    op = _SHFILEOPSTRUCTW(None, FO_DELETE, str(path) + "\0", None,
                          FOF_SILENT | FOF_NOCONFIRMATION | FOF_ALLOWUNDO | FOF_NOERRORUI, False, None, None)
    rc = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
    if rc != 0 or op.fAnyOperationsAborted:
        raise FsError("io", f"не удалось удалить (код {rc})")
