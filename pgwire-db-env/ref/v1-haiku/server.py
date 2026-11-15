"""PostgreSQL-wire-compatible SQL server whose persistent state lives only in file names.

Persistence: a write-ahead log in $PGDATA_DIR/wal. Each committed transaction is a set of
EMPTY files whose names carry the hex-encoded SQL text of its statements, followed by an
empty commit marker file. Startup replays committed transactions in sequence order.
"""
import os
import re
import socket
import socketserver
import struct
import sys
import threading
from decimal import Decimal, ROUND_HALF_UP, getcontext
from functools import cmp_to_key

getcontext().prec = 40


class PgError(Exception):
    def __init__(self, code, msg):
        super().__init__(msg)
        self.code = code
        self.msg = msg


# ---------------------------------------------------------------- WAL (names only)
PGDATA = os.environ.get("PGDATA_DIR", "/tmp/pgdata")
WAL_DIR = os.path.join(PGDATA, "wal")
CHUNK = 200  # hex chars per file name part (100 bytes)


def _touch(path):
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    os.close(fd)


def _fsync_dir():
    fd = os.open(WAL_DIR, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def wal_write(seq, stmts):
    for idx, text in enumerate(stmts):
        hx = text.encode("utf-8").hex()
        parts = [hx[i:i + CHUNK] for i in range(0, len(hx), CHUNK)] or [""]
        for part, chunk in enumerate(parts):
            _touch(os.path.join(WAL_DIR, f"s{seq:012d}.{idx:06d}.{part:05d}.{chunk}"))
    _fsync_dir()
    _touch(os.path.join(WAL_DIR, f"s{seq:012d}.commit"))
    _fsync_dir()


WAL_RE = re.compile(r"^s(\d{12})\.(\d{6})\.(\d{5})\.([0-9a-f]*)$")
COMMIT_RE = re.compile(r"^s(\d{12})\.commit$")


def wal_load():
    os.makedirs(WAL_DIR, exist_ok=True)
    names = os.listdir(WAL_DIR)
    committed = set()
    chunks = {}
    for n in names:
        m = COMMIT_RE.match(n)
        if m:
            committed.add(int(m.group(1)))
            continue
        m = WAL_RE.match(n)
        if m:
            seq, idx, part = int(m.group(1)), int(m.group(2)), int(m.group(3))
            chunks.setdefault(seq, {}).setdefault(idx, {})[part] = m.group(4)
    # discard uncommitted leftovers
    for n in names:
        m = WAL_RE.match(n)
        if m and int(m.group(1)) not in committed:
            os.unlink(os.path.join(WAL_DIR, n))
    txns = []
    for seq in sorted(committed):
        stmts = []
        by_idx = chunks.get(seq, {})
        for idx in sorted(by_idx):
            parts = by_idx[idx]
            hx = "".join(parts[p] for p in sorted(parts))
            stmts.append(bytes.fromhex(hx).decode("utf-8"))
        txns.append((seq, stmts))
    next_seq = (max(committed) + 1) if committed else 1
    return txns, next_seq


# ---------------------------------------------------------------- tokenizer / parser
NUM_RE = re.compile(r"(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?")
ID_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*")


def tokenize(s):
    toks = []
    i, n = 0, len(s)
    while i < n:
        c = s[i]
        if c.isspace():
            i += 1
        elif s.startswith("--", i):
            j = s.find("\n", i)
            i = n if j < 0 else j
        elif s.startswith("/*", i):
            j = s.find("*/", i + 2)
            i = n if j < 0 else j + 2
        elif c == "'":
            j = i + 1
            buf = []
            while True:
                if j >= n:
                    raise PgError("42601", "unterminated quoted string")
                if s[j] == "'":
                    if j + 1 < n and s[j + 1] == "'":
                        buf.append("'")
                        j += 2
                        continue
                    break
                buf.append(s[j])
                j += 1
            toks.append(("str", "".join(buf)))
            i = j + 1
        elif c == '"':
            j = s.find('"', i + 1)
            if j < 0:
                raise PgError("42601", "unterminated quoted identifier")
            toks.append(("id", s[i + 1:j]))
            i = j + 1
        elif c.isdigit() or (c == "." and i + 1 < n and s[i + 1].isdigit()):
            m = NUM_RE.match(s, i)
            toks.append(("num", m.group(0)))
            i = m.end()
        elif c.isalpha() or c == "_":
            m = ID_RE.match(s, i)
            toks.append(("id", m.group(0).lower()))
            i = m.end()
        else:
            two = s[i:i + 2]
            if two in ("<>", "!=", "<=", ">=", "||", "::"):
                toks.append(("op", two))
                i += 2
            else:
                toks.append(("op", c))
                i += 1
    return toks


def split_statements(text):
    out, cur = [], []
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c == "-" and text.startswith("--", i):
            j = text.find("\n", i)
            i = n if j < 0 else j
            continue
        if c == "'" or c == '"':
            j = i + 1
            while j < n:
                if text[j] == c:
                    if j + 1 < n and text[j + 1] == c:
                        j += 2
                        continue
                    break
                j += 1
            cur.append(text[i:j + 1])
            i = j + 1
            continue
        if c == ";":
            s = "".join(cur).strip()
            if s:
                out.append(s)
            cur = []
            i += 1
            continue
        cur.append(c)
        i += 1
    s = "".join(cur).strip()
    if s:
        out.append(s)
    return out


RESERVED = {"from", "all", "where", "group", "order", "limit", "offset", "having", "union", "intersect",
            "except", "on", "join", "inner", "left", "right", "full", "cross", "and", "or",
            "returning", "for", "end", "then", "else", "when", "as", "set", "values", "into"}


class Parser:
    def __init__(self, toks):
        self.t = toks
        self.p = 0

    def peek(self, k=0):
        j = self.p + k
        return self.t[j] if j < len(self.t) else ("eof", "")

    def next(self):
        tok = self.peek()
        self.p += 1
        return tok

    def is_kw(self, word, k=0):
        tok = self.peek(k)
        return tok[0] == "id" and tok[1] == word

    def accept(self, word):
        if self.is_kw(word):
            self.p += 1
            return True
        return False

    def expect(self, word):
        if not self.accept(word):
            raise self.err()
        return word

    def is_op(self, op, k=0):
        tok = self.peek(k)
        return tok[0] == "op" and tok[1] == op

    def accept_op(self, op):
        if self.is_op(op):
            self.p += 1
            return True
        return False

    def expect_op(self, op):
        if not self.accept_op(op):
            raise self.err()

    def err(self):
        tok = self.peek()
        return PgError("42601", f'syntax error at or near "{tok[1]}"')

    def ident(self):
        tok = self.next()
        if tok[0] != "id":
            self.p -= 1
            raise self.err()
        return tok[1]

    def at_end(self):
        return self.p >= len(self.t)

    # expressions
    def expr(self):
        return self.or_()

    def or_(self):
        a = self.and_()
        while self.accept("or"):
            a = ("or", a, self.and_())
        return a

    def and_(self):
        a = self.not_()
        while self.accept("and"):
            a = ("and", a, self.not_())
        return a

    def not_(self):
        if self.accept("not"):
            return ("not", self.not_())
        return self.cmp()

    def cmp(self):
        a = self.add()
        while True:
            tok = self.peek()
            if tok[0] == "op" and tok[1] in ("=", "<>", "!=", "<", "<=", ">", ">="):
                self.p += 1
                op = "<>" if tok[1] == "!=" else tok[1]
                a = ("cmp", op, a, self.add())
            elif self.accept("is"):
                neg = self.accept("not")
                if self.accept("null"):
                    a = ("isnull", a, neg)
                elif self.accept("true"):
                    a = ("istrue", a, neg)
                elif self.accept("false"):
                    a = ("isfalse", a, neg)
                elif self.accept("distinct"):
                    self.expect("from")
                    b = self.add()
                    a = ("distinct", a, b, neg)
                else:
                    raise self.err()
            elif self.is_kw("not") and (self.is_kw("in", 1) or self.is_kw("between", 1) or self.is_kw("like", 1) or self.is_kw("ilike", 1)):
                self.p += 1
                a = self.postfix_pred(a, True)
            elif self.is_kw("in") or self.is_kw("between") or self.is_kw("like") or self.is_kw("ilike"):
                a = self.postfix_pred(a, False)
            else:
                return a

    def postfix_pred(self, a, neg):
        if self.accept("in"):
            self.expect_op("(")
            if self.is_kw("select") or self.is_kw("with"):
                raise PgError("0A000", "subqueries not supported")
            items = [self.expr()]
            while self.accept_op(","):
                items.append(self.expr())
            self.expect_op(")")
            return ("in", a, items, neg)
        if self.accept("between"):
            lo = self.add()
            self.expect("and")
            hi = self.add()
            return ("between", a, lo, hi, neg)
        if self.accept("like"):
            return ("like", a, self.add(), neg, False)
        if self.accept("ilike"):
            return ("like", a, self.add(), neg, True)
        raise self.err()

    def add(self):
        a = self.mul()
        while True:
            tok = self.peek()
            if tok[0] == "op" and tok[1] in ("+", "-", "||"):
                self.p += 1
                a = ("bin", tok[1], a, self.mul())
            else:
                return a

    def mul(self):
        a = self.unary()
        while True:
            tok = self.peek()
            if tok[0] == "op" and tok[1] in ("*", "/", "%"):
                self.p += 1
                a = ("bin", tok[1], a, self.unary())
            else:
                return a

    def unary(self):
        if self.accept_op("-"):
            return ("neg", self.unary())
        if self.accept_op("+"):
            return self.unary()
        return self.postfix()

    def postfix(self):
        a = self.primary()
        while self.accept_op("::"):
            a = ("cast", a, self.type_name()[0])
        return a

    def type_name(self):
        base = self.ident()
        if base == "double" and self.accept("precision"):
            base = "double"
        if base == "character" and self.accept("varying"):
            base = "varchar"
        if base == "character":
            base = "char"
        args = []
        if self.accept_op("("):
            while not self.accept_op(")"):
                tok = self.next()
                if tok[0] == "num":
                    args.append(int(tok[1]))
        while self.accept_op("["):
            self.expect_op("]")
        return base, args

    def primary(self):
        tok = self.peek()
        kind, val = tok
        if kind == "num":
            self.p += 1
            if re.fullmatch(r"\d+", val):
                return ("lit", int(val))
            if "e" in val.lower():
                return ("lit", float(val))
            return ("lit", Decimal(val))
        if kind == "str":
            self.p += 1
            return ("lit", val)
        if kind == "op" and val == "(":
            self.p += 1
            if self.is_kw("select"):
                raise PgError("0A000", "subqueries not supported")
            e = self.expr()
            self.expect_op(")")
            return e
        if kind == "op" and val == "*":
            self.p += 1
            return ("star",)
        if kind == "id":
            if val == "null":
                self.p += 1
                return ("lit", None)
            if val == "true":
                self.p += 1
                return ("lit", True)
            if val == "false":
                self.p += 1
                return ("lit", False)
            if val == "case":
                self.p += 1
                return self.case_()
            if val == "cast" and self.is_op("(", 1):
                self.p += 2
                e = self.expr()
                self.expect("as")
                tn = self.type_name()[0]
                self.expect_op(")")
                return ("cast", e, tn)
            if val in ("current_date", "current_timestamp", "now"):
                self.p += 1
                if self.accept_op("("):
                    self.expect_op(")")
                return ("func", "now", [], False, False)
            if val == "exists":
                raise PgError("0A000", "subqueries not supported")
            self.p += 1
            if self.accept_op("("):
                distinct = self.accept("distinct")
                if self.accept_op("*"):
                    self.expect_op(")")
                    return ("func", val, [], True, distinct)
                args = []
                if not self.is_op(")"):
                    args.append(self.expr())
                    while self.accept_op(","):
                        args.append(self.expr())
                self.expect_op(")")
                return ("func", val, args, False, distinct)
            if self.is_op(".") :
                self.p += 1
                if self.accept_op("*"):
                    return ("star", val)
                col = self.ident()
                return ("col", f"{val}.{col}")
            return ("col", val)
        raise self.err()

    def case_(self):
        operand = None
        if not self.is_kw("when"):
            operand = self.expr()
        whens = []
        while self.accept("when"):
            c = self.expr()
            self.expect("then")
            whens.append((c, self.expr()))
        els = None
        if self.accept("else"):
            els = self.expr()
        self.expect("end")
        return ("case", operand, whens, els)


# ---------------------------------------------------------------- values
INT_TYPES = {"int", "integer", "int4", "smallint", "int2", "bigint", "int8", "serial", "bigserial", "smallserial", "serial4", "serial8", "serial2"}
INT_RANGE = {"smallint": (-2**15, 2**15 - 1), "int2": (-2**15, 2**15 - 1), "smallserial": (-2**15, 2**15 - 1), "serial2": (-2**15, 2**15 - 1),
             "int": (-2**31, 2**31 - 1), "integer": (-2**31, 2**31 - 1), "int4": (-2**31, 2**31 - 1), "serial": (-2**31, 2**31 - 1), "serial4": (-2**31, 2**31 - 1)}
SERIAL_TYPES = {"serial", "bigserial", "smallserial", "serial2", "serial4", "serial8"}
FLOAT_TYPES = {"double", "real", "float", "float4", "float8"}


def quant(d, scale):
    return d.quantize(Decimal(1).scaleb(-scale), rounding=ROUND_HALF_UP)


def coerce(col, v):
    if v is None:
        return None
    t = col["base"]
    if t in INT_TYPES:
        if isinstance(v, bool):
            raise PgError("42804", f'column "{col["name"]}" is of type integer but expression is of type boolean')
        if isinstance(v, str):
            try:
                v = int(v.strip())
            except ValueError:
                raise PgError("22P02", f'invalid input syntax for type integer: "{v}"')
        elif isinstance(v, (Decimal, float)):
            v = int(Decimal(v).quantize(Decimal(1), rounding=ROUND_HALF_UP))
        if t in INT_RANGE:
            lo, hi = INT_RANGE[t]
            if not lo <= v <= hi:
                raise PgError("22003", "integer out of range")
        elif not -2**63 <= v <= 2**63 - 1:
            raise PgError("22003", "bigint out of range")
        return v
    if t in ("numeric", "decimal"):
        if isinstance(v, str):
            try:
                v = Decimal(v.strip())
            except Exception:
                raise PgError("22P02", f'invalid input syntax for type numeric: "{v}"')
        elif isinstance(v, bool):
            raise PgError("42804", "column is of type numeric but expression is of type boolean")
        else:
            v = Decimal(str(v)) if isinstance(v, float) else Decimal(v)
        args = col.get("args") or []
        if len(args) >= 2:
            p, s = args[0], args[1]
            v = quant(v, s)
            if len(v.as_tuple().digits) > p:
                raise PgError("22003", "numeric field overflow")
        elif len(args) == 1:
            v = quant(v, 0)
        return v
    if t in ("text", "varchar", "char", "name"):
        if isinstance(v, bool):
            v = "true" if v else "false"
        elif not isinstance(v, str):
            v = fmt(v)
        args = col.get("args") or []
        if args and t in ("varchar", "char") and len(v) > args[0]:
            if t == "varchar" or v[args[0]:].strip(" "):
                raise PgError("22001", f"value too long for type character varying({args[0]})")
            v = v[:args[0]]
        return v
    if t in FLOAT_TYPES:
        return float(v)
    if t in ("boolean", "bool"):
        if isinstance(v, bool):
            return v
        if isinstance(v, str):
            s = v.strip().lower()
            if s in ("true", "t", "yes", "y", "1", "on"):
                return True
            if s in ("false", "f", "no", "n", "0", "off"):
                return False
            raise PgError("22P02", f'invalid input syntax for type boolean: "{v}"')
        return bool(v)
    return v


def cast_value(v, t):
    if v is None:
        return None
    col = {"base": t, "name": "?", "args": []}
    return coerce(col, v)


def fmt(v):
    if v is None:
        return None
    if isinstance(v, bool):
        return "t" if v else "f"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, Decimal):
        return format(v, "f")
    if isinstance(v, float):
        if v != v:
            return "NaN"
        if v in (float("inf"), float("-inf")):
            return "Infinity" if v > 0 else "-Infinity"
        r = repr(v)
        if r.endswith(".0"):
            r = r[:-2]
        return r
    return str(v)


