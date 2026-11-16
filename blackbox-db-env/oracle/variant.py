"""A seeded variant of the PostgreSQL v3 protocol and SQL dialect.

`make_spec(seed)` returns a JSON-able dict that fully determines one variant. `Codec` frames and
encodes messages for either PostgreSQL itself (`PG`) or a variant. `Dialect` rewrites SQL both ways:
variant -> PostgreSQL for requests (with a position map for error offsets) and PostgreSQL -> variant
for the text PostgreSQL sends back (command tags, error text, column names).

Shared by the oracle proxy (oracle/proxy.py) and the grader. Layers, used for ablations:
  wire       framing, type bytes, integer/string encoding, field order, handshake, OIDs, status bytes
  dialect    renamed keywords, types and functions, parameter marker
  semantics  SQLSTATE classes, severity words, value formatting, command tags, column case,
             DateStyle, product name
"""

from __future__ import annotations

import random
import re
import struct

LAYERS = ("wire", "dialect", "semantics")

PG_FRONT = {"query": "Q", "parse": "P", "bind": "B", "describe": "D", "execute": "E", "sync": "S", "flush": "H",
            "close": "C", "terminate": "X", "password": "p", "copydata": "d", "copydone": "c", "copyfail": "f"}
PG_BACK = {"auth": "R", "paramstatus": "S", "keydata": "K", "ready": "Z", "rowdesc": "T", "datarow": "D",
           "complete": "C", "emptyquery": "I", "error": "E", "notice": "N", "parsecomplete": "1",
           "bindcomplete": "2", "closecomplete": "3", "nodata": "n", "paramdesc": "t", "suspended": "s",
           "copyin": "G", "copyout": "H", "copydata": "d", "copydone": "c", "notification": "A"}
PG_MAGIC, PG_CANCEL, PG_SSL = 196608, 80877102, 80877103
PG_ERR_FIELDS = "SVCMDHPWstcdn"  # F, L, R (source file/line/routine) and p, q (internal query) are dropped

# Keywords, types and functions a variant may rename. Chosen so none is also used as another kind.
KEYWORDS = ["SELECT", "FROM", "WHERE", "INSERT", "INTO", "VALUES", "UPDATE", "SET", "DELETE", "CREATE", "TABLE",
            "DROP", "AND", "OR", "NOT", "NULL", "IS", "IN", "BETWEEN", "LIKE", "ORDER", "BY", "GROUP", "HAVING",
            "LIMIT", "OFFSET", "JOIN", "INNER", "ON", "AS", "DISTINCT", "PRIMARY", "KEY", "UNIQUE",
            "DEFAULT", "CHECK", "REFERENCES", "BEGIN", "COMMIT", "ROLLBACK", "SAVEPOINT", "RELEASE", "ASC",
            "DESC", "CASE", "WHEN", "THEN", "ELSE", "END", "EXISTS", "IF", "RETURNING", "INDEX", "TRUE", "FALSE",
            "UNION", "ALL", "TO", "TRANSACTION"]
TYPES = {"integer": ["integer", "int", "int4"], "bigint": ["bigint", "int8"], "smallint": ["smallint", "int2"],
         "text": ["text"], "boolean": ["boolean", "bool"], "numeric": ["numeric", "decimal"], "date": ["date"],
         "real": ["real", "float4"]}
FUNCTIONS = ["count", "sum", "avg", "min", "max", "length", "upper", "lower", "coalesce", "abs", "round", "substr",
             "concat", "replace", "nullif", "greatest", "least", "version"]
ALWAYS_RENAMED_FUNCTIONS = ["pg_sleep"]
TYPE_OIDS = [16, 17, 18, 19, 20, 21, 23, 25, 26, 114, 700, 701, 705, 1042, 1043, 1082, 1083, 1114, 1184, 1186,
             1700, 2950, 3802, 2249, 2278, 1007, 1009, 1016]
SQLSTATE_CLASSES = ["00", "01", "02", "03", "08", "0A", "0B", "0L", "0P", "20", "21", "22", "23", "24", "25", "26",
                    "27", "28", "2B", "2D", "2F", "34", "38", "39", "3B", "3D", "3F", "40", "42", "44", "53", "54",
                    "55", "57", "58", "72", "F0", "HV", "P0", "XX"]
SEVERITIES = ["ERROR", "FATAL", "PANIC", "WARNING", "NOTICE", "DEBUG", "INFO", "LOG"]
PARAM_STATUS = ["application_name", "client_encoding", "DateStyle", "default_transaction_read_only",
                "in_hot_standby", "integer_datetimes", "IntervalStyle", "is_superuser", "scram_iterations",
                "server_encoding", "server_version", "session_authorization", "standard_conforming_strings",
                "TimeZone"]
