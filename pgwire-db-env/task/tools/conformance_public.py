"""Protocol conformance: scripted byte-level message sequences, compared with PostgreSQL 16's responses.

Each test sends a fixed sequence of frontend messages with `pgproto` and returns a transcript: the backend
messages normalized so that only what a correct server must reproduce remains:

  ErrorResponse / NoticeResponse  severity + SQLSTATE class (first two characters)
  RowDescription                  name, type OID, type size, typmod, format (not table OID / attnum)
  DataRow, CopyData               exact bytes (text or binary)
  ParameterStatus                 only the GUC_REPORT names clients depend on (REPORTED); server_version
                                  is reduced to its major version
  BackendKeyData                  presence only
  NotificationResponse            channel, payload, and whether the pid is the notifying session's

The expected transcript of every test is recorded from PostgreSQL 16 (make_reference.py), twice, and a test
whose two recordings differ is dropped. score = share of tests whose transcript matches exactly.

  python3 tools/conformance_public.py <host> <port> [filter]     # print transcripts (debugging)
"""

from __future__ import annotations

import socket
import struct
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.append(str(HERE.parent / "task/tools"))  # after grader/, so the kit's dbbox shim never shadows grader/dbbox.py
import pgproto as pp  # noqa: E402
from pgproto import Session  # noqa: E402

REPORTED = {"application_name", "client_encoding", "DateStyle", "integer_datetimes", "IntervalStyle", "is_superuser",
            "server_encoding", "server_version", "session_authorization", "standard_conforming_strings", "TimeZone"}
T_IO = 8.0


def _val(b: bytes | None):
    if b is None:
        return None
    try:
        s = b.decode()
        if s.isprintable() or all(c.isprintable() or c in "\n\t\r" for c in s):
            return s
    except UnicodeDecodeError:
        pass
    return "\\x" + b.hex()


def norm(t: bytes, p: bytes, ctx: dict | None = None):
    ctx = ctx if ctx is not None else {}
    if t in (b"E", b"N"):
        f = pp.fields(p)
        return [t.decode(), f.get("V", f.get("S", "")), f.get("C", "")[:2]]
    if t == b"T":
        return ["T", [[c["name"], c["type"], c["size"], c["typmod"], c["format"]] for c in pp.row_description(p)]]
    if t == b"D":
        return ["D", [_val(v) for v in pp.data_row(p)]]
    if t == b"C":
        return ["C", p.rstrip(b"\0").decode(errors="replace")]
    if t == b"Z":
        return ["Z", p.decode()]
    if t == b"t":
        return ["t", pp.param_description(p)]
    if t in (b"G", b"H", b"W"):
        fmt, cols = pp.copy_response(p)
        return [t.decode(), fmt, cols]
    if t == b"d":
        return ["d", _val(p)]
    if t == b"A":
        pid, ch, payload = pp.notification(p)
        return ["A", ch, payload, pid == ctx.get("notifier_pid", pid)]
    if t == b"S":
        k, v = pp.parameter_status(p)
        if k not in REPORTED:
            return None
        return ["S", k, v.split(".")[0] if k == "server_version" else v]
    if t == b"K":
        return ["K"]
    if t == b"V":
        (n,) = struct.unpack("!i", p[:4])
        return ["V", None if n < 0 else _val(p[4:4 + n])]
    if t == b"v":
        return ["v", p.hex()]
    if t == b"R":
        return ["R", struct.unpack("!I", p[:4])[0]]
    return [t.decode(errors="replace")]


class Tx:
    """A transcript under construction."""

    def __init__(self):
        self.items: list = []
        self.ctx: dict = {}

    def add(self, msgs, label: str | None = None) -> None:
        """Append normalized messages; a run of consecutive ParameterStatus messages is sorted (order is free)."""
        if label:
            self.items.append(["#", label])
        run: list = []
        for t, p in msgs:
            n = norm(t, p, self.ctx)
            if n is not None and n[0] == "S":
                run.append(n)
                continue
            self.items += sorted(run)
            run = []
            if n is not None:
                self.items.append(n)
        self.items += sorted(run)

    def note(self, *x) -> None:
        self.items.append(list(x))


def conn(h, user="postgres", **kw) -> Session:
    s = Session.connect(h["host"], h["port"], user=user, timeout=T_IO, **kw)
    if not any(t == b"Z" for t, _ in s.startup_msgs):
        raise pp.ProtocolError("startup failed: " + str([norm(t, p) for t, p in s.startup_msgs][-1:]))
    return s


