"""The speed-up of iroh's bindings: what arrives is exactly what was sent, and fast."""
import asyncio
import hashlib
import os
import secrets
import time

from marinremote.net import FAST_BINDINGS, Node


def test_bulk_bytes_arrive_intact_and_fast():
    assert FAST_BINDINGS
    a = Node(secrets.token_hex(32), port=0, mode="lan", bind_host="127.0.0.1", lan=False)
    b = Node(secrets.token_hex(32), port=0, mode="lan", bind_host="127.0.0.1", lan=False)
    a.start()
    b.start()
    payload = os.urandom(48 << 20) + b"tail"           # an odd length on purpose

    async def scenario():
        done = asyncio.get_running_loop().create_future()

        async def serve(conn):
            rs = await conn.accept_uni()
            h, n = hashlib.sha256(), 0
            while True:
                d = await rs.read(1 << 20)
                if not d:
                    break
                h.update(d)
                n += len(d)
            done.set_result((n, h.hexdigest()))

        a.on_incoming = serve
        conn = await b.connect(a.id, None, [f"127.0.0.1:{a.port}"])
        st = await conn.open_uni()
        t0 = time.perf_counter()
        for i in range(0, len(payload), 1 << 20):
            await st.write_all(payload[i:i + (1 << 20)])
        await st.finish()
        result = await asyncio.wait_for(done, 60)
        conn.close(0, b"done")
        return result, time.perf_counter() - t0

    try:
        (n, digest), dt = a.net.submit(scenario()).result(90)
    finally:
        a.stop()
        b.stop()
    assert n == len(payload) and digest == hashlib.sha256(payload).hexdigest()
    assert len(payload) / dt > 25 << 20, f"only {len(payload) / dt / (1 << 20):.0f} MB/s"
