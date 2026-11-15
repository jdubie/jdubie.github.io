"""psycopg 3 against the server under test. Prints one JSON object {item: result}.

Each item is independent: it opens its own connection(s) and records a deterministic result, or
"ERR <exception class> <sqlstate class>" if it raised. The grader compares every item with what the same
script printed against PostgreSQL 16.

  /opt/clients/venv/bin/python psycopg_suite.py <host> [port] [item-filter]
"""

import datetime as dt
import json
import sys
import threading
import time
import traceback
import uuid
from decimal import Decimal

import psycopg
from psycopg import sql
from psycopg.rows import dict_row
from psycopg.types import TypeInfo
from psycopg.types.enum import EnumInfo
from psycopg.types.json import Json, Jsonb

HOST = sys.argv[1]
PORT = int(sys.argv[2]) if len(sys.argv) > 2 else 5432
FILTER = sys.argv[3] if len(sys.argv) > 3 else ""
ITEMS = {}


def item(fn):
    ITEMS[fn.__name__] = fn
    return fn


def connect(user="postgres", password=None, **kw):
    return psycopg.connect(host=HOST, port=PORT, user=user, password=password, dbname="regression", connect_timeout=10, **kw)


def r(v):
    """Deterministic, JSON-safe rendering of Python values psycopg returns."""
    if isinstance(v, (list, tuple)):
        return [r(x) for x in v]
    if isinstance(v, dict):
        return {str(k): r(x) for k, x in sorted(v.items())}
    if isinstance(v, (bytes, bytearray, memoryview)):
        return "bytes:" + bytes(v).hex()
    if isinstance(v, float):
        return "float:" + repr(v)
    if v is None or isinstance(v, (bool, int, str)):
        return v
    return f"{type(v).__name__}:{v!s}"


# ---------------------------------------------------------------- authentication

@item
def auth_scram():
    with connect("alice", "wonderland") as c:
        return r(c.execute("SELECT current_user").fetchone())


@item
def auth_md5():
    with connect("bob", "builder") as c:
        return r(c.execute("SELECT current_user").fetchone())


@item
def auth_cleartext():
    with connect("carol", "cleartext") as c:
        return r(c.execute("SELECT current_user").fetchone())


@item
def auth_wrong_password():
    try:
        connect("alice", "bad").close()
        return "connected"
    except psycopg.OperationalError:
        return "refused"


@item
def connection_info():
    with connect() as c:
        i = c.info
        return [i.server_version // 10000, i.parameter_status("integer_datetimes"), i.parameter_status("client_encoding"),
                i.parameter_status("standard_conforming_strings"), i.encoding, i.transaction_status.name]


# ---------------------------------------------------------------- queries and types

@item
def params_text():
    with connect() as c:
        return r(c.execute("SELECT %s::int + 1, %s, %s, %s::text IS NULL", (41, "x'y", None, None)).fetchall())


TYPES_DDL = ("CREATE TABLE pg_types_t (i2 int2, i4 int4, i8 int8, f4 float4, f8 float8, n numeric(12,4), b bool, t text, "
             "vc varchar(10), by bytea, d date, tm time, ts timestamp, tz timestamptz, iv interval, u uuid, j json, jb jsonb, "
             "ai int4[], at text[])")
TYPES_ROW = (-3, 70000, 2**40, 1.25, -0.1, Decimal("1234.5678"), True, "tëxt\\", "short", b"\x00\xff", dt.date(2024, 2, 29),
             dt.time(23, 59, 1, 500), dt.datetime(2001, 2, 3, 4, 5, 6, 789000),
             dt.datetime(2001, 2, 3, 4, 5, 6, tzinfo=dt.timezone(dt.timedelta(hours=2))), dt.timedelta(days=3, seconds=7, microseconds=8),
             uuid.UUID("12345678-1234-5678-1234-567812345678"), Json({"a": [1, None]}), Jsonb({"b": {"c": "d"}}), [1, None, 3], ["x", "y z"])


@item
def types_roundtrip_text():
    with connect() as c:
        c.execute(TYPES_DDL)
        c.execute("INSERT INTO pg_types_t VALUES (" + ", ".join(["%s"] * len(TYPES_ROW)) + ")", TYPES_ROW)
        c.execute("SET TimeZone = 'UTC'")
        return r(c.execute("SELECT * FROM pg_types_t").fetchone())