PRODUCTS = ["Quartzite", "Feldspar", "Obsidian", "Tessera", "Basalt", "Marlstone", "Gneiss", "Vellum"]
# PostgreSQL's own keyword list (lowercase), so invented words never collide with real syntax.
PG_RESERVED = set("""abort absolute access action add admin after aggregate all also alter always analyse analyze
and any array as asc asensitive assertion assignment asymmetric at atomic attach attribute authorization backward
before begin between bigint binary bit boolean both breadth by cache call called cascade cascaded case cast catalog
chain char character characteristics check checkpoint class close cluster coalesce collate collation column columns
comment comments commit committed compression concurrently configuration conflict connection constraint constraints
content continue conversion copy cost create cross csv cube current cursor cycle data database day deallocate dec
decimal declare default defaults deferrable deferred definer delete delimiter delimiters depends depth desc detach
dictionary disable discard distinct do document domain double drop each else enable encoding encrypted end enum
escape event except exclude excluding exclusive execute exists explain expression extension external extract false
family fetch filter finalize first float following for force foreign format forward freeze from full function
functions generated global grant granted greatest group grouping groups handler having header hold hour identity if
ilike immediate immutable implicit import in include including increment indent index indexes inherit inherits
initially inline inner inout input insensitive insert instead int integer intersect interval into invoker is isnull
isolation join json key keys label language large last lateral leading leakproof least left level like limit listen
load local localtime localtimestamp location lock locked logged mapping match matched materialized maxvalue merge
method minute minvalue mode month move name names national natural nchar new next nfc nfd nfkc nfkd no none normalize
normalized not nothing notify notnull nowait null nullif nulls numeric object of off offset oids old on only operator
option options or order ordinality others out outer over overlaps overlay overriding owned owner parallel parameter
parser partial partition passing password placing plans policy position preceding precision prepare prepared
preserve primary prior privileges procedural procedure procedures program publication quote range read real
reassign recheck recursive ref references referencing refresh reindex relative release rename repeatable replace
replica reset restart restrict return returning returns revoke right role rollback rollup routine routines row rows
rule savepoint scalar schema schemas scroll search second security select sequence sequences serializable server
session session_user set setof sets share show similar simple skip smallint snapshot some sql stable standalone start
statement statistics stdin stdout storage stored strict strip subscription substring support symmetric sysid system
table tables tablesample tablespace temp template temporary text then ties time timestamp to trailing transaction
transform treat trigger trim true truncate trusted type types uescape unbounded uncommitted unencrypted union unique
unknown unlisten unlogged until update user using vacuum valid validate validator value values varchar variadic
varying verbose version view views volatile when where whitespace window with within without work wrapper write xml
year yes zone count sum avg min max length upper lower abs round substr concat date bool int2 int4 int8 float4 float8
oid regclass pg_sleep""".split())


# ------------------------------------------------------------------ spec

def _word(rng: random.Random, used: set[str], syll: tuple[int, int] = (2, 3)) -> str:
    cons, vows = "bdfgklmnprstvz", "aeiou"
    while True:
        w = "".join(rng.choice(cons) + rng.choice(vows) for _ in range(rng.randint(*syll)))
        if rng.random() < 0.4:
            w += rng.choice("klmnrs")
        if w not in used and w not in PG_RESERVED:
            used.add(w)
            return w


