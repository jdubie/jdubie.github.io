"""Feasibility baseline, not a solution: a tiny SQL subset whose whole state is a snapshot encoded in file names.

Each commit writes $PGDATA_DIR/v<N>.tmp/<seq>_<base64 chunk> (empty files), renames it to v<N>
(the atomic commit point) and removes older versions. One global lock; no real isolation.
"""

import asyncio
import base64
import json
import os
import re
import shutil
import struct

DATA = os.environ["PGDATA_DIR"]
CHUNK = 180
state: dict = {}
version = 0
lock = asyncio.Lock()


def load() -> None:
    global state, version
    vs = sorted(int(d[1:]) for d in os.listdir(DATA) if re.fullmatch(r"v\d+", d))
    for d in os.listdir(DATA):
        if d.endswith(".tmp"):
            shutil.rmtree(os.path.join(DATA, d))
    if vs:
        version = vs[-1]
        names = sorted(os.listdir(os.path.join(DATA, f"v{version}")))
        blob = "".join(n.split("_", 1)[1] for n in names)
        state = json.loads(base64.urlsafe_b64decode(blob + "=" * (-len(blob) % 4)))


def persist(new: dict) -> None:
    global version
    blob = base64.urlsafe_b64encode(json.dumps(new).encode()).decode().rstrip("=")
    v = version + 1
    tmp = os.path.join(DATA, f"v{v}.tmp")
    os.mkdir(tmp)
    for i in range(0, max(1, len(blob)), CHUNK):
        open(os.path.join(tmp, f"{i // CHUNK:08d}_{blob[i:i + CHUNK]}"), "w").close()
    os.rename(tmp, os.path.join(DATA, f"v{v}"))
    dfd = os.open(DATA, os.O_RDONLY)
    os.fsync(dfd)
    os.close(dfd)
    if version:
        shutil.rmtree(os.path.join(DATA, f"v{version}"), ignore_errors=True)
    version = v


def msg(t: bytes, body: bytes) -> bytes:
    return t + struct.pack("!I", len(body) + 4) + body


def lit(s: str):
    s = s.strip()
    if s.upper() == "NULL":
        return None
    if s.startswith("'"):
        return s[1:-1].replace("''", "'")
    return int(s) if re.fullmatch(r"-?\d+", s) else s


def text(v) -> bytes | None:
    if v is None:
        return None
    if isinstance(v, bool):
        return b"t" if v else b"f"
    return str(v).encode()


def rows_msg(cols: list[str], rows: list[list]) -> bytes:
    out = msg(b"T", struct.pack("!H", len(cols)) + b"".join(c.encode() + b"\0" + struct.pack("!IhIhih", 0, 0, 25, -1, -1, 0) for c in cols))
    for r in rows:
        body = struct.pack("!H", len(r))
        for v in r:
            b = text(v)
            body += struct.pack("!i", -1) if b is None else struct.pack("!i", len(b)) + b
        out += msg(b"D", body)
    return out + msg(b"C", f"SELECT {len(rows)}".encode() + b"\0")


def err(code: str, m: str) -> bytes:
    return msg(b"E", b"SERROR\0C" + code.encode() + b"\0M" + m.encode() + b"\0\0")


def split_values(s: str) -> list[list]:
    return [[lit(x) for x in re.findall(r"'(?:[^']|'')*'|[^,]+", g)] for g in re.findall(r"\(([^)]*)\)", s)]


