"""Generate the hidden workloads from a workload seed, in a given variant.

Workloads are written as PostgreSQL SQL and abstract protocol steps, then rendered into the variant's
bytes (`render`). The seed that generates them is never shown to the model: what it saw while probing
cannot be replayed, because table names, values and statement mixes are all drawn fresh here.

  sql       sessions as user guest: DDL, DML, queries, errors, transactions (simple query flow)
  protocol  handshakes, the extended flow, pipelining, malformed and unknown messages, cancel, terminate
"""

from __future__ import annotations

import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "oracle"))
from variant import Codec, Dialect  # noqa: E402

N_SQL, N_PROTOCOL_PER_TEMPLATE = 60, 2
ADMIN_PW = "opensesame"
OID = {"bool": 16, "int8": 20, "int2": 21, "int4": 23, "text": 25, "float8": 701, "numeric": 1700, "date": 1082, "unknown": 0}


# ------------------------------------------------------------------ values

def tname(rng: random.Random, tag: str = "t") -> str:
    return tag + "".join(rng.choice("qxjvwz") for _ in range(3)) + str(rng.randint(10, 999))


def word(rng: random.Random) -> str:
    s = "".join(rng.choice(["qy", "xa", "jo", "wu", "zy", "vex", "kyq", "jux", "ow", "ix"]) for _ in range(rng.randint(1, 3)))
    if rng.random() < 0.08:
        s += "'s"
    if rng.random() < 0.08:
        s = s.capitalize() + " " + s
    return s


