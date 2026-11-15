"""Minimal PostgreSQL v3 wire-protocol client: startup + simple Query flow only.

This is exactly the subset of the protocol the grader speaks. Every value comes back as text
(or None for SQL NULL), the way psql shows it.
"""

from __future__ import annotations

import socket
import struct
from dataclasses import dataclass, field


class ProtocolError(Exception):
    pass


@dataclass
class Result:
    """One statement's outcome. kind: 'rows' | 'command' | 'error' | 'empty'."""

    kind: str
    columns: list[str] = field(default_factory=list)
    rows: list[list[str | None]] = field(default_factory=list)
    tag: str = ""
    sqlstate: str = ""
    message: str = ""


class Conn:
    def __init__(self, host: str = "127.0.0.1", port: int = 5432, timeout: float = 30.0, user: str = "postgres",
                 database: str = "regression"):
        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.buf = b""
        self.tx_status = "?"
        body = struct.pack("!I", 196608) + b"".join(
            k.encode() + b"\0" + v.encode() + b"\0" for k, v in (("user", user), ("database", database))) + b"\0"
        self.sock.sendall(struct.pack("!I", len(body) + 4) + body)
        while True:
            t, payload = self._read()
            if t == b"R":
                if struct.unpack("!I", payload[:4])[0] != 0:
                    raise ProtocolError("server asked for authentication; none is supported")
            elif t == b"E":
                raise ProtocolError("startup error: " + _fields(payload).get("M", "?"))
            elif t == b"Z":
                self.tx_status = payload.decode()
                return
            # S (ParameterStatus), K (BackendKeyData), N (notice) are ignored

    def settimeout(self, t: float | None) -> None:
        self.sock.settimeout(t)

    def _recv(self, n: int) -> bytes:
        while len(self.buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ProtocolError("connection closed by server")
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def _read(self) -> tuple[bytes, bytes]:
        t = self._recv(1)
        (n,) = struct.unpack("!I", self._recv(4))
        if n < 4 or n > 1 << 30:
            raise ProtocolError(f"bad message length {n}")
        return t, self._recv(n - 4)

    def query(self, sql: str) -> list[Result]:
        """Send one Query message (may hold several statements); return one Result per statement."""
        body = sql.encode() + b"\0"
        self.sock.sendall(b"Q" + struct.pack("!I", len(body) + 4) + body)
        results: list[Result] = []
        cur: Result | None = None
        while True:
            t, p = self._read()
            if t == b"T":
                (ncol,) = struct.unpack("!H", p[:2])
                cols, off = [], 2
                for _ in range(ncol):
                    end = p.index(b"\0", off)
                    cols.append(p[off:end].decode())
                    off = end + 1 + 18
                cur = Result("rows", columns=cols)
            elif t == b"D":
                (ncol,) = struct.unpack("!H", p[:2])
                row, off = [], 2
                for _ in range(ncol):
                    (ln,) = struct.unpack("!i", p[off:off + 4])
                    off += 4
                    if ln < 0:
                        row.append(None)
                    else:
                        row.append(p[off:off + ln].decode())
                        off += ln
                if cur is None:
                    cur = Result("rows")
                cur.rows.append(row)
            elif t == b"C":
                tag = p.rstrip(b"\0").decode()
                if cur is None:
                    cur = Result("command")
                cur.tag = tag
                results.append(cur)
                cur = None
            elif t == b"I":
                results.append(Result("empty"))
            elif t == b"E":
                f = _fields(p)
                results.append(Result("error", sqlstate=f.get("C", ""), message=f.get("M", "")))
                cur = None
            elif t == b"Z":
                self.tx_status = p.decode()
                return results
            # N (NoticeResponse), S (ParameterStatus) are ignored

    def one(self, sql: str) -> Result:
        r = self.query(sql)
        if len(r) != 1:
            raise ProtocolError(f"expected 1 result for {sql!r}, got {len(r)}")
        return r[0]

    def close(self) -> None:
        try:
            self.sock.sendall(b"X" + struct.pack("!I", 4))
        except OSError:
            pass
        self.sock.close()


def _fields(p: bytes) -> dict[str, str]:
    out, off = {}, 0
    while off < len(p) and p[off] != 0:
        end = p.index(b"\0", off + 1)
        out[chr(p[off])] = p[off + 1:end].decode(errors="replace")
        off = end + 1
    return out
