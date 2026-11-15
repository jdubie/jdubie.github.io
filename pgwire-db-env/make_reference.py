"""Record PostgreSQL 16's answers for every graded suite, twice on fresh servers, and keep what agrees.

  python3 make_reference.py [conformance,clients,regress]

Writes grader/expected/{conformance,clients,regress}.json and the raw runs under ref/postgres/.
"""

from __future__ import annotations

import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "grader"))
import clients as cl  # noqa: E402
import conformance as cf  # noqa: E402
import regress as rg  # noqa: E402
from dbbox import ClientBox, PgBox  # noqa: E402

EXP = ROOT / "grader/expected"
RAW = ROOT / "ref/postgres"


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def conformance_once(tag: str) -> dict:
    box = PgBox(tag=tag)
    try:
        assert box.start(), "postgres did not start"
        return cf.run_all(box.host, box.port)
    finally:
        box.close()


def do_conformance() -> None:
    with ThreadPoolExecutor(2) as ex:
        a, b = ex.map(conformance_once, ["refcf-a", "refcf-b"])
    keep = {k: v for k, v in a.items() if b.get(k) == v and not any(x[0] in ("TIMEOUT", "EXC", "CLOSED") for x in v)}
    dropped = sorted(set(a) - set(keep))
    (RAW / "conformance_a.json").write_text(json.dumps(a, indent=0, ensure_ascii=False))
    (RAW / "conformance_b.json").write_text(json.dumps(b, indent=0, ensure_ascii=False))
    (EXP / "conformance.json").write_text(json.dumps({"tests": keep, "dropped": dropped}, indent=0, ensure_ascii=False))
    log(f"conformance: kept {len(keep)}/{len(a)}; dropped {dropped}")


def clients_once(tag: str) -> dict:
    cli = ClientBox(tag=tag)
    try:
        return cl.run_all(lambda: PgBox(), cli)
    finally:
        cli.close()


def do_clients() -> None:
    with ThreadPoolExecutor(2) as ex:
        a, b = ex.map(clients_once, ["refcl-a", "refcl-b"])
    (RAW / "clients_a.json").write_text(json.dumps(a, indent=1, ensure_ascii=False))
    (RAW / "clients_b.json").write_text(json.dumps(b, indent=1, ensure_ascii=False))
    exp, dropped = {}, []
    for g in ("psql", "psycopg", "node", "jdbc"):
        exp[g] = {}
        for k, v in a.get(g, {}).items():
            if b.get(g, {}).get(k) == v and not (isinstance(v, str) and v.startswith("ERR")):
                exp[g][k] = v
            else:
                dropped.append(f"{g}.{k}")
    (EXP / "clients.json").write_text(json.dumps({"groups": exp, "dropped": dropped}, indent=1, ensure_ascii=False))
    pg = {k: v.get("ok") for k, v in a.get("pgbench", {}).items()}
    log(f"clients: kept {sum(len(v) for v in exp.values())} items; dropped {dropped}; pgbench a {pg}")


def regress_once(tag: str) -> dict:
    cli = ClientBox(tag=tag)
    box = PgBox(tag=tag)
    try:
        assert box.start(), "postgres did not start"
        return rg.run_schedule(box, cli, log=lambda m: log(tag, m))
    finally:
        box.close()
        cli.close()


def do_regress() -> None:
    with ThreadPoolExecutor(2) as ex:
        a, b = ex.map(regress_once, ["refrg-a", "refrg-b"])
    (RAW / "regress_a.json").write_text(json.dumps(a, ensure_ascii=False))
    (RAW / "regress_b.json").write_text(json.dumps(b, ensure_ascii=False))
    a = json.loads((RAW / "regress_a.json").read_text())  # normalise int keys to str, as a grading run reads them
    b = json.loads((RAW / "regress_b.json").read_text())
    exp = rg.build_expected(a, b)
    exp["excluded_tests"] = rg.EXCLUDED_TESTS
    (EXP / "regress.json").write_text(json.dumps(exp, ensure_ascii=False))
    log("regress:", json.dumps(exp["stats"]))


PUBLIC_REGRESS = ["boolean", "char", "varchar", "text", "int2", "int4", "int8", "float8", "numeric", "strings", "date",
                  "timestamp", "interval", "create_table", "insert", "select", "select_distinct", "select_having", "subselect",
                  "union", "case", "join", "aggregates", "transactions", "arrays", "update", "delete", "with", "window",
                  "json", "copy", "sequence", "plpgsql", "psql"]


def public_regress_tests() -> list[str]:
    order = rg.schedule()
    return ["test_setup"] + sorted(PUBLIC_REGRESS, key=order.index)


def public_regress_once(tag: str) -> dict:
    cli = ClientBox(tag=tag)
    box = PgBox(tag=tag)
    try:
        assert box.start(), "postgres did not start"
        return rg.run_schedule(box, cli, public_regress_tests(), log=lambda m: log(tag, m))
    finally:
        box.close()
        cli.close()


def do_public_regress() -> None:
    """The public subset, run as its own schedule, with the data files where the agent's kit has them."""
    rg.CLIENT_REGRESS = "/work/public/regress"
    with ThreadPoolExecutor(2) as ex:
        a, b = ex.map(public_regress_once, ["refpr-a", "refpr-b"])
    a = json.loads(json.dumps(a))
    b = json.loads(json.dumps(b))
    exp = rg.build_expected(a, b)
    exp["schedule"] = public_regress_tests()
    (ROOT / "task/public/expected").mkdir(parents=True, exist_ok=True)
    (ROOT / "task/public/expected/regress.json").write_text(json.dumps(exp, ensure_ascii=False))
    log("public regress:", json.dumps(exp["stats"]))


if __name__ == "__main__":
    EXP.mkdir(parents=True, exist_ok=True)
    RAW.mkdir(parents=True, exist_ok=True)
    which = sys.argv[1].split(",") if len(sys.argv) > 1 else ["conformance", "clients", "regress"]
    jobs = {"conformance": do_conformance, "clients": do_clients, "regress": do_regress, "public_regress": do_public_regress}
    with ThreadPoolExecutor(3) as ex:
        list(ex.map(lambda w: jobs[w](), which))
    log("done")
