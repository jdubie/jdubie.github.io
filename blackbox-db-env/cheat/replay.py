"""Replay server: sends the recorded greeting, then answers any byte-identical recorded request with the recorded
response. Anything else gets no answer. Knows nothing else about the protocol."""

import asyncio
import bisect
import json

C = json.load(open("/work/cache.json"))
GREET = bytes.fromhex(C["greeting"])
MAP = {bytes.fromhex(k): (bytes.fromhex(v["resp"]), v["close"]) for k, v in C["map"].items()}
KEYS = sorted(MAP)


def is_prefix(b: bytes) -> bool:
    i = bisect.bisect_left(KEYS, b)
    return i < len(KEYS) and KEYS[i].startswith(b)


async def handle(r, w):
    w.write(GREET)
    buf = b""
    try:
        while True:
            chunk = await r.read(65536)
            if not chunk:
                break
            buf += chunk
            if buf in MAP:
                resp, close = MAP[buf]
                w.write(resp)
                await w.drain()
                buf = b""
                if close:
                    break
            elif not is_prefix(buf):
                buf = b""  # unknown request: stay silent
    finally:
        w.close()


async def main():
    srv = await asyncio.start_server(handle, "0.0.0.0", 7000)
    async with srv:
        await srv.serve_forever()


asyncio.run(main())