@item
def types_roundtrip_binary():
    with connect() as c:
        c.execute(TYPES_DDL.replace("pg_types_t", "pg_types_b"))
        c.execute("INSERT INTO pg_types_b VALUES (" + ", ".join(["%b"] * len(TYPES_ROW)) + ")", TYPES_ROW)
        c.execute("SET TimeZone = 'UTC'")
        with c.cursor(binary=True) as cur:
            return r(cur.execute("SELECT * FROM pg_types_b").fetchone())


@item
def binary_results_expressions():
    with connect() as c, c.cursor(binary=True) as cur:
        return r(cur.execute("SELECT 1.5::numeric * 3, -0.000001::numeric, 99999999999999999999::numeric, 'NaN'::numeric, "
                             "'2000-01-01'::date - 1, interval '-1 mon 2 days', ARRAY[[1,2],[3,4]], "
                             "ARRAY[]::text[], '{\"k\": 1}'::jsonb").fetchone())


@item
def dict_rows_and_description():
    with connect(row_factory=dict_row) as c:
        cur = c.execute("SELECT 1 AS one, 'two'::varchar(5) AS two, 3.50::numeric(6,2) AS three")
        desc = [(d.name, d.type_code, d.internal_size, d.precision, d.scale) for d in cur.description]
        return r([cur.fetchone(), desc])


@item
def large_result():
    with connect() as c:
        rows = c.execute("SELECT g, md5(g::text) FROM generate_series(1, 50000) g").fetchall()
        return [len(rows), rows[0][1], rows[-1][1]]


# ---------------------------------------------------------------- server-side prepared statements

@item
def prepared_statements():
    with connect() as c:
        c.prepare_threshold = 0
        c.execute("CREATE TABLE pg_prep (id int PRIMARY KEY, v text)")
        out = []
        for i in range(8):
            c.execute("INSERT INTO pg_prep VALUES (%s, %s)", (i, f"v{i}"), prepare=True)
            out.append(c.execute("SELECT v FROM pg_prep WHERE id = %s", (i,), prepare=True).fetchone()[0])
        names = c.execute("SELECT count(*) FROM pg_prepared_statements WHERE NOT from_sql").fetchone()[0]
        return [out, names]


@item
def executemany_pipelined():
    with connect() as c:
        c.execute("CREATE TABLE pg_many (id int PRIMARY KEY, v text)")
        with c.cursor() as cur:
            cur.executemany("INSERT INTO pg_many VALUES (%s, %s)", [(i, "r%d" % i) for i in range(500)])
        return r(c.execute("SELECT count(*), md5(string_agg(v, ',' ORDER BY id)) FROM pg_many").fetchone())


@item
def executemany_returning():
    with connect() as c:
        c.execute("CREATE TABLE pg_many_r (id serial PRIMARY KEY, v text)")
        with c.cursor() as cur:
            cur.executemany("INSERT INTO pg_many_r (v) VALUES (%s) RETURNING id, v", [("a",), ("b",), ("c",)], returning=True)
            out = []
            while True:
                out.append(cur.fetchone())
                if not cur.nextset():
                    break
        return r(out)


@item
def pipeline_mode():
    with connect() as c:
        c.execute("CREATE TABLE pg_pipe (id int PRIMARY KEY)")
        c.commit()
        res = []
        with c.pipeline() as p:
            curs = [c.execute("INSERT INTO pg_pipe VALUES (%s) RETURNING id", (i,)) for i in range(5)]
            p.sync()
            res = [cur.fetchone()[0] for cur in curs]
        c.commit()
        err = None
        try:
            with c.pipeline():
                c.execute("INSERT INTO pg_pipe VALUES (100)")
                c.execute("INSERT INTO pg_pipe VALUES (1)")
                c.execute("INSERT INTO pg_pipe VALUES (101)")
        except psycopg.Error as e:
            err = e.sqlstate
        c.rollback()
        return r([res, err, c.execute("SELECT array_agg(id ORDER BY id) FROM pg_pipe").fetchone()[0]])


# ---------------------------------------------------------------- COPY

