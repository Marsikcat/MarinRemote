"""
The remote PC's files, from the viewer's side: folder requests with answers, and transfers both
ways — at most two at a time, each on a stream of its own.

A download goes into «name.mrpart» next to where it belongs, with «name.mrpart.json» beside it
saying what it is a part of (which PC, which file, its size and time). Coming back to it — after
a network drop (the session reconnects by itself) or another day — continues from the part's
size. An upload continues the same way from the part the host kept. Only when the SHA-256 of the
whole file matches on both ends is the part renamed into place.

Transfers that were not finished are listed in transfers.json, so they can be continued after
the app is restarted.
"""

import asyncio
import concurrent.futures
import hashlib
import json
import os
import secrets

from .. import protocol as P
from ..config import write_json
from ..net import stop_stream
from ..transfer import PRIORITY, Speed, receive_file, send_file, unique_name

MAX_ACTIVE = 2
ACTIVE = ("starting", "running", "finishing")


class Transfer:
    def __init__(self, kind, name, local, remote, size=0, mtime=0, x=None):
        self.x = x or secrets.token_hex(6)
        self.kind = kind                    # "get" (to here) | "put" (to there)
        self.name = name
        self.local = local                  # get: the final path here; put: the file here
        self.remote = remote                # get: the file there; put: the folder there
        self.size, self.mtime = size, mtime
        self.state = "queued"               # queued | starting | running | finishing | done | error | cancelled | interrupted
        self.done = 0                       # bytes of the file in place on the receiving end
        self.offset = 0
        self.error = ""
        self.result_name = ""
        self.speed = Speed()
        self.sha_remote = None
        self.sha_local = None
        self.complete = False
        self.task = None
        self.restarted = False

    def record(self):
        return {"x": self.x, "kind": self.kind, "name": self.name, "local": self.local, "remote": self.remote,
                "size": self.size, "mtime": self.mtime}