def startup_summary(s: Session) -> list:
    ps = sorted(["S", k, v.split(".")[0] if k == "server_version" else v] for k, v in s.params.items() if k in REPORTED)
    kinds = [t.decode() for t, _ in s.startup_msgs if t not in (b"S",)]
    return [["auth", s.auth], ["K", any(t == b"K" for t, _ in s.startup_msgs)], ["last", kinds[-1:]], ["params", ps]]


def until_quiet(s: Session, wait: float) -> list[tuple[bytes, bytes]]:
    """Read whatever arrives within `wait` seconds (for asynchronous messages)."""
    out = []
    s.settimeout(wait)
    try:
        while True:
            out.append(s.recv())
    except (socket.timeout, TimeoutError):
        pass
    except pp.Closed:
        out.append((b"!", b""))  # normalized to ["!"]: the server closed the connection
    finally:
        s.settimeout(T_IO)
    return out


# ================================================================ startup and authentication

def t_startup_trust(h, tx):
    s = conn(h)
    tx.items += startup_summary(s)
    tx.add(s.simple("SELECT current_user, current_database(), session_user"))


def t_ssl_request_refused(h, tx):
    s = Session.open(h["host"], h["port"], T_IO)
    s.send(pp.ssl_request())
    tx.note("answer", s.recv_byte().decode(errors="replace"))
    s.handshake("postgres")
    tx.add(s.simple("SELECT 1"))




def _auth(h, tx, user, pw, sql=True):
    s = Session.open(h["host"], h["port"], T_IO)
    msgs = s.handshake(user, password=pw)
    tx.note("auth", s.auth)
    tx.note("end", norm(*msgs[-1]))
    if sql and msgs[-1][0] == b"Z":
        tx.add(s.simple("SELECT current_user"))


def t_scram_ok(h, tx):
    _auth(h, tx, "alice", "wonderland")




def t_md5_ok(h, tx):
    _auth(h, tx, "bob", "builder")




def t_cleartext_ok(h, tx):
    _auth(h, tx, "carol", "cleartext")




def t_unknown_role(h, tx):
    _auth(h, tx, "nobody_here", None)








# ================================================================ simple query

TYPES_SQL = ("SELECT 1::int2, 2::int4, 3::int8, 1.50::numeric(5,2), 1.5::float4, 2.25::float8, true, 'x'::text, "
             "'ab'::varchar(5), 'a'::char(3), '2024-02-29'::date, '12:34:56.5'::time, '2024-02-29 12:34:56.789'::timestamp, "
             "'2024-02-29 12:34:56+00'::timestamptz, '1 day 02:03:04'::interval, '\\xdeadbeef'::bytea, "
             "'a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11'::uuid, '{\"a\": 1}'::json, '{\"b\": [1, 2]}'::jsonb, ARRAY[1,2,3], "
             "ARRAY['a','b c'], NULL::int, 'nm'::name, 7::oid, -0.0001234::numeric, 1e300::float8, 'NaN'::float8")


def t_simple_types_text(h, tx):
    s = conn(h)
    tx.add(s.simple(TYPES_SQL))


def t_simple_multi_statement(h, tx):
    s = conn(h)
    tx.add(s.simple("SELECT 1 AS a; SELECT 'two' AS b, 2.0 AS c; VALUES (1), (2)"))


def t_simple_empty_query(h, tx):
    s = conn(h)
    tx.add(s.simple(""))
    tx.add(s.simple("  ;  "))


def t_simple_error_stops_rest(h, tx):
    s = conn(h)
    tx.add(s.simple("SELECT 1; SELECT 1/0; SELECT 3"))
    tx.add(s.simple("SELECT 4"))




def t_txn_status_bytes(h, tx):
    s = conn(h)
    for q in ("BEGIN", "SELECT 1", "SELECT nope", "SELECT 1", "ROLLBACK", "BEGIN", "SAVEPOINT a", "SELECT 1/0",
              "ROLLBACK TO a", "SELECT 2", "COMMIT", "COMMIT", "START TRANSACTION ISOLATION LEVEL SERIALIZABLE", "END"):
        tx.add(s.simple(q), q)




