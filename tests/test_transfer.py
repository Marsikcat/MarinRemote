"""
Files end to end over two iroh endpoints: folder requests, downloads and uploads, a network drop
in the middle (the session comes back by itself and the transfer continues from where it broke
off), continuing after a restart, a damaged part caught by SHA-256, a file that changed meanwhile,
cancelling, whole folders, and the "files" right.
"""
import hashlib
import os
import time

import pytest

from marinremote import protocol as P
from marinremote import transfer as T
from marinremote.config import Settings
from marinremote.host import fs as hostfs
from marinremote.host.core import Approver, HostCore
from marinremote.host.files import FilesService
from marinremote.journal import Journal
from marinremote.net import Node
from marinremote.viewer.files import FileClient
from marinremote.viewer.session import ViewerSession

MB = 1 << 20


class Grant(Approver):
    def __init__(self, rights):
        self.rights = rights

    async def ask(self, request):
        return {"allow": True, "rights": self.rights, "remember": None, "block": False}


def until(cond, timeout=30):
    deadline = time.time() + timeout
    while not cond():
        assert time.time() < deadline, "condition never came true"
        time.sleep(0.02)


def sha(path):
    return hashlib.sha256(open(path, "rb").read()).hexdigest()


def make_file(path, size):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(os.urandom(size))
    return path


class Pair:
    def __init__(self, tmp_path, rights=("view", "control", "files", "clipboard")):
        self.tmp = tmp_path
        self.hs, self.vs = Settings(tmp_path / "h"), Settings(tmp_path / "v")
        self.root = tmp_path / "shared"
        self.root.mkdir()
        self.hs["roots"] = [{"id": "home", "path": str(self.root), "label": "Тест"}]
        self.here = tmp_path / "here"
        self.here.mkdir()
        self.host = Node(self.hs.secret_key, port=0, mode="lan", bind_host="127.0.0.1", lan=False)
        self.viewer = Node(self.vs.secret_key, port=0, mode="lan", bind_host="127.0.0.1", lan=False)
        self.host.start()
        self.viewer.start()
        self.journal = Journal(tmp_path / "j.jsonl")
        self.core = HostCore(self.hs, self.host, self.journal, Grant(list(rights)))
        self.core.services.append(FilesService(self.core))
        self.clients = []

    def connect(self):
        s = ViewerSession(self.viewer, {"id": self.host.id, "relay": None, "addrs": [f"127.0.0.1:{self.host.port}"]},
                          ["view", "control", "files"], "зритель")
        fc = FileClient(s, self.tmp / "transfers.json")
        s.start()
        until(lambda: s.state == "connected")
        self.clients.append(s)
        return s, fc

    def drop(self, s):
        """The network goes away for a moment: no goodbye, the session will come back."""
        s.net.call(s._close_conn, s.conn, P.CLOSE_IDLE, "gone")

    def events(self, name):
        return [e for e in self.journal.read() if e["event"] == name]

    def close(self):
        for s in self.clients:
            s.stop()
        time.sleep(0.4)
        self.viewer.stop()
        self.host.stop()


@pytest.fixture
def pair(tmp_path, monkeypatch):
    monkeypatch.setattr(hostfs, "recycle", os.remove)          # the tests' files don't go to the real bin
    p = Pair(tmp_path)
    yield p
    p.close()


@pytest.fixture
def slow(monkeypatch):
    """Small chunks with a little pause each, so a transfer can be broken off in the middle."""
    monkeypatch.setattr(T, "CHUNK", 256 * 1024)
    real_write = T._write_hash

    def write(f, data, h):
        time.sleep(0.004)
        real_write(f, data, h)
    monkeypatch.setattr(T, "_write_hash", write)