def make_spec(seed: int) -> dict:
    """Everything that defines variant `seed`. Deterministic; JSON-able."""
    rng = random.Random(f"bbx-variant-{seed}")
    printable = [chr(c) for c in range(0x21, 0x7F)]
    front = dict(zip(PG_FRONT, rng.sample(printable, len(PG_FRONT))))
    back_kinds = list(PG_BACK) + ["greeting"]
    back = dict(zip(back_kinds, rng.sample(printable, len(back_kinds))))
    len_enc = rng.choice(["be32", "le32", "varint"])
    used: set[str] = set()
    letters = [chr(c) for c in range(0x41, 0x5B)] + [chr(c) for c in range(0x61, 0x7B)]
    err_codes = dict(zip(PG_ERR_FIELDS, rng.sample(letters, len(PG_ERR_FIELDS))))
    err_order = rng.sample(list(PG_ERR_FIELDS), len(PG_ERR_FIELDS))
    magics = rng.sample(range(0x01000000, 0x7FFFFFFF), 3)
    auth_codes = rng.sample(range(1, 250), 2)
    kept_ps = sorted(rng.sample(PARAM_STATUS, rng.randint(7, 10)), key=PARAM_STATUS.index)
    oids = rng.sample(range(1000, 9000), len(TYPE_OIDS))
    kw = sorted(rng.sample(KEYWORDS, int(len(KEYWORDS) * 0.6)), key=KEYWORDS.index)
    types = sorted(rng.sample(list(TYPES), 4), key=list(TYPES).index)
    funcs = sorted(rng.sample(FUNCTIONS, len(FUNCTIONS) // 2), key=FUNCTIONS.index) + ALWAYS_RENAMED_FUNCTIONS
    classes = rng.sample(SQLSTATE_CLASSES, len(SQLSTATE_CLASSES))
    product = rng.choice(PRODUCTS)
    spec = {
        "seed": seed,
        "wire": {
            "front_types": front, "back_types": back,
            "len_enc": len_enc, "len_includes_self": False if len_enc == "varint" else rng.random() < 0.5,
            "header": rng.choice(["type_first", "len_first"]),
            "int_endian": rng.choice(["big", "little"]),
            "str_enc": rng.choice(["cstr", "u16"]),
            "rowdesc_order": rng.sample(["name", "typoid", "typlen", "typmod", "format"], 5),
            "err_codes": err_codes, "err_order": err_order,
            "ready_status": dict(zip("ITE", rng.sample(letters, 3))),
            "sub_types": dict(zip("SP", rng.sample(letters, 2))),
            "magic": magics[0], "cancel_magic": magics[1], "ssl_magic": magics[2],
            "ssl_reply": rng.choice(letters),
            "startup_keys": {"user": _word(rng, used, (2, 2)), "database": _word(rng, used, (2, 2)),
                             "application_name": _word(rng, used, (2, 3))},
            "auth_codes": {"ok": auth_codes[0], "cleartext": auth_codes[1]},
            "param_status": {k: _word(rng, used, (2, 3)) for k in kept_ps},
            "oids": dict(zip(map(str, TYPE_OIDS), oids)), "oid_offset": rng.randrange(20000, 60000, 1000),
        },
        "dialect": {
            "keywords": {k: _word(rng, used).upper() for k in kw},
            "types": {t: _word(rng, used) for t in types},
            "functions": {f: _word(rng, used) for f in funcs},
            "param_prefix": rng.choice(["$", "?", ":"]),
        },
        "semantics": {
            "sqlstate_classes": dict(zip(SQLSTATE_CLASSES, classes)),
            "severities": {s: _word(rng, used, (2, 2)).upper() for s in SEVERITIES},
            "bool": rng.choice([["yes", "no"], ["on", "off"], ["1", "0"], ["TRUE", "FALSE"], ["Y", "N"]]),
            "int_style": rng.choice(["plain", "plus", "grouped"]),
            "float_style": rng.choice(["pg", "g6", "fixed3", "exp4"]),
            "numeric_trim": rng.random() < 0.5,
            "colcase": rng.choice(["lower", "upper"]),
            "tag_style": rng.choice(["pg", "nooid", "countfirst"]),
            "datestyle": rng.choice(["ISO, MDY", "SQL, DMY", "German, DMY", "Postgres, MDY", "SQL, MDY"]),
            "product": product, "version": f"{rng.randint(2, 9)}.{rng.randint(0, 12)}",
        },
    }
    return spec


def summarize(spec: dict) -> dict:
    """A short human-readable account of what a variant changes (for the results page)."""
    w, d, s = spec["wire"], spec["dialect"], spec["semantics"]
    return {
        "seed": spec["seed"],
        "framing": f"{'type byte, then ' if w['header'] == 'type_first' else ''}length as {w['len_enc']}"
                   f"{' incl. itself' if w['len_includes_self'] else ' (body only)'}"
                   f"{', then type byte' if w['header'] == 'len_first' else ''}; ints {w['int_endian']}-endian; "
                   f"strings {'NUL-terminated' if w['str_enc'] == 'cstr' else 'u16 length-prefixed'}",
        "type_bytes": "Query " + w["front_types"]["query"] + ", Parse " + w["front_types"]["parse"] + ", RowDescription "
                      + w["back_types"]["rowdesc"] + ", DataRow " + w["back_types"]["datarow"] + ", Error " + w["back_types"]["error"],
        "handshake": f"server-first greeting; magic {w['magic']:#x}; keys user={w['startup_keys']['user']}, "
                     f"database={w['startup_keys']['database']}; auth codes ok={w['auth_codes']['ok']}, "
                     f"cleartext={w['auth_codes']['cleartext']}; {len(w['param_status'])} of 14 ParameterStatus kept, renamed",
        "rowdesc_order": ",".join(w["rowdesc_order"]),
        "keywords": ", ".join(f"{k}->{v}" for k, v in list(d["keywords"].items())[:8]) + f" ... ({len(d['keywords'])} renamed)",
        "types": ", ".join(f"{k}->{v}" for k, v in d["types"].items()),
        "functions": ", ".join(f"{k}->{v}" for k, v in list(d["functions"].items())[:5]) + f" ... ({len(d['functions'])})",
        "param_prefix": d["param_prefix"],
        "sqlstate": "23xxx->" + s["sqlstate_classes"]["23"] + "xxx, 42xxx->" + s["sqlstate_classes"]["42"] + "xxx",
        "values": f"bool {s['bool'][0]}/{s['bool'][1]}, ints {s['int_style']}, floats {s['float_style']}, "
                  f"numeric trim {s['numeric_trim']}, dates '{s['datestyle']}'",
        "tags_cols": f"tags {s['tag_style']}, column names {s['colcase']}case, ERROR->{s['severities']['ERROR']}",
        "product": f"{s['product']} {s['version']}",
    }


# ------------------------------------------------------------------ codec

class Malformed(Exception):
    pass


class Codec:
    """Message framing and body encoding for PostgreSQL (`Codec.pg()`) or a variant (`Codec(spec)`)."""

    def __init__(self, spec: dict | None = None):
        if spec is None:
            self.front = dict(PG_FRONT)
            self.back = dict(PG_BACK)
            self.len_enc, self.incl, self.header, self.endian, self.str_enc = "be32", True, "type_first", "big", "cstr"
            self.rowdesc = ["name", "tableoid", "attnum", "typoid", "typlen", "typmod", "format"]
            self.err_codes = {c: c for c in PG_ERR_FIELDS + "FLRpq"}
            self.err_order = None  # keep PostgreSQL's own order
            self.pg = True
        else:
            w = spec["wire"]
            self.front, self.back = w["front_types"], w["back_types"]
            self.len_enc, self.incl, self.header = w["len_enc"], w["len_includes_self"], w["header"]
            self.endian, self.str_enc = w["int_endian"], w["str_enc"]
            self.rowdesc = w["rowdesc_order"]
            self.err_codes = w["err_codes"]
            self.err_order = w["err_order"]
            self.pg = False
        self.front_kind = {v: k for k, v in self.front.items()}
        self.back_kind = {v: k for k, v in self.back.items()}
        self.err_field = {v: k for k, v in self.err_codes.items()}
        e = ">" if self.endian == "big" else "<"
        self._i16, self._i32, self._u16, self._u32 = (struct.Struct(e + c) for c in "hiHI")

    @staticmethod
    def pg_codec() -> "Codec":
        return Codec(None)

    # ---- primitives
    def i16(self, v: int) -> bytes:
        return self._i16.pack(v)

    def i32(self, v: int) -> bytes:
        return self._i32.pack(v)

    def u32(self, v: int) -> bytes:
        return self._u32.pack(v)

    def s(self, v: str | bytes) -> bytes:
        b = v.encode() if isinstance(v, str) else v
        if self.str_enc == "cstr":
            if b"\0" in b:
                raise Malformed("NUL in string")
            return b + b"\0"
        if len(b) > 0xFFFF:
            raise Malformed("string too long")
        return self._u16.pack(len(b)) + b

    def length(self, n: int) -> bytes:
        if self.len_enc == "varint":
            out = bytearray()
            while True:
                b = n & 0x7F
                n >>= 7
                out.append(b | (0x80 if n else 0))
                if not n:
                    return bytes(out)
        v = n + (4 if self.incl else 0)
        return struct.pack(">I" if self.len_enc == "be32" else "<I", v)

    def read_length(self, buf: bytes | bytearray, pos: int) -> tuple[int, int] | None:
        """(body length, position after the length field), None if incomplete. Raises Malformed."""
        if self.len_enc == "varint":
            n, shift = 0, 0
            for i in range(pos, min(len(buf), pos + 5)):
                n |= (buf[i] & 0x7F) << shift
                shift += 7
                if not buf[i] & 0x80:
                    return n, i + 1
            if len(buf) >= pos + 5:
                raise Malformed("invalid message length")
            return None
        if len(buf) < pos + 4:
            return None
        v = struct.unpack_from(">I" if self.len_enc == "be32" else "<I", buf, pos)[0]
        n = v - (4 if self.incl else 0)
        if n < 0 or v > 0x7FFFFFFF:
            raise Malformed("invalid message length")
        return n, pos + 4

    # ---- frames
    def frame(self, t: str, body: bytes) -> bytes:
        tb = t.encode("latin1")
        if self.header == "type_first":
            return tb + self.length(len(body)) + body
        return self.length(len(body)) + tb + body

    def frame_untyped(self, body: bytes) -> bytes:
        return self.length(len(body)) + body

    def split(self, buf: bytes | bytearray, pos: int = 0, typed: bool = True, max_len: int = 1 << 28):
        """Next frame at `pos`: (type char or None, body, next pos), or None if incomplete. Raises Malformed."""
        t = None
        p = pos
        if typed and self.header == "type_first":
            if len(buf) <= p:
                return None
            t = chr(buf[p])
            p += 1
        r = self.read_length(buf, p)
        if r is None:
            return None
        n, p = r
        if n > max_len:
            raise Malformed("invalid message length")
        if typed and self.header == "len_first":
            if len(buf) <= p:
                return None
            t = chr(buf[p])
            p += 1
        if len(buf) < p + n:
            return None
        return t, bytes(buf[p:p + n]), p + n

    # ---- body reader
    class R:
        def __init__(self, codec: "Codec", b: bytes):
            self.c, self.b, self.p = codec, b, 0

        def need(self, n: int) -> None:
            if self.p + n > len(self.b) or n < 0:
                raise Malformed("invalid message format")

        def i16(self) -> int:
            self.need(2)
            v = self.c._i16.unpack_from(self.b, self.p)[0]
            self.p += 2
            return v

        def i32(self) -> int:
            self.need(4)
            v = self.c._i32.unpack_from(self.b, self.p)[0]
            self.p += 4
            return v

        def u32(self) -> int:
            self.need(4)
            v = self.c._u32.unpack_from(self.b, self.p)[0]
            self.p += 4
            return v

        def byte(self) -> str:
            self.need(1)
            v = chr(self.b[self.p])
            self.p += 1
            return v

        def raw(self, n: int) -> bytes:
            self.need(n)
            v = self.b[self.p:self.p + n]
            self.p += n
            return v

        def s(self) -> str:
            if self.c.str_enc == "cstr":
                i = self.b.find(b"\0", self.p)
                if i < 0:
                    raise Malformed("invalid string in message")
                v = self.b[self.p:i]
                self.p = i + 1
            else:
                self.need(2)
                n = self.c._u16.unpack_from(self.b, self.p)[0]
                self.p += 2
                v = self.raw(n)
            try:
                return v.decode()
            except UnicodeDecodeError:
                raise Malformed("invalid byte sequence") from None

        def rest(self) -> bytes:
            v = self.b[self.p:]
            self.p = len(self.b)
            return v

        def end(self) -> None:
            if self.p != len(self.b):
                raise Malformed("invalid message format")

    # ---- message bodies: dict <-> bytes, for every kind either side sends
    def encode(self, kind: str, m: dict) -> bytes:
        b = self._encode_body(kind, m)
        t = (self.front if kind.startswith("f:") else self.back)[kind[2:]]
        return self.frame(t, b)

    def _encode_body(self, kind: str, m: dict) -> bytes:
        k = kind[2:]
        if kind.startswith("f:"):
            if k == "query":
                return self.s(m["sql"])
            if k == "parse":
                return self.s(m["name"]) + self.s(m["sql"]) + self.i16(len(m["types"])) + b"".join(self.i32(t) for t in m["types"])
            if k == "bind":
                out = self.s(m["portal"]) + self.s(m["stmt"]) + self.i16(len(m["fmts"])) + b"".join(self.i16(f) for f in m["fmts"])
                out += self.i16(len(m["params"]))
                for v in m["params"]:
                    out += self.i32(-1) if v is None else self.i32(len(v)) + v
                return out + self.i16(len(m["rfmts"])) + b"".join(self.i16(f) for f in m["rfmts"])
            if k in ("describe", "close"):
                return m["sub"].encode("latin1") + self.s(m["name"])
            if k == "execute":
                return self.s(m["portal"]) + self.i32(m["max"])
            if k in ("sync", "flush", "terminate", "copydone"):
                return b""
            if k in ("password", "copyfail"):
                return self.s(m["s"])
            if k == "copydata":
                return m["data"]
        else:
            if k == "auth":
                return self.i32(m["code"]) + m.get("rest", b"")
            if k == "paramstatus":
                return self.s(m["name"]) + self.s(m["value"])
            if k == "keydata":
                return self.i32(m["pid"]) + self.i32(m["key"])
            if k == "ready":
                return m["status"].encode("latin1")
            if k == "rowdesc":
                out = self.i16(len(m["fields"]))
                for f in m["fields"]:
                    for col in self.rowdesc:
                        v = f.get(col, 0)
                        out += self.s(v) if col == "name" else (self.i16(v) if col in ("attnum", "typlen", "format") else self.i32(v))
                return out
            if k == "datarow":
                out = self.i16(len(m["values"]))
                for v in m["values"]:
                    out += self.i32(-1) if v is None else self.i32(len(v)) + v
                return out
            if k == "complete":
                return self.s(m["tag"])
            if k in ("error", "notice"):
                fields = m["fields"]
                order = list(fields) if self.err_order is None else [c for c in self.err_order if c in fields]
                out = b""
                for c in order:
                    out += self.err_codes[c].encode("latin1") + self.s(fields[c])
                return out + b"\0"
            if k in ("emptyquery", "parsecomplete", "bindcomplete", "closecomplete", "nodata", "suspended", "copydone"):
                return b""
            if k == "paramdesc":
                return self.i16(len(m["types"])) + b"".join(self.i32(t) for t in m["types"])
            if k in ("copyin", "copyout"):
                return bytes([m["fmt"]]) + self.i16(len(m["fmts"])) + b"".join(self.i16(f) for f in m["fmts"])
            if k == "copydata":
                return m["data"]
            if k == "notification":
                return self.i32(m["pid"]) + self.s(m["channel"]) + self.s(m["payload"])
            if k == "greeting":
                return self.s(m["banner"]) + self.u32(m["magic"])
        raise ValueError(kind)

    def decode(self, kind: str, body: bytes) -> dict:
        r = Codec.R(self, body)
        k = kind[2:]
        m: dict = {}
        if kind.startswith("f:"):
            if k == "query":
                m["sql"] = r.s()
            elif k == "parse":
                m["name"], m["sql"] = r.s(), r.s()
                m["types"] = [r.i32() for _ in range(_count(r.i16()))]
            elif k == "bind":
                m["portal"], m["stmt"] = r.s(), r.s()
                m["fmts"] = [r.i16() for _ in range(_count(r.i16()))]
                m["params"] = []
                for _ in range(_count(r.i16())):
                    n = r.i32()
                    m["params"].append(None if n == -1 else r.raw(n))
                m["rfmts"] = [r.i16() for _ in range(_count(r.i16()))]
            elif k in ("describe", "close"):
                m["sub"], m["name"] = r.byte(), r.s()
            elif k == "execute":
                m["portal"], m["max"] = r.s(), r.i32()
            elif k in ("password", "copyfail"):
                m["s"] = r.s()
            elif k == "copydata":
                m["data"] = r.rest()
        else:
            if k == "auth":
                m["code"], m["rest"] = r.i32(), r.rest()
            elif k == "paramstatus":
                m["name"], m["value"] = r.s(), r.s()
            elif k == "keydata":
                m["pid"], m["key"] = r.i32(), r.i32()
            elif k == "ready":
                m["status"] = r.byte()
            elif k == "rowdesc":
                m["fields"] = []
                for _ in range(_count(r.i16())):
                    f = {}
                    for col in self.rowdesc:
                        f[col] = r.s() if col == "name" else (r.i16() if col in ("attnum", "typlen", "format") else r.i32())
                    m["fields"].append(f)
            elif k == "datarow":
                m["values"] = []
                for _ in range(_count(r.i16())):
                    n = r.i32()
                    m["values"].append(None if n == -1 else r.raw(n))
            elif k == "complete":
                m["tag"] = r.s()
            elif k in ("error", "notice"):
                m["fields"] = {}
                while True:
                    c = r.byte()
                    if c == "\0":
                        break
                    m["fields"][self.err_field.get(c, "?" + c)] = r.s()
            elif k == "paramdesc":
                m["types"] = [r.i32() for _ in range(_count(r.i16()))]
            elif k in ("copyin", "copyout"):
                m["fmt"] = ord(r.byte())
                m["fmts"] = [r.i16() for _ in range(_count(r.i16()))]
            elif k == "copydata":
                m["data"] = r.rest()
            elif k == "notification":
                m["pid"], m["channel"], m["payload"] = r.i32(), r.s(), r.s()
            elif k == "greeting":
                m["banner"], m["magic"] = r.s(), r.u32()
        r.end()
        return m


def _count(n: int) -> int:
    if n < 0:
        raise Malformed("invalid message format")
    return n


# ------------------------------------------------------------------ SQL scanning

_IDENT_START = re.compile(r"[A-Za-z_\u0080-￿]")
_IDENT_REST = re.compile(r"[A-Za-z0-9_$\u0080-￿]*")
_DOLLAR_TAG = re.compile(r"\$([A-Za-z_\u0080-￿][A-Za-z0-9_\u0080-￿]*)?\$")


def scan(sql: str, prefix: str = "$"):
    """Yield (kind, start, end) tokens: ident, param, string, qident, comment, space, other.

    Lenient: an unterminated literal or comment runs to the end (PostgreSQL then reports the error).
    """
    i, n = 0, len(sql)
    while i < n:
        c = sql[i]
        if c.isspace():
            j = i + 1
            while j < n and sql[j].isspace():
                j += 1
            tok = ("space", j)
        elif sql.startswith("--", i):
            j = sql.find("\n", i)
            j = n if j < 0 else j
            tok = ("comment", j)
        elif sql.startswith("/*", i):
            depth, j = 1, i + 2
            while j < n and depth:
                if sql.startswith("/*", j):
                    depth, j = depth + 1, j + 2
                elif sql.startswith("*/", j):
                    depth, j = depth - 1, j + 2
                else:
                    j += 1
            tok = ("comment", j)
        elif c == "'" or (c in "eEbBxXnN" and sql.startswith("'", i + 1)):
            esc = c in "eE"
            j = i + (1 if c == "'" else 2)
            while j < n:
                if esc and sql[j] == "\\":
                    j += 2
                    continue
                if sql[j] == "'":
                    if sql.startswith("''", j):
                        j += 2
                        continue
                    j += 1
                    break
                j += 1
            else:
                j = n
            tok = ("string", j)
        elif c == '"':
            j = i + 1
            while j < n:
                if sql[j] == '"':
                    if sql.startswith('""', j):
                        j += 2
                        continue
                    j += 1
                    break
                j += 1
            else:
                j = n
            tok = ("qident", j)
        elif c == "$" and (m := _DOLLAR_TAG.match(sql, i)):
            tag = m.group(0)
            k = sql.find(tag, m.end())
            j = n if k < 0 else k + len(tag)
            tok = ("string", j)
        elif c in "$?:" and i + 1 < n and sql[i + 1].isdigit() and not (c == ":" and i and sql[i - 1] == ":"):
            j = i + 1
            while j < n and sql[j].isdigit():
                j += 1
            tok = (("param" if c in ("$", prefix) else "other"), j)
        elif _IDENT_START.match(c):
            j = _IDENT_REST.match(sql, i + 1).end()
            tok = ("ident", j)
        elif c == ":" and sql.startswith("::", i):
            tok = ("other", i + 2)
        else:
            tok = ("other", i + 1)
        yield tok[0], i, tok[1]
        i = tok[1]


def _next_sig(toks: list, k: int, sql: str) -> str:
    for kind, a, b in toks[k + 1:]:
        if kind not in ("space", "comment"):
            return sql[a:b]
    return ""


def _case(src: str, word: str) -> str:
    return word.lower() if src == src.lower() else word.upper()


class Dialect:
    """SQL rewriting for one variant. A disabled dialect layer makes every method the identity."""

    MANGLE = "__"

    def __init__(self, spec: dict, enabled: bool = True):
        d = spec["dialect"]
        self.on = enabled
        self.prefix = d["param_prefix"] if enabled else "$"
        self.kw_p2v = {k.lower(): v.lower() for k, v in d["keywords"].items()}
        self.kw_v2p = {v: k for k, v in self.kw_p2v.items()}
        self.type_v2p = {v: k for k, v in d["types"].items()}
        self.type_p2v = {}  # every alias -> variant name
        for canon, v in d["types"].items():
            for alias in TYPES[canon]:
                self.type_p2v[alias] = v
        self.type_canon_p2v = dict(d["types"])
        self.fn_p2v = dict(d["functions"])
        self.fn_v2p = {v: k for k, v in self.fn_p2v.items()}
        self.old = set(self.kw_p2v) | set(self.type_p2v)  # old spellings that become plain identifiers
        self.old_fn = set(self.fn_p2v)
        self.upper_kw = {k.upper(): v.upper() for k, v in self.kw_p2v.items()}

    # variant -> PostgreSQL, with a map of replaced spans for error positions
    def to_pg(self, sql: str) -> tuple[str, list]:
        if not self.on:
            return sql, []
        toks = list(scan(sql, self.prefix))
        out, spans, pos = [], [], 0
        for k, (kind, a, b) in enumerate(toks):
            src = sql[a:b]
            new = src
            if kind == "ident":
                lw = src.lower()
                nxt = _next_sig(toks, k, sql)
                if lw in self.kw_v2p:
                    new = _case(src, self.kw_v2p[lw])
                elif lw in self.type_v2p:
                    new = _case(src, self.type_v2p[lw])
                elif lw in self.fn_v2p and nxt == "(":
                    new = _case(src, self.fn_v2p[lw])
                elif lw in self.old or (lw in self.old_fn and nxt == "("):
                    new = src + self.MANGLE
            elif kind == "param":
                if src[0] == self.prefix:
                    new = "$" + src[1:]
                else:  # PostgreSQL's own $n is not a parameter in this dialect
                    new = '"' + src + '"'
            if new != src:
                spans.append((pos, pos + len(new), a, b))
            out.append(new)
            pos += len(new)
        return "".join(out), spans

    @staticmethod
    def map_pos(spans: list, p: int) -> int:
        """PostgreSQL's 1-based character position in the rewritten text -> position in the client's text."""
        q, delta = p - 1, 0
        for na, nb, oa, ob in spans:
            if q < na:
                break
            if q < nb:
                return oa + 1
            delta = ob - nb
        return q + delta + 1

    # PostgreSQL -> variant, for the grader's workload generator
    def to_variant(self, sql: str) -> str:
        if not self.on:
            return sql
        toks = list(scan(sql, "$"))
        out = []
        for k, (kind, a, b) in enumerate(toks):
            src = sql[a:b]
            if kind == "ident":
                lw = src.lower()
                if lw in self.kw_p2v:
                    src = _case(src, self.kw_p2v[lw])
                elif lw in self.type_p2v:
                    src = _case(src, self.type_p2v[lw])
                elif lw in self.fn_p2v and _next_sig(toks, k, sql) == "(":
                    src = _case(src, self.fn_p2v[lw])
            elif kind == "param":
                src = self.prefix + src[1:]
            out.append(src)
        return "".join(out)

    # PostgreSQL text sent back -> variant text
    _WORD = re.compile(r'"([A-Za-z_][A-Za-z0-9_]*)"|\b([A-Za-z_][A-Za-z0-9_]*)\b(\s*\()?')

    def _demangle(self, w: str) -> str | None:
        if w.lower().endswith(self.MANGLE):
            base = w[: -len(self.MANGLE)]
            if base.lower() in self.old or base.lower() in self.old_fn:
                return base
        return None

    def message(self, text: str) -> str:
        """Error/notice text: mangled names back to what the client wrote, PostgreSQL's spellings to the variant's."""
        if not self.on:
            return text

        def rep(m: re.Match) -> str:
            q, w, paren = m.group(1), m.group(2), m.group(3) or ""
            if q is not None:
                d = self._demangle(q)
                if d is not None:
                    return f'"{d}"'
                if q.lower() in self.kw_p2v:
                    return '"' + _case(q, self.kw_p2v[q.lower()]) + '"'
                if q in self.type_canon_p2v:
                    return f'"{self.type_canon_p2v[q]}"'
                return m.group(0)
            d = self._demangle(w)
            if d is not None:
                return d + paren
            if w in self.upper_kw:
                return self.upper_kw[w] + paren
            if w in self.type_canon_p2v:
                return self.type_canon_p2v[w] + paren
            if paren and w in self.fn_p2v:
                return self.fn_p2v[w] + paren
            return m.group(0)

        return self._WORD.sub(rep, text)

    def tag(self, tag: str) -> str:
        if not self.on:
            return tag
        return " ".join(self.upper_kw.get(w, w) for w in tag.split(" "))

    def column(self, name: str) -> str:
        if not self.on:
            return name
        d = self._demangle(name)
        if d is not None:
            return d
        return self.fn_p2v.get(name, name)


# ------------------------------------------------------------------ semantics

_VERSION_RE = re.compile(rb"PostgreSQL [0-9.]+(?: \([^)]*\))?(?: on [^,]*, compiled by [^,]*, \d+-bit)?")
INT_OIDS, FLOAT_OIDS, BOOL_OID, NUMERIC_OID = {20, 21, 23}, {700, 701}, 16, 1700


class Semantics:
    def __init__(self, spec: dict, enabled: bool = True):
        s = spec["semantics"]
        self.on = enabled
        self.s = s
        self.cls = s["sqlstate_classes"]
        self.product = s["product"]

    def sqlstate(self, code: str) -> str:
        if not self.on or len(code) != 5:
            return code
        return self.cls.get(code[:2], code[:2]) + code[2:]

    def severity(self, sev: str) -> str:
        return self.s["severities"].get(sev, sev) if self.on else sev

    def text(self, t: str) -> str:
        return t.replace("PostgreSQL", self.product) if self.on else t

    def value(self, oid: int, v: bytes) -> bytes:
        if not self.on:
            return v
        s = self.s
        if oid == BOOL_OID:
            return {b"t": s["bool"][0].encode(), b"f": s["bool"][1].encode()}.get(v, v)
        if oid in INT_OIDS:
            st = s["int_style"]
            if st == "plus" and not v.startswith(b"-"):
                return b"+" + v
            if st == "grouped":
                neg = v.startswith(b"-")
                d = v[1:] if neg else v
                g = f"{int(d):_}".encode()
                return (b"-" if neg else b"") + g
            return v
        if oid in FLOAT_OIDS:
            st = s["float_style"]
            if st == "pg" or v in (b"NaN", b"Infinity", b"-Infinity"):
                return v
            x = float(v)
            return {"g6": "%.6g", "fixed3": "%.3f", "exp4": "%.4e"}[st].__mod__(x).encode()
        if oid == NUMERIC_OID and s["numeric_trim"] and b"." in v and v != b"NaN":
            v = v.rstrip(b"0").rstrip(b".")
            return v if v not in (b"", b"-") else b"0"
        if b"PostgreSQL" in v:
            v = _VERSION_RE.sub(f"{self.product} {self.s['version']}".encode(), v)
            return v.replace(b"PostgreSQL", self.product.encode())
        return v

    def tag(self, tag: str) -> str:
        if not self.on:
            return tag
        parts = tag.split(" ")
        st = self.s["tag_style"]
        if parts[0] == "INSERT" and len(parts) == 3 and st in ("nooid", "countfirst"):
            parts = [parts[0], parts[2]]
        if st == "countfirst" and len(parts) >= 2 and parts[-1].isdigit():
            parts = [parts[-1]] + parts[:-1]
        return " ".join(parts)

    def column(self, name: str) -> str:
        return name.upper() if self.on and self.s["colcase"] == "upper" else name