# ---------------------------------------------------------------- expression evaluation
AGGS = {"count", "sum", "avg", "min", "max"}


def contains_agg(e):
    if isinstance(e, tuple):
        if e and e[0] == "func" and e[1] in AGGS:
            return True
        return any(contains_agg(x) for x in e[1:] if isinstance(x, (tuple, list)))
    if isinstance(e, list):
        return any(contains_agg(x) for x in e)
    return False


def num_pair(a, b):
    if isinstance(a, float) or isinstance(b, float):
        return float(a), float(b)
    if isinstance(a, Decimal) or isinstance(b, Decimal):
        return Decimal(a), Decimal(b)
    return a, b


def truthy(v):
    return v is True


def compare(op, a, b):
    if isinstance(a, bool) or isinstance(b, bool):
        a, b = bool(a), bool(b)
    elif isinstance(a, (int, Decimal, float)) and isinstance(b, (int, Decimal, float)):
        a, b = num_pair(a, b)
    elif type(a) is not type(b):
        if isinstance(a, str) or isinstance(b, str):
            a, b = str(a), str(b)
    if op == "=":
        return a == b
    if op == "<>":
        return a != b
    if op == "<":
        return a < b
    if op == "<=":
        return a <= b
    if op == ">":
        return a > b
    return a >= b


