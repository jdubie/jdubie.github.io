#!/usr/bin/env python3
"""Serve a directory over the tailnet, with byte-range support, and print its URLs.

Stdlib only, so it runs on a rig, the dataprep VM or a workstation with no
install step.

Range is the whole reason this file exists. `python -m http.server` answers every
request with the entire file and never sends `Accept-Ranges`, which a browser
reads as "not seekable": `<video>` plays from the start and the scrub bar does
nothing. The failure is silent and it looks like a codec problem, so it costs an
hour before anyone suspects the server.

Usage:
    python3 serve_data.py [DIR] [--port N]

DIR defaults to the script's own directory.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
from functools import partial
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

RANGE = re.compile(r"bytes=(\d*)-(\d*)$")


class RangeHandler(SimpleHTTPRequestHandler):
    """SimpleHTTPRequestHandler that honours a single `Range: bytes=a-b`."""

    range_remaining: int | None = None

    def send_head(self):
        header = self.headers.get("Range")
        if not header:
            self.range_remaining = None
            return super().send_head()

        path = self.translate_path(self.path)
        if os.path.isdir(path):
            self.range_remaining = None
            return super().send_head()
        try:
            # Not a context manager on purpose: `send_head` hands the open handle
            # back for the base class to `copyfile` and close, which is
            # `SimpleHTTPRequestHandler`'s own contract.
            handle = open(path, "rb")  # noqa: SIM115
        except OSError:
            self.send_error(HTTPStatus.NOT_FOUND, "File not found")
            return None

        size = os.fstat(handle.fileno()).st_size
        match = RANGE.match(header.strip())
        if not match:
            handle.close()
            self.send_error(HTTPStatus.BAD_REQUEST, "Malformed Range")
            return None

        first, last = match.groups()
        if first:
            start = int(first)
            end = int(last) if last else size - 1
        elif last:  # suffix range: the final N bytes
            start = max(0, size - int(last))
            end = size - 1
        else:
            handle.close()
            self.send_error(HTTPStatus.BAD_REQUEST, "Malformed Range")
            return None

        end = min(end, size - 1)
        if start > end or start >= size:
            handle.close()
            self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
            self.send_header("Content-Range", f"bytes */{size}")
            self.end_headers()
            return None

        self.send_response(HTTPStatus.PARTIAL_CONTENT)
        self.send_header("Content-Type", self.guess_type(path))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Content-Length", str(end - start + 1))
        self.end_headers()
        handle.seek(start)
        self.range_remaining = end - start + 1
        return handle

    def copyfile(self, source, outputfile):
        remaining = self.range_remaining
        if remaining is None:
            return super().copyfile(source, outputfile)
        while remaining > 0:
            chunk = source.read(min(64 * 1024, remaining))
            if not chunk:
                break
            outputfile.write(chunk)
            remaining -= len(chunk)

    def send_response(self, code, message=None):
        super().send_response(code, message)
        # Advertised on the plain 200 too: without it the player never issues a
        # Range request in the first place, so seeking stays broken.
        if code == HTTPStatus.OK:
            self.send_header("Accept-Ranges", "bytes")

    def log_message(self, fmt, *args):  # quiet: the access log is not the point
        pass


def tailnet_urls(port: int) -> list[str]:
    """This node's reachable URLs, best effort — never fatal if tailscale is absent."""
    try:
        raw = subprocess.run(
            ["tailscale", "status", "--json"], capture_output=True, text=True, timeout=10
        )
        self_node = json.loads(raw.stdout)["Self"]
    except Exception:
        return []
    name = str(self_node.get("DNSName", "")).rstrip(".")
    urls = []
    if name:
        urls.append(f"http://{name.split('.')[0]}:{port}/")
        urls.append(f"http://{name}:{port}/")
    urls += [
        f"http://{ip}:{port}/" if ":" not in ip else f"http://[{ip}]:{port}/"
        for ip in self_node.get("TailscaleIPs", [])
    ]
    return urls


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("directory", nargs="?", default=None)
    ap.add_argument("--port", type=int, default=8099)
    args = ap.parse_args()

    root = os.path.abspath(args.directory or os.path.dirname(os.path.abspath(__file__)))
    if not os.path.isdir(root):
        raise SystemExit(f"not a directory: {root}")

    server = ThreadingHTTPServer(("0.0.0.0", args.port), partial(RangeHandler, directory=root))
    print(f"serving {root} on 0.0.0.0:{args.port}", flush=True)
    urls = tailnet_urls(args.port)
    if urls:
        print("reachable on the tailnet at:", flush=True)
        for url in urls:
            print(f"  {url}", flush=True)
    else:
        print("tailscale did not answer; falling back to the host's own address", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