class FileClient:
    def __init__(self, session, store=None):
        self.session = session
        self.net = session.net
        self.store = store                  # transfers.json, or None
        self.transfers = {}                 # x -> Transfer, in the order they were made
        self._requests = {}                 # id -> asyncio future (network thread)
        self._next_id = 0
        self._ever_connected = False
        session.handlers.append(self)
        self._load()

    # ── unfinished transfers on disk ────────────────────────────────
    def _load(self):
        if not self.store:
            return
        try:
            data = json.loads(self.store.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        for rec in (data.get(self.session.target["id"]) or []):
            try:
                t = Transfer(rec["kind"], rec["name"], rec["local"], rec["remote"], rec.get("size", 0),
                             rec.get("mtime", 0), rec["x"])
            except KeyError:
                continue
            t.state = "interrupted"
            t.error = "прервано в прошлый раз"
            self.transfers[t.x] = t

    def _save(self):
        if not self.store:
            return
        try:
            data = json.loads(self.store.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        mine = [t.record() for t in self.transfers.values() if t.state not in ("done", "cancelled", "error")]
        if mine:
            data[self.session.target["id"]] = mine
        else:
            data.pop(self.session.target["id"], None)
        write_json(self.store, data)

    # ── session handler (network thread) ────────────────────────────
    def connected(self, session):
        """(Re)connected: whatever was on its way when the line dropped carries on by itself."""
        for t in self.transfers.values():
            if t.state in ACTIVE or (t.state == "interrupted" and self._ever_connected):
                t.state, t.error = "queued", ""
        self._ever_connected = True
        self._pump()

    def on_datagram(self, data):
        return False

    def on_message(self, msg):
        t = msg["t"]
        if t == "r":
            fut = self._requests.pop(msg.get("id"), None)
            if fut is not None and not fut.done():
                fut.set_result(msg)
            return True
        tr = self.transfers.get(msg.get("x"))
        if t not in ("get.ok", "put.ok", "xfer.done", "xfer.error"):
            return False
        if tr is None or tr.state not in ACTIVE:
            return True
        if t == "get.ok":
            tr.size, tr.mtime, tr.state = msg.get("size", 0), msg.get("mtime", 0), "running"
            self._write_sidecar(tr)
        elif t == "put.ok":
            tr.offset = tr.done = int(msg.get("offset") or 0)
            tr.state = "running"
            tr.task = self.net.spawn(self._send(tr))
        elif t == "xfer.done":
            tr.sha_remote = str(msg.get("sha256", "")).lower()
            tr.result_name = msg.get("name") or ""
            if tr.kind == "put":
                self._finished(tr)
            else:
                self._finish_get(tr)
        elif t == "xfer.error":
            if msg.get("code") == "changed" and not tr.restarted:
                tr.restarted = True              # the file there changed: start it over
                self._discard_part(tr)
                tr.state = "queued"
                self._pump()
            else:
                self._failed(tr, msg.get("msg") or msg.get("code") or "ошибка")
        return True

    async def on_stream(self, header, rest, rs):
        if header.get("t") != "file":
            return False
        tr = self.transfers.get(header.get("x"))
        if tr is None or tr.kind != "get" or tr.state != "running" or header.get("offset") != tr.offset:
            await stop_stream(rs)
            return True
        tr.task = asyncio.current_task()
        h = hashlib.sha256()
        try:
            f = open(self._part(tr), "r+b")
        except OSError as e:
            await stop_stream(rs)
            return self._failed(tr, str(e)) or True

        def progress(n):
            tr.done += n
            tr.speed.add(n)
        try:
            ok = await receive_file(f, tr.offset, tr.size, rs, rest, h, progress)
        finally:
            f.close()
        if tr.state != "running":
            return True
        if ok:
            tr.complete, tr.sha_local, tr.state = True, h.hexdigest(), "finishing"
            self._finish_get(tr)
        else:
            tr.state, tr.error = "interrupted", "связь прервалась — продолжится с того же места"
            self._pump()
        return True

    # ── thread-safe API ─────────────────────────────────────────────
    def request(self, msg):
        """A folder request; a concurrent Future of the reply ({"ok": …})."""
        out = concurrent.futures.Future()

        def go():
            fut = self._ask(msg)
            fut.add_done_callback(lambda f: out.set_result(f.result()) if not out.done() else None)
        self.net.call(go)
        return out

    def download(self, remote, size, mtime, local_folder, name):
        local = os.path.join(local_folder, name)
        t = Transfer("get", name, local, remote, size, mtime)
        self._add(t)
        return t

    def upload(self, local_path, remote_folder, overwrite=False):
        st = os.stat(local_path)
        t = Transfer("put", os.path.basename(local_path), local_path, remote_folder, st.st_size, int(st.st_mtime))
        t.overwrite = overwrite
        self._add(t)
        return t

    def download_tree(self, remote, local_folder, name):
        """A remote folder with everything in it."""
        self.net.call(lambda: self.net.spawn(self._download_tree(remote, os.path.join(local_folder, name))))

    def upload_tree(self, local_dir, remote_folder):
        self.net.call(lambda: self.net.spawn(self._upload_tree(local_dir, remote_folder)))

    def cancel(self, x):
        self.net.call(self._cancel, x)

    def resume(self, x):
        def go():
            t = self.transfers.get(x)
            if t is not None and t.state in ("interrupted", "error"):
                t.state, t.error = "queued", ""
                self._pump()
        self.net.call(go)

    def clear_finished(self):
        def go():
            for x in [x for x, t in self.transfers.items() if t.state in ("done", "cancelled", "error")]:
                del self.transfers[x]
        self.net.call(go)

    def snapshot(self):
        return list(self.transfers.values())

    # ── the queue (network thread) ──────────────────────────────────
    def _add(self, t):
        def go():
            self.transfers[t.x] = t
            self._save()
            self._pump()
        self.net.call(go)

    def _pump(self):
        if self.session.state != "connected":
            for t in self.transfers.values():
                if t.state in ACTIVE:
                    t.state, t.error = "interrupted", "нет связи — продолжится сам"
            return
        active = sum(1 for t in self.transfers.values() if t.state in ACTIVE)
        for t in self.transfers.values():
            if active >= MAX_ACTIVE:
                break
            if t.state == "queued":
                active += 1
                self._begin(t)

    def _begin(self, t):
        t.state, t.error, t.complete, t.sha_remote, t.sha_local = "starting", "", False, None, None
        if t.kind == "get":
            t.offset = self._prepare_part(t)
            t.done = t.offset
            self.session.post({"t": "get", "x": t.x, "path": t.remote, "offset": t.offset,
                               "size": t.size, "mtime": t.mtime})
        else:
            try:
                st = os.stat(t.local)
            except OSError as e:
                return self._failed(t, f"файл недоступен: {e}")
            if (st.st_size, int(st.st_mtime)) != (t.size, t.mtime):
                t.size, t.mtime = st.st_size, int(st.st_mtime)
            self.session.post({"t": "put", "x": t.x, "path": join(t.remote, t.name), "size": t.size,
                               "mtime": t.mtime, "overwrite": getattr(t, "overwrite", False)})

    async def _send(self, t):
        h = hashlib.sha256()
        try:
            stream = await self.session.conn.open_uni()
            await stream.set_priority(PRIORITY)
            await stream.write_all(P.stream_header("file", self.session.sid, x=t.x, offset=t.offset))

            def progress(n):
                t.done += n
                t.speed.add(n)
            with open(t.local, "rb") as f:
                await send_file(f, t.offset, t.size, stream, h, progress)
            t.state = "finishing"
            self.session.post({"t": "put.end", "x": t.x, "sha256": h.hexdigest()})
        except asyncio.CancelledError:
            raise
        except Exception:
            if t.state == "running":
                t.state, t.error = "interrupted", "связь прервалась — продолжится с того же места"
                self._pump()

    def _cancel(self, x):
        t = self.transfers.get(x)
        if t is None or t.state in ("done", "cancelled"):
            return
        was_active = t.state in ACTIVE
        t.state, t.error = "cancelled", ""
        if t.task is not None:
            t.task.cancel()
        if was_active:
            self.session.post({"t": "xfer.cancel", "x": x})
        if t.kind == "get":
            self._discard_part(t)
        self._save()
        self._pump()

    # ── downloads: the part here ────────────────────────────────────
    @staticmethod
    def _part(t):
        return t.local + ".mrpart"

    def _sidecar_record(self, t):
        return {"host": self.session.target["id"], "remote": t.remote, "size": t.size, "mtime": t.mtime}

    def _prepare_part(self, t):
        """The size to continue from: the part's, if it is a part of this very file."""
        part, side = self._part(t), self._part(t) + ".json"
        try:
            rec = json.loads(open(side, encoding="utf-8").read())
            have = os.path.getsize(part)
            if rec == self._sidecar_record(t) and have <= t.size:
                return have
        except (OSError, ValueError):
            pass
        os.makedirs(os.path.dirname(part) or ".", exist_ok=True)
        open(part, "wb").close()
        self._write_sidecar(t)
        return 0

    def _write_sidecar(self, t):
        try:
            with open(self._part(t) + ".json", "w", encoding="utf-8") as f:
                json.dump(self._sidecar_record(t), f)
        except OSError:
            pass

    def _discard_part(self, t):
        for p in (self._part(t), self._part(t) + ".json"):
            try:
                os.remove(p)
            except OSError:
                pass

    def _finish_get(self, t):
        if not (t.complete and t.sha_remote):
            return
        if t.sha_local != t.sha_remote:
            self._discard_part(t)
            return self._failed(t, "контрольная сумма не совпала — файл не сохранён")
        folder = os.path.dirname(t.local)
        name = unique_name(folder, os.path.basename(t.local))
        try:
            os.replace(self._part(t), os.path.join(folder, name))
            os.remove(self._part(t) + ".json")
        except OSError as e:
            return self._failed(t, str(e))
        t.result_name = name
        self._finished(t)

    def _finished(self, t):
        t.state, t.done = "done", t.size
        t.task = None
        self._save()
        self._pump()

    def _failed(self, t, text):
        t.state, t.error = "error", text
        t.task = None
        self._save()
        self._pump()

    # ── folders ─────────────────────────────────────────────────────
    def _ask(self, msg):
        self._next_id += 1
        rid = self._next_id
        fut = asyncio.get_running_loop().create_future()
        self._requests[rid] = fut
        self.session.post({**msg, "id": rid})
        return fut

    async def _download_tree(self, remote, local_dir):
        os.makedirs(local_dir, exist_ok=True)
        reply = await self._ask({"t": "fs.list", "path": remote})
        if not reply.get("ok"):
            return
        for e in reply.get("entries") or []:
            sub = join(remote, e["n"])
            if e.get("d"):
                await self._download_tree(sub, os.path.join(local_dir, e["n"]))
            else:
                t = Transfer("get", e["n"], os.path.join(local_dir, e["n"]), sub, e.get("s", 0), e.get("m", 0))
                self.transfers[t.x] = t
        self._save()
        self._pump()

    async def _upload_tree(self, local_dir, remote_folder):
        top = join(remote_folder, os.path.basename(local_dir.rstrip("\\/")))
        for here, _, files in os.walk(local_dir):
            rel = os.path.relpath(here, local_dir)
            there = top if rel == "." else join(top, *rel.split(os.sep))
            reply = await self._ask({"t": "fs.mkdir", "path": there})
            if not reply.get("ok"):
                return
            for name in files:
                path = os.path.join(here, name)
                try:
                    st = os.stat(path)
                except OSError:
                    continue
                t = Transfer("put", name, path, there, st.st_size, int(st.st_mtime))
                self.transfers[t.x] = t
        self._save()
        self._pump()


def join(*parts):
    return "/".join(p.strip("/") for p in parts if p)


def human_speed(bps):
    for unit in ("Б/с", "КБ/с", "МБ/с", "ГБ/с"):
        if bps < 1024 or unit == "ГБ/с":
            return f"{bps:.0f} {unit}" if unit == "Б/с" else f"{bps:.1f} {unit}"
        bps /= 1024

