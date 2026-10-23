"""
The picture on the viewer's side of the network: video segments (one-way streams of frames,
each from a keyframe on) go to the decoder; what arrived is acknowledged back by datagram,
which is how the host notices this end falling behind. A new segment supersedes the old one,
which is stopped. The pointer's position comes by datagram too.
"""

import time

from .. import protocol as P
from ..net import BufferedStream, stop_stream

ACK_EVERY = 0.05


class VideoReceiver:
    def __init__(self, session, decoder):
        self.session = session
        self.decoder = decoder
        decoder.ask_key = self.ask_key
        self.seg = 0
        self.rs = None
        self.received = 0
        self.size = (0, 0)
        self.bytes = 0
        self.frames = 0
        self.cursor = None          # (x, y, flags), 0..65535 on the watched monitor
        self._cursor_seq = -1
        self._last_ack = 0.0
        self.clock_offset = 0       # host clock - ours, µs
        session.handlers.append(self)

    def ask_key(self):
        """The picture is broken (any thread): the host should send a keyframe."""
        self.session.post({"t": "video.key"})

    def connected(self, session):
        self.seg = 0                # a new session numbers its segments from 1 again
        self._cursor_seq = -1

    async def on_stream(self, header, rest, rs):
        if header.get("t") != "video":
            return False
        seg = header.get("seg")
        if not isinstance(seg, int) or seg <= self.seg:
            await stop_stream(rs)
            return True
        old, self.rs = self.rs, rs
        self.seg, self.received = seg, 0
        self.size = (int(header.get("w") or 0), int(header.get("h") or 0))
        if old is not None:
            await stop_stream(old)
        reader = BufferedStream(rs, rest)
        try:
            while self.seg == seg:
                n, ts, flags = P.unpack_frame_header(await reader.read_exact(P.FRAME.size))
                data = await reader.read_exact(n)
                if self.seg != seg:
                    break
                self.received += 1
                self.frames += 1
                self.bytes += n
                self.decoder.feed(seg, data, ts, bool(flags & P.FRAME_KEY))
                now = time.monotonic()
                if now - self._last_ack >= ACK_EVERY:
                    self._last_ack = now
                    self.session.send_datagram(P.pack_ack(self.session.sid, seg, self.received,
                                                          self.decoder.shown_in_seg))
        except Exception:
            pass                    # reset by the host (a new segment follows) or the session ended
        return True

    def on_datagram(self, data):
        if data[0] != P.DG_CURSOR:
            return False
        c = P.unpack_cursor(data)
        if c is not None:
            seq, x, y, flags = c
            if seq > self._cursor_seq or self._cursor_seq - seq > 1 << 30:     # newest only
                self._cursor_seq = seq
                self.cursor = (x, y, flags)
                self.decoder.cursor_moved.emit()
        return True
