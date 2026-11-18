"""Oracle v3: semantic changes that real PostgreSQL computes exactly, so the reference stays free and exact.

Three mechanisms, all deterministic per seed:
  1. Overrides: the variant's database defines operators / functions / aggregates in schema `public` with the
     same signatures as PostgreSQL's built-ins, and every session runs with search_path = public, pg_catalog,
     so PostgreSQL's own name resolution picks the variant's version ("the one earliest in the path wins").
     Each scalar override is a single-expression SQL function with every operator schema-qualified, declared
     STABLE so the planner inlines it before constant-folding (IMMUTABLE would be pre-evaluated as a function
     call and its errors would carry a CONTEXT naming it): no extra error fields, no internal names, no recursion.
  2. Query rewriting: NULL ordering. The proxy appends NULLS FIRST / NULLS LAST to every ORDER BY item that
     does not state one (`null_order_rewrite`), with a position map for error offsets.
  3. Database definition: the database's default collation (ICU), chosen when the oracle initialises.

`make_sem3(seed)` draws the changes from a stream separate from `variant.make_spec`, so a seed's wire format and
dialect are identical under v2 and v3: grading a v2 server against a v3 oracle isolates the semantic layer.
"""

from __future__ import annotations

import random

from variant import scan

COLLATIONS = {  # ICU locale -> what a prober sees
    "und": "ICU root: case-insensitive first, then lowercase before uppercase ('a' < 'A' < 'b')",
    "und-u-kf-upper": "ICU, uppercase first ('A' < 'a' < 'B')",
    "und-u-kn-true": "ICU with numeric ordering ('a2' < 'a10'), case-insensitive first",
}
OPTIONAL = ["concat_null", "null_eq", "round_half_even", "sum_empty_zero", "like_ci", "add_promotes"]


def make_sem3(seed: int) -> dict:
    rng = random.Random(f"bbx-sem3-{seed}")
    on = set(rng.sample(OPTIONAL, 4))
    return {
        "null_order": rng.choice(["first", "last", "flip"]),  # never PostgreSQL's (ASC last, DESC first)
        "collation": rng.choice(sorted(COLLATIONS)),
        "int_div": rng.choice(["floor", "numeric"]),  # never PostgreSQL's truncation
        **{k: k in on for k in OPTIONAL},
    }


def describe(s: dict) -> list[str]:
    out = [{"first": "NULLs sort first in both directions", "last": "NULLs sort last in both directions",
            "flip": "NULLs sort first ascending, last descending (PostgreSQL's reverse)"}[s["null_order"]],
           "text collation: " + COLLATIONS[s["collation"]],
           {"floor": "integer division floors (-7 / 2 = -4)", "numeric": "integer division returns an exact numeric (7 / 2 = 3.5000000000000000)"}[s["int_div"]]]
    text = {"concat_null": "|| treats NULL as an empty string", "null_eq": "= and <> are NULL-safe (NULL = NULL is true)",
            "round_half_even": "round() on numerics rounds half to even", "sum_empty_zero": "sum() over no values is 0, not NULL",
            "like_ci": "LIKE is case-insensitive", "add_promotes": "integer + integer returns bigint"}
    return out + [text[k] for k in OPTIONAL if s[k]]


# ------------------------------------------------------------------ overrides (run as postgres in database main)

def _fn(name: str, args: str, ret: str, body: str) -> str:
    return (f"CREATE FUNCTION public.{name}({args}) RETURNS {ret} LANGUAGE sql STABLE PARALLEL SAFE "
            f"AS $f$SELECT {body}$f$;")


