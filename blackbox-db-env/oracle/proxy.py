"""The oracle: PostgreSQL 16 behind a seeded translating proxy.

Listens on 0.0.0.0:$BBX_PORT (default 7000) speaking variant $BBX_SEED, and talks to PostgreSQL over its unix
socket. $BBX_LAYERS (default "wire,dialect,semantics") turns perturbation layers on for ablations; the wire
layer is always on (stock PostgreSQL is the "no layers" case). Every behaviour here is deterministic given
the seed: this file plus PostgreSQL 16.16 IS the variant's specification.
"""

from __future__ import annotations

import asyncio
import os
import struct
import sys

from variant import PG_CANCEL, Codec, Dialect, Malformed, Semantics, make_spec

PG_SOCK = os.environ.get("BBX_PG_SOCK", "/var/run/postgresql/.s.PGSQL.5432")
USERS = {"admin": os.environ.get("BBX_ADMIN_PW", "opensesame"), "guest": None}
MAX_MSG = 1 << 28
# Oracle v2: errors carry HINTs naming the startup keys and the dialect's spelling of PostgreSQL words the
# client used, so every renamed word is discoverable by probing. v1 (BBX_HINTS=0) had no discovery channel.
HINTS = os.environ.get("BBX_HINTS", "1") == "1"


class Fatal(Exception):
    def __init__(self, code: str, msg: str, hint: str | None = None):
        self.code, self.msg, self.hint = code, msg, hint


class Terminate(Exception):
    pass


class Proxy:
    def __init__(self, seed: int, layers: set[str]):
        self.spec = make_spec(seed)
        self.w = self.spec["wire"]
        self.V, self.P = Codec(self.spec), Codec.pg_codec()
        self.dia = Dialect(self.spec, "dialect" in layers)
        self.sem = Semantics(self.spec, "semantics" in layers)
        self.oid_v = {int(k): v for k, v in self.w["oids"].items()}
        self.oid_p = {v: k for k, v in self.oid_v.items()}
        self.off = self.w["oid_offset"]
        self.ready = {k: v for k, v in self.w["ready_status"].items()}
        self.sub_p = {v: k for k, v in self.w["sub_types"].items()}
        self.ps = self.w["param_status"]
        self.keys = {v: k for k, v in self.w["startup_keys"].items()}

    def oid_to_v(self, o: int) -> int:
        return self.oid_v.get(o, o + self.off if o > 0 else o)

    def oid_to_p(self, o: int) -> int:
        if o in self.oid_p:
            return self.oid_p[o]
        return o - self.off if o > self.off else (0 if o > 0 else o)

    # ------------------------------------------------------------ backend -> client
    def error_fields(self, f: dict, spans: list | None) -> dict:
        out = {}
        for c in "SVCMDHPWstcdn":
            if c not in f:
                continue
            v = f[c]
            if c in "SV":
                v = self.sem.severity(v)
            elif c == "C":
                v = self.sem.sqlstate(v)
            elif c in "MDHWstcdn":
                v = self.sem.text(self.dia.message(v))
            elif c == "P":
                v = str(self.dia.map_pos(spans or [], int(v))) if spans else v
            out[c] = v
        return out

    def fatal_bytes(self, code: str, msg: str, sev: str = "FATAL", hint: str | None = None) -> bytes:
        f = self.error_fields({"S": sev, "V": sev, "C": code, "M": msg}, None)
        if hint and HINTS:
            f["H"] = hint
        return self.V.encode("b:error", {"fields": f})


