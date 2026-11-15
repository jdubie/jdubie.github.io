"""Run the public checks against your server with PostgreSQL 16's expected answers (minus the grader's sandbox).

  python3 tools/run_public.py                      # everything (takes a while: sql and scale are long)
  python3 tools/run_public.py protocol             # the public conformance tests
  python3 tools/run_public.py protocol copy -v     # those whose name contains "copy", with diffs
  python3 tools/run_public.py clients              # psql, pgbench, psycopg 3, node-postgres, pgJDBC scripts
  python3 tools/run_public.py sql                  # the public regression files, as one schedule
  python3 tools/run_public.py sql int4 -v          # only files whose name contains int4 (test_setup always runs first)
  python3 tools/run_public.py acid                 # the public ACID scenarios
  python3 tools/run_public.py scale                # pgbench -i -s 10, restart time, throughput

Every check starts `sh start.sh` from /work on a fresh, empty data directory (PGDATA_DIR) and listens on
127.0.0.1:5432; crashes are SIGKILLs of the server's whole process group. The data directory is checked with
the grader's empty-files scanner (/opt/efs/scan.py) throughout. Not applied here: the 4 KB tmpfs, seccomp,
and the hidden commit-window crash points. The grader applies all of them.
"""

from __future__ import annotations

import json
import random
import sys
import time
import zlib
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
WORK = TOOLS.parent
PUBLIC = WORK / "public/expected"
sys.path.insert(0, str(TOOLS))
import acid  # noqa: E402
import clients_suite as cl  # noqa: E402
import conformance_public as cf  # noqa: E402
import regress as rg  # noqa: E402
import scale  # noqa: E402
from dbbox import LocalBox, LocalCli, Scanner, handshake  # noqa: E402

WEIGHTS = {"protocol": 0.20, "clients": 0.15, "sql": 0.30, "acid": 0.25, "scale": 0.10}
ACID_PUBLIC = ["atomic_rollback", "atomic_error_aborts", "crash_uncommitted", "crash_during_commit", "durability_acked",
               "no_dirty_read", "no_lost_update", "repeatable_read", "write_skew_serializable", "bank_invariant",
               "pgbench_crash"]
ACID_SEED = 101  # the hidden grader uses other seeds


def say(*a):
    print(*a, flush=True)


def run_protocol(pattern: str, verbose: bool) -> tuple[float, list]:
    exp = json.loads((PUBLIC / "conformance.json").read_text())["tests"]
    names = [n for n in exp if pattern in n]
    box = LocalBox()
    passed = 0
    try:
        if not box.start():
            say(f"server did not start (log: {box.logpath})")
            return 0.0, []
        with Scanner(box) as sc:
            for n in names:
                if not box.alive() or not handshake(box.host, box.port):
                    box.restart()
                got = cf.run_test(n, box.host, box.port)
                ok = got == exp[n]
                passed += ok
                say(f"protocol {n}: {'PASS' if ok else 'FAIL'}")
                if verbose and not ok:
                    say(f"    want {json.dumps(exp[n], ensure_ascii=False)[:1500]}\n    got  {json.dumps(got, ensure_ascii=False)[:1500]}")
    finally:
        box.close()
    return passed / max(1, len(names)), sc.violations


def run_clients(verbose: bool) -> tuple[float, list]:
    exp = json.loads((TOOLS / "clients/expected.json").read_text())["groups"]
    got = cl.run_all(LocalBox, LocalCli())
    s = cl.score(got, exp)
    for g, v in s["groups"].items():
        say(f"clients {g}: {v['passed']}/{v['total']}" + (f"  failed: {', '.join(v['failed'])}" if v["failed"] else ""))
        if verbose and g == "pgbench":
            for k, item in (got.get("pgbench") or {}).items():
                if isinstance(item, dict) and not item.get("ok"):
                    say(f"    {k}: {item.get('why', '')[:400]}")
        elif verbose:
            for k in v["failed"]:
                say(f"    {g}.{k}\n      want {json.dumps(exp[g][k], ensure_ascii=False)[:800]}"
                    f"\n      got  {json.dumps((got.get(g) or {}).get(k), ensure_ascii=False)[:800]}")
    return s["score"], got.get("violations", [])


