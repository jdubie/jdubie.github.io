"""Real off-the-shelf clients against a server: psql, pgbench, psycopg 3, node-postgres, pgJDBC.

All run from the pinned client container (ClientBox) on the grading network; nothing has network access.
Every psql / psycopg / node / JDBC item is compared with the same item run against PostgreSQL 16. pgbench
items are checks that need no reference: exit status, no failed transactions, minimum throughput, and the
TPC-B balance invariant sum(abalance) = sum(bbalance) = sum(tbalance) = sum(history.delta), with one history
row per processed transaction.

score = mean over the five clients of the share of that client's items passed.
"""

from __future__ import annotations

import json
import re
import time

JAR = "/opt/clients/postgresql-42.7.4.jar"
PY = "/opt/clients/venv/bin/python"
GROUPS = ["psql", "pgbench", "psycopg", "node", "jdbc"]

PSQL_ITEMS = {
    "scram_login": ("alice", "wonderland", ["-Atc", "SELECT current_user, current_database()"]),
    "md5_login": ("bob", "builder", ["-Atc", "SELECT current_user"]),
    "describe_objects": ("postgres", None, ["-f", "/clients/psql_describe.sql"]),
    "list_catalog": ("postgres", None, ["-c", "\\dn", "-c", "\\du", "-c", "\\l"]),
    "copy_roundtrip": ("postgres", None, ["-f", "/clients/psql_copy.sql"]),
    "variables_and_conditionals": ("postgres", None, ["-f", "/clients/psql_vars.sql"]),
    "formats": ("postgres", None, ["-f", "/clients/psql_formats.sql"]),
    "on_error_stop": ("postgres", None, ["-v", "ON_ERROR_STOP=1", "-c", "SELECT 1", "-c", "SELECT 1/0", "-c", "SELECT 3"]),
}


def _psql_norm(out: str) -> str:
    return re.sub(r"^psql:[^:\n]*:(\d+): ", "psql:<f>:\\1: ", out, flags=re.M)


def run_psql(cli, host: str) -> dict:
    out = {}
    for name, (user, pw, args) in PSQL_ITEMS.items():
        env = {"PGPASSWORD": pw} if pw else {}
        code, so, se = cli.run(["psql", "-X", "-h", host, "-U", user, "-d", "regression", *args], 60, env=env)
        out[name] = [code, _psql_norm(so + se)]
    return out


