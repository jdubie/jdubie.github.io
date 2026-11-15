"""Byte-level PostgreSQL v3 protocol client: every frontend message, raw access to every backend message.

This is the client the conformance suite is written with. It does no protocol logic of its own beyond
framing and authentication, so a test can send any sequence (pipelines, errors before Sync, COPY data split
mid-row, CancelRequest on a second socket) and see exactly what comes back.

    s = Session.connect(host, port, user="alice", password="wonderland")
    s.send(parse("", "SELECT $1::int + 1"), bind("", "", [b"41"]), execute(""), sync())
    msgs = s.until_ready()            # [(b"1", payload), (b"2", ...), (b"D", ...), (b"C", ...), (b"Z", b"I")]
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import socket
import struct

PROTO_3_0 = 196608
CANCEL_CODE = 80877102
SSL_CODE = 80877103
GSSENC_CODE = 80877104


class ProtocolError(Exception):
    pass


class Closed(ProtocolError):
    pass


def _msg(t: bytes, body: bytes) -> bytes:
    return t + struct.pack("!I", len(body) + 4) + body


def _cstr(s: str | bytes) -> bytes:
    return (s.encode() if isinstance(s, str) else s) + b"\0"


# ---------------------------------------------------------------- frontend messages

def startup(params: dict[str, str], version: int = PROTO_3_0) -> bytes:
    body = struct.pack("!I", version) + b"".join(_cstr(k) + _cstr(v) for k, v in params.items()) + b"\0"
    return struct.pack("!I", len(body) + 4) + body


def ssl_request() -> bytes:
    return struct.pack("!II", 8, SSL_CODE)


def gssenc_request() -> bytes:
    return struct.pack("!II", 8, GSSENC_CODE)


def cancel_request(pid: int, key: int) -> bytes:
    return struct.pack("!IIii", 16, CANCEL_CODE, pid, key)


def query(sql: str) -> bytes:
    return _msg(b"Q", _cstr(sql))


def parse(name: str, sql: str, oids: list[int] | tuple = ()) -> bytes:
    return _msg(b"P", _cstr(name) + _cstr(sql) + struct.pack("!H", len(oids)) + b"".join(struct.pack("!I", o) for o in oids))


def bind(portal: str, stmt: str, params: list[bytes | None] | tuple = (), param_formats: list[int] | tuple = (),
         result_formats: list[int] | tuple = ()) -> bytes:
    b = _cstr(portal) + _cstr(stmt)
    b += struct.pack("!H", len(param_formats)) + b"".join(struct.pack("!h", f) for f in param_formats)
    b += struct.pack("!H", len(params))
    for p in params:
        b += struct.pack("!i", -1) if p is None else struct.pack("!i", len(p)) + p
    b += struct.pack("!H", len(result_formats)) + b"".join(struct.pack("!h", f) for f in result_formats)
    return _msg(b"B", b)


def describe(kind: str, name: str) -> bytes:
    return _msg(b"D", kind.encode() + _cstr(name))


def execute(portal: str, max_rows: int = 0) -> bytes:
    return _msg(b"E", _cstr(portal) + struct.pack("!i", max_rows))


def close(kind: str, name: str) -> bytes:
    return _msg(b"C", kind.encode() + _cstr(name))


def sync() -> bytes:
    return _msg(b"S", b"")


def flush() -> bytes:
    return _msg(b"H", b"")


def terminate() -> bytes:
    return _msg(b"X", b"")


def copy_data(data: bytes) -> bytes:
    return _msg(b"d", data)


def copy_done() -> bytes:
    return _msg(b"c", b"")


def copy_fail(why: str) -> bytes:
    return _msg(b"f", _cstr(why))


def function_call(oid: int, args: list[bytes | None], arg_formats: list[int] | tuple = (), result_format: int = 0) -> bytes:
    b = struct.pack("!I", oid) + struct.pack("!H", len(arg_formats)) + b"".join(struct.pack("!h", f) for f in arg_formats)
    b += struct.pack("!H", len(args))
    for a in args:
        b += struct.pack("!i", -1) if a is None else struct.pack("!i", len(a)) + a
    return _msg(b"F", b + struct.pack("!h", result_format))


def raw(t: bytes, body: bytes) -> bytes:
    return _msg(t, body)


# ---------------------------------------------------------------- backend message decoding

def fields(p: bytes) -> dict[str, str]:
    """ErrorResponse / NoticeResponse fields: {'S': 'ERROR', 'V': 'ERROR', 'C': '42601', 'M': ..., ...}."""
    out, off = {}, 0
    while off < len(p) and p[off] != 0:
        end = p.index(b"\0", off + 1)
        out[chr(p[off])] = p[off + 1:end].decode(errors="replace")
        off = end + 1
    return out


def row_description(p: bytes) -> list[dict]:
    (n,) = struct.unpack("!H", p[:2])
    cols, off = [], 2
    for _ in range(n):
        end = p.index(b"\0", off)
        name = p[off:end].decode(errors="replace")
        table, attnum, typ, size, mod, fmt = struct.unpack("!IhIhih", p[end + 1:end + 19])
        cols.append({"name": name, "table": table, "attnum": attnum, "type": typ, "size": size, "typmod": mod, "format": fmt})
        off = end + 19
    return cols


def data_row(p: bytes) -> list[bytes | None]:
    (n,) = struct.unpack("!H", p[:2])
    vals, off = [], 2
    for _ in range(n):
        (ln,) = struct.unpack("!i", p[off:off + 4])
        off += 4
        if ln < 0:
            vals.append(None)
        else:
            vals.append(p[off:off + ln])
            off += ln
    return vals


def param_description(p: bytes) -> list[int]:
    (n,) = struct.unpack("!H", p[:2])
    return list(struct.unpack(f"!{n}I", p[2:2 + 4 * n]))


def copy_response(p: bytes) -> tuple[int, list[int]]:
    fmt, n = struct.unpack("!bH", p[:3])
    return fmt, list(struct.unpack(f"!{n}h", p[3:3 + 2 * n]))


def notification(p: bytes) -> tuple[int, str, str]:
    (pid,) = struct.unpack("!i", p[:4])
    a = p.index(b"\0", 4)
    b = p.index(b"\0", a + 1)
    return pid, p[4:a].decode(errors="replace"), p[a + 1:b].decode(errors="replace")


def parameter_status(p: bytes) -> tuple[str, str]:
    a = p.index(b"\0")
    return p[:a].decode(errors="replace"), p[a + 1:-1].decode(errors="replace")


# ---------------------------------------------------------------- SCRAM-SHA-256 (RFC 5802 / 7677)

def _hi(pw: bytes, salt: bytes, it: int) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", pw, salt, it)


class Scram:
    def __init__(self, password: str):
        self.pw = password.encode()
        self.nonce = base64.b64encode(os.urandom(18)).decode()
        self.first_bare = f"n=,r={self.nonce}"
        self.server_sig = b""

    def client_first(self) -> bytes:
        return ("n,," + self.first_bare).encode()

    def client_final(self, server_first: bytes) -> bytes:
        attrs = dict(kv.split("=", 1) for kv in server_first.decode().split(","))
        if not attrs["r"].startswith(self.nonce):
            raise ProtocolError("SCRAM: server nonce does not extend ours")
        salted = _hi(self.pw, base64.b64decode(attrs["s"]), int(attrs["i"]))
        ck = hmac.digest(salted, b"Client Key", "sha256")
        sk = hmac.digest(salted, b"Server Key", "sha256")
        without_proof = f"c=biws,r={attrs['r']}"
        auth = f"{self.first_bare},{server_first.decode()},{without_proof}".encode()
        sig = hmac.digest(hashlib.sha256(ck).digest(), auth, "sha256")
        proof = bytes(a ^ b for a, b in zip(ck, sig))
        self.server_sig = hmac.digest(sk, auth, "sha256")
        return f"{without_proof},p={base64.b64encode(proof).decode()}".encode()

    def verify(self, server_final: bytes) -> bool:
        attrs = dict(kv.split("=", 1) for kv in server_final.decode().split(","))
        return "v" in attrs and base64.b64decode(attrs["v"]) == self.server_sig


# ---------------------------------------------------------------- session

class Session:
    """One socket. `recv()` returns (type, payload); `until_ready()` reads through ReadyForQuery."""

    def __init__(self, sock: socket.socket):
        self.sock = sock
        self.buf = b""
        self.pid = 0
        self.key = 0
        self.params: dict[str, str] = {}
        self.auth: list[str] = []  # what authentication happened, e.g. ['sasl:SCRAM-SHA-256', 'scram-verified', 'ok']
        self.startup_msgs: list[tuple[bytes, bytes]] = []

    @classmethod
    def open(cls, host: str, port: int, timeout: float = 10.0) -> "Session":
        return cls(socket.create_connection((host, port), timeout=timeout))

    @classmethod
    def connect(cls, host: str, port: int, user: str = "postgres", database: str = "regression", password: str | None = None,
                timeout: float = 10.0, extra: dict[str, str] | None = None) -> "Session":
        s = cls.open(host, port, timeout)
        s.handshake(user, database, password, extra)
        return s

    def handshake(self, user: str, database: str = "regression", password: str | None = None,
                  extra: dict[str, str] | None = None, version: int = PROTO_3_0) -> list[tuple[bytes, bytes]]:
        """Send StartupMessage and authenticate. Returns every message up to ReadyForQuery (or the error)."""
        self.send(startup({"user": user, "database": database, **(extra or {})}, version))
        scram = None
        out = []
        while True:
            t, p = self.recv()
            out.append((t, p))
            if t == b"R":
                (code,) = struct.unpack("!I", p[:4])
                if code == 0:
                    self.auth.append("ok")
                elif code == 3:
                    self.auth.append("cleartext")
                    self.send(_msg(b"p", _cstr(password or "")))
                elif code == 5:
                    self.auth.append("md5")
                    inner = hashlib.md5((password or "").encode() + user.encode()).hexdigest()
                    outer = "md5" + hashlib.md5(inner.encode() + p[4:8]).hexdigest()
                    self.send(_msg(b"p", _cstr(outer)))
                elif code == 10:
                    mechs = [m.decode() for m in p[4:].split(b"\0") if m]
                    self.auth.append("sasl:" + ",".join(sorted(mechs)))
                    if "SCRAM-SHA-256" not in mechs:
                        raise ProtocolError(f"no SCRAM-SHA-256 in {mechs}")
                    scram = Scram(password or "")
                    first = scram.client_first()
                    self.send(_msg(b"p", _cstr("SCRAM-SHA-256") + struct.pack("!i", len(first)) + first))
                elif code == 11:
                    assert scram is not None
                    self.send(_msg(b"p", scram.client_final(p[4:])))
                elif code == 12:
                    assert scram is not None
                    self.auth.append("scram-verified" if scram.verify(p[4:]) else "scram-bad-server-signature")
                else:
                    self.auth.append(f"unsupported:{code}")
                    raise ProtocolError(f"unsupported auth request {code}")
            elif t == b"K":
                self.pid, self.key = struct.unpack("!ii", p[:8])
            elif t == b"S":
                k, v = parameter_status(p)
                self.params[k] = v
            elif t == b"E":
                self.startup_msgs = out
                return out
            elif t == b"Z":
                self.startup_msgs = out
                return out

    def send(self, *msgs: bytes) -> None:
        self.sock.sendall(b"".join(msgs))

    def settimeout(self, t: float | None) -> None:
        self.sock.settimeout(t)

    def _fill(self, n: int) -> None:
        while len(self.buf) < n:
            chunk = self.sock.recv(1 << 16)
            if not chunk:
                raise Closed("connection closed by server")
            self.buf += chunk

    def recv_byte(self) -> bytes:
        self._fill(1)
        b, self.buf = self.buf[:1], self.buf[1:]
        return b

    def recv(self) -> tuple[bytes, bytes]:
        self._fill(5)
        t = self.buf[:1]
        (n,) = struct.unpack("!I", self.buf[1:5])
        if n < 4 or n > 1 << 30:
            raise ProtocolError(f"bad message length {n} for type {t!r}")
        self._fill(1 + n)
        p = self.buf[5:1 + n]
        self.buf = self.buf[1 + n:]
        return t, p

    def until(self, stop: set[bytes]) -> list[tuple[bytes, bytes]]:
        out = []
        while True:
            t, p = self.recv()
            out.append((t, p))
            if t in stop:
                return out

    def until_ready(self) -> list[tuple[bytes, bytes]]:
        return self.until({b"Z"})

    def simple(self, sql: str) -> list[tuple[bytes, bytes]]:
        self.send(query(sql))
        return self.until_ready()

    def close(self) -> None:
        try:
            self.send(terminate())
        except OSError:
            pass
        self.sock.close()
