"""
The host's side of files (the session has already checked the "files" right for every message
and stream that gets here): folder listings and changes inside the allowed folders, downloads
(this PC → the viewer) and uploads (the viewer → this PC) with resume, SHA-256 and cancel.
Every finished, broken off or refused transfer goes into the journal.
"""

import asyncio
import hashlib
import json
import os
import shutil
import time

from .. import protocol as P
from ..net import stop_stream
from ..transfer import PRIORITY, host_temp_name, receive_file, send_file, unique_name
from .core import Service
from .fs import FsError, Roots, check_name, drive_mask, drive_roots

MAX_ACTIVE = 8          # transfers at once per session


class _Put:
    def __init__(self, x, vpath, folder, name, root, temp, f, size, mtime, overwrite, offset):
        self.x, self.vpath, self.folder, self.name, self.root = x, vpath, folder, name, root
        self.temp, self.f, self.size, self.mtime = temp, f, size, mtime
        self.overwrite, self.offset = overwrite, offset
        self.sha_sender = None
        self.sha_here = None
        self.complete = False
        self.task = None


class FilesService(Service):
    def __init__(self, core):
        self.core, self.net, self.s = core, core.net, core.s
        self._roots = (None, None)
        self.gets = {}           # (sid, x) -> sending task
        self.puts = {}           # (sid, x) -> _Put
        self.cancelled = set()   # (sid, x) the viewer cancelled (rather than lost)

    def roots(self):
        """The allowed folders — and, if the person here allowed it, every drive there is now."""
        every = bool(self.s["files_all_drives"])
        snapshot = json.dumps([self.s["roots"] or [], every and drive_mask()], sort_keys=True)
        if self._roots[0] != snapshot:
            items = list(self.s["roots"] or [])
            if every:
                taken = {os.path.normcase(r["path"].rstrip("\\")) for r in items}
                items += [d for d in drive_roots() if os.path.normcase(d["path"].rstrip("\\")) not in taken]
            self._roots = (snapshot, Roots(items))
        return self._roots[1]

    # ── messages ────────────────────────────────────────────────────
    def on_message(self, s, msg):
        t = msg["t"]
        if t.startswith("fs."):
            self.net.spawn(self._fs(s, msg))
        elif t == "get":
            self._start(s, msg, self._get(s, msg))
        elif t == "put":
            self._start(s, msg, self._put(s, msg))
        elif t == "put.end":
            p = self.puts.get((s.sid, msg.get("x")))
            if p is not None and isinstance(msg.get("sha256"), str):
                p.sha_sender = msg["sha256"].lower()
                self._finish_put(s, p)
        elif t == "xfer.cancel":
            self._cancel(s, msg.get("x"))
        else:
            return False
        return True

    def _start(self, s, msg, coro):
        x = msg.get("x")
        active = sum(1 for (sid, _) in list(self.gets) + list(self.puts) if sid == s.sid)
        if not isinstance(x, str) or not 0 < len(x) <= 64 or (s.sid, x) in self.gets or (s.sid, x) in self.puts:
            coro.close()
            return
        if active >= MAX_ACTIVE:
            coro.close()
            s.post({"t": "xfer.error", "x": x, "code": "busy", "msg": "слишком много передач сразу"})
            return
        task = self.net.spawn(coro)
        if msg["t"] == "get":
            self.gets[(s.sid, x)] = task

    async def _fs(self, s, msg):
        rid = msg.get("id")
        t = msg["t"]
        roots = self.roots()
        try:
            if t == "fs.roots":
                result = {"roots": roots.listing(), "all_drives": bool(self.s["files_all_drives"])}
            elif t == "fs.list":
                result = {"path": msg.get("path"), "entries": await asyncio.to_thread(roots.list, msg.get("path"))}
            elif t == "fs.mkdir":
                await asyncio.to_thread(roots.mkdir, msg.get("path"))
                result = {}
            elif t == "fs.rename":
                await asyncio.to_thread(roots.rename, msg.get("path"), msg.get("name"))
                result = {}
            elif t == "fs.delete":
                await asyncio.to_thread(roots.delete, msg.get("path"))
                result = {}
            else:
                raise FsError("bad_path", "неизвестная операция")
            if t in ("fs.mkdir", "fs.rename", "fs.delete"):
                self.core.journal.write("fs", s.key, name=s.name, action=t[3:], path=msg.get("path"),
                                        to=msg.get("name"))
            s.post({"t": "r", "id": rid, "ok": True, **result})
        except FsError as e:
            s.post({"t": "r", "id": rid, "ok": False, "code": e.code, "msg": e.message})
        except Exception as e:
            s.post({"t": "r", "id": rid, "ok": False, "code": "io", "msg": str(e)})

    # ── this PC → the viewer ────────────────────────────────────────
    async def _get(self, s, msg):
        x, vpath = msg["x"], msg.get("path")
        offset = msg.get("offset") if isinstance(msg.get("offset"), int) and msg.get("offset") >= 0 else 0
        f = stream = None
        sent = 0
        size = 0
        result = "interrupted"
        try:
            f, _ = await asyncio.to_thread(self.roots().open_read, vpath)
            st = os.fstat(f.fileno())
            size, mtime = st.st_size, int(st.st_mtime)
            if offset > size or (offset and (msg.get("size") != size or msg.get("mtime") != mtime)):
                raise FsError("changed", "файл изменился с прошлого раза — начинаем заново")
            s.post({"t": "get.ok", "x": x, "size": size, "mtime": mtime})
            stream = await s.conn.open_uni()
            await stream.set_priority(PRIORITY)
            await stream.write_all(P.stream_header("file", s.sid, x=x, offset=offset))
            h = hashlib.sha256()

            def progress(n):
                nonlocal sent
                sent += n
            await send_file(f, offset, size, stream, h, progress)
            s.post({"t": "xfer.done", "x": x, "sha256": h.hexdigest()})
            result = "ok"
            self.core.journal.write("file_get", s.key, name=s.name, path=vpath, size=size, sha256=h.hexdigest(),
                                    offset=offset or None, result="ok")
        except FsError as e:
            result = e.code
            s.post({"t": "xfer.error", "x": x, "code": e.code, "msg": e.message})
        except asyncio.CancelledError:
            result = "cancelled" if (s.sid, x) in self.cancelled else "interrupted"
            self.cancelled.discard((s.sid, x))
            if stream is not None:
                self.net.spawn(_reset(stream))
            raise
        except Exception:
            pass                                   # the connection went: the viewer will be back for the rest
        finally:
            self.gets.pop((s.sid, x), None)
            if f is not None:
                f.close()
            if result != "ok":
                self.core.journal.write("file_get", s.key, name=s.name, path=vpath, size=size or None,
                                        sent=sent, offset=offset or None, result=result)

    # ── the viewer → this PC ────────────────────────────────────────
    async def _put(self, s, msg):
        x, vpath = msg["x"], msg.get("path")
        size, mtime = msg.get("size"), msg.get("mtime")
        try:
            if not isinstance(size, int) or size < 0 or not isinstance(mtime, int):
                raise FsError("bad_path", "размер или время файла не указаны")
            roots = self.roots()
            folder, name, root = await asyncio.to_thread(roots.resolve_new, vpath)
            check_name(name)
            await asyncio.to_thread(_sweep_parts, folder)
            temp = host_temp_name(s.key, name, size, mtime)
            f, temp_final = await asyncio.to_thread(roots.open_write, folder, temp, root, True)
            have = os.fstat(f.fileno()).st_size
            if have > size:
                f.truncate(0)
                have = 0
            free = shutil.disk_usage(folder).free
            if free < size - have:
                f.close()
                raise FsError("space", "на удалённом ПК не хватает места")
            self.puts[(s.sid, x)] = _Put(x, vpath, folder, name, root, temp_final, f, size, mtime,
                                         bool(msg.get("overwrite")), have)
            s.post({"t": "put.ok", "x": x, "offset": have})
        except FsError as e:
            s.post({"t": "xfer.error", "x": x, "code": e.code, "msg": e.message})
            self.core.journal.write("file_put", s.key, name=s.name, path=vpath, size=size, result=e.code)

    async def on_stream(self, s, header, rest, rs):
        if header.get("t") != "file":
            return False
        p = self.puts.get((s.sid, header.get("x")))
        if p is None or header.get("offset") != p.offset or p.task is not None:
            await stop_stream(rs)
            return True
        p.task = asyncio.current_task()
        h = hashlib.sha256()
        try:
            ok = await receive_file(p.f, p.offset, p.size, rs, rest, h)
        except asyncio.CancelledError:             # the session ended (or the viewer cancelled)
            ok = False
        if self.puts.get((s.sid, p.x)) is not p:
            return True                            # cancelled: already thrown away
        if ok:
            p.complete = True
            p.sha_here = h.hexdigest()
            self._finish_put(s, p)
        else:                                      # broke off: the part stays for a resume
            self._drop_put(s, p, keep=True)
            self.core.journal.write("file_put", s.key, name=s.name, path=p.vpath, size=p.size,
                                    received=os.path.getsize(p.temp) if os.path.exists(p.temp) else None,
                                    result="interrupted")
        return True

    def _finish_put(self, s, p):
        if not (p.complete and p.sha_sender):
            return
        self.puts.pop((s.sid, p.x), None)
        p.f.close()
        if p.sha_sender != p.sha_here:
            _remove(p.temp)
            s.post({"t": "xfer.error", "x": p.x, "code": "hash", "msg": "контрольная сумма не совпала — файл не сохранён"})
            self.core.journal.write("file_put", s.key, name=s.name, path=p.vpath, size=p.size, result="hash")
            return
        try:
            self.roots().checked(p.folder, p.root)
            target = p.name if p.overwrite else unique_name(p.folder, p.name)
            final = os.path.join(p.folder, target)
            os.replace(p.temp, final)
            _unhide(final)
        except (FsError, OSError) as e:
            s.post({"t": "xfer.error", "x": p.x, "code": "io", "msg": str(getattr(e, "message", e))})
            self.core.journal.write("file_put", s.key, name=s.name, path=p.vpath, size=p.size, result="io")
            return
        s.post({"t": "xfer.done", "x": p.x, "sha256": p.sha_here, "name": target})
        self.core.journal.write("file_put", s.key, name=s.name, path=p.vpath.rsplit("/", 1)[0] + "/" + target,
                                size=p.size, sha256=p.sha_here, offset=p.offset or None, result="ok")

    def _drop_put(self, s, p, keep):
        self.puts.pop((s.sid, p.x), None)
        try:
            p.f.close()
        except OSError:
            pass
        if not keep:
            _remove(p.temp)

    def _cancel(self, s, x):
        task = self.gets.get((s.sid, x))
        if task is not None:
            self.cancelled.add((s.sid, x))
            task.cancel()
        p = self.puts.get((s.sid, x))
        if p is not None:
            if p.task is not None:
                p.task.cancel()
            self._drop_put(s, p, keep=False)
            self.core.journal.write("file_put", s.key, name=s.name, path=p.vpath, size=p.size, result="cancelled")

    def rights_changed(self, s, old):
        if "files" in old and not s.has("files"):
            self.session_ended(s)          # transfers stop; the parts stay for later

    def session_ended(self, s):
        for (sid, _), task in list(self.gets.items()):
            if sid == s.sid:
                task.cancel()
        for (sid, _), p in list(self.puts.items()):
            if sid != s.sid:
                continue
            if p.task is not None and not p.task.done():
                p.task.cancel()                    # its receiver keeps the part and notes it
            else:
                self._drop_put(s, p, keep=True)


async def _reset(stream):
    try:
        await stream.reset(0)
    except Exception:
        pass


PART_KEEP_DAYS = 7


def _sweep_parts(folder):
    """Parts of uploads nobody came back for in a week are thrown away."""
    cutoff = time.time() - PART_KEEP_DAYS * 86400
    try:
        with os.scandir(folder) as it:
            for e in it:
                if e.name.startswith(".") and e.name.endswith(".mrpart") and e.is_file(follow_symlinks=False):
                    try:
                        if e.stat(follow_symlinks=False).st_mtime < cutoff:
                            os.remove(e.path)
                    except OSError:
                        pass
    except OSError:
        pass


def _remove(path):
    try:
        os.remove(path)
    except OSError:
        pass


def _unhide(path):
    try:
        import ctypes
        ctypes.windll.kernel32.SetFileAttributesW(str(path), 0x80)        # FILE_ATTRIBUTE_NORMAL
    except (OSError, AttributeError):
        pass