def like_re(pat, icase):
    out = []
    i = 0
    while i < len(pat):
        c = pat[i]
        if c == "\\" and i + 1 < len(pat):
            out.append(re.escape(pat[i + 1]))
            i += 2
            continue
        out.append(".*" if c == "%" else "." if c == "_" else re.escape(c))
        i += 1
    return re.compile("".join(out), re.DOTALL | (re.IGNORECASE if icase else 0))


def ev(e, env, grp):
    k = e[0]
    if k == "lit":
        return e[1]
    if k == "col":
        name = e[1]
        if name in env:
            return env[name]
        raise PgError("42703", f'column "{name}" does not exist')
    if k == "cmp":
        a, b = ev(e[2], env, grp), ev(e[3], env, grp)
        if a is None or b is None:
            return None
        return compare(e[1], a, b)
    if k == "and":
        a = ev(e[1], env, grp)
        if a is False:
            return False
        b = ev(e[2], env, grp)
        if b is False:
            return False
        if a is None or b is None:
            return None
        return True
    if k == "or":
        a = ev(e[1], env, grp)
        if a is True:
            return True
        b = ev(e[2], env, grp)
        if b is True:
            return True
        if a is None or b is None:
            return None
        return False
    if k == "not":
        a = ev(e[1], env, grp)
        return None if a is None else (not a)
    if k == "isnull":
        a = ev(e[1], env, grp) is None
        return (not a) if e[2] else a
    if k == "istrue":
        a = ev(e[1], env, grp) is True
        return (not a) if e[2] else a
    if k == "isfalse":
        a = ev(e[1], env, grp) is False
        return (not a) if e[2] else a
    if k == "distinct":
        a, b = ev(e[1], env, grp), ev(e[2], env, grp)
        if a is None or b is None:
            r = (a is None) != (b is None)
        else:
            r = not compare("=", a, b)
        return (not r) if e[3] else r
    if k == "neg":
        a = ev(e[1], env, grp)
        return None if a is None else -a
    if k == "bin":
        op = e[1]
        a, b = ev(e[2], env, grp), ev(e[3], env, grp)
        if a is None or b is None:
            return None
        if op == "||":
            return fmt(a) + fmt(b)
        if op in ("+", "-", "*"):
            a, b = num_pair(a, b)
            return a + b if op == "+" else a - b if op == "-" else a * b
        if op == "/":
            if isinstance(a, int) and isinstance(b, int) and not isinstance(a, bool):
                if b == 0:
                    raise PgError("22012", "division by zero")
                q = abs(a) // abs(b)
                return q if (a >= 0) == (b >= 0) else -q
            a, b = num_pair(a, b)
            if b == 0:
                raise PgError("22012", "division by zero")
            return a / b
        if op == "%":
            if b == 0:
                raise PgError("22012", "division by zero")
            if isinstance(a, int) and isinstance(b, int):
                r = abs(a) % abs(b)
                return -r if a < 0 else r
            a, b = num_pair(a, b)
            return a % b
    if k == "in":
        a = ev(e[1], env, grp)
        if a is None:
            return None
        found, nul = False, False
        for it in e[2]:
            v = ev(it, env, grp)
            if v is None:
                nul = True
            elif compare("=", a, v):
                found = True
                break
        r = True if found else (None if nul else False)
        if r is None:
            return None
        return (not r) if e[3] else r
    if k == "between":
        a, lo, hi = ev(e[1], env, grp), ev(e[2], env, grp), ev(e[3], env, grp)
        if a is None or lo is None or hi is None:
            return None
        r = compare(">=", a, lo) and compare("<=", a, hi)
        return (not r) if e[4] else r
    if k == "like":
        a, p = ev(e[1], env, grp), ev(e[2], env, grp)
        if a is None or p is None:
            return None
        r = bool(like_re(p, e[4]).fullmatch(fmt(a)))
        return (not r) if e[3] else r
    if k == "cast":
        return cast_value(ev(e[1], env, grp), e[2])
    if k == "case":
        operand = ev(e[1], env, grp) if e[1] is not None else None
        for cond, res in e[2]:
            if e[1] is not None:
                c = ev(cond, env, grp)
                if c is not None and operand is not None and compare("=", operand, c):
                    return ev(res, env, grp)
            elif ev(cond, env, grp) is True:
                return ev(res, env, grp)
        return ev(e[3], env, grp) if e[3] is not None else None
    if k == "func":
        return call(e, env, grp)
    if k == "star":
        raise PgError("42601", 'syntax error at or near "*"')
    raise PgError("0A000", f"unsupported expression {k}")