@item
def copy_text_in_out():
    with connect() as c:
        c.execute("CREATE TABLE pg_copy (id int, v text, d date, n numeric)")
        rows = [(i, f"tab\there,\"quote\" \\ {i}" if i % 3 else None, dt.date(2020, 1, 1) + dt.timedelta(days=i), Decimal(i) / 7)
                for i in range(2000)]
        with c.cursor().copy("COPY pg_copy FROM STDIN") as cp:
            for row in rows:
                cp.write_row(row)
        out = []
        with c.cursor().copy("COPY (SELECT * FROM pg_copy ORDER BY id DESC LIMIT 3) TO STDOUT") as cp:
            for row in cp.rows():
                out.append(row)
        agg = c.execute("SELECT count(*), md5(string_agg(coalesce(v, '~') || d || n, '|' ORDER BY id)) FROM pg_copy").fetchone()
        return r([agg, out])


@item
def copy_binary_in_out():
    with connect() as c:
        c.execute("CREATE TABLE pg_copyb (id int8, v text, f float8, ts timestamptz, j jsonb)")
        with c.cursor().copy("COPY pg_copyb FROM STDIN (FORMAT BINARY)") as cp:
            cp.set_types(["int8", "text", "float8", "timestamptz", "jsonb"])
            for i in range(1000):
                cp.write_row((i, None if i % 10 == 0 else f"row {i}", i / 3,
                              dt.datetime(2020, 1, 1, tzinfo=dt.timezone.utc) + dt.timedelta(seconds=i), Jsonb({"i": i})))
        c.execute("SET TimeZone = 'UTC'")
        out = []
        with c.cursor().copy("COPY (SELECT * FROM pg_copyb WHERE id % 250 = 1 ORDER BY id) TO STDOUT (FORMAT BINARY)") as cp:
            cp.set_types(["int8", "text", "float8", "timestamptz", "jsonb"])
            out = list(cp.rows())
        agg = c.execute("SELECT count(*), count(v), sum(f)::numeric(20,6), max(ts)::text FROM pg_copyb").fetchone()
        return r([agg, out])


@item
def copy_csv_header_in():
    with connect() as c:
        c.execute("CREATE TABLE pg_csv (a int, b text)")
        with c.cursor().copy("COPY pg_csv FROM STDIN WITH (FORMAT csv, HEADER)") as cp:
            cp.write(b'a,b\n1,"x,y"\n2,\n3,"line\nbreak"\n')
        return r(c.execute("SELECT * FROM pg_csv ORDER BY a").fetchall())


# ---------------------------------------------------------------- transactions and errors

@item
def transactions_and_savepoints():
    with connect() as c:
        c.autocommit = True
        c.execute("CREATE TABLE pg_tx (v int)")
        with c.transaction():
            c.execute("INSERT INTO pg_tx VALUES (1)")
            try:
                with c.transaction():
                    c.execute("INSERT INTO pg_tx VALUES (2)")
                    c.execute("SELECT 1/0")
            except psycopg.errors.DivisionByZero:
                pass
            c.execute("INSERT INTO pg_tx VALUES (3)")
        try:
            with c.transaction():
                c.execute("INSERT INTO pg_tx VALUES (4)")
                raise RuntimeError("abort")
        except RuntimeError:
            pass
        return r(c.execute("SELECT array_agg(v ORDER BY v) FROM pg_tx").fetchone())


@item
def isolation_and_readonly():
    with connect() as c:
        c.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ
        c.read_only = True
        a = c.execute("SELECT current_setting('transaction_isolation'), current_setting('transaction_read_only')").fetchone()
        try:
            c.execute("CREATE TABLE pg_ro (a int)")
            b = "created"
        except psycopg.Error as e:
            b = e.sqlstate
        c.rollback()
        return r([a, b])


@item
def error_diagnostics():
    with connect() as c:
        c.execute("CREATE TABLE pg_err (id int CONSTRAINT pg_err_pk PRIMARY KEY, v int CONSTRAINT v_pos CHECK (v > 0))")
        c.execute("INSERT INTO pg_err VALUES (1, 1)")
        c.commit()
        out = []
        for q in ("INSERT INTO pg_err VALUES (1, 1)", "INSERT INTO pg_err VALUES (2, -1)", "SELECT * FROM pg_nope", "SELEC"):
            try:
                c.execute(q)
            except psycopg.Error as e:
                d = e.diag
                out.append([type(e).__name__, d.sqlstate, d.severity_nonlocalized, d.table_name, d.constraint_name,
                            d.statement_position is not None])
                c.rollback()
        return out