class Session:
    def __init__(self):
        self.tx: dict | None = None
        self.failed = False

    def view(self) -> dict:
        return self.tx if self.tx is not None else state

    async def run(self, sql: str) -> bytes:
        global state
        s = sql.strip().rstrip(";").strip()
        u = s.upper()
        if u in ("BEGIN", "START TRANSACTION"):
            self.tx = json.loads(json.dumps(state))
            return msg(b"C", b"BEGIN\0")
        if u in ("COMMIT", "END"):
            if self.tx is None:
                return msg(b"C", b"COMMIT\0")
            new, failed, self.tx, self.failed = self.tx, self.failed, None, False
            if failed:
                return msg(b"C", b"ROLLBACK\0")
            async with lock:
                persist(new)
                state = new
            return msg(b"C", b"COMMIT\0")
        if u == "ROLLBACK":
            self.tx, self.failed = None, False
            return msg(b"C", b"ROLLBACK\0")
        if self.failed:
            return err("25P02", "current transaction is aborted, commands ignored until end of transaction block")
        work = json.loads(json.dumps(self.view()))
        try:
            out, changed = self.exec(work, s)
        except KeyError as e:
            out, changed = err("42P01", f"relation {e} does not exist"), False
        except Exception as e:  # noqa: BLE001
            out, changed = err("42601", f"unsupported: {e}"), False
        if out[:1] == b"E":
            if self.tx is not None:
                self.failed = True
            return out
        if changed:
            if self.tx is not None:
                self.tx = work
            else:
                async with lock:
                    persist(work)
                    state = work
        return out

    def exec(self, db: dict, s: str):
        if m := re.fullmatch(r"(?is)create table (\w+)\s*\((.*)\)", s):
            if m[1] in db:
                return err("42P07", "exists"), False
            db[m[1]] = {"cols": [c.split()[0] for c in m[2].split(",")], "rows": []}
            return msg(b"C", b"CREATE TABLE\0"), True
        if m := re.fullmatch(r"(?is)drop table (\w+)", s):
            del db[m[1]]
            return msg(b"C", b"DROP TABLE\0"), True
        if m := re.fullmatch(r"(?is)insert into (\w+) values\s*(.*)", s):
            rows = split_values(m[2])
            db[m[1]]["rows"] += rows
            return msg(b"C", f"INSERT 0 {len(rows)}".encode() + b"\0"), True
        if m := re.fullmatch(r"(?is)select count\(\*\) from (\w+)", s):
            return rows_msg(["count"], [[len(db[m[1]]["rows"])]]), False
        if m := re.fullmatch(r"(?is)select \* from (\w+)(?: order by (\w+))?", s):
            t = db[m[1]]
            rows = t["rows"]
            if m[2]:
                k = t["cols"].index(m[2])
                rows = sorted(rows, key=lambda r: (r[k] is None, r[k]))
            return rows_msg(t["cols"], rows), False
        if m := re.fullmatch(r"(?is)delete from (\w+)", s):
            n = len(db[m[1]]["rows"])
            db[m[1]]["rows"] = []
            return msg(b"C", f"DELETE {n}".encode() + b"\0"), True
        if re.fullmatch(r"(?is)select 1", s):
            return rows_msg(["?column?"], [[1]]), False
        raise ValueError(s[:40])


async def handle(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
    try:
        n = struct.unpack("!I", await r.readexactly(4))[0]
        body = await r.readexactly(n - 4)
        if struct.unpack("!I", body[:4])[0] == 80877103:
            w.write(b"N")
            n = struct.unpack("!I", await r.readexactly(4))[0]
            await r.readexactly(n - 4)
        sess = Session()
        w.write(msg(b"R", struct.pack("!I", 0)) + msg(b"Z", b"I"))
        while True:
            t = await r.readexactly(1)
            n = struct.unpack("!I", await r.readexactly(4))[0]
            body = await r.readexactly(n - 4)
            if t == b"X":
                break
            if t != b"Q":
                continue
            q = body.rstrip(b"\0").decode()
            out = b""
            stmts = [x for x in re.findall(r"(?:'(?:[^']|'')*'|[^;'])+", q) if x.strip()]
            for st in stmts or [""]:
                out += await sess.run(st) if st.strip() else msg(b"I", b"")
            status = b"I" if sess.tx is None else (b"E" if sess.failed else b"T")
            w.write(out + msg(b"Z", status))
            await w.drain()
    except (asyncio.IncompleteReadError, ConnectionError):
        pass
    finally:
        w.close()


async def main() -> None:
    load()
    srv = await asyncio.start_server(handle, "0.0.0.0", 5432)
    async with srv:
        await srv.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