def call(e, env, grp):
    name, args, star, distinct = e[1], e[2], e[3], e[4]
    if name in AGGS:
        if grp is None:
            raise PgError("42803", f"aggregate functions are not allowed here")
        if star:
            return len(grp)
        vals = [ev(args[0], g, None) for g in grp]
        vals = [v for v in vals if v is not None]
        if distinct:
            seen = []
            for v in vals:
                if not any(compare("=", v, s) for s in seen):
                    seen.append(v)
            vals = seen
        if name == "count":
            return len(vals)
        if not vals:
            return None
        if name == "sum":
            total = vals[0]
            for v in vals[1:]:
                a, b = num_pair(total, v)
                total = a + b
            return total
        if name == "avg":
            total = vals[0]
            for v in vals[1:]:
                a, b = num_pair(total, v)
                total = a + b
            if isinstance(total, float):
                return total / len(vals)
            return quant(Decimal(total) / Decimal(len(vals)), 16)
        if name == "min":
            return min(vals, key=cmp_to_key(lambda x, y: (x > y) - (x < y)))
        return max(vals, key=cmp_to_key(lambda x, y: (x > y) - (x < y)))
    vals = [ev(a, env, grp) for a in args]
    if name == "coalesce":
        for v in vals:
            if v is not None:
                return v
        return None
    if name == "nullif":
        return None if vals[0] is not None and vals[1] is not None and compare("=", vals[0], vals[1]) else vals[0]
    if name == "now":
        return "2026-01-01 00:00:00"
    if any(v is None for v in vals):
        return None
    if name == "abs":
        return abs(vals[0])
    if name in ("length", "char_length", "character_length"):
        return len(vals[0])
    if name == "upper":
        return vals[0].upper()
    if name == "lower":
        return vals[0].lower()
    if name == "round":
        d = Decimal(vals[0])
        return quant(d, vals[1] if len(vals) > 1 else 0)
    if name == "trim":
        return vals[0].strip(" ")
    raise PgError("42883", f"function {name}() does not exist")


# ---------------------------------------------------------------- catalog helpers
def get_table(db, name):
    t = db.get(name)
    if t is None:
        raise PgError("42P01", f'relation "{name}" does not exist')
    return t


def table_env(t, alias, row):
    env = {}
    for i, c in enumerate(t["cols"]):
        env[c["name"]] = row[i]
        env[(alias or t["name"]) + "." + c["name"]] = row[i]
    return env


def null_env(t, alias):
    return table_env(t, alias, [None] * len(t["cols"]))


def check_uniques(t, rows):
    for idxs, _ in t["uniq"]:
        seen = set()
        for r in rows:
            key = tuple(r[i] for i in idxs)
            if None in key:
                continue
            if key in seen:
                raise PgError("23505", f'duplicate key value violates unique constraint "{t["name"]}_key"')
            seen.add(key)


def build_uniq(t, rows):
    out = []
    for idxs, _ in t["uniq"]:
        s = set()
        for r in rows:
            key = tuple(r[i] for i in idxs)
            if None not in key:
                s.add(key)
        out.append((idxs, s))
    return out


def validate_row(db, t, row, newkeys_sets, own_keys_idx=None):
    for i, c in enumerate(t["cols"]):
        if row[i] is None and c["notnull"]:
            raise PgError("23502", f'null value in column "{c["name"]}" of relation "{t["name"]}" violates not-null constraint')
    env = table_env(t, None, row)
    for expr, cname in t["checks"]:
        if ev(expr, env, None) is False:
            raise PgError("23514", f'new row for relation "{t["name"]}" violates check constraint "{cname}"')
    for idx, ref, refcol in t["fks"]:
        v = row[idx]
        if v is None:
            continue
        rt = db.get(ref)
        if rt is None:
            raise PgError("42P01", f'relation "{ref}" does not exist')
        ci = [c["name"] for c in rt["cols"]].index(refcol)
        if not any(r[ci] == v for r in rt["rows"]):
            raise PgError("23503", f'insert or update on table "{t["name"]}" violates foreign key constraint')


def insert_keys_check(t, row, sets):
    for (idxs, s), _ in zip(sets, t["uniq"]):
        key = tuple(row[i] for i in idxs)
        if None in key:
            continue
        if key in s:
            raise PgError("23505", f'duplicate key value violates unique constraint "{t["name"]}_key"')
        s.add(key)


# ---------------------------------------------------------------- statements
class Result:
    def __init__(self, tag, cols=None, rows=None):
        self.tag = tag
        self.cols = cols
        self.rows = rows


def parse_items(p):
    items = []
    while True:
        if p.accept_op("*"):
            items.append((("star",), None))
        else:
            e = p.expr()
            alias = None
            if p.accept("as"):
                alias = p.ident()
            elif p.peek()[0] == "id" and p.peek()[1] not in RESERVED:
                alias = p.ident()
            items.append((e, alias))
        if not p.accept_op(","):
            return items


def col_name_of(e, alias):
    if alias:
        return alias
    if e[0] == "col":
        return e[1].split(".")[-1]
    if e[0] == "func":
        return e[1]
    return "?column?"


