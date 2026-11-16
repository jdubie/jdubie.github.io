"""Grade one server against the oracle of variant --seed on hidden workloads from --wseed.

  python3 grader/grade.py --work <dir> <out>                 # a submission (start.sh, optional build.sh)
  python3 grader/grade.py --oracle-seed 7101 [--layers wire] <out>   # an oracle (calibration / ablation)
  python3 grader/grade.py --postgres <out>                   # stock PostgreSQL 16 on the same port
BBX_ORACLE_VERSION=1 selects the oracle as the Opus run saw it (no discovery hints); default 2.

reward = 0.45 sql + 0.25 protocol + 0.30 durability
  sql         mean over 60 SQL sessions of the per-message credit after login (0 for a session that never logs in)
  protocol    mean over 50 protocol workloads (25 templates x 2) of the per-message credit
  durability  share of 14 crash runs whose recovered state matches the oracle for some set of in-flight commits
Per message: 1 if byte-identical (BackendKeyData compared by shape only), up to 0.5 for the right message type
with some fields right, 0 otherwise; a step's denominator is max(oracle messages, candidate messages), plus one
when the oracle closed the connection.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT / "oracle"))
import durability  # noqa: E402
import workloads as W  # noqa: E402
from boxes import ORACLE_VERSION, SANDBOX, CandBox, OracleBox, PgBox  # noqa: E402
from variant import make_spec  # noqa: E402
from wire import run_workload, workload_score  # noqa: E402

WEIGHTS = {"sql": 0.45, "protocol": 0.25, "durability": 0.30}
CACHE = ROOT / "cache"
DEFAULT_SEED, DEFAULT_WSEED = 7101, 90210  # the task's variant; the hidden workload seed
BUILD_TIMEOUT_S = 900


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, file=sys.stderr, flush=True)


def build(work: Path, out: Path) -> dict:
    code = out / "code"
    if code.exists():
        shutil.rmtree(code)
    shutil.copytree(work, code, symlinks=True, ignore=shutil.ignore_patterns("data", "data-*", ".git", "node_modules", "target"))
    if not (code / "start.sh").exists():
        return {"ok": False, "why": "no start.sh"}
    if not (code / "build.sh").exists():
        return {"ok": True, "why": "no build.sh"}
    try:
        r = subprocess.run(["docker", "run", "--rm", "--label", "bbx=1", "--network", "none", "--cap-drop", "ALL", "--read-only",
                            "--tmpfs", "/tmp:size=4g,exec", "--cpus", "8", "--memory", "8g", "--user", f"{os.getuid()}:{os.getgid()}",
                            "-e", "HOME=/tmp", "-v", f"{code.resolve()}:/work", "-w", "/work", SANDBOX, "sh", "/work/build.sh"],
                           capture_output=True, text=True, timeout=BUILD_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        return {"ok": False, "why": "build.sh timed out"}
    (out / "build.log").write_text(r.stdout[-20000:] + "\n--- stderr ---\n" + r.stderr[-20000:])
    return {"ok": r.returncode == 0, "why": f"build.sh exit {r.returncode}"}


def oracle_transcripts(seed: int, wseed: int, rendered: list[dict]) -> list:
    """The oracle's answers to every workload, in order, on one fresh oracle. Cached per (seed, wseed)."""
    CACHE.mkdir(exist_ok=True)
    path = CACHE / f"oracle-v{ORACLE_VERSION}-{seed}-{wseed}.json"
    if path.exists():
        return json.loads(path.read_text())
    box = OracleBox(seed)
    try:
        if not box.start():
            raise RuntimeError("oracle did not start")
        out = []
        for wl in rendered:
            out.append(run_workload(box.host, box_port(), make_spec(seed), wl))
    finally:
        box.close()
    path.write_text(json.dumps(out))
    return out


def box_port() -> int:
    from boxes import PORT
    return PORT