def t_command_tags(h, tx):
    s = conn(h)
    for q in ("CREATE TABLE c_tags (id int PRIMARY KEY, v text)", "INSERT INTO c_tags VALUES (1, 'a'), (2, 'b'), (3, 'c')",
              "UPDATE c_tags SET v = 'z' WHERE id > 1", "DELETE FROM c_tags WHERE id = 3", "SELECT * FROM c_tags ORDER BY id",
              "CREATE TABLE c_tags2 AS SELECT * FROM c_tags", "SELECT * INTO c_tags3 FROM c_tags",
              "MERGE INTO c_tags t USING (VALUES (1), (5)) v(id) ON t.id = v.id WHEN MATCHED THEN UPDATE SET v = 'm' "
              "WHEN NOT MATCHED THEN INSERT VALUES (v.id, 'n')",
              "CREATE INDEX c_tags_v ON c_tags (v)", "CREATE VIEW c_tags_view AS SELECT 1", "ALTER TABLE c_tags ADD COLUMN w int",
              "TRUNCATE c_tags2", "DROP VIEW c_tags_view", "DROP TABLE c_tags2, c_tags3", "CREATE SEQUENCE c_tags_seq",
              "SET search_path = public", "SHOW search_path", "RESET search_path", "BEGIN", "DECLARE c1 CURSOR FOR SELECT 1 UNION SELECT 2",
              "FETCH 1 FROM c1", "MOVE ALL IN c1", "CLOSE c1", "COMMIT", "VACUUM c_tags", "ANALYZE c_tags", "DISCARD ALL",
              "DO $$ BEGIN END $$", "PREPARE p1 AS SELECT $1::int", "EXECUTE p1(5)", "DEALLOCATE p1",
              "LOCK TABLE c_tags", "COPY (SELECT 1) TO STDOUT"):
        if q.startswith("COPY"):
            s.send(pp.query(q))
            tx.add(s.until_ready(), q)
        else:
            tx.add(s.simple(q), q)


def t_notice_response(h, tx):
    s = conn(h)
    tx.add(s.simple("DO $$ BEGIN RAISE NOTICE 'hello %', 42; RAISE WARNING 'careful'; END $$"))
    tx.add(s.simple("DROP TABLE IF EXISTS c_no_such_table"))
    tx.add(s.simple("SET client_min_messages = warning; DROP TABLE IF EXISTS c_no_such_table"))


















def _is_closed(s: Session) -> bool:
    s.settimeout(3)
    try:
        while True:
            if not s.sock.recv(1 << 16):
                return True
    except (socket.timeout, TimeoutError):
        return False
    except OSError:
        return True


# ================================================================ extended query

def t_ext_unnamed_basic(h, tx):
    s = conn(h)
    s.send(pp.parse("", "SELECT $1::int + 1 AS r, $2::text || '!' AS t"), pp.bind("", "", [b"41", b"hey"]),
           pp.describe("P", ""), pp.execute(""), pp.sync())
    tx.add(s.until_ready())


def t_ext_describe_statement(h, tx):
    s = conn(h)
    s.simple("CREATE TABLE c_desc (id int PRIMARY KEY, name varchar(20), amount numeric(10,2), at timestamptz, ok bool)")
    for sql in ("SELECT $1 + 1", "SELECT * FROM c_desc WHERE id = $1 AND name = $2",
                "INSERT INTO c_desc VALUES ($1, $2, $3, $4, $5)", "INSERT INTO c_desc (id) VALUES ($1) RETURNING id, amount",
                "UPDATE c_desc SET amount = $1 WHERE at < $2", "SELECT $1", "SELECT $1::numeric(8,3), $2::bigint[]",
                "SELECT count(*) FROM c_desc WHERE ok = $1", "SELECT $1 = ANY($2::int[])", "DELETE FROM c_desc",
                "SET search_path = public", "SELECT * FROM c_desc LIMIT $1 OFFSET $2", "SELECT $2::int, $1::text"):
        s.send(pp.parse("", sql), pp.describe("S", ""), pp.sync())
        tx.add(s.until_ready(), sql)






def t_ext_named_statement_reuse(h, tx):
    s = conn(h)
    s.send(pp.parse("s1", "SELECT $1::int * 10"), pp.sync())
    tx.add(s.until_ready())
    for v in (b"1", b"2", b"3"):
        s.send(pp.bind("", "s1", [v]), pp.execute(""), pp.sync())
        tx.add(s.until_ready())
    s.send(pp.parse("s1", "SELECT 1"), pp.sync())
    tx.add(s.until_ready(), "duplicate name")
    s.send(pp.close("S", "s1"), pp.sync())
    tx.add(s.until_ready(), "close")
    s.send(pp.bind("", "s1", [b"4"]), pp.execute(""), pp.sync())
    tx.add(s.until_ready(), "after close")
    s.send(pp.close("S", "never_existed"), pp.close("P", "never_existed"), pp.sync())
    tx.add(s.until_ready(), "close missing")
    s.send(pp.parse("", "SELECT 1"), pp.parse("", "SELECT 2"), pp.bind("", ""), pp.execute(""), pp.sync())
    tx.add(s.until_ready(), "unnamed replaced")