def setup_sql(s: dict) -> str:
    q = []
    if s["int_div"]:
        res = {("int2", "int2"): "int2", ("int4", "int4"): "int4", ("int8", "int8"): "int8", ("int2", "int4"): "int4",
               ("int4", "int2"): "int4", ("int2", "int8"): "int8", ("int8", "int2"): "int8", ("int4", "int8"): "int8",
               ("int8", "int4"): "int8"}
        for (a, b), r in res.items():
            if s["int_div"] == "floor":
                body, ret = f"pg_catalog.floor($1::numeric OPERATOR(pg_catalog./) $2::numeric)::{r}", r
            else:
                body, ret = "$1::numeric OPERATOR(pg_catalog./) $2::numeric", "numeric"
            q.append(_fn(f"bbx_div_{a}_{b}", f"{a}, {b}", ret, body))
            q.append(f"CREATE OPERATOR public./ (LEFTARG = {a}, RIGHTARG = {b}, FUNCTION = public.bbx_div_{a}_{b});")
    if s["concat_null"]:
        q.append(_fn("bbx_cat", "text, text", "text", "pg_catalog.textcat(COALESCE($1, ''), COALESCE($2, ''))"))
        q.append("CREATE OPERATOR public.|| (LEFTARG = text, RIGHTARG = text, FUNCTION = public.bbx_cat);")
    if s["null_eq"]:
        for t in ("int2", "int4", "int8", "text", "numeric", "bool", "date", "float8"):
            eq = f"CASE WHEN $1 IS NULL THEN $2 IS NULL WHEN $2 IS NULL THEN false ELSE $1 OPERATOR(pg_catalog.=) $2 END"
            q.append(_fn(f"bbx_eq_{t}", f"{t}, {t}", "bool", eq))
            q.append(_fn(f"bbx_ne_{t}", f"{t}, {t}", "bool", f"NOT ({eq})"))
            q.append(f"CREATE OPERATOR public.= (LEFTARG = {t}, RIGHTARG = {t}, FUNCTION = public.bbx_eq_{t});")
            q.append(f"CREATE OPERATOR public.<> (LEFTARG = {t}, RIGHTARG = {t}, FUNCTION = public.bbx_ne_{t});")
    if s["round_half_even"]:
        p = "pg_catalog.power(10::numeric, $2)"
        y = f"($1 OPERATOR(pg_catalog.*) {p})"
        f = f"pg_catalog.floor{y}"
        d = f"({y} OPERATOR(pg_catalog.-) {f})"
        r = (f"CASE WHEN {d} OPERATOR(pg_catalog.>) 0.5 THEN {f} OPERATOR(pg_catalog.+) 1 "
             f"WHEN {d} OPERATOR(pg_catalog.<) 0.5 THEN {f} "
             f"WHEN pg_catalog.mod({f}, 2) OPERATOR(pg_catalog.=) 0 THEN {f} ELSE {f} OPERATOR(pg_catalog.+) 1 END")
        q.append(_fn("round", "numeric, int4", "numeric", f"pg_catalog.round(({r}) OPERATOR(pg_catalog./) {p}, $2)"))
        q.append(_fn("round", "numeric", "numeric", "public.round($1, 0)"))
    if s["sum_empty_zero"]:
        for t, st, add in (("int2", "int8", "$1 OPERATOR(pg_catalog.+) COALESCE($2, 0)::int8"),
                           ("int4", "int8", "$1 OPERATOR(pg_catalog.+) COALESCE($2, 0)::int8"),
                           ("int8", "numeric", "$1 OPERATOR(pg_catalog.+) COALESCE($2, 0)::numeric"),
                           ("numeric", "numeric", "$1 OPERATOR(pg_catalog.+) COALESCE($2, 0)"),
                           ("float4", "float4", "$1 OPERATOR(pg_catalog.+) COALESCE($2, 0)::float4"),
                           ("float8", "float8", "$1 OPERATOR(pg_catalog.+) COALESCE($2, 0)::float8")):
            q.append(_fn(f"bbx_sum_{t}", f"{st}, {t}", st, add))
            q.append(f"CREATE AGGREGATE public.sum({t}) (SFUNC = public.bbx_sum_{t}, STYPE = {st}, INITCOND = '0');")
    if s["like_ci"]:
        q.append(_fn("bbx_like", "text, text", "bool", "$1 OPERATOR(pg_catalog.~~*) $2"))
        q.append(_fn("bbx_nlike", "text, text", "bool", "$1 OPERATOR(pg_catalog.!~~*) $2"))
        q.append("CREATE OPERATOR public.~~ (LEFTARG = text, RIGHTARG = text, FUNCTION = public.bbx_like);")
        q.append("CREATE OPERATOR public.!~~ (LEFTARG = text, RIGHTARG = text, FUNCTION = public.bbx_nlike);")
    if s["add_promotes"]:
        q.append(_fn("bbx_add", "int4, int4", "int8", "$1::int8 OPERATOR(pg_catalog.+) $2::int8"))
        q.append("CREATE OPERATOR public.+ (LEFTARG = int4, RIGHTARG = int4, FUNCTION = public.bbx_add);")
    return "\n".join(q) + "\n"


