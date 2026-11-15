"""Split SQL scripts into Query messages, and compare a server's results with PostgreSQL's."""

from __future__ import annotations

import re
from collections import Counter

from pgwire import Result


def split_statements(text: str) -> list[str]:
    """Split on top-level ';'. Handles '..', E'..', "..", $tag$..$tag$, -- and /* */ comments.

    Lines starting with a backslash (psql meta-commands) are dropped. A script whose first line is
    '-- mode: lines' is instead sent one line per Query message (to exercise multi-statement queries).
    """
    if text.startswith("-- mode: lines"):
        return [ln for ln in text.splitlines()[1:] if ln.strip()]
    text = "\n".join(ln for ln in text.splitlines() if not ln.lstrip().startswith("\\"))
    out, cur, i, n = [], [], 0, len(text)
    depth = 0
    while i < n:
        c = text[i]
        if c == "-" and text.startswith("--", i):
            j = text.find("\n", i)
            i = n if j < 0 else j
            continue
        if c == "/" and text.startswith("/*", i):
            d, i = 1, i + 2
            while i < n and d:
                if text.startswith("/*", i):
                    d, i = d + 1, i + 2
                elif text.startswith("*/", i):
                    d, i = d - 1, i + 2
                else:
                    i += 1
            cur.append(" ")
            continue
        if c in "'\"":
            esc = c == "'" and i > 0 and text[i - 1] in "eE" and (i < 2 or not (text[i - 2].isalnum() or text[i - 2] == "_"))
            j = i + 1
            while j < n:
                if esc and text[j] == "\\":
                    j += 2
                    continue
                if text[j] == c:
                    if j + 1 < n and text[j + 1] == c:
                        j += 2
                        continue
                    break
                j += 1
            cur.append(text[i:j + 1])
            i = j + 1
            continue
        if c == "$":
            m = re.match(r"\$([A-Za-z_][A-Za-z0-9_]*)?\$", text[i:])
            if m and not (i > 0 and (text[i - 1].isalnum() or text[i - 1] == "_")):
                tag = m.group(0)
                j = text.find(tag, i + len(tag))
                j = n if j < 0 else j + len(tag)
                cur.append(text[i:j])
                i = j
                continue
        if c == "(":
            depth += 1
        elif c == ")":
            depth = max(0, depth - 1)
        if c == ";" and depth == 0:
            s = "".join(cur).strip()
            if s:
                out.append(s)
            cur, i = [], i + 1
            continue
        cur.append(c)
        i += 1
    s = "".join(cur).strip()
    if s:
        out.append(s)
    return out


def top_level_order_by(sql: str) -> bool:
    """True if the statement ends with an ORDER BY outside any parentheses (so row order is defined)."""
    depth, s = 0, re.sub(r"'(?:[^']|'')*'", "''", sql.lower())
    flat = []
    for ch in s:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        flat.append(ch if depth == 0 else " ")
    return re.search(r"\border\s+by\b", "".join(flat)) is not None


def result_to_json(r: Result) -> dict:
    d = {"kind": r.kind}
    if r.kind == "rows":
        d |= {"ncol": len(r.columns), "rows": r.rows, "tag": r.tag}
    elif r.kind == "command":
        d["tag"] = r.tag
    elif r.kind == "error":
        d |= {"sqlstate": r.sqlstate, "message": r.message}
    return d


def same(expected: list[dict], got: list[dict] | None, sql: str) -> bool:
    """One Query message passes if every statement's result matches PostgreSQL's.

    rows: same column count and same rows (in order if the query has a top-level ORDER BY, else as a
    multiset); command: identical command tag (e.g. 'INSERT 0 3'); error: any error.
    """
    if got is None or len(got) != len(expected):
        return False
    ordered = top_level_order_by(sql)
    for e, g in zip(expected, got):
        if e["kind"] != g["kind"]:
            return False
        if e["kind"] == "rows":
            if e["ncol"] != g.get("ncol"):
                return False
            er = [tuple(r) for r in e["rows"]]
            gr = [tuple(r) for r in g["rows"]]
            if (er != gr) if ordered else (Counter(er) != Counter(gr)):
                return False
        elif e["kind"] == "command" and e["tag"] != g.get("tag"):
            return False
    return True


def expects_error(expected: list[dict]) -> bool:
    return any(r["kind"] == "error" for r in expected)


class Scorer:
    """Credit an expected error only if the latest statement expected to succeed also passed.

    Otherwise a server that rejects everything would collect every error PostgreSQL raises.
    """

    def __init__(self):
        self.last_success_ok = True

    def score(self, expected: list[dict], got: list[dict] | None, sql: str) -> bool:
        ok = same(expected, got, sql)
        if expects_error(expected):
            return ok and self.last_success_ok
        self.last_success_ok = ok
        return ok