def t_ext_portal_row_limit(h, tx):
    s = conn(h)
    s.send(pp.parse("", "SELECT g, g * g FROM generate_series(1, 5) g"), pp.bind("", ""), pp.execute("", 2),
           pp.execute("", 2), pp.execute("", 2), pp.execute("", 2), pp.sync())
    tx.add(s.until_ready())






def t_ext_error_skips_to_sync(h, tx):
    s = conn(h)
    s.send(pp.parse("", "SELEC oops"), pp.bind("", ""), pp.describe("P", ""), pp.execute(""), pp.parse("x", "SELECT 1"),
           pp.sync(), pp.parse("", "SELECT 'recovered'"), pp.bind("", ""), pp.execute(""), pp.sync())
    tx.add(s.until_ready())
    tx.add(s.until_ready())
    s.send(pp.bind("", "x"), pp.execute(""), pp.sync())
    tx.add(s.until_ready(), "statement parsed after the error was skipped")


def t_ext_pipeline_implicit_txn(h, tx):
    s = conn(h)
    s.simple("CREATE TABLE c_pipe (id int PRIMARY KEY)")
    ins = pp.parse("ins", "INSERT INTO c_pipe VALUES ($1)")
    s.send(ins, pp.sync())
    s.until_ready()
    batch = b"".join(pp.bind("", "ins", [str(i).encode()]) + pp.execute("") for i in (1, 2, 3))
    s.send(batch, pp.bind("", "ins", [b"2"]), pp.execute(""), pp.bind("", "ins", [b"4"]), pp.execute(""), pp.sync())
    tx.add(s.until_ready(), "batch with duplicate key")
    s.send(pp.parse("", "SELECT count(*) FROM c_pipe"), pp.bind("", ""), pp.execute(""), pp.sync())
    tx.add(s.until_ready(), "nothing kept")
    s.send(*(pp.bind("", "ins", [str(i).encode()]) + pp.execute("") + pp.sync() for i in (10, 11, 10, 12)))
    for _ in range(4):
        tx.add(s.until_ready(), "one per sync")
    s.send(pp.parse("", "SELECT array_agg(id ORDER BY id) FROM c_pipe"), pp.bind("", ""), pp.execute(""), pp.sync())
    tx.add(s.until_ready())


















# ---------------------------------------------------------------- binary format

BIN_SQL = ("SELECT 1::int2, -2::int4, 3000000000::int8, 1.5::float4, -2.25::float8, true, false, 'tëxt'::text, "
           "'vc'::varchar(9), 'ch'::char(4), 'nm'::name, '\\x00ff10'::bytea, 12345.678::numeric, 0::numeric, "
           "-0.000123::numeric, 1e20::numeric, 'NaN'::numeric, '2024-02-29'::date, '1999-12-31'::date, '23:59:59.123456'::time, "
           "'2024-02-29 12:34:56.789'::timestamp, '1970-01-01 00:00:00+00'::timestamptz, '1 year 2 mons 3 days 04:05:06.7'::interval, "
           "'a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11'::uuid, '{\"k\": [1, 2]}'::json, '{\"k\": [1, 2]}'::jsonb, ARRAY[1, NULL, 3]::int4[], "
           "ARRAY[['a','b'],['c','d']]::text[], 42::oid, NULL::int4, 'x'::\"char\", ARRAY[]::int8[]")


def t_bin_results_all(h, tx):
    s = conn(h)
    s.send(pp.parse("", BIN_SQL), pp.bind("", "", result_formats=[1]), pp.describe("P", ""), pp.execute(""), pp.sync())
    tx.add(s.until_ready())




def _b(fmt: str, *v) -> bytes:
    return struct.pack("!" + fmt, *v)


def t_bin_params(h, tx):
    s = conn(h)
    import uuid as _uuid

    params = [_b("h", -7), _b("i", 123456), _b("q", -9_000_000_000), _b("f", 0.5), _b("d", 1e-3), b"\x01", "héllo".encode(),
              b"\x00\x01\xfe", _b("i", 8826), _b("q", 3_600_000_000 + 250_000), _b("q", 0), _b("qii", 3_723_000_000, 5, 14),
              _uuid.UUID("12345678-1234-5678-1234-567812345678").bytes,
              _b("hhhh", 2, 0, 0, 2) + _b("hh", 12, 3400),  # numeric 12.34: ndigits, weight, sign, dscale, base-10000 digits
              _b("iii", 1, 0, 23) + _b("ii", 2, 1) + _b("i", 4) + _b("i", 7) + _b("i", 4) + _b("i", 8)]
    oids = [21, 23, 20, 700, 701, 16, 25, 17, 1082, 1083, 1114, 1186, 2950, 1700, 1007]
    sql = "SELECT " + ", ".join(f"${i + 1}::text" for i in range(len(oids)))
    s.send(pp.parse("", sql, oids), pp.bind("", "", params, [1]), pp.execute(""), pp.sync())
    tx.add(s.until_ready())