# Catalog objects that would print the overrides' source: the variant does not have them.
BLOCKED = {"pg_proc", "pg_operator", "pg_aggregate", "pg_amop", "pg_get_functiondef", "pg_get_function_sqlbody", "routines",
           "pg_depend", "pg_rewrite", "pg_get_ruledef", "pg_get_viewdef"}


# ------------------------------------------------------------------ NULL ordering by query rewrite

_ORDER_END = {"limit", "offset", "fetch", "for", "union", "intersect", "except", "window", "rows", "range", "groups", "returning"}


def null_order_rewrite(sql: str, mode: str) -> tuple[str, list]:
    """Append NULLS FIRST/LAST to every ORDER BY item that has none. Returns (sql, spans) where spans map the new
    text back to `sql` in the same form as Dialect.to_pg's (zero-width original spans for insertions)."""
    toks = [t for t in scan(sql)]
    sig = [(k, a, b) for (k, a, b) in toks if k not in ("space", "comment")]
    inserts: list[tuple[int, str]] = []
    depth = 0
    ctx: list[dict] = []  # open ORDER BY lists: {depth, item_end, desc, explicit, using}

    def close_item(c: dict) -> None:
        if c["item_end"] is not None and not c["explicit"] and not c["using"]:
            first = {"first": True, "last": False, "flip": not c["desc"]}[mode]
            inserts.append((c["item_end"], " NULLS FIRST" if first else " NULLS LAST"))
        c.update(item_end=None, desc=False, explicit=False, using=False)

    i = 0
    while i < len(sig):
        k, a, b = sig[i]
        w = sql[a:b].lower() if k == "ident" else sql[a:b]
        i += 1
        if w == ")":
            if ctx and ctx[-1]["depth"] == depth:  # this paren ends the ORDER BY list (aggregate / window / subquery)
                close_item(ctx.pop())
            depth -= 1
            if ctx and ctx[-1]["depth"] == depth:  # an item that contained parentheses continues
                ctx[-1]["item_end"] = b
            continue
        top = ctx[-1] if ctx and ctx[-1]["depth"] == depth else None
        if top is not None:
            if w in (",", ";") or (k == "ident" and w in _ORDER_END):
                close_item(top)
                if w != ",":
                    ctx.pop()
                if w == "(":
                    depth += 1
                continue
            if k == "ident" and w == "nulls":
                top["explicit"] = True
            elif k == "ident" and w == "using":
                top["using"] = True
            elif k == "ident" and w in ("asc", "desc"):
                top["desc"] = w == "desc"
            top["item_end"] = b
        if w == "(":
            depth += 1
            continue
        if k == "ident" and w == "order" and i < len(sig) and sig[i][0] == "ident" and sql[sig[i][1]:sig[i][2]].lower() == "by":
            ctx.append({"depth": depth, "item_end": None, "desc": False, "explicit": False, "using": False})
            i += 1
    for c in reversed(ctx):  # lists running to the end of the text
        close_item(c)
    if not inserts:
        return sql, []
    inserts.sort()
    out, spans, last, pos = [], [], 0, 0
    for at, text in inserts:
        out.append(sql[last:at])
        pos += at - last
        spans.append((pos, pos + len(text), at, at))
        out.append(text)
        pos += len(text)
        last = at
    out.append(sql[last:])
    return "".join(out), spans