def run_sql(pattern: str, verbose: bool) -> tuple[float, list]:
    exp = json.loads((PUBLIC / "regress.json").read_text())
    tests = [t for t in exp["schedule"] if t == "test_setup" or pattern in t]
    sub = {"tests": {t: v for t, v in exp["tests"].items() if t in tests}}
    box = LocalBox()
    try:
        if not box.start():
            say(f"server did not start (log: {box.logpath})")
            return 0.0, []
        with Scanner(box, every_s=5) as sc:
            run = rg.run_schedule(box, LocalCli(), tests, log=lambda m: say("sql", m))
    finally:
        box.close()
    s = rg.score(json.loads(json.dumps(run)), sub)
    for t, (p, n) in s["per_file"].items():
        say(f"sql {t}: {p}/{n}")
        if verbose:
            for f in s["fails"].get(t, []):
                say(f"    unit {f['i']}: {f['sql'].strip()[:200]!r}\n      want {json.dumps(f['exp'], ensure_ascii=False)[:600]}"
                    f"\n      got  {json.dumps(f['got'], ensure_ascii=False)[:600]}")
    say(f"sql statements passed: {s['statements']} (score is the mean over files)")
    return s["score"], sc.violations


def run_acid(pattern: str) -> tuple[float, list]:
    oks, violations = [], []
    cli = LocalCli()
    for name in ACID_PUBLIC:
        if pattern not in name:
            continue
        fn, _ = acid.SCENARIOS[name]
        box = LocalBox()
        box.cli = cli
        try:
            if not box.start():
                ok, why = False, "server did not start"
            else:
                with Scanner(box) as sc:
                    try:
                        ok, why = True, fn(box, random.Random(ACID_SEED * 7919 + zlib.crc32(name.encode())))
                    except acid.Fail as e:
                        ok, why = False, str(e)
                    except Exception as e:  # noqa: BLE001
                        ok, why = False, f"{type(e).__name__}: {e}"[:300]
                violations += sc.violations
        finally:
            box.close()
        say(f"acid {name}: {'PASS' if ok else 'FAIL'}  {why}")
        oks.append(ok)
    return (sum(oks) / len(oks) if oks else 0.0), violations


def run_scale() -> tuple[float, list]:
    box = LocalBox()
    try:
        r = scale.run(box, LocalCli())
    finally:
        box.close()
    for k, v in r["items"].items():
        say(f"scale {k}: credit {v['credit']:.2f}  {json.dumps({x: y for x, y in v.items() if x != 'credit'})[:300]}")
    return r["score"], r.get("violations", [])


def main() -> None:
    args = [a for a in sys.argv[1:] if a != "-v"]
    verbose = "-v" in sys.argv
    which = args[0] if args else "all"
    pattern = args[1] if len(args) > 1 else ""
    comps = list(WEIGHTS) if which == "all" else [which]
    scores, violations = {}, []
    for c in comps:
        t0 = time.time()
        if c == "protocol":
            s, v = run_protocol(pattern, verbose)
        elif c == "clients":
            s, v = run_clients(verbose)
        elif c == "sql":
            s, v = run_sql(pattern, verbose)
        elif c == "acid":
            s, v = run_acid(pattern)
        elif c == "scale":
            s, v = run_scale()
        else:
            sys.exit(f"unknown component {c}; use one of {', '.join(WEIGHTS)}")
        scores[c], violations = s, violations + list(v)
        say(f"== public {c} score {s:.3f} ({time.time() - t0:.0f} s)")
    if which == "all":
        say(f"== weighted public score {sum(WEIGHTS[k] * scores[k] for k in WEIGHTS):.3f}")
    say("== storage OK" if not violations else f"== {len(violations)} STORAGE VIOLATIONS, e.g. {violations[:3]}: "
        "the hidden grader would score this 0")


if __name__ == "__main__":
    main()