# ---------------------------------------------------------------- COPY

def t_copy_from_text(h, tx):
    s = conn(h)
    s.simple("CREATE TABLE c_cp (id int, name text, amt numeric(6,2), at date)")
    s.send(pp.query("COPY c_cp FROM STDIN"))
    tx.add(s.until({b"G", b"E", b"Z"}))
    s.send(pp.copy_data(b"1\talpha\t1.50\t2024-01-01\n2\tbe"), pp.copy_data(b"ta\t\\N\t2024-01-02\n3\ttab\\there\t-0.5\t\\N\n"),
           pp.copy_data(b"4\tback\\\\slash\t9999.99\t2024-12-31\n"), pp.copy_done())
    tx.add(s.until_ready())
    tx.add(s.simple("SELECT * FROM c_cp ORDER BY id"))






def t_copy_to_text_csv_binary(h, tx):
    s = conn(h)
    s.simple("CREATE TABLE c_out (id int, v text, d date); INSERT INTO c_out VALUES (1, 'a\tb', '2020-02-02'), (2, NULL, NULL), "
             "(3, 'q\"uote,', '1999-01-01')")
    for q in ("COPY c_out TO STDOUT", "COPY c_out TO STDOUT WITH (FORMAT csv, HEADER)", "COPY (SELECT id, v FROM c_out ORDER BY id DESC) TO STDOUT (FORMAT binary)",
              "COPY c_out (v) TO STDOUT WITH (DELIMITER '|', NULL 'nil')", "COPY (SELECT 1 WHERE false) TO STDOUT"):
        s.send(pp.query(q))
        tx.add(s.until_ready(), q)








# ---------------------------------------------------------------- cancel

def t_cancel_request(h, tx):
    a = conn(h)
    a.send(pp.query("SELECT pg_sleep(20)"))
    time.sleep(1.0)
    c = Session.open(h["host"], h["port"], T_IO)
    c.send(pp.cancel_request(a.pid, a.key))
    t0 = time.time()
    msgs = a.until_ready()
    tx.add(msgs)
    tx.note("cancelled within 5 s", time.time() - t0 < 5)
    tx.note("cancel socket closed", _is_closed(c))
    tx.add(a.simple("SELECT 'still usable'"))






# ---------------------------------------------------------------- LISTEN / NOTIFY

def t_notify_other_session(h, tx):
    a, b = conn(h), conn(h)
    tx.ctx["notifier_pid"] = b.pid
    tx.add(a.simple("LISTEN chan_one"))
    tx.add(b.simple("NOTIFY chan_one, 'hello'"))
    tx.add(until_quiet(a, 2), "idle listener receives")
    tx.add(b.simple("SELECT pg_notify('chan_one', 'via function'), pg_notify('other_chan', 'ignored')"))
    tx.add(until_quiet(a, 2), "pg_notify")








# ---------------------------------------------------------------- function call (optional in the task)



TESTS = {name[2:]: fn for name, fn in sorted(globals().items()) if name.startswith("t_") and callable(fn)}


def run_test(name: str, host: str, port: int) -> list:
    tx = Tx()
    h = {"host": host, "port": port}
    try:
        TESTS[name](h, tx)
    except (socket.timeout, TimeoutError):
        tx.note("TIMEOUT")
    except pp.Closed:
        tx.note("CLOSED")
    except (pp.ProtocolError, OSError, ValueError, IndexError, struct.error, KeyError, AssertionError) as e:
        tx.note("EXC", type(e).__name__)
    return tx.items


def run_all(host: str, port: int, names: list[str] | None = None) -> dict[str, list]:
    return {n: run_test(n, host, port) for n in (names or list(TESTS))}


if __name__ == "__main__":
    import json

    flt = sys.argv[3] if len(sys.argv) > 3 else ""
    for n in TESTS:
        if flt in n:
            print(n, json.dumps(run_test(n, sys.argv[1], int(sys.argv[2])), ensure_ascii=False)[:3000])