@item
def error_then_usable():
    with connect() as c:
        try:
            c.execute("SELECT 1/0")
        except psycopg.errors.DivisionByZero:
            pass
        st = c.info.transaction_status.name
        c.rollback()
        return [st, c.execute("SELECT 'ok'").fetchone()[0]]


# ---------------------------------------------------------------- cursors, notify, cancel, catalogs

@item
def server_side_cursor():
    with connect() as c:
        with c.cursor(name="big_cursor") as cur:
            cur.itersize = 100
            cur.execute("SELECT g FROM generate_series(1, 1000) g")
            first = cur.fetchmany(3)
            rest = sum(1 for _ in cur)
            return r([first, rest])


@item
def scrollable_cursor():
    with connect() as c:
        with c.cursor(name="scroller", scrollable=True) as cur:
            cur.execute("SELECT g FROM generate_series(1, 10) g")
            cur.scroll(5)
            a = cur.fetchone()
            cur.scroll(0, "absolute")
            b = cur.fetchone()
            return r([a, b])


@item
def listen_notify():
    with connect(autocommit=True) as listener, connect(autocommit=True) as talker:
        listener.execute("LISTEN pg3_chan")
        talker.execute("NOTIFY pg3_chan, 'first'")
        talker.execute("SELECT pg_notify('pg3_chan', 'second')")
        got = []
        for n in listener.notifies(timeout=5, stop_after=2):
            got.append([n.channel, n.payload, n.pid == talker.info.backend_pid])
        return got


@item
def cancel_query():
    with connect() as c:
        def later():
            time.sleep(1)
            c.cancel_safe()
        threading.Thread(target=later, daemon=True).start()
        t0 = time.time()
        try:
            c.execute("SELECT pg_sleep(15)")
            out = "not cancelled"
        except psycopg.errors.QueryCanceled:
            out = "QueryCanceled"
        c.rollback()
        return [out, time.time() - t0 < 6, c.execute("SELECT 1").fetchone()[0]]


@item
def statement_timeout():
    with connect() as c:
        c.execute("SET statement_timeout = '300ms'")
        try:
            c.execute("SELECT pg_sleep(5)")
            return "no timeout"
        except psycopg.errors.QueryCanceled as e:
            return e.sqlstate


@item
def typeinfo_and_enum():
    with connect() as c:
        ti = TypeInfo.fetch(c, "numeric")
        c.execute("CREATE TYPE pg_mood AS ENUM ('sad', 'ok', 'happy')")
        ei = EnumInfo.fetch(c, "pg_mood")
        c.execute("CREATE TABLE pg_moods (m pg_mood)")
        c.execute("INSERT INTO pg_moods VALUES ('happy'), ('sad')")
        return r([ti.oid, ti.array_oid, ei.labels, c.execute("SELECT m FROM pg_moods ORDER BY m").fetchall()])


@item
def sql_composition_identifiers():
    with connect() as c:
        c.execute(sql.SQL("CREATE TABLE {} ({} int)").format(sql.Identifier("Weird Table"), sql.Identifier("Col\"umn")))
        c.execute(sql.SQL("INSERT INTO {} VALUES ({})").format(sql.Identifier("Weird Table"), sql.Literal(5)))
        return r(c.execute('SELECT "Col""umn" FROM "Weird Table"').fetchall())


def main():
    out = {}
    for name, fn in ITEMS.items():
        if FILTER and FILTER not in name:
            continue
        t0 = time.time()
        try:
            out[name] = fn()
        except psycopg.Error as e:
            out[name] = f"ERR {type(e).__name__} {(e.sqlstate or '')[:2]}"
        except Exception as e:  # noqa: BLE001
            out[name] = f"ERR {type(e).__name__}"
            print(name, traceback.format_exc()[-800:], file=sys.stderr)
        print(f"{name} {time.time() - t0:.1f}s", file=sys.stderr, flush=True)
    print(json.dumps(out, ensure_ascii=False))


if __name__ == "__main__":
    main()