def run_select(db, p, parse_only=False):
    distinct = p.accept("distinct")
    items = parse_items(p)
    t = None
    alias = None
    if p.accept("from"):
        name = p.ident()
        alias = None
        if p.accept("as"):
            alias = p.ident()
        elif p.peek()[0] == "id" and p.peek()[1] not in RESERVED:
            alias = p.ident()
        t = get_table(db, name)
    where = p.expr() if p.accept("where") else None
    group = []
    if p.accept("group"):
        p.expect("by")
        group.append(p.expr())
        while p.accept_op(","):
            group.append(p.expr())
    having = p.expr() if p.accept("having") else None
    order = []
    if p.accept("order"):
        p.expect("by")
        while True:
            e = p.expr()
            desc = False
            if p.accept("desc"):
                desc = True
            else:
                p.accept("asc")
            nf = None
            if p.accept("nulls"):
                nf = p.accept("first") or not p.accept("last")
            order.append((e, desc, nf))
            if not p.accept_op(","):
                break
    limit = offset = None
    if p.accept("limit"):
        limit = p.expr() if not p.accept("all") else None
    if p.accept("offset"):
        offset = p.expr()
    if p.accept("fetch"):
        raise PgError("0A000", "FETCH not supported")
    if parse_only:
        return None

    cols_defs = t["cols"] if t else []
    if t is not None:
        envs = []
        for row in t["rows"]:
            env = table_env(t, alias, row)
            if where is None or ev(where, env, None) is True:
                envs.append(env)
        nenv = null_env(t, alias)
    else:
        envs = [{}]
        if where is not None and ev(where, {}, None) is not True:
            envs = []
        nenv = {}

    agg_mode = bool(group) or any(contains_agg(e) for e, _ in items) or (having is not None) or any(contains_agg(e) for e, _, _ in order)
    groups = []
    if agg_mode:
        if group:
            buckets = {}
            order_keys = []
            for env in envs:
                key = tuple(ev(g, env, None) for g in group)
                if key not in buckets:
                    buckets[key] = []
                    order_keys.append(key)
                buckets[key].append(env)
            groups = [(buckets[k][0], buckets[k]) for k in order_keys]
        else:
            groups = [(envs[0] if envs else nenv, envs)]
    else:
        groups = [(env, None) for env in envs]

    colnames = []
    for e, alias_ in items:
        if e[0] == "star":
            colnames.extend(c["name"] for c in cols_defs)
        else:
            colnames.append(col_name_of(e, alias_))

    out = []
    for env, grp in groups:
        if having is not None and ev(having, env, grp) is not True:
            continue
        vals = []
        for e, _ in items:
            if e[0] == "star":
                vals.extend(env.get(c["name"]) for c in cols_defs)
            else:
                vals.append(ev(e, env, grp))
        keys = []
        for e, desc, nf in order:
            if e[0] == "lit" and isinstance(e[1], int) and not isinstance(e[1], bool):
                if not 1 <= e[1] <= len(vals):
                    raise PgError("42P10", "ORDER BY position is not in select list")
                keys.append(vals[e[1] - 1])
            elif e[0] == "col" and e[1] in colnames and e[1] not in env:
                keys.append(vals[colnames.index(e[1])])
            else:
                keys.append(ev(e, env, grp))
        out.append((keys, vals))

    if order:
        def cmpf(a, b):
            for (ka, kb), (e, desc, nf) in zip(zip(a[0], b[0]), order):
                null_first = nf if nf is not None else desc
                if ka is None and kb is None:
                    continue
                if ka is None:
                    return -1 if null_first else 1
                if kb is None:
                    return 1 if null_first else -1
                r = compare("<", ka, kb)
                r = -1 if r else (1 if compare(">", ka, kb) else 0)
                if desc:
                    r = -r
                if r:
                    return r
            return 0
        out.sort(key=cmp_to_key(cmpf))

    rows = [vals for _, vals in out]
    if distinct:
        seen = []
        uniq_rows = []
        for r in rows:
            if r not in seen:
                seen.append(r)
                uniq_rows.append(r)
        rows = uniq_rows
    if offset is not None:
        off = ev(offset, {}, None)
        rows = rows[off:]
    if limit is not None:
        lim = ev(limit, {}, None)
        if lim is not None:
            rows = rows[:max(lim, 0)]
    return Result(f"SELECT {len(rows)}", colnames, rows)


def select_set(db, p):
    res = run_select(db, p)
    while p.is_kw("union"):
        p.p += 1
        allf = p.accept("all")
        p.expect("select")
        r2 = run_select(db, p)
        rows = res.rows + r2.rows
        if not allf:
            dedup = []
            for r in rows:
                if r not in dedup:
                    dedup.append(r)
            rows = dedup
        res = Result(f"SELECT {len(rows)}", res.cols, rows)
    return res


def returning_rows(t, envs_rows, items, cols_defs):
    colnames = []
    for e, alias in items:
        if e[0] == "star":
            colnames.extend(c["name"] for c in cols_defs)
        else:
            colnames.append(col_name_of(e, alias))
    rows = []
    for env in envs_rows:
        vals = []
        for e, _ in items:
            if e[0] == "star":
                vals.extend(env[c["name"]] for c in cols_defs)
            else:
                vals.append(ev(e, env, None))
        rows.append(vals)
    return colnames, rows


def parse_returning(p):
    if p.accept("returning"):
        return parse_items(p)
    return None


def create_table(db, p):
    if_not = False
    if p.accept("if"):
        p.expect("not")
        p.expect("exists")
        if_not = True
    name = p.ident()
    if p.accept("as"):
        raise PgError("0A000", "CREATE TABLE AS not supported")
    if name in db:
        if if_not:
            return db, Result("CREATE TABLE")
        raise PgError("42P07", f'relation "{name}" already exists')
    p.expect_op("(")
    cols = []
    uniq = []
    checks = []
    fks = []
    pk_cols = None
    while True:
        if p.is_kw("constraint"):
            p.p += 2
        if p.accept("primary"):
            p.expect("key")
            pk_cols = _ident_list(p)
        elif p.accept("unique"):
            uniq.append(_ident_list(p))
        elif p.accept("check"):
            p.expect_op("(")
            checks.append((p.expr(), f"{name}_check"))
            p.expect_op(")")
        elif p.accept("foreign"):
            p.expect("key")
            local = _ident_list(p)
            p.expect("references")
            ref = p.ident()
            refcols = _ident_list(p) if p.is_op("(") else ["id"]
            for lc, rc in zip(local, refcols):
                fks.append((lc, ref, rc))
        else:
            cname = p.ident()
            base, args = p.type_name()
            col = {"name": cname, "base": base, "args": args, "notnull": False, "default": None, "serial": False}
            if base in SERIAL_TYPES:
                col["serial"] = True
                col["notnull"] = True
                col["base"] = {"bigserial": "int8", "serial8": "int8", "smallserial": "int2", "serial2": "int2"}.get(base, "int4")
            col["_unique"] = False
            col["_pk"] = False
            col["_check"] = None
            col["_ref"] = None
            while True:
                if p.accept("constraint"):
                    p.ident()
                elif p.accept("not"):
                    p.expect("null")
                    col["notnull"] = True
                elif p.accept("null"):
                    pass
                elif p.accept("primary"):
                    p.expect("key")
                    col["_pk"] = True
                    col["notnull"] = True
                elif p.accept("unique"):
                    col["_unique"] = True
                elif p.accept("default"):
                    col["default"] = p.add() if not p.is_op("(") else p.primary()
                elif p.accept("check"):
                    p.expect_op("(")
                    checks.append((p.expr(), f"{name}_{cname}_check"))
                    p.expect_op(")")
                elif p.accept("references"):
                    ref = p.ident()
                    rc = _ident_list(p)[0] if p.is_op("(") else "id"
                    fks.append((cname, ref, rc))
                elif p.accept("generated"):
                    p.accept("always") or p.accept("by")
                    if p.accept("default"):
                        pass
                    p.expect("as")
                    p.expect("identity")
                    if p.accept_op("("):
                        while not p.accept_op(")"):
                            p.next()
                    col["serial"] = True
                    col["notnull"] = True
                elif p.accept("collate"):
                    p.ident()
                else:
                    break
            cols.append(col)
            if col["_pk"]:
                pk_cols = [cname]
            if col["_unique"]:
                uniq.append([cname])
            if col["_check"]:
                pass
        if p.accept_op(","):
            continue
        p.expect_op(")")
        break

    names = [c["name"] for c in cols]
    for c in cols:
        if c["name"] in names and names.count(c["name"]) > 1:
            raise PgError("42701", f'column "{c["name"]}" specified more than once')
    if pk_cols:
        for pc in pk_cols:
            if pc not in names:
                raise PgError("42703", f'column "{pc}" named in key does not exist')
            for c in cols:
                if c["name"] == pc:
                    c["notnull"] = True
        uniq.insert(0, pk_cols)
    uniq_idx = []
    for u in uniq:
        for uc in u:
            if uc not in names:
                raise PgError("42703", f'column "{uc}" named in key does not exist')
        uniq_idx.append((tuple(names.index(x) for x in u), set()))
    fk_idx = []
    for lc, ref, rc in fks:
        if lc not in names:
            raise PgError("42703", f'column "{lc}" referenced in foreign key constraint does not exist')
        fk_idx.append((names.index(lc), ref, rc))
    for c in cols:
        c.pop("_unique", None)
        c.pop("_pk", None)
        c.pop("_check", None)
        c.pop("_ref", None)
    t = {"name": name, "cols": cols, "rows": [], "uniq": uniq_idx, "checks": checks, "fks": fk_idx, "seq": 0}
    nd = dict(db)
    nd[name] = t
    return nd, Result("CREATE TABLE")