def _json_tail(text: str) -> dict:
    for line in reversed(text.strip().splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                return {}
    return {}


def run_psycopg(cli, host: str) -> dict:
    code, so, se = cli.run([PY, "/clients/psycopg_suite.py", host], 900)
    return _json_tail(so) or {"_crashed": f"exit {code}: {se[-300:]}"}


def run_node(cli, host: str) -> dict:
    code, so, se = cli.run(["node", "/clients/node_suite.js", host], 600)
    return _json_tail(so) or {"_crashed": f"exit {code}: {se[-300:]}"}


def run_jdbc(cli, host: str) -> dict:
    code, so, se = cli.run(["java", "-cp", JAR, "/clients/JdbcSuite.java", host], 600)
    return _json_tail(so) or {"_crashed": f"exit {code}: {se[-300:]}"}


# ---------------------------------------------------------------- pgbench

def _sql(cli, host: str, q: str, timeout: float = 120) -> str:
    code, so, se = cli.run(["psql", "-X", "-h", host, "-U", "postgres", "-d", "regression", "-Atc", q], timeout)
    return so.strip() if code == 0 else f"ERR {se.strip()[-200:]}"


def _sums(cli, host: str) -> list[str]:
    r = _sql(cli, host, "SELECT (SELECT sum(abalance) FROM pgbench_accounts), (SELECT sum(bbalance) FROM pgbench_branches), "
                        "(SELECT sum(tbalance) FROM pgbench_tellers), (SELECT coalesce(sum(delta), 0) FROM pgbench_history), "
                        "(SELECT count(*) FROM pgbench_history)")
    return r.split("|")


def _invariant(before: list[str], after: list[str], n: int) -> tuple[bool, str]:
    """pgbench truncates pgbench_history before each run, so compare balance deltas with the new history."""
    if len(before) != 5 or len(after) != 5:
        return False, f"sums unreadable: {before} {after}"
    try:
        da, db, dt = (int(after[i]) - int(before[i]) for i in range(3))
        d, rows = int(after[3]), int(after[4])
    except ValueError:
        return False, f"sums unreadable: {before} {after}"
    return (da == db == dt == d and rows == n), f"deltas {da} {db} {dt}, history sum {d}, history rows {rows} for {n} tx"


def _processed(text: str) -> tuple[int, int]:
    m = re.search(r"number of transactions actually processed: (\d+)", text)
    f = re.search(r"number of failed transactions: (\d+)", text)
    return (int(m.group(1)) if m else -1), (int(f.group(1)) if f else 0)


def run_pgbench(cli, host: str, duration: int = 8) -> dict:
    """Each item: {"ok": bool, "why": str}."""
    res: dict[str, dict] = {}
    base = ["-h", host, "-U", "postgres"]
    code, so, se = cli.run(["pgbench", "-i", "-s", "1", *base, "regression"], 600)
    counts = _sql(cli, host, "SELECT (SELECT count(*) FROM pgbench_accounts), (SELECT count(*) FROM pgbench_tellers), "
                             "(SELECT count(*) FROM pgbench_branches), (SELECT count(*) FROM pgbench_history)")
    res["init_client_side"] = {"ok": code == 0 and counts == "100000|10|1|0", "why": f"exit {code}, counts {counts}; {se.strip()[-160:]}"}
    for mode in ("simple", "extended", "prepared"):
        if not res["init_client_side"]["ok"]:
            res[f"tpcb_{mode}"] = {"ok": False, "why": "init failed"}
            continue
        before = _sums(cli, host)
        code, so, se = cli.run(["pgbench", "-c", "4", "-j", "2", "-T", str(duration), "-M", mode, *base, "regression"], duration + 120)
        n, failed = _processed(so)
        inv, why = _invariant(before, _sums(cli, host), n)
        ok = code == 0 and failed == 0 and n >= 50 and inv
        res[f"tpcb_{mode}"] = {"ok": ok, "why": f"exit {code}, {n} tx, {failed} failed, {why}; {se.strip()[-160:]}",
                               "tps": round(n / duration, 1)}
    code, so, se = cli.run(["pgbench", "-S", "-c", "4", "-j", "2", "-T", str(duration), "-M", "prepared", *base, "regression"], duration + 120)
    n, failed = _processed(so)
    res["select_only_prepared"] = {"ok": code == 0 and failed == 0 and n >= 200, "why": f"exit {code}, {n} tx; {se.strip()[-160:]}",
                                   "tps": round(n / duration, 1)}
    code, so, se = cli.run(["pgbench", "-i", "-I", "dtGvp", "-s", "1", *base, "regression"], 600)
    counts = _sql(cli, host, "SELECT (SELECT count(*) FROM pgbench_accounts), (SELECT sum(abalance) FROM pgbench_accounts), "
                             "(SELECT count(*) FROM pgbench_tellers)")
    res["init_server_side"] = {"ok": code == 0 and counts == "100000|0|10", "why": f"exit {code}, counts {counts}; {se.strip()[-160:]}"}
    return res


def run_all(box_factory, cli) -> dict:
    """Two fresh servers: one for psql/psycopg/node/JDBC (one shared database, distinct object names), one for pgbench."""
    from dbbox import Scanner

    out: dict = {"violations": []}
    box = box_factory()
    try:
        t0 = time.time()
        if not box.start():
            out |= {g: {"_crashed": "server did not start"} for g in ("psql", "psycopg", "node", "jdbc")}
        else:
            with Scanner(box) as sc:
                for g, fn in (("psql", run_psql), ("psycopg", run_psycopg), ("node", run_node), ("jdbc", run_jdbc)):
                    if not box.alive() and not box.start():
                        out[g] = {"_crashed": "server down"}
                        continue
                    out[g] = fn(cli, box.host)
            out["violations"] += sc.violations[:20]
        out["seconds_clients"] = round(time.time() - t0, 1)
    finally:
        box.close()
    box = box_factory()
    try:
        if not box.start():
            out["pgbench"] = {"_crashed": "server did not start"}
        else:
            with Scanner(box, every_s=5) as sc:
                out["pgbench"] = run_pgbench(cli, box.host)
            out["violations"] += sc.violations[:20]
    finally:
        box.close()
    return out


def score(got: dict, expected: dict) -> dict:
    """Per group: items passed / items expected. pgbench items carry their own ok flag."""
    per = {}
    for g in GROUPS:
        if g == "pgbench":
            items = got.get("pgbench", {})
            names = ["init_client_side", "tpcb_simple", "tpcb_extended", "tpcb_prepared", "select_only_prepared", "init_server_side"]
            passed = [n for n in names if isinstance(items.get(n), dict) and items[n].get("ok")]
            per[g] = {"passed": len(passed), "total": len(names), "failed": [n for n in names if n not in passed]}
            continue
        exp = expected.get(g, {})
        mine = got.get(g, {})
        passed = [k for k, v in exp.items() if k in mine and mine[k] == v]
        per[g] = {"passed": len(passed), "total": len(exp), "failed": [k for k in exp if k not in passed]}
    s = sum(v["passed"] / max(1, v["total"]) for v in per.values()) / len(per)
    return {"score": s, "groups": per}