def test_folder_requests(pair):
    s, fc = pair.connect()
    make_file(pair.root / "docs" / "a.bin", 10)
    assert fc.request({"t": "fs.roots"}).result(5)["roots"] == [{"id": "home", "label": "Тест"}]
    r = fc.request({"t": "fs.list", "path": "home/docs"}).result(5)
    assert r["ok"] and [(e["n"], e["s"]) for e in r["entries"]] == [("a.bin", 10)]
    assert fc.request({"t": "fs.mkdir", "path": "home/docs/new"}).result(5)["ok"]
    assert fc.request({"t": "fs.rename", "path": "home/docs/a.bin", "name": "b.bin"}).result(5)["ok"]
    assert fc.request({"t": "fs.delete", "path": "home/docs/b.bin"}).result(5)["ok"]
    assert sorted(os.listdir(pair.root / "docs")) == ["new"]
    bad = fc.request({"t": "fs.list", "path": "home/../.."}).result(5)
    assert not bad["ok"] and bad["code"] == "bad_path"
    assert {e["action"] for e in pair.events("fs")} == {"mkdir", "rename", "delete"}


def test_download_and_upload(pair):
    s, fc = pair.connect()
    src = make_file(pair.root / "big.bin", 5 * MB + 123)
    t = fc.download("home/big.bin", src.stat().st_size, int(src.stat().st_mtime), str(pair.here), "big.bin")
    until(lambda: t.state == "done")
    assert sha(pair.here / "big.bin") == sha(src)
    assert not (pair.here / "big.bin.mrpart").exists() and not (pair.here / "big.bin.mrpart.json").exists()

    up = make_file(pair.here / "up.bin", 3 * MB + 7)
    u = fc.upload(str(up), "home")
    until(lambda: u.state == "done")
    assert sha(pair.root / "up.bin") == sha(up)
    u2 = fc.upload(str(up), "home")                   # again: a new name, nothing overwritten
    until(lambda: u2.state == "done")
    assert u2.result_name == "up (1).bin" and sha(pair.root / "up (1).bin") == sha(up)
    assert not [n for n in os.listdir(pair.root) if n.endswith(".mrpart")]
    got = pair.events("file_get")[-1]
    assert got["result"] == "ok" and got["sha256"] == sha(src)
    assert [e["result"] for e in pair.events("file_put")] == ["ok", "ok"]


def test_download_survives_a_network_drop(pair, slow):
    s, fc = pair.connect()
    src = make_file(pair.root / "film.bin", 24 * MB)
    t = fc.download("home/film.bin", src.stat().st_size, int(src.stat().st_mtime), str(pair.here), "film.bin")
    until(lambda: t.done > 6 * MB)
    pair.drop(s)
    until(lambda: t.state == "done", 60)
    assert sha(pair.here / "film.bin") == sha(src)
    gets = pair.events("file_get")
    assert gets[0]["result"] == "interrupted"
    assert gets[-1]["result"] == "ok" and gets[-1]["offset"] >= 6 * MB      # it continued, not restarted


def test_upload_survives_a_network_drop(pair, slow):
    s, fc = pair.connect()
    up = make_file(pair.here / "backup.bin", 24 * MB)
    u = fc.upload(str(up), "home")
    until(lambda: u.done > 6 * MB)
    pair.drop(s)
    until(lambda: u.state == "done", 60)
    assert sha(pair.root / "backup.bin") == sha(up)
    puts = pair.events("file_put")
    assert [e["result"] for e in puts] == ["interrupted", "ok"]
    # it continued from exactly what the host had kept, not from the start
    assert puts[0]["received"] > 0 and puts[1]["offset"] == puts[0]["received"]


def test_continue_after_a_restart_and_a_damaged_part_is_caught(pair, slow):
    s, fc = pair.connect()
    src = make_file(pair.root / "f.bin", 16 * MB)
    t = fc.download("home/f.bin", src.stat().st_size, int(src.stat().st_mtime), str(pair.here), "f.bin")
    until(lambda: t.done > 4 * MB)
    s.stop()                                          # the app closes in the middle
    until(lambda: s.state == "closed")
    part = pair.here / "f.bin.mrpart"
    assert part.exists() and (pair.here / "f.bin.mrpart.json").exists()
    with open(part, "r+b") as f:                      # and the part gets damaged meanwhile
        f.seek(1000)
        f.write(b"\xff\xfe\xfd")

    s2, fc2 = pair.connect()                          # next time: it's still listed
    t2 = next(x for x in fc2.snapshot() if x.name == "f.bin")
    assert t2.state == "interrupted"
    fc2.resume(t2.x)
    until(lambda: t2.state in ("done", "error"), 60)
    assert t2.state == "error" and "контрольная сумма" in t2.error
    assert not part.exists() and not (pair.here / "f.bin").exists()

    fc2.resume(t2.x)                                  # once more, from scratch this time
    until(lambda: t2.state == "done", 60)
    assert sha(pair.here / "f.bin") == sha(src)