def _ident_list(p):
    p.expect_op("(")
    out = [p.ident()]
    while p.accept_op(","):
        out.append(p.ident())
    p.expect_op(")")
    return out


def next_default(t, c):
    if c["serial"]:
        t["seq"] += 1
        return t["seq"]
    if c["default"] is not None:
        return ev(c["default"], {}, None)
    return None


def insert_stmt(db, p):
    p.expect("into")
    name = p.ident()
    t0 = get_table(db, name)
    t = dict(t0)
    t["rows"] = list(t0["rows"])
    t["uniq"] = [(idx, set(s)) for idx, s in t0["uniq"]]
    cols = [c["name"] for c in t["cols"]]
    target = list(range(len(cols)))
    if p.is_op("("):
        names = _ident_list(p)
        target = []
        for nme in names:
            if nme not in cols:
                raise PgError("42703", f'column "{nme}" of relation "{name}" does not exist')
            target.append(cols.index(nme))
    if p.is_kw("select"):
        raise PgError("0A000", "INSERT ... SELECT not supported")
    p.expect("values")
    value_rows = []
    while True:
        p.expect_op("(")
        vals = [p.expr()]
        while p.accept_op(","):
            vals.append(p.expr())
        p.expect_op(")")
        if len(vals) != len(target):
            raise PgError("42601", "INSERT has more expressions than target columns" if len(vals) > len(target) else "INSERT has more target columns than expressions")
        value_rows.append(vals)
        if not p.accept_op(","):
            break
    items = parse_returning(p)
    new_rows = []
    for vals in value_rows:
        row = [None] * len(cols)
        given = set()
        for tgt, e in zip(target, vals):
            row[tgt] = coerce(t["cols"][tgt], ev(e, {}, None))
            given.add(tgt)
        for i, c in enumerate(t["cols"]):
            if i not in given:
                row[i] = coerce(c, next_default(t, c))
        validate_row(db, t, row, None)
        insert_keys_check(t, row, t["uniq"])
        new_rows.append(row)
    t["rows"].extend(new_rows)
    nd = dict(db)
    nd[name] = t
    if items is not None:
        envs = [table_env(t, None, r) for r in new_rows]
        cn, rows = returning_rows(t, envs, items, t["cols"])
        return nd, Result(f"INSERT 0 {len(new_rows)}", cn, rows)
    return nd, Result(f"INSERT 0 {len(new_rows)}")


def update_stmt(db, p):
    name = p.ident()
    alias = None
    if p.peek()[0] == "id" and not p.is_kw("set"):
        alias = p.ident()
    t0 = get_table(db, name)
    p.expect("set")
    assigns = []
    while True:
        cname = p.ident()
        p.expect_op("=")
        assigns.append((cname, p.expr()))
        if not p.accept_op(","):
            break
    where = p.expr() if p.accept("where") else None
    items = parse_returning(p)
    cols = [c["name"] for c in t0["cols"]]
    for cname, _ in assigns:
        if cname not in cols:
            raise PgError("42703", f'column "{cname}" of relation "{name}" does not exist')
    t = dict(t0)
    new_rows = []
    count = 0
    returned_envs = []
    for row in t0["rows"]:
        env = table_env(t0, alias, row)
        if where is None or ev(where, env, None) is True:
            count += 1
            nr = list(row)
            for cname, e in assigns:
                i = cols.index(cname)
                nr[i] = coerce(t0["cols"][i], ev(e, env, None))
            new_rows.append(nr)
            returned_envs.append(table_env(t0, alias, nr))
        else:
            new_rows.append(row)
    for nr in new_rows:
        pass
    for i, c in enumerate(t0["cols"]):
        pass
    for row in [r for r in new_rows]:
        pass
    # validate all changed rows
    for orig, nr in zip(t0["rows"], new_rows):
        if nr is not orig:
            validate_row(db, t0, nr, None)
    t["rows"] = new_rows
    check_uniques(t, new_rows)
    t["uniq"] = build_uniq(t, new_rows)
    nd = dict(db)
    nd[name] = t
    if items is not None:
        cn, rows = returning_rows(t, returned_envs, items, t0["cols"])
        return nd, Result(f"UPDATE {count}", cn, rows)
    return nd, Result(f"UPDATE {count}")


def delete_stmt(db, p):
    p.expect("from")
    name = p.ident()
    alias = None
    if p.peek()[0] == "id" and not p.is_kw("where") and not p.is_kw("returning"):
        alias = p.ident()
    t0 = get_table(db, name)
    where = p.expr() if p.accept("where") else None
    items = parse_returning(p)
    keep, gone = [], []
    for row in t0["rows"]:
        env = table_env(t0, alias, row)
        if where is None or ev(where, env, None) is True:
            gone.append(env)
        else:
            keep.append(row)
    check_referenced(db, name, t0, gone)
    t = dict(t0)
    t["rows"] = keep
    t["uniq"] = build_uniq(t, keep)
    nd = dict(db)
    nd[name] = t
    if items is not None:
        cn, rows = returning_rows(t, gone, items, t0["cols"])
        return nd, Result(f"DELETE {len(gone)}", cn, rows)
    return nd, Result(f"DELETE {len(gone)}")