def lit(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


COLS = {  # name -> (SQL type, value generator)
    "qty": ("integer", lambda r: str(r.randint(-5, 120))),
    "price": ("numeric(10,2)", lambda r: f"{r.randint(0, 9999) / 100:.2f}"),
    "flag": ("boolean", lambda r: r.choice(["true", "false"])),
    "score": ("double precision", lambda r: r.choice(["0.5", "1.25", "-3.75", "2.1", "100", "0.1", "1e10", "3.14159"])),
    "born": ("date", lambda r: lit(f"{r.randint(1950, 2030)}-{r.randint(1, 12):02d}-{r.randint(1, 28):02d}")),
    "big": ("bigint", lambda r: str(r.choice([r.randint(-10**12, 10**12), r.randint(0, 50), 9007199254740993]))),
    "small": ("smallint", lambda r: str(r.randint(-300, 300))),
    "ratio": ("real", lambda r: r.choice(["0.25", "1.5", "-2", "7.125"])),
}


# ------------------------------------------------------------------ sql sessions

def sql_session(rng: random.Random) -> list[str]:
    t, u = tname(rng), tname(rng, "u")
    extra = rng.sample(list(COLS), rng.randint(3, 5))
    if "qty" not in extra:
        extra[0] = "qty"
    defs = ["id integer PRIMARY KEY", "name text NOT NULL"]
    for c in extra:
        d = f"{c} {COLS[c][0]}"
        if c == "qty" and rng.random() < 0.6:
            d += " CHECK (qty >= -10)"
        if c == "flag" and rng.random() < 0.5:
            d += " DEFAULT false"
        defs.append(d)
    out = [f"CREATE TABLE {t} ({', '.join(defs)})"]
    ids = rng.sample(range(1, 200), 14)

    def row(i: int) -> str:
        vals = [str(i), lit(word(rng))]
        for c in extra:
            vals.append("NULL" if rng.random() < 0.12 else COLS[c][1](rng))
        return "(" + ", ".join(vals) + ")"

    k = 0
    while k < len(ids):
        n = rng.randint(1, 4)
        stmt = f"INSERT INTO {t} VALUES " + ", ".join(row(i) for i in ids[k:k + n])
        if rng.random() < 0.3:
            stmt += rng.choice([" RETURNING id", " RETURNING id, name", " RETURNING *"])
        out.append(stmt)
        k += n
    q = "qty"
    some = rng.sample(ids, 4)
    num = [c for c in extra if c in ("qty", "price", "score", "big", "small", "ratio")]
    queries = [
        f"SELECT * FROM {t} ORDER BY id",
        f"SELECT id, name FROM {t} WHERE {q} > {rng.randint(0, 60)} ORDER BY id",
        f"SELECT count(*) FROM {t} WHERE {q} < {rng.randint(10, 100)}",
        f"SELECT count(*), sum({q}), min({q}), max({q}) FROM {t}",
        f"SELECT avg({q}) FROM {t}",
        f"SELECT upper(name), length(name) FROM {t} ORDER BY id LIMIT {rng.randint(2, 6)} OFFSET {rng.randint(0, 4)}",
        f"SELECT DISTINCT {q} % 3 AS m FROM {t} WHERE {q} IS NOT NULL ORDER BY m",
        f"SELECT id FROM {t} WHERE name LIKE '{rng.choice(['qy', 'xa', 'jo', 'wu', 'zy'])}%' ORDER BY id DESC",
        f"SELECT id, CASE WHEN {q} > {rng.randint(10, 80)} THEN 'hi' WHEN {q} IS NULL THEN 'none' ELSE 'lo' END AS lvl FROM {t} ORDER BY id",
        f"SELECT id, coalesce({q}, -1) FROM {t} ORDER BY id",
        f"SELECT id FROM {t} WHERE {q} BETWEEN {rng.randint(0, 30)} AND {rng.randint(40, 100)} ORDER BY id",
        f"SELECT id, name FROM {t} WHERE id IN ({', '.join(map(str, some))}) ORDER BY id",
        f"SELECT id FROM {t} WHERE {q} IS NULL OR NOT ({q} > 50) ORDER BY id",
        f"SELECT {q} > 20 AS big, count(*) FROM {t} GROUP BY {q} > 20 ORDER BY 1",
        f"SELECT {q} % 4 AS g, count(*), max(name) FROM {t} GROUP BY g HAVING count(*) > 1 ORDER BY g",
        f"SELECT id::text || '-' || name AS tag, {q}::bigint * 1000 FROM {t} ORDER BY id DESC LIMIT 5",
        f"SELECT CAST({q} AS text), {q}::numeric / 7 FROM {t} WHERE {q} IS NOT NULL ORDER BY id LIMIT 4",
        f"SELECT abs(-{rng.randint(1, 99)}), round({rng.randint(100, 9999)}.{rng.randint(10, 99)}, 1), "
        f"substr('{word(rng).replace(chr(39), '')}xyz', 2, 3), replace('aXbXc', 'X', '{rng.choice('pqr')}')",
        f"SELECT greatest({rng.randint(1, 9)}, {rng.randint(1, 9)}, {rng.randint(1, 9)}), least(4, 2, 8), nullif(1, 1), nullif(2, 1), "
        f"concat('a', NULL, 'b'), lower('MiXeD')",
        f"SELECT {rng.randint(1, 50)} + {rng.randint(1, 50)}, {rng.randint(10, 99)} / 4, {rng.randint(10, 99)}.0 / 4, 'ab' || 'cd', "
        f"true AND false, NULL IS NULL, 2 > 1, {rng.randint(1, 9)}::boolean",
        f"SELECT 1.5::real, 2.25::double precision, 3::smallint, 9999999999::bigint, '12.50'::numeric, 0.1 + 0.2, '2021-02-03'::date",
    ]
    for c in num[1:]:
        queries.append(f"SELECT id, {c}, {c} * 2 FROM {t} ORDER BY id")
    if "born" in extra:
        queries.append(f"SELECT born, born + 30 FROM {t} WHERE born IS NOT NULL ORDER BY born, id")
    if "flag" in extra:
        queries.append(f"SELECT flag, count(*) FROM {t} GROUP BY flag ORDER BY flag")
        queries.append(f"SELECT id FROM {t} WHERE flag IS TRUE OR flag IS NULL ORDER BY id")
    errors = [
        f"INSERT INTO {t} (id, name) VALUES ({ids[0]}, 'dup')",
        f"INSERT INTO {t} (id, name) VALUES ({rng.randint(300, 400)}, NULL)",
        f"INSERT INTO {t} (id, name, qty) VALUES ({rng.randint(400, 500)}, 'c', -{rng.randint(11, 99)})",
        f"SELECT {rng.randint(1, 9)} / 0",
        f"SELECT nosuch{rng.randint(1, 9)} FROM {t}",
        f"SELECT * FROM missing{rng.randint(10, 99)} WHERE id = 1",
        f"SELECT '{rng.choice(['abc', '1x', ''])}'::integer",
        f"SELECT id, FROM {t}",
        f"SELEC id FROM {t}",
        f"SELECT nosuchfn({rng.randint(1, 9)})",
        f"SELECT count(*) FROM {t} WHERE name = 1",
        f"INSERT INTO {t} (id, name) VALUES ('x{rng.randint(1, 9)}', 'n')",
        f"SELECT 2147483647::integer + {rng.randint(1, 9)}",
        f"SELECT id FROM {t} ORDER BY nosuch",
        f"UPDATE {t} SET nosuch = 1",
    ]
    dml = [
        f"UPDATE {t} SET {q} = {q} + {rng.randint(1, 9)} WHERE id < {rng.randint(50, 150)}",
        f"UPDATE {t} SET name = name || 'z' WHERE id = {rng.choice(ids)} RETURNING id, name",
        f"DELETE FROM {t} WHERE id = {rng.choice(ids)}",
        f"DELETE FROM {t} WHERE {q} > {rng.randint(90, 120)} RETURNING id",
        f"UPDATE {t} SET {q} = NULL WHERE id = {rng.choice(ids)}",
    ]
    txn = [
        ["BEGIN", f"INSERT INTO {t} (id, name) VALUES ({rng.randint(500, 600)}, 'tx')", f"SELECT count(*) FROM {t}", "COMMIT"],
        ["BEGIN", f"DELETE FROM {t}", f"SELECT count(*) FROM {t}", "ROLLBACK", f"SELECT count(*) FROM {t}"],
        ["BEGIN", f"SELECT {rng.randint(1, 9)} / 0", "SELECT 1", "ROLLBACK", "SELECT 2"],
        ["BEGIN", f"INSERT INTO {t} (id, name) VALUES ({rng.randint(600, 700)}, 'sp')", "SAVEPOINT s1",
         f"INSERT INTO {t} (id, name) VALUES ({ids[1]}, 'dup')", "ROLLBACK TO SAVEPOINT s1", "RELEASE SAVEPOINT s1", "COMMIT",
         f"SELECT id, name FROM {t} WHERE id >= 600 ORDER BY id"],
        ["COMMIT"],
        [f"INSERT INTO {t} (id, name) VALUES ({rng.randint(700, 800)}, 'm'); SELECT max(id) FROM {t}; SELECT 1 / 0; SELECT 3"],
    ]
    second = [
        f"CREATE TABLE {u} (k integer PRIMARY KEY, tid integer REFERENCES {t}(id), note text)",
        f"INSERT INTO {u} VALUES " + ", ".join(f"({j}, {rng.choice(ids[2:])}, {lit(word(rng))})" for j in range(1, rng.randint(4, 8))),
        f"INSERT INTO {u} VALUES (99, 9999, 'orphan')",
        f"SELECT a.id, a.name, b.note FROM {t} a JOIN {u} b ON a.id = b.tid ORDER BY b.k",
        f"SELECT id FROM {t} WHERE EXISTS (SELECT 1 FROM {u} WHERE {u}.tid = {t}.id) ORDER BY id",
        f"SELECT id FROM {t} WHERE id NOT IN (SELECT tid FROM {u}) ORDER BY id LIMIT 3",
        f"SELECT tid, count(*) FROM {u} GROUP BY tid ORDER BY tid",
        f"SELECT id FROM {t} WHERE {q} < 10 UNION SELECT tid FROM {u} ORDER BY 1",
        f"DROP TABLE {u}",
    ]
    misc = [
        f"CREATE INDEX ON {t} (name)",
        f"ALTER TABLE {t} ADD COLUMN extra integer DEFAULT {rng.randint(1, 9)}",
        f"SELECT id, extra FROM {t} ORDER BY id LIMIT 3",
        f"DROP TABLE IF EXISTS gone{rng.randint(10, 99)}",
        f"CREATE TABLE {t} (id integer)",
    ]
    body: list[str] = []
    body += rng.sample(queries, min(len(queries), rng.randint(9, 13)))
    body += rng.sample(errors, rng.randint(4, 6))
    body += rng.sample(dml, rng.randint(2, 4))
    for blk in rng.sample(txn, rng.randint(1, 3)):
        body.append(blk)
    if rng.random() < 0.6:
        body.append(second)
    if rng.random() < 0.5:
        body.append(misc)
    rng.shuffle(body)
    for b in body:
        out.extend(b if isinstance(b, list) else [b])
    out += rng.sample(queries[:2], 1)
    if rng.random() < 0.5:
        out.append(f"DROP TABLE {t}")
    return out


# ------------------------------------------------------------------ protocol templates
# each returns a list of abstract steps

def S_connect():
    return [("connect",)]


def appname(rng: random.Random) -> str:
    return rng.choice(["app", "svc", "job", "cli", "web"]) + "-" + "".join(rng.choice("abcdefghjkmnpqrstuvwxyz23456789") for _ in range(6))


def S_login(rng: random.Random, user="guest", db="main", pw=None):
    p = {"user": user, "database": db, "application_name": appname(rng)}
    st = [("connect",), ("startup", p, "Z" if pw is None else "count1")]
    if pw is not None:
        st.append(("password", pw))
    return st


def P_guest(rng):
    return S_login(rng) + [("query", f"SELECT {rng.randint(1, 999)} AS one"), ("terminate",)]


def P_admin(rng):
    return S_login(rng, "admin", pw=ADMIN_PW) + [("query", f"SELECT current_user, session_user, {rng.randint(1, 999)}"), ("terminate",)]


def P_admin_badpw(rng):
    return [("connect",), ("startup", {"user": "admin", "database": "main", "application_name": appname(rng)}, "count1"), ("password", "wrong" + str(rng.randint(1, 999)))]


def P_unknown_user(rng):
    return [("connect",), ("startup", {"user": "nobody" + str(rng.randint(1, 999)), "database": "main", "application_name": appname(rng)}, "close")]


def P_unknown_db(rng):
    return [("connect",), ("startup", {"user": "guest", "database": "nodb" + str(rng.randint(1, 999)), "application_name": appname(rng)}, "close")]


def P_no_user(rng):
    return [("connect",), ("startup", {"database": "main", "application_name": appname(rng)}, "close")]


def P_default_db(rng):
    return [("connect",), ("startup", {"user": "guest", "application_name": appname(rng)}, "close")]  # database defaults to the user name


def P_bad_magic(rng):
    return [("connect",), ("startup_magic", rng.choice([196608, rng.randint(1, 9999), rng.randint(1, 2**31)]), {"user": "guest", "application_name": appname(rng)}), ("terminate",)]


def P_bad_key(rng):
    return [("connect",), ("startup_rawkeys", {"user_raw": "guest", rng.choice(["colour", "user", "database", "role", "db"]): appname(rng)}), ("terminate",)]


def P_ssl(rng):
    return [("connect",), ("ssl",)] + S_login(rng)[1:] + [("query", f"SELECT {rng.randint(1, 999)}"), ("terminate",)]


def P_ext_basic(rng):
    a, b = rng.randint(1, 1000), word(rng)
    return S_login(rng) + [("ext", [("parse", "", "SELECT $1::integer + 1 AS n, $2::text AS s, $1::integer * 2", []),
                                  ("bind", "", "", [str(a), b]), ("describe", "P", ""), ("execute", "", 0), ("sync",)]),
                         ("terminate",)]


def P_ext_named(rng):
    t = tname(rng)
    rows = [(i, word(rng), rng.randint(0, 99)) for i in range(1, rng.randint(4, 7))]
    steps = S_login(rng) + [("query", f"CREATE TABLE {t} (id integer PRIMARY KEY, name text, v integer)"),
                         ("ext", [("parse", "ins", f"INSERT INTO {t} VALUES ($1, $2, $3)", [OID["int4"], OID["text"], OID["int4"]]),
                                  ("describe", "S", "ins"), ("sync",)])]
    for r in rows:
        steps.append(("ext", [("bind", "", "ins", [str(r[0]), r[1], str(r[2])]), ("execute", "", 0), ("sync",)]))
    steps += [("ext", [("parse", "sel", f"SELECT id, name, v FROM {t} WHERE v >= $1 ORDER BY id", []), ("describe", "S", "sel"),
                       ("bind", "", "sel", [str(rng.randint(0, 50))]), ("execute", "", 0), ("close", "S", "ins"), ("sync",)]),
              ("ext", [("bind", "", "ins", ["1", "x", "1"]), ("execute", "", 0), ("sync",)]),
              ("terminate",)]
    return steps


def P_ext_suspend(rng):
    n, k = rng.randint(6, 12), rng.randint(2, 4)
    return S_login(rng) + [("query", "BEGIN"),
                        ("ext", [("parse", "g", f"SELECT x, x * x AS sq FROM generate_series(1, {n}) AS x", []),
                                 ("bind", "cur", "g", []), ("execute", "cur", k), ("execute", "cur", k), ("sync",)]),
                        ("ext", [("execute", "cur", 0), ("sync",)]), ("query", "COMMIT"),
                        ("ext", [("execute", "cur", 0), ("sync",)]), ("terminate",)]


def P_ext_error(rng):
    return S_login(rng) + [("ext", [("parse", "", f"SELEC {rng.randint(1, 9)}", []), ("bind", "", "", []), ("execute", "", 0), ("sync",)]),
                        ("ext", [("parse", "", "SELECT $1::integer", []), ("bind", "", "", ["notanint"]), ("execute", "", 0), ("sync",)]),
                        ("ext", [("parse", "", "SELECT 7", []), ("bind", "", "", []), ("execute", "", 0), ("sync",)]),
                        ("terminate",)]


def P_ext_pipeline(rng):
    z = rng.randint(1, 9)
    grp = lambda sql: [("parse", "", sql, []), ("bind", "", "", []), ("execute", "", 0), ("sync",)]  # noqa: E731
    return S_login(rng) + [("ext", grp("SELECT 1") + grp(f"SELECT {z} / 0") + grp("SELECT 3") + grp("SELECT 'p'"), 4), ("terminate",)]


def P_ext_flush(rng):
    return S_login(rng) + [("ext_idle", [("parse", "f", f"SELECT {rng.randint(1, 99)} AS v", []), ("bind", "", "f", []), ("flush",)]),
                        ("ext", [("execute", "", 0), ("sync",)]), ("terminate",)]


def P_ext_nodata(rng):
    t = tname(rng)
    return S_login(rng) + [("query", f"CREATE TABLE {t} (a integer)"),
                        ("ext", [("parse", "w", f"INSERT INTO {t} VALUES ($1)", []), ("describe", "S", "w"), ("bind", "", "w", ["5"]),
                                 ("describe", "P", ""), ("execute", "", 0), ("parse", "", "", []), ("bind", "", "", []),
                                 ("execute", "", 0), ("sync",)]),
                        ("ext", [("describe", "S", "nosuch"), ("sync",)]), ("ext", [("describe", "X", "w"), ("sync",)]),
                        ("terminate",)]


def P_simple_multi(rng):
    return S_login(rng) + [("query", f"SELECT 1 AS a; SELECT {rng.randint(2, 9)} AS b, 'x' AS c"),
                        ("query", f"SELECT 1; SELECT {rng.randint(1, 9)} / 0; SELECT 3"),
                        ("query", ""), ("query", " ; "), ("query", "-- only a comment"), ("terminate",)]


def P_txn_status(rng):
    return S_login(rng) + [("query", "BEGIN"), ("query", "SELECT 1"), ("query", "SELECT nosuch"), ("query", "SELECT 1"),
                        ("query", "ROLLBACK"), ("query", "BEGIN; SELECT 1"), ("query", "COMMIT"), ("query", "ROLLBACK"),
                        ("terminate",)]


def P_bad_type(rng):
    return S_login(rng) + [("query", "SELECT 1"), ("rawtype", rng.randint(0, 255)), ("query", "SELECT 2")]


def P_bad_len(rng):
    return S_login(rng) + [("badlen",)]


def P_bad_body(rng):
    return S_login(rng) + [("badbody", rng.choice(["query", "execute", "bind"]))]


def P_cancel(rng):
    return S_login(rng) + [("cancel", "SELECT pg_sleep(4)", rng.uniform(0.3, 0.8)), ("query", "SELECT 'after'"), ("terminate",)]


def P_terminate(rng):
    return S_login(rng) + [("query_terminate", f"SELECT {rng.randint(1, 99)} AS last")]


def P_paramstatus(rng):
    return S_login(rng) + [("query", f"SET application_name = 'a{rng.randint(1, 99)}'"), ("query", "SET DateStyle = 'ISO, YMD'"),
                        ("query", "SELECT '2020-01-02'::date"), ("query", "RESET DateStyle"), ("query", "SELECT '2020-01-02'::date"),
                        ("terminate",)]


PROTOCOL = [P_guest, P_admin, P_admin_badpw, P_unknown_user, P_unknown_db, P_no_user, P_default_db, P_bad_magic, P_bad_key,
            P_ssl, P_ext_basic, P_ext_named, P_ext_suspend, P_ext_error, P_ext_pipeline, P_ext_flush, P_ext_nodata,
            P_simple_multi, P_txn_status, P_bad_type, P_bad_len, P_bad_body, P_cancel, P_terminate, P_paramstatus]


def generate(wseed: int) -> list[dict]:
    """Abstract workloads (PostgreSQL SQL); deterministic in `wseed`."""
    wls = []
    for i in range(N_SQL):
        rng = random.Random(f"bbx-sql-{wseed}-{i}")
        steps = S_login(rng) + [("query", s) for s in sql_session(rng)] + [("terminate",)]
        wls.append({"id": f"sql{i:03d}", "kind": "sql", "abstract": steps, "gate": 1, "score_from": 2})
    for j, tpl in enumerate(PROTOCOL):
        for k in range(N_PROTOCOL_PER_TEMPLATE):
            rng = random.Random(f"bbx-proto-{wseed}-{j}-{k}")
            wls.append({"id": f"{tpl.__name__[2:]}{k}", "kind": "protocol", "abstract": tpl(rng), "score_from": 0})
    return wls


# ------------------------------------------------------------------ rendering into a variant's bytes

def render(spec: dict, wl: dict) -> dict:
    V, d, w = Codec(spec), Dialect(spec), spec["wire"]
    voids = {int(k): v for k, v in w["oids"].items()}
    keys = w["startup_keys"]

    def startup(params: dict, magic: int | None = None, rawkeys: bool = False) -> bytes:
        body = V.u32(w["magic"] if magic is None else magic)
        for k, v in params.items():
            if rawkeys:  # "<name>_raw" is the variant's key for <name>; anything else is sent literally
                key = keys[k[:-4]] if k.endswith("_raw") else k
            else:
                key = keys[k]
            body += V.s(key) + V.s(v)
        return V.frame_untyped(body + V.s(""))

    def ext(msgs: list) -> bytes:
        out = b""
        for m in msgs:
            k = m[0]
            if k == "parse":
                out += V.encode("f:parse", {"name": m[1], "sql": d.to_variant(m[2]), "types": [voids.get(o, o) for o in m[3]]})
            elif k == "bind":
                out += V.encode("f:bind", {"portal": m[1], "stmt": m[2], "fmts": [],
                                           "params": [None if p is None else p.encode() for p in m[3]], "rfmts": []})
            elif k in ("describe", "close"):
                sub = w["sub_types"].get(m[1], "?")
                out += V.encode("f:" + k, {"sub": sub, "name": m[2]})
            elif k == "execute":
                out += V.encode("f:execute", {"portal": m[1], "max": m[2]})
            else:
                out += V.encode("f:" + k, {})
        return out

    steps = []
    for st in wl["abstract"]:
        k = st[0]
        if k == "connect":
            steps.append({"send": "", "until": "count", "n": 1})
        elif k == "startup":
            until = st[2]
            steps.append({"send": startup(st[1]).hex(), "until": "count" if until == "count1" else until, "n": 1})
        elif k == "startup_magic":
            steps.append({"send": startup(st[2], magic=st[1]).hex(), "until": "close"})
        elif k == "startup_rawkeys":
            steps.append({"send": startup(st[1], rawkeys=True).hex(), "until": "close"})
        elif k == "password":
            steps.append({"send": V.encode("f:password", {"s": st[1]}).hex(), "until": "Z"})
        elif k == "ssl":
            steps.append({"send": V.frame_untyped(V.u32(w["ssl_magic"])).hex(), "until": "raw1"})
        elif k == "query":
            steps.append({"send": V.encode("f:query", {"sql": d.to_variant(st[1])}).hex(), "until": "Z"})
        elif k == "ext":
            nz = st[2] if len(st) > 2 else 1
            steps.append({"send": ext(st[1]).hex(), "until": "Zn", "n": nz})
        elif k == "ext_idle":
            steps.append({"send": ext(st[1]).hex(), "until": "idle"})
        elif k == "terminate":
            steps.append({"send": V.encode("f:terminate", {}).hex(), "until": "close"})
        elif k == "query_terminate":
            steps.append({"send": (V.encode("f:query", {"sql": d.to_variant(st[1])}) + V.encode("f:terminate", {})).hex(),
                          "until": "close"})
        elif k == "rawtype":
            used = set(w["front_types"].values())
            b = st[1]
            while chr(b) in used:
                b = (b + 1) % 256
            steps.append({"send": V.frame(chr(b), b"\x01\x02\x03").hex(), "until": "close"})
        elif k == "badlen":
            if w["len_enc"] == "varint":
                ln = b"\xff\xff\xff\xff\xff\x01"
            else:
                ln = (0x7FFFFFF0).to_bytes(4, "big" if w["len_enc"] == "be32" else "little")
            t = w["front_types"]["query"].encode("latin1")
            raw = t + ln if w["header"] == "type_first" else ln + t
            steps.append({"send": (raw + b"SELECT 1").hex(), "until": "close"})
        elif k == "badbody":
            kind = st[1]
            body = {"query": b"SELECT 1" if w["str_enc"] == "cstr" else V._u16.pack(200) + b"SELECT 1",
                    "execute": V.s("") + b"\x01", "bind": V.s("") + V.s("") + V.i16(3)}[kind]
            steps.append({"send": V.frame(w["front_types"][kind], body).hex(), "until": "close"})
        elif k == "cancel":
            steps.append({"send": V.encode("f:query", {"sql": d.to_variant(st[1])}).hex(), "until": "Z", "cancel": st[2]})
        else:
            raise ValueError(k)
    return {k: v for k, v in wl.items() if k != "abstract"} | {"steps": steps}