class Conn:
    def __init__(self, px: Proxy, r: asyncio.StreamReader, w: asyncio.StreamWriter):
        self.px, self.cr, self.cw = px, r, w
        self.pr: asyncio.StreamReader | None = None
        self.pw: asyncio.StreamWriter | None = None
        self.buf = bytearray()
        self.pending: list[dict] = []  # one entry per forwarded message that PostgreSQL answers
        self.rowdesc: list | None = None  # PG typoids + formats of the result being streamed
        self.portal_rd: dict[str, list | None] = {}
        self.stmt_spans: dict[str, list] = {}
        self.portal_stmt: dict[str, str] = {}

    # ---- client side reading
    async def read_frame(self, typed: bool, max_len: int = MAX_MSG):
        V = self.px.V
        while True:
            try:
                r = V.split(self.buf, 0, typed, max_len)
            except Malformed as e:
                raise Fatal("08P01", "invalid length of startup packet" if not typed else str(e)) from None
            if r is not None:
                t, body, n = r
                del self.buf[:n]
                return t, body
            chunk = await self.cr.read(65536)
            if not chunk:
                raise ConnectionError
            self.buf += chunk

    def send(self, b: bytes) -> None:
        self.cw.write(b)

    async def run(self) -> None:
        px = self.px
        try:
            self.send(px.V.encode("b:greeting", {"banner": f"{px.sem.product if px.sem.on else 'PostgreSQL'} "
                                                           f"{px.sem.s['version'] if px.sem.on else '16'}",
                                                 "magic": px.w["magic"]}))
            await self.cw.drain()
            params = await self.startup()
            if params is None:
                return
            await self.connect_pg(params)
            await self.session()
        except Fatal as f:
            self.send(px.fatal_bytes(f.code, f.msg, hint=f.hint))
        except (ConnectionError, asyncio.IncompleteReadError, OSError):
            pass
        finally:
            for w in (self.cw, self.pw):
                if w is not None:
                    try:
                        await w.drain()
                    except Exception:  # noqa: BLE001 - peer already gone
                        pass
                    w.close()

    async def session(self) -> None:
        """Relay both ways. A protocol error or Terminate from the client takes effect only after every
        earlier request has been answered, as in a server that handles messages one at a time."""
        c2p, p2c = asyncio.create_task(self.c2p()), asyncio.create_task(self.p2c())
        try:
            await asyncio.wait({c2p, p2c}, return_when=asyncio.FIRST_COMPLETED)
            if c2p.done() and not p2c.done():
                exc = c2p.exception()
                if isinstance(exc, (Fatal, Terminate)):
                    if isinstance(exc, Fatal):
                        self.pw.write(self.px.P.encode("f:flush", {}))
                    for _ in range(3000):
                        if not self.pending or p2c.done():
                            break
                        await asyncio.sleep(0.01)
                    await self.cw.drain()
                    if isinstance(exc, Fatal):
                        raise exc
                    self.pw.write(self.px.P.encode("f:terminate", {}))
        finally:
            for t in (c2p, p2c):
                t.cancel()
            for t in (c2p, p2c):
                try:
                    await t
                except BaseException:  # noqa: BLE001 - collected above
                    pass

    async def startup(self) -> dict | None:
        px, w = self.px, self.px.w
        while True:
            _, body = await self.read_frame(False, 10000)
            if len(body) < 4:
                raise Fatal("08P01", "invalid length of startup packet")
            r = Codec.R(px.V, body)
            code = r.u32()
            if code == w["ssl_magic"]:
                self.send(w["ssl_reply"].encode())
                await self.cw.drain()
                continue
            if code == w["cancel_magic"]:
                try:
                    pid, key = r.i32(), r.i32()
                    r.end()
                except Malformed:
                    return None
                await cancel_pg(pid, key)
                return None
            if code != w["magic"]:
                raise Fatal("0A000", f"unsupported frontend protocol {code}: server supports {w['magic']}")
            params: dict[str, str] = {}
            try:
                while True:
                    k = r.s()
                    if k == "":
                        break
                    params[k] = r.s()
                r.end()
            except Malformed:
                raise Fatal("08P01", "invalid startup packet layout: expected terminator as last byte") from None
            out = {}
            sk = w["startup_keys"]
            key_hint = (f'startup parameters are "{sk["user"]}" (user name), "{sk["database"]}" (database) '
                        f'and "{sk["application_name"]}" (application name)')
            for k, v in params.items():
                if k not in px.keys:
                    raise Fatal("42704", f'unrecognized configuration parameter "{k}"', key_hint)
                out[px.keys[k]] = v
            if "user" not in out:
                raise Fatal("28000", "no PostgreSQL user name specified in startup packet", key_hint)
            out.setdefault("database", out["user"])
            user = out["user"]
            if user not in USERS:
                raise Fatal("28000", f'role "{user}" does not exist')
            if USERS[user] is not None:
                self.send(px.V.encode("b:auth", {"code": w["auth_codes"]["cleartext"]}))
                await self.cw.drain()
                t, pbody = await self.read_frame(True, 10000)
                if px.V.front_kind.get(t) != "password":
                    raise Fatal("08P01", f"expected password response, got message type {ord(t)}")
                try:
                    pw = px.V.decode("f:password", pbody)["s"]
                except Malformed:
                    raise Fatal("08P01", "invalid password packet size") from None
                if pw != USERS[user]:
                    raise Fatal("28P01", f'password authentication failed for user "{user}"')
            return out

    async def connect_pg(self, params: dict) -> None:
        px = self.px
        self.pr, self.pw = await asyncio.open_unix_connection(PG_SOCK)
        kv = {"user": params["user"], "database": params["database"]}
        if "application_name" in params:
            kv["application_name"] = params["application_name"]
        kv["TimeZone"] = "UTC"
        if px.sem.on:
            kv["DateStyle"] = px.sem.s["datestyle"]
        body = struct.pack(">I", 196608) + b"".join(k.encode() + b"\0" + v.encode() + b"\0" for k, v in kv.items()) + b"\0"
        self.pw.write(struct.pack(">I", len(body) + 4) + body)
        # PostgreSQL's handshake (trust): AuthenticationOk, ParameterStatus*, BackendKeyData, ReadyForQuery
        while True:
            t, body = await self.read_pg()
            kind = px.P.back_kind.get(t)
            if kind == "auth":
                code = px.P.decode("b:auth", body)["code"]
                if code != 0:
                    raise Fatal("28000", "authentication method not supported")
                self.send(px.V.encode("b:auth", {"code": px.w["auth_codes"]["ok"]}))
                continue
            self.forward_backend(t, body)
            if kind in ("ready", "error"):
                await self.cw.drain()
                if kind == "error":
                    raise ConnectionError
                return

    async def read_pg(self) -> tuple[str, bytes]:
        hdr = await self.pr.readexactly(5)
        n = struct.unpack(">I", hdr[1:])[0] - 4
        return chr(hdr[0]), await self.pr.readexactly(n)

    # ---- client -> PostgreSQL
    async def c2p(self) -> None:
        px = self.px
        V, P = px.V, px.P
        while True:
            t, body = await self.read_frame(True)
            kind = V.front_kind.get(t)
            if kind is None:
                raise Fatal("08P01", f"invalid frontend message type {ord(t)}")
            try:
                m = V.decode("f:" + kind, body)
            except Malformed as e:
                raise Fatal("08P01", "invalid message format" if "string" not in str(e) else "invalid string in message")
            out = b""
            if kind == "query":
                sql, spans = px.dia.to_pg(m["sql"])
                self.pending.append({"k": "query", "spans": spans, "hints": px.dia.hints(m["sql"]) if HINTS else []})
                out = P.encode("f:query", {"sql": sql})
            elif kind == "parse":
                sql, spans = px.dia.to_pg(m["sql"])
                self.stmt_spans[m["name"]] = spans
                self.pending.append({"k": "parse", "spans": spans, "hints": px.dia.hints(m["sql"]) if HINTS else []})
                out = P.encode("f:parse", {"name": m["name"], "sql": sql, "types": [px.oid_to_p(o) for o in m["types"]]})
            elif kind == "bind":
                self.portal_stmt[m["portal"]] = m["stmt"]
                self.pending.append({"k": "bind"})
                out = P.encode("f:bind", m)
            elif kind == "describe":
                sub = px.sub_p.get(m["sub"], "?")
                self.pending.append({"k": "describe_" + sub, "name": m["name"]})
                out = P.encode("f:describe", {"sub": sub, "name": m["name"]})
            elif kind == "close":
                sub = px.sub_p.get(m["sub"], "?")
                self.pending.append({"k": "close"})
                out = P.encode("f:close", {"sub": sub, "name": m["name"]})
            elif kind == "execute":
                # learn the portal's result types first, so DataRows can be formatted
                self.pending.append({"k": "inject", "name": m["portal"]})
                out = P.encode("f:describe", {"sub": "P", "name": m["portal"]})
                spans = self.stmt_spans.get(self.portal_stmt.get(m["portal"], ""), [])
                self.pending.append({"k": "execute", "name": m["portal"], "spans": spans})
                out += P.encode("f:execute", m)
            elif kind == "sync":
                self.pending.append({"k": "sync"})
                out = P.encode("f:sync", {})
            elif kind == "flush":
                out = P.encode("f:flush", {})
            elif kind == "terminate":
                raise Terminate
            elif kind in ("copydata", "copydone", "copyfail", "password"):
                out = P.encode("f:" + kind, m)
            self.pw.write(out)
            await self.pw.drain()

    # ---- PostgreSQL -> client
    async def p2c(self) -> None:
        while True:
            try:
                t, body = await self.read_pg()
            except asyncio.IncompleteReadError:
                await self.cw.drain()
                raise ConnectionError from None
            self.forward_backend(t, body)
            await self.cw.drain()

    def forward_backend(self, t: str, body: bytes) -> None:
        px = self.px
        P, V, dia, sem = px.P, px.V, px.dia, px.sem
        kind = P.back_kind.get(t)
        m = P.decode("b:" + kind, body)
        head = self.pending[0] if self.pending else None
        hk = head["k"] if head else None
        swallow = False
        if kind in ("paramstatus", "notice", "notification"):
            pass  # asynchronous, never ends a pending entry
        elif kind == "error":
            spans = head.get("spans") if head else None
            if hk not in ("query", None):
                while self.pending and self.pending[0]["k"] != "sync":
                    self.pending.pop(0)
        elif hk == "query":
            if kind == "ready":
                self.pending.pop(0)
        elif hk in ("parse", "bind", "close"):
            self.pending.pop(0)
        elif hk == "describe_S":
            if kind in ("rowdesc", "nodata"):
                self.pending.pop(0)
        elif hk == "describe_P":
            if kind in ("rowdesc", "nodata"):
                self.portal_rd[head["name"]] = m["fields"] if kind == "rowdesc" else None
                self.pending.pop(0)
        elif hk == "inject":
            self.rowdesc = m["fields"] if kind == "rowdesc" else None
            self.pending.pop(0)
            swallow = True
        elif hk == "execute":
            if kind in ("complete", "emptyquery", "suspended"):
                self.pending.pop(0)
        elif hk == "sync":
            if kind == "ready":
                self.pending.pop(0)
        if swallow:
            return
        # translate
        if kind == "auth":
            out = {"code": px.w["auth_codes"]["ok"] if m["code"] == 0 else m["code"], "rest": m["rest"]}
        elif kind == "paramstatus":
            if m["name"] not in px.ps:
                return
            v = m["value"]
            if m["name"] == "server_version" and sem.on:
                v = sem.s["version"]
            out = {"name": px.ps[m["name"]], "value": sem.text(v)}
        elif kind == "ready":
            out = {"status": px.ready.get(m["status"], m["status"])}
        elif kind == "rowdesc":
            if hk == "query" or hk is None:
                self.rowdesc = m["fields"]
            out = {"fields": [{"name": sem.column(dia.column(f["name"])), "typoid": px.oid_to_v(f["typoid"]),
                               "typlen": f["typlen"], "typmod": f["typmod"], "format": f["format"]} for f in m["fields"]]}
        elif kind == "datarow":
            rd = self.rowdesc or []
            vals = []
            for i, v in enumerate(m["values"]):
                if v is not None and i < len(rd) and rd[i]["format"] == 0:
                    v = sem.value(rd[i]["typoid"], v)
                vals.append(v)
            out = {"values": vals}
        elif kind == "complete":
            out = {"tag": dia.tag(sem.tag(m["tag"]))}
        elif kind in ("error", "notice"):
            out = {"fields": px.error_fields(m["fields"], head.get("spans") if head else None)}
            hints = head.get("hints") if head else None
            if kind == "error" and hints:
                out["fields"]["H"] = "\n".join(([out["fields"]["H"]] if "H" in out["fields"] else []) + hints)
        elif kind == "paramdesc":
            out = {"types": [px.oid_to_v(o) for o in m["types"]]}
        else:
            out = m
        self.send(V.encode("b:" + kind, out))


async def cancel_pg(pid: int, key: int) -> None:
    try:
        r, w = await asyncio.open_unix_connection(PG_SOCK)
        w.write(struct.pack(">IIII", 16, PG_CANCEL, pid & 0xFFFFFFFF, key & 0xFFFFFFFF))
        await w.drain()
        await r.read()
        w.close()
    except OSError:
        pass


async def main() -> None:
    seed = int(os.environ["BBX_SEED"])
    layers = set(os.environ.get("BBX_LAYERS", "wire,dialect,semantics").split(","))
    px = Proxy(seed, layers)
    port = int(os.environ.get("BBX_PORT", "7000"))

    async def handle(r, w):
        await Conn(px, r, w).run()

    srv = await asyncio.start_server(handle, "0.0.0.0", port, limit=1 << 20)
    print(f"bbx oracle seed {seed} layers {sorted(layers)} on :{port}", file=sys.stderr, flush=True)
    async with srv:
        await srv.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
