"""
File transfer pieces both sides share. A file travels on a one-way QUIC stream of its own as raw
bytes (no base64, no size limit), at the lowest priority so it never holds up the picture or
the mouse. The receiver writes into a temporary file and renames it only when the SHA-256 of the
whole file matches the sender's. A transfer that broke off continues from the size of that
temporary file; one that was cancelled throws it away.
"""

import asyncio
import hashlib
import os
import time

CHUNK = 1 << 20
PRIORITY = -1           # below video (1) and control/input (3)


def host_temp_name(key, name, size, mtime):
    """The host's temporary name for an upload: hidden next to the target, and specific to the
    sender, the name, the size and the time — a different file never continues someone's part."""
    tag = hashlib.sha256(f"{key}|{name}|{size}|{mtime}".encode()).hexdigest()[:10]
    return f".{name}.{tag}.mrpart"


def unique_name(folder, name):
    """name, or «name (1).ext», «name (2).ext»… — whichever isn't taken in the folder."""
    if not os.path.exists(os.path.join(folder, name)):
        return name
    stem, ext = os.path.splitext(name)
    if name.startswith(".") and not ext:
        stem, ext = name, ""
    n = 1
    while os.path.exists(os.path.join(folder, f"{stem} ({n}){ext}")):
        n += 1
    return f"{stem} ({n}){ext}"


def _read_hash(f, n, h):
    data = f.read(n)
    h.update(data)
    return data


def _write_hash(f, data, h):
    f.write(data)
    h.update(data)


async def hash_prefix(f, n, h):
    """Hash the first n bytes of an open file (to continue a transfer); leaves it at n."""
    f.seek(0)
    left = n
    while left > 0:
        data = await asyncio.to_thread(_read_hash, f, min(CHUNK, left), h)
        if not data:
            raise EOFError("the file is shorter than its part")
        left -= len(data)


async def send_file(f, offset, size, stream, h, progress=None):
    """Bytes [offset, size) of `f` onto `stream`; `h` gets the whole file. progress(sent)."""
    await hash_prefix(f, offset, h)
    left = size - offset
    while left > 0:
        data = await asyncio.to_thread(_read_hash, f, min(CHUNK, left), h)
        if not data:
            raise EOFError("the file got shorter while it was being sent")
        await stream.write_all(data)
        left -= len(data)
        if progress is not None:
            progress(len(data))
    await stream.finish()


async def receive_file(f, offset, size, rs, rest, h, progress=None):
    """The stream into `f` from `offset`; `h` gets the whole file. True when all `size` bytes
    came, False when the stream ended (or broke) short of that."""
    await hash_prefix(f, offset, h)
    f.seek(offset)
    got = offset
    pending = bytes(rest)
    try:
        while got < size:
            if not pending:
                pending = await rs.read(CHUNK)
                if not pending:
                    break
            if got + len(pending) > size:            # more than announced: not ours to keep
                return False
            await asyncio.to_thread(_write_hash, f, pending, h)
            got += len(pending)
            if progress is not None:
                progress(len(pending))
            pending = b""
    except Exception:
        return False
    finally:
        try:
            await asyncio.to_thread(f.flush)
        except (OSError, ValueError):
            pass
    return got == size


class Speed:
    """Bytes per second over the last few seconds."""

    def __init__(self):
        self.marks = []

    def add(self, n):
        now = time.monotonic()
        self.marks.append((now, n))
        while self.marks and now - self.marks[0][0] > 3:
            self.marks.pop(0)

    def value(self):
        if len(self.marks) < 2:
            return 0.0
        span = self.marks[-1][0] - self.marks[0][0]
        return sum(n for _, n in self.marks[1:]) / span if span > 0 else 0.0
