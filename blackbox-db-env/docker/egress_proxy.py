"""CONNECT-only forward proxy with a host allowlist (copied from cad-rl-env). Agent containers have no other route out."""

import asyncio
import os
import sys
import time

# PROXY_ALLOW (comma-separated) narrows a proxy to one vendor, e.g. the Grok proxy allows only api.x.ai.
ALLOW = set(os.environ["PROXY_ALLOW"].split(",")) if os.environ.get("PROXY_ALLOW") else {"api.anthropic.com", "chatgpt.com", "api.openai.com", "generativelanguage.googleapis.com"}
PORT = 3128


async def pipe(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
    try:
        while data := await r.read(65536):
            w.write(data)
            await w.drain()
    except (ConnectionError, asyncio.CancelledError):
        pass
    finally:
        w.close()


async def handle(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
    try:
        head = await asyncio.wait_for(r.readuntil(b"\r\n\r\n"), 15)
        method, target, *_ = head.decode("latin1").split()
        host, _, port = target.rpartition(":")
        if method != "CONNECT" or host not in ALLOW or port != "443":
            print(f"{time.time():.0f} DENY {method} {target}", flush=True)
            w.write(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
            await w.drain()
            w.close()
            return
        ur, uw = await asyncio.open_connection(host, 443)
        w.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        await w.drain()
        await asyncio.gather(pipe(r, uw), pipe(ur, w))
    except (ValueError, OSError, asyncio.TimeoutError, asyncio.IncompleteReadError) as e:
        print(f"{time.time():.0f} ERROR {type(e).__name__}", flush=True, file=sys.stderr)
        w.close()


async def main() -> None:
    server = await asyncio.start_server(handle, "0.0.0.0", PORT)
    async with server:
        await server.serve_forever()


asyncio.run(main())
