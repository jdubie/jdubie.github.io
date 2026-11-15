"""Grade one submission (a /work directory), or calibrate against real PostgreSQL 16.

  python3 grader/grade.py <work_dir> <out_dir> [--jobs 4] [--only protocol,clients,regress,acid,scale]
  python3 grader/grade.py --postgres <out_dir>       # every suite against PostgreSQL (no storage rule)

reward = 0.20 protocol + 0.15 clients + 0.30 sql + 0.25 acid + 0.10 scale, and 0 if the storage ever broke
the empty-files rule.
  protocol  share of byte-level conformance tests whose normalized transcript matches PostgreSQL's
  clients   mean over psql, pgbench, psycopg 3, node-postgres, pgJDBC of the share of items passed
  sql       PostgreSQL's regression schedule through psql: mean over files of statements passed / scored
  acid      0.5 x crash/isolation scenario runs passed + 0.5 x commit-window crash points passed
  scale     pgbench scale 10: init, restart time, select and TPC-B throughput, restart after writes
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import acid  # noqa: E402
import clients as cl  # noqa: E402
import conformance as cf  # noqa: E402
import crashpoint  # noqa: E402
import regress as rg  # noqa: E402
import scale  # noqa: E402
from dbbox import IMAGE, AgentBox, ClientBox, PgBox, Scanner, handshake  # noqa: E402

EXPECTED = HERE / "expected"
WEIGHTS = {"protocol": 0.20, "clients": 0.15, "sql": 0.30, "acid": 0.25, "scale": 0.10}
BUILD_TIMEOUT_S = 900


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, file=sys.stderr, flush=True)


def build(work: Path, out: Path) -> dict:
    """Copy the submission and run its build.sh (if any) offline. The servers mount the result read-only."""
    code = out / "code"
    if code.exists():
        shutil.rmtree(code)
    shutil.copytree(work, code, symlinks=True, ignore=shutil.ignore_patterns("data", "data-*", ".git", "node_modules"))
    if not (code / "start.sh").exists():
        return {"ok": False, "why": "no start.sh"}
    if not (code / "build.sh").exists():
        return {"ok": True, "why": "no build.sh"}
    t0 = time.time()
    try:
        r = subprocess.run(["docker", "run", "--rm", "--label", "pgw=1", "--network", "none", "--cap-drop", "ALL", "--read-only",
                            "--tmpfs", "/tmp:size=4g,exec", "--cpus", "8", "--memory", "8g",
                            "--user", f"{os.getuid()}:{os.getgid()}", "-e", "HOME=/tmp", "-v", f"{code.resolve()}:/work",
                            "-w", "/work", IMAGE, "sh", "/work/build.sh"], capture_output=True, text=True, timeout=BUILD_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        return {"ok": False, "why": "build.sh timed out"}
    (out / "build.log").write_text(r.stdout[-20000:] + "\n--- stderr ---\n" + r.stderr[-20000:])
    return {"ok": r.returncode == 0, "why": f"build.sh exit {r.returncode}", "seconds": round(time.time() - t0, 1)}


# ---------------------------------------------------------------- components

def comp_protocol(factory) -> dict:
    exp = json.loads((EXPECTED / "conformance.json").read_text())["tests"]
    box = factory()
    results, violations = {}, []
    try:
        if not box.start():
            return {"score": 0.0, "passed": 0, "total": len(exp), "why": "server did not start", "violations": []}
        with Scanner(box) as sc:
            for name in exp:
                if not box.alive() or not handshake(box.host, box.port):
                    box.restart()
                got = cf.run_test(name, box.host, box.port)
                results[name] = {"ok": got == exp[name], "got": got if got != exp[name] else None}
        violations = sc.violations[:20]
    finally:
        box.close()
    passed = sum(r["ok"] for r in results.values())
    fails = {k: {"exp": exp[k], "got": v["got"]} for k, v in results.items() if not v["ok"]}
    return {"score": passed / len(exp), "passed": passed, "total": len(exp), "failed": sorted(fails), "detail": fails,
            "violations": violations}


def comp_clients(factory, cli) -> dict:
    exp = json.loads((EXPECTED / "clients.json").read_text())["groups"]
    got = cl.run_all(factory, cli)
    s = cl.score(got, exp)
    return {"score": s["score"], "groups": s["groups"], "pgbench": got.get("pgbench"),
            "got": {g: got.get(g) for g in ("psql", "psycopg", "node", "jdbc")}, "violations": got.get("violations", [])}


def comp_sql(factory, cli, budget_s: float) -> dict:
    exp = json.loads((EXPECTED / "regress.json").read_text())
    box = factory()
    try:
        if not box.start():
            return {"score": 0.0, "why": "server did not start", "violations": []}
        with Scanner(box, every_s=5) as sc:
            run = rg.run_schedule(box, cli, list(exp["tests"]), budget_s=budget_s, log=lambda m: log("sql", m))
        run = json.loads(json.dumps(run))  # int keys -> str, as recorded for the reference
        s = rg.score(run, exp)
        return {**s, "violations": sc.violations[:20], "restarts": sum(v["restarts"] for v in run.values())}
    finally:
        box.close()


def comp_acid(factory, sup_factory, cli, jobs: int) -> dict:
    tasks = [(name, s) for name, (_, seeds) in acid.SCENARIOS.items() for s in range(seeds)]

    def do(t):
        try:
            return acid.run_one(factory, t[0], t[1], cli)
        except Exception as e:  # noqa: BLE001 - harness trouble on one item must not sink the rest
            return {"scenario": t[0], "seed": t[1], "ok": False, "why": f"harness: {e}"[:300], "violations": []}

    with ThreadPoolExecutor(jobs) as ex:
        runs = list(ex.map(do, tasks))
    cp = crashpoint.run_all(sup_factory, jobs)
    scen = sum(r["ok"] for r in runs) / len(runs)
    return {"score": 0.5 * scen + 0.5 * cp["score"], "scenarios_score": scen, "runs": f"{sum(r['ok'] for r in runs)}/{len(runs)}",
            "scenario_runs": runs, "crashpoint": cp,
            "violations": [v for r in runs for v in r.get("violations", [])][:20] + cp["violations"]}


def comp_scale(factory, cli) -> dict:
    box = factory()
    try:
        return scale.run(box, cli)
    finally:
        box.close()


def run_all(factory, sup_factory, out: Path, jobs: int, only: list[str], sql_budget: float, keep: bool = False) -> dict:
    cli_a, cli_b = ClientBox(), ClientBox()
    comps = {
        "protocol": lambda: comp_protocol(factory),
        "clients": lambda: comp_clients(factory, cli_b),
        "sql": lambda: comp_sql(factory, cli_a, sql_budget),
        "acid": lambda: comp_acid(factory, sup_factory, cli_b, jobs),
        "scale": lambda: comp_scale(factory, cli_b),
    }
    res: dict = {}

    def do(k):
        t0 = time.time()
        log(k, "start")
        try:
            r = comps[k]()
        except Exception as e:  # noqa: BLE001
            r = {"score": 0.0, "why": f"harness: {type(e).__name__}: {e}", "trace": traceback.format_exc()[-2000:], "violations": []}
        r["seconds"] = round(time.time() - t0, 1)
        log(k, "done", round(r["score"], 4), r["seconds"], "s")
        (out / f"{k}.json").write_text(json.dumps(r, indent=1, ensure_ascii=False, default=str))
        return k, r

    try:
        with ThreadPoolExecutor(len(only)) as ex:
            res = dict(ex.map(do, only))
    finally:
        cli_a.close()
        cli_b.close()
    if keep:  # regrading some components: reuse the saved results of the others
        for k in WEIGHTS:
            if k not in res and (out / f"{k}.json").exists():
                res[k] = json.loads((out / f"{k}.json").read_text())
    scores = {k: res[k]["score"] if k in res else 0.0 for k in WEIGHTS}
    violations = [v for r in res.values() for v in r.get("violations", [])]
    raw = sum(WEIGHTS[k] * v for k, v in scores.items())
    summary = {**{k: round(v, 4) for k, v in scores.items()}, "raw_reward": round(raw, 4), "violations": len(violations),
               "reward": 0.0 if violations else round(raw, 4)}
    if "protocol" in res:
        summary["protocol_tests"] = f"{res['protocol'].get('passed', 0)}/{res['protocol'].get('total', 0)}"
    if "sql" in res:
        summary["sql_statements"] = res["sql"].get("statements", "")
    if "acid" in res:
        summary["acid_runs"] = res["acid"].get("runs", "")
        summary["crash_points"] = res["acid"].get("crashpoint", {}).get("points", "")
    if "clients" in res:
        summary["clients_groups"] = {g: f"{v['passed']}/{v['total']}" for g, v in res["clients"].get("groups", {}).items()}
    if "scale" in res:
        summary["scale_items"] = {k: round(v.get("credit", 0), 3) for k, v in res["scale"].get("items", {}).items()}
    report = {"summary": summary, "violations": violations[:50]}
    (out / "grade.json").write_text(json.dumps(report, indent=1, ensure_ascii=False))
    return report


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("work", nargs="?")
    ap.add_argument("out")
    ap.add_argument("--postgres", action="store_true")
    ap.add_argument("--jobs", type=int, default=4)
    ap.add_argument("--only", default="protocol,clients,sql,acid,scale")
    ap.add_argument("--sql-budget", type=float, default=rg.TOTAL_BUDGET_S)
    ap.add_argument("--keep", action="store_true", help="with --only: keep the other components' saved results in <out_dir>")
    a = ap.parse_args()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    if a.postgres:
        factory, sup = PgBox, (lambda: PgBox(supervise=True))
    else:
        b = build(Path(a.work), out)
        if not b["ok"]:
            report = {"summary": {"reward": 0.0, "raw_reward": 0.0, **{k: 0 for k in WEIGHTS}, "build": b["why"]}}
            (out / "grade.json").write_text(json.dumps(report, indent=1))
            print(json.dumps(report["summary"]))
            return
        code = out / "code"
        factory = lambda: AgentBox(code)  # noqa: E731
        sup = lambda: AgentBox(code, supervise=True)  # noqa: E731
    report = run_all(factory, sup, out, a.jobs, a.only.split(","), a.sql_budget, a.keep)
    print(json.dumps(report["summary"]))


if __name__ == "__main__":
    main()