def run_workloads(make_box, spec: dict, rendered: list[dict], expected: list, out: Path) -> dict:
    box = make_box()
    per = []
    try:
        started = box.start()
        if not started:
            log("candidate did not start")
        for wl, exp in zip(rendered, expected):
            if started and not box.alive():
                log("candidate died; restarting on the same data")
                started = box.start()
            if not started:
                got = [{"msgs": [], "closed": True, "timeout": False, "junk": None, "skipped": True} for _ in exp]
            else:
                got = run_workload(box.host, box_port(), spec, wl, oracle=exp)
            s = workload_score(spec, wl, exp, got)
            per.append({"id": wl["id"], "kind": wl["kind"], **s})
            (out / "transcripts").mkdir(exist_ok=True)
            (out / "transcripts" / f"{wl['id']}.json").write_text(json.dumps({"oracle": exp, "candidate": got}))
    finally:
        restarts = box.starts
        box.close()
    res = {}
    for kind in ("sql", "protocol"):
        xs = [p for p in per if p["kind"] == kind]
        res[kind] = {"score": sum(p["score"] for p in xs) / len(xs), "n": len(xs),
                     "perfect": sum(p["score"] >= 0.9999 for p in xs), "zero": sum(p["score"] == 0 for p in xs)}
    res["per_workload"] = per
    res["starts"] = restarts
    return res


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("out")
    ap.add_argument("--work")
    ap.add_argument("--oracle-seed", type=int)
    ap.add_argument("--layers", default="wire,dialect,semantics")
    ap.add_argument("--postgres", action="store_true")
    ap.add_argument("--seed", type=int, default=DEFAULT_SEED)
    ap.add_argument("--wseed", type=int, default=DEFAULT_WSEED)
    ap.add_argument("--only", default="workloads,durability")
    a = ap.parse_args()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    spec = make_spec(a.seed)
    t0 = time.time()
    meta: dict = {"seed": a.seed, "wseed": a.wseed, "oracle_version": ORACLE_VERSION}
    if a.work:
        b = build(Path(a.work), out)
        meta["build"] = b
        if not b["ok"]:
            log("build failed:", b["why"])
        make_box = lambda: CandBox(out / "code")  # noqa: E731
        meta["target"] = f"submission {a.work}"
    elif a.oracle_seed is not None:
        make_box = lambda: OracleBox(a.oracle_seed, a.layers)  # noqa: E731
        meta["target"] = f"oracle seed {a.oracle_seed} layers {a.layers}"
    elif a.postgres:
        make_box = PgBox
        meta["target"] = "stock PostgreSQL 16"
    else:
        ap.error("need --work, --oracle-seed or --postgres")
    rendered = [W.render(spec, wl) for wl in W.generate(a.wseed)]
    res: dict = {}
    only = a.only.split(",")
    if "workloads" in only:
        log("oracle transcripts")
        expected = oracle_transcripts(a.seed, a.wseed, rendered)
        log("candidate workloads")
        res.update(run_workloads(make_box, spec, rendered, expected, out))
        log("sql", round(res["sql"]["score"], 4), "protocol", round(res["protocol"]["score"], 4))
    elif (out / "grade.json").exists():
        old = json.loads((out / "grade.json").read_text())
        res.update({k: old[k] for k in ("sql", "protocol", "per_workload") if k in old})
    if "durability" in only:
        ob = OracleBox(a.seed)
        try:
            if not ob.start():
                raise RuntimeError("oracle did not start")
            res["durability"] = durability.run_all(make_box, ob.host, spec, a.wseed, log=lambda m: log(m))
        finally:
            ob.close()
    elif (out / "grade.json").exists():
        old = json.loads((out / "grade.json").read_text())
        if "durability" in old:
            res["durability"] = old["durability"]
    scores = {k: res[k]["score"] if k in res else 0.0 for k in WEIGHTS}
    summary = {**{k: round(v, 4) for k, v in scores.items()}, "reward": round(sum(WEIGHTS[k] * v for k, v in scores.items()), 4)}
    if "sql" in res:
        summary["sql_perfect"] = f"{res['sql']['perfect']}/{res['sql']['n']}"
        summary["protocol_perfect"] = f"{res['protocol']['perfect']}/{res['protocol']['n']}"
    if "durability" in res:
        summary["durability_runs"] = f"{res['durability']['passed']}/{res['durability']['total']}"
    meta["seconds"] = round(time.time() - t0, 1)
    (out / "grade.json").write_text(json.dumps({"summary": summary, "meta": meta, **res}, indent=1))
    log("summary", json.dumps(summary))


if __name__ == "__main__":
    main()