def check_referenced(db, name, t0, gone):
    if not gone:
        return
    cols = [c["name"] for c in t0["cols"]]
    for tname, child in db.items():
        for idx, ref, refcol in child["fks"]:
            if ref != name:
                continue
            ci = cols.index(refcol)
            removed = {g[refcol] for g in gone}
            for r in child["rows"]:
                if r[idx] in removed and not (tname == name):
                    raise PgError("23503", f'update or delete on table "{name}" violates foreign key constraint on table "{tname}"')


def drop_table(db, p):
    if_exists = False
    if p.accept("if"):
        p.expect("exists")
        if_exists = True
    names = [p.ident()]
    while p.accept_op(","):
        names.append(p.ident())
    p.accept("cascade") or p.accept("restrict")
    nd = dict(db)
    for name in names:
        if name not in nd:
            if if_exists:
                continue
            raise PgError("42P01", f'table "{name}" does not exist')
        del nd[name]
    return nd, Result("DROP TABLE")


def truncate_stmt(db, p):
    p.accept("table")
    names = [p.ident()]
    while p.accept_op(","):
        names.append(p.ident())
    nd = dict(db)
    for name in names:
        t0 = get_table(db, name)
        t = dict(t0)
        t["rows"] = []
        t["uniq"] = [(idx, set()) for idx, _ in t0["uniq"]]
        nd[name] = t
    return nd, Result("TRUNCATE TABLE")


def create_index(db, p):
    unique = p.accept("unique")
    p.expect("index")
    if p.accept("if"):
        p.expect("not")
        p.expect("exists")
    if p.peek()[0] == "id" and not p.is_kw("on"):
        p.ident()
    p.expect("on")
    tname = p.ident()
    if p.accept("using"):
        p.ident()
    cols = _ident_list(p)
    t0 = get_table(db, tname)
    if unique:
        names = [c["name"] for c in t0["cols"]]
        idx = tuple(names.index(c) for c in cols)
        t = dict(t0)
        t["uniq"] = list(t0["uniq"]) + [(idx, set())]
        t["uniq"] = build_uniq(t, t0["rows"])
        check_uniques(dict(t, uniq=[(idx, None)]), t0["rows"])
        nd = dict(db)
        nd[tname] = t
        return nd, Result("CREATE INDEX")
    return db, Result("CREATE INDEX")


def alter_table(db, p):
    name = p.ident()
    t0 = get_table(db, name)
    if p.accept("add"):
        p.accept("column")
        cname = p.ident()
        base, args = p.type_name()
        notnull = False
        default = None
        while True:
            if p.accept("not"):
                p.expect("null")
                notnull = True
            elif p.accept("default"):
                default = p.primary()
            elif p.accept("null"):
                pass
            else:
                break
        col = {"name": cname, "base": base, "args": args, "notnull": notnull, "default": default, "serial": False}
        if base in SERIAL_TYPES:
            raise PgError("0A000", "serial not supported in ALTER")
        if any(c["name"] == cname for c in t0["cols"]):
            raise PgError("42701", f'column "{cname}" of relation "{name}" already exists')
        dv = coerce(col, ev(default, {}, None)) if default is not None else None
        if notnull and dv is None and t0["rows"]:
            raise PgError("23502", "column contains null values")
        t = dict(t0)
        t["cols"] = t0["cols"] + [col]
        t["rows"] = [r + [dv] for r in t0["rows"]]
        nd = dict(db)
        nd[name] = t
        return nd, Result("ALTER TABLE")
    raise PgError("0A000", "ALTER TABLE form not supported")


MUTATING = {"create", "drop", "insert", "update", "delete", "truncate", "alter"}


def apply(db, text):
    toks = tokenize(text)
    p = Parser(toks)
    kw = p.ident() if p.peek()[0] == "id" else ""
    if kw == "create":
        if p.is_kw("table") or p.is_kw("temp") or p.is_kw("temporary") or p.is_kw("unlogged"):
            p.accept("temp") or p.accept("temporary") or p.accept("unlogged")
            p.expect("table")
            return create_table(db, p)
        return create_index(db, p)
    if kw == "unique":
        return create_index(db, Parser([("id", "unique")] + toks[1:]))
    if kw == "drop":
        if p.accept("table"):
            return drop_table(db, p)
        raise PgError("0A000", "DROP form not supported")
    if kw == "insert":
        return insert_stmt(db, p)
    if kw == "update":
        return update_stmt(db, p)
    if kw == "delete":
        return delete_stmt(db, p)
    if kw == "truncate":
        return truncate_stmt(db, p)
    if kw == "alter":
        p.expect("table")
        return alter_table(db, p)
    if kw == "select" or kw == "values" or kw == "with":
        if kw == "with":
            raise PgError("0A000", "WITH not supported")
        if kw == "values":
            raise PgError("0A000", "VALUES not supported")
        return db, select_set(db, p)
    raise PgError("42601", f'syntax error at or near "{toks[0][1] if toks else ""}"')


# ---------------------------------------------------------------- engine state
LOCK = threading.RLock()
BASE = {"db": {}, "ver": 0}
TXN_CMDS = {"begin", "start", "commit", "end", "rollback", "abort", "savepoint", "release"}


class Session:
    def __init__(self):
        self.in_txn = False
        self.failed = False
        self.local = None
        self.begin_ver = 0
        self.log = []
        self.savepoints = []


def run_txn_cmd(sess, kw, toks):
    if kw in ("begin", "start"):
        if sess.in_txn:
            return Result("BEGIN")
        with LOCK:
            sess.local = dict(BASE["db"])
            sess.begin_ver = BASE["ver"]
        sess.in_txn = True
        sess.failed = False
        sess.log = []
        sess.savepoints = []
        return Result("START TRANSACTION" if kw == "start" else "BEGIN")
    if kw in ("commit", "end"):
        if not sess.in_txn:
            return Result("COMMIT")
        if sess.failed:
            _reset(sess)
            return Result("ROLLBACK")
        log = sess.log
        with LOCK:
            if BASE["ver"] == sess.begin_ver:
                candidate = sess.local
            else:
                candidate = BASE["db"]
                for s in log:
                    candidate, _ = apply(candidate, s)
            if log:
                seq = BASE.setdefault("seq", 1)
                wal_write(seq, log)
                BASE["seq"] = seq + 1
            BASE["db"] = candidate
            BASE["ver"] += 1
        _reset(sess)
        return Result("COMMIT")
    if kw in ("rollback", "abort"):
        if toks[1:2] and toks[1][1] == "to":
            return _rollback_to(sess, toks)
        _reset(sess)
        return Result("ROLLBACK")
    if kw == "savepoint":
        name = toks[1][1]
        sess.savepoints.append((name, sess.local, len(sess.log)))
        return Result("SAVEPOINT")
    if kw == "release":
        name = toks[1][1] if toks[1][1] != "savepoint" else toks[2][1]
        for i in range(len(sess.savepoints) - 1, -1, -1):
            if sess.savepoints[i][0] == name:
                del sess.savepoints[i:]
                return Result("RELEASE")
        raise PgError("3B001", f'savepoint "{name}" does not exist')
    raise PgError("0A000", "unsupported")