def test_a_file_that_changed_meanwhile_starts_over(pair, slow):
    s, fc = pair.connect()
    src = make_file(pair.root / "log.bin", 16 * MB)
    t = fc.download("home/log.bin", src.stat().st_size, int(src.stat().st_mtime), str(pair.here), "log.bin")
    until(lambda: t.done > 4 * MB)
    s.stop()
    until(lambda: s.state == "closed")
    with open(src, "ab") as f:
        f.write(os.urandom(MB))
    os.utime(src, (time.time() + 5, time.time() + 5))
    s2, fc2 = pair.connect()
    t2 = next(x for x in fc2.snapshot() if x.name == "log.bin")
    t2.size, t2.mtime = src.stat().st_size, int(src.stat().st_mtime)    # as a fresh listing would say
    fc2.resume(t2.x)
    until(lambda: t2.state == "done", 60)
    assert sha(pair.here / "log.bin") == sha(src)


def test_cancel_throws_the_part_away(pair, slow):
    s, fc = pair.connect()
    up = make_file(pair.here / "c.bin", 16 * MB)
    u = fc.upload(str(up), "home")
    until(lambda: u.done > 2 * MB)
    fc.cancel(u.x)
    until(lambda: pair.events("file_put") and pair.events("file_put")[-1]["result"] == "cancelled")
    time.sleep(0.3)
    assert u.state == "cancelled"
    assert not [n for n in os.listdir(pair.root) if "c.bin" in n]


def test_parts_nobody_came_back_for_are_swept_after_a_week(pair):
    s, fc = pair.connect()
    old, fresh = pair.root / ".old.bin.0123456789.mrpart", pair.root / ".new.bin.0123456789.mrpart"
    old.write_bytes(b"x")
    fresh.write_bytes(b"y")
    week_ago = time.time() - 8 * 86400
    os.utime(old, (week_ago, week_ago))
    u = fc.upload(str(make_file(pair.here / "z.bin", 10)), "home")
    until(lambda: u.state == "done")
    assert not old.exists() and fresh.exists()


def test_whole_folders_both_ways(pair):
    s, fc = pair.connect()
    for rel in ("proj/a.txt", "proj/src/b.txt", "proj/src/deep/c.txt"):
        make_file(pair.here / rel, 1000)
    fc.upload_tree(str(pair.here / "proj"), "home")
    until(lambda: (pair.root / "proj" / "src" / "deep" / "c.txt").exists()
          and all(t.state == "done" for t in fc.snapshot()) and len(fc.snapshot()) == 3)
    fc.download_tree("home/proj", str(pair.tmp / "back"), "proj")
    until(lambda: len(fc.snapshot()) == 6 and all(t.state == "done" for t in fc.snapshot()))
    for rel in ("a.txt", "src/b.txt", "src/deep/c.txt"):
        assert sha(pair.tmp / "back" / "proj" / rel) == sha(pair.here / "proj" / rel)


def test_paths_out_of_the_root_are_refused(pair):
    s, fc = pair.connect()
    outside = make_file(pair.tmp / "secret.bin", 100)
    t = fc.download("home/../secret.bin", 100, int(outside.stat().st_mtime), str(pair.here), "s.bin")
    until(lambda: t.state == "error")
    up = make_file(pair.here / "x.bin", 10)
    u = fc.upload(str(up), "home/..")
    until(lambda: u.state == "error")
    assert not (pair.tmp / "x.bin").exists()


def test_without_the_files_right_nothing_happens(tmp_path, monkeypatch):
    p = Pair(tmp_path, rights=("view",))
    try:
        s, fc = p.connect()
        make_file(p.root / "a.bin", 10)
        fut = fc.request({"t": "fs.list", "path": "home"})
        up = make_file(p.here / "u.bin", 10)
        fc.upload(str(up), "home")
        time.sleep(0.8)
        assert not fut.done()
        assert not (p.root / "u.bin").exists()
        assert {e["what"] for e in p.events("violation")} >= {"fs.list", "put"}
    finally:
        p.close()