def _rollback_to(sess, toks):
    name = toks[2][1] if toks[2][1] != "savepoint" else toks[3][1]
    for i in range(len(sess.savepoints) - 1, -1, -1):
        if sess.savepoints[i][0] == name:
            _, db, loglen = sess.savepoints[i]
            del sess.savepoints[i + 1:]
            sess.local = db
            sess.log = sess.log[:loglen]
            sess.failed = False
            return Result("ROLLBACK")
    raise PgError("3B001", f'savepoint "{name}" does not exist')


def _reset(sess):
    sess.in_txn = False
    sess.failed = False
    sess.local = None
    sess.log = []
    sess.savepoints = []


def execute(sess, text):
    toks = tokenize(text)
    if not toks:
        return None
    kw = toks[0][1] if toks[0][0] == "id" else ""
    with LOCK:
        if kw in TXN_CMDS:
            if sess.failed and kw in ("savepoint", "release") :
                raise PgError("25P02", "current transaction is aborted, commands ignored until end of transaction block")
            if kw in ("rollback", "abort", "commit", "end") or (kw == "release" and False):
                pass
            if kw == "savepoint" and not sess.in_txn:
                raise PgError("25P01", "SAVEPOINT can only be used in transaction blocks")
            if kw in ("rollback", "abort", "release") and sess.failed and toks[1:2] and toks[1][1] == "to":
                return _rollback_to(sess, toks)
            if kw in ("rollback", "abort", "commit", "end", "release", "savepoint") and kw not in ("commit", "end") and not sess.in_txn and kw != "savepoint":
                return Result("ROLLBACK" if kw in ("rollback", "abort") else "RELEASE")
            return run_txn_cmd(sess, kw, toks)
        if kw in ("set", "reset", "discard", "deallocate", "listen", "unlisten", "notify"):
            if sess.failed:
                raise PgError("25P02", "current transaction is aborted, commands ignored until end of transaction block")
            return Result("SET" if kw == "set" else "RESET" if kw == "reset" else "DISCARD" if kw == "discard" else "DEALLOCATE" if kw == "deallocate" else kw.upper())
        if sess.failed:
            raise PgError("25P02", "current transaction is aborted, commands ignored until end of transaction block")
        if sess.in_txn:
            new_db, res = apply(sess.local, text)
            sess.local = new_db
            if kw in MUTATING:
                sess.log.append(text)
            return res
        new_db, res = apply(BASE["db"], text)
        if kw in MUTATING:
            seq = BASE.setdefault("seq", 1)
            wal_write(seq, [text])
            BASE["seq"] = seq + 1
            BASE["db"] = new_db
            BASE["ver"] += 1
        return res


# ---------------------------------------------------------------- pgwire
def read_n(f, n):
    data = f.read(n)
    if data is None or len(data) < n:
        return None
    return data


def send(conn, typ, body=b""):
    conn.sendall(typ + struct.pack("!i", len(body) + 4) + body)


def cstr(s):
    return s.encode("utf-8") + b"\x00"


def error_msg(code, msg):
    body = b"S" + cstr("ERROR") + b"V" + cstr("ERROR") + b"C" + cstr(code) + b"M" + cstr(msg) + b"\x00"
    return body


def send_result(conn, res):
    if res.cols is not None:
        body = struct.pack("!h", len(res.cols))
        for c in res.cols:
            body += cstr(c) + struct.pack("!ihihih", 0, 0, 25, -1, -1, 0)
        send(conn, b"T", body)
        for row in res.rows:
            rb = struct.pack("!h", len(row))
            for v in row:
                s = fmt(v)
                if s is None:
                    rb += struct.pack("!i", -1)
                else:
                    raw = s.encode("utf-8")
                    rb += struct.pack("!i", len(raw)) + raw
            send(conn, b"D", rb)
    send(conn, b"C", cstr(res.tag))


def handle(conn):
    f = conn.makefile("rb")
    sess = Session()
    try:
        while True:
            hdr = read_n(f, 4)
            if hdr is None:
                return
            ln = struct.unpack("!i", hdr)[0]
            body = read_n(f, ln - 4) if ln > 4 else b""
            if body is None:
                return
            code = struct.unpack("!i", body[:4])[0]
            if code == 80877103:
                conn.sendall(b"N")
                continue
            if code == 80877102:
                return
            if code == 196608:
                break
            return
        send(conn, b"R", struct.pack("!i", 0))
        for k, v in [("server_version", "16.0"), ("server_encoding", "UTF8"), ("client_encoding", "UTF8"),
                     ("DateStyle", "ISO, MDY"), ("integer_datetimes", "on"), ("standard_conforming_strings", "on"),
                     ("TimeZone", "UTC")]:
            send(conn, b"S", cstr(k) + cstr(v))
        send(conn, b"K", struct.pack("!ii", os.getpid() & 0x7FFFFFFF, 1))
        send(conn, b"Z", b"I")
        while True:
            t = read_n(f, 1)
            if t is None:
                return
            hdr = read_n(f, 4)
            if hdr is None:
                return
            ln = struct.unpack("!i", hdr)[0]
            body = read_n(f, ln - 4) if ln > 4 else b""
            if body is None:
                return
            if t == b"X":
                return
            if t == b"Q":
                sql = body[:-1].decode("utf-8", "replace")
                handle_query(conn, sess, sql)
            else:
                send(conn, b"E", error_msg("0A000", "extended protocol not supported"))
                send(conn, b"Z", status(sess))
    except (OSError, struct.error):
        return
    finally:
        try:
            conn.close()
        except OSError:
            pass


def status(sess):
    if sess.failed:
        return b"E"
    if sess.in_txn:
        return b"T"
    return b"I"


def handle_query(conn, sess, sql):
    stmts = split_statements(sql)
    if not stmts:
        send(conn, b"I")
    else:
        for s in stmts:
            try:
                res = execute(sess, s)
                if res is None:
                    send(conn, b"I")
                else:
                    if sess.in_txn and res.tag not in ("BEGIN",) and not sess.failed:
                        pass
                    send_result(conn, res)
            except PgError as e:
                if sess.in_txn:
                    sess.failed = True
                send(conn, b"E", error_msg(e.code, e.msg))
                break
            except RecursionError:
                if sess.in_txn:
                    sess.failed = True
                send(conn, b"E", error_msg("54001", "stack depth limit exceeded"))
                break
            except Exception as e:  # internal failure: report as syntax-ish error
                if sess.in_txn:
                    sess.failed = True
                send(conn, b"E", error_msg("XX000", f"internal error: {e}"))
                break
    send(conn, b"Z", status(sess))


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


class Handler(socketserver.BaseRequestHandler):
    def handle(self):
        handle(self.request)


def recover():
    txns, next_seq = wal_load()
    for seq, stmts in txns:
        db = BASE["db"]
        for s in stmts:
            db, _ = apply(db, s)
        BASE["db"] = db
        BASE["ver"] += 1
    BASE["seq"] = next_seq


def main():
    os.makedirs(PGDATA, exist_ok=True)
    recover()
    sys.setrecursionlimit(10000)
    srv = Server(("0.0.0.0", 5432), Handler)
    print(f"listening on 0.0.0.0:5432, data in {PGDATA}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
