"""Grade one or more find-duplicates submissions against the hidden cases.

  python3 grader/grade.py OUT_ROOT LABEL=WORK_DIR [LABEL=WORK_DIR ...] [--cases a,b] [--calibrate]

Each case's fixture is generated once on host tmpfs and shared by every submission (mounted read-only),
so N submissions cost one generation. Submissions run one at a time so timings don't contend.

per case:  speedup = t_naive / t   if the groups are exactly right and it finished in time, else FAIL
           gate cases (cases.GATES, tiny correctness traps) count 1.0 when right
reward  =  geometric mean over cases of max(speedup, 0.01), a FAIL counting as 0.01 (100x slower than naive)
--calibrate runs the naive reference too and (re)writes baseline.json and the expected answers.
"""

from __future__ import annotations

import argparse
import gzip
import json
import math
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import cases  # noqa: E402
import reffind  # noqa: E402

IMAGE = "dupfind-sandbox:1"
SHM = Path("/dev/shm/dupfind-grade")
EXPECTED, BASELINE = HERE / "expected", HERE / "baseline.json"
FLOOR, BUILD_TIMEOUT_S = 0.01, 900
REPS_UNDER_S = 3.0  # cases whose naive time is under this run 3 times; the best counts


def parse(b: bytes) -> set[frozenset[bytes]] | None:
    """NUL-terminated paths, an empty record ends a group. None if malformed."""
    if not b:
        return set()
    if not b.endswith(b"\0\0"):
        return None
    groups, cur = set(), []
    for rec in b[:-1].split(b"\0"):
        if rec:
            cur.append(rec)
        elif cur:
            groups.add(frozenset(cur))
            cur = []
        else:
            return None
    return groups


def dump_expected(name: str, groups: list[list[bytes]]) -> None:
    EXPECTED.mkdir(exist_ok=True)
    body = sorted(sorted(p.hex() for p in g) for g in groups)
    with gzip.open(EXPECTED / f"{name}.json.gz", "wt") as f:
        json.dump(body, f)


def load_expected(name: str) -> set[frozenset[bytes]]:
    with gzip.open(EXPECTED / f"{name}.json.gz", "rt") as f:
        return {frozenset(bytes.fromhex(p) for p in g) for g in json.load(f)}


def docker_base(code: Path) -> list[str]:
    return ["docker", "run", "--rm", "--init", "--network", "none", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
            # docker-default AppArmor mediates by full path and returns ENAMETOOLONG past PATH_MAX even for
            # *at() calls, which would make deep_pathmax unsolvable. Caps, seccomp and the network stay locked.
            "--security-opt", "apparmor=unconfined", "--read-only", "--user", f"{os.getuid()}:{os.getgid()}", "--cpus", "8", "--memory", "8g", "--memory-swap", "8g",
            "--pids-limit", "4096", "--tmpfs", "/tmp:size=1g,exec", "-e", "HOME=/tmp", "-v", f"{code.resolve()}:/work:ro"]


def build(work: Path, out: Path) -> dict:
    code = out / "code"
    if code.exists():
        shutil.rmtree(code)
    shutil.copytree(work, code, symlinks=True, ignore=shutil.ignore_patterns(".git", "public_cases"))
    if not (code / "run.sh").exists():
        return {"ok": False, "why": "no run.sh"}
    if not (code / "build.sh").exists():
        return {"ok": True, "why": "no build.sh"}
    argv = docker_base(code) + ["-w", "/work", IMAGE, "sh", "/work/build.sh"]
    argv[argv.index(f"{code.resolve()}:/work:ro")] = f"{code.resolve()}:/work"
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=BUILD_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        return {"ok": False, "why": "build.sh timed out"}
    (out / "build.log").write_text(r.stdout[-20000:] + "\n--- stderr ---\n" + r.stderr[-20000:])
    return {"ok": r.returncode == 0, "why": f"build.sh exit {r.returncode}"}


def solve(code: Path, fixture: Path, out: Path, timeout: float) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    argv = docker_base(code) + ["-v", f"{fixture}:/data/root:ro", "-v", f"{HERE / 'timer.py'}:/opt/timer.py:ro",
                                "-v", f"{out.resolve()}:/out", IMAGE, "python3", "/opt/timer.py", str(timeout)]
    try:
        subprocess.run(argv, capture_output=True, timeout=timeout + 120)
        return json.loads((out / "result.json").read_text())
    except (subprocess.TimeoutExpired, FileNotFoundError, json.JSONDecodeError) as e:
        return {"seconds": timeout, "exit": None, "timeout": True, "stderr_tail": f"harness: {type(e).__name__}"}


def run_case(name: str, code: Path, fixture: Path, out: Path, base: float, expected) -> dict:
    timeout = min(900.0, 10 * base + 20)
    best = None
    for rep in range(3 if base < REPS_UNDER_S else 1):
        r = solve(code, fixture, out / f"rep{rep}", timeout)
        got = parse((out / f"rep{rep}" / "stdout").read_bytes()) if (out / f"rep{rep}" / "stdout").exists() else None
        r["correct"] = (not r["timeout"]) and r["exit"] == 0 and got == expected
        if not r["correct"]:
            r["why"] = ("timeout" if r["timeout"] else f"exit {r['exit']}" if r["exit"] != 0 else
                        "malformed output" if got is None else diff_summary(got, expected))
            (out / f"rep{rep}" / "stdout").unlink(missing_ok=True)
            return {"case": name, "correct": False, "why": r["why"], "seconds": round(r["seconds"], 4),
                    "stderr_tail": r["stderr_tail"][-2000:], "naive_s": base}
        (out / f"rep{rep}" / "stdout").unlink(missing_ok=True)
        if best is None or r["seconds"] < best:
            best = r["seconds"]
    speedup = 1.0 if name in cases.GATES else round(base / best, 3)
    return {"case": name, "correct": True, "seconds": round(best, 4), "naive_s": base, "speedup": speedup}


def diff_summary(got, exp) -> str:
    missing, extra = exp - got, got - exp
    ex = lambda s: [sorted(p.decode("utf-8", "backslashreplace")[-60:] for p in g)[:3] for g in list(s)[:2]]  # noqa: E731
    return f"{len(missing)} expected groups missing, {len(extra)} wrong groups; e.g. missing {ex(missing)} wrong {ex(extra)}"


def summarize(results: list[dict]) -> dict:
    logs = [math.log(max(r.get("speedup", 0) if r["correct"] else FLOOR, FLOOR)) for r in results]
    return {"reward": round(math.exp(sum(logs) / len(logs)), 4) if logs else 0.0, "cases": len(results),
            "correct": sum(r["correct"] for r in results), "total_s": round(sum(r["seconds"] for r in results), 2),
            "failed": [r["case"] for r in results if not r["correct"]]}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("out_root")
    ap.add_argument("subs", nargs="*", help="LABEL=WORK_DIR")
    ap.add_argument("--cases", default="")
    ap.add_argument("--calibrate", action="store_true")
    a = ap.parse_args()
    names = a.cases.split(",") if a.cases else list(cases.HIDDEN)
    out_root = Path(a.out_root)
    subs = {s.split("=", 1)[0]: Path(s.split("=", 1)[1]) for s in a.subs}
    if a.calibrate:
        subs = {"naive": HERE.parent / "ref" / "naive"} | subs
    baseline = json.loads(BASELINE.read_text()) if BASELINE.exists() else {}
    status = {}
    for label, work in subs.items():
        (out_root / label).mkdir(parents=True, exist_ok=True)
        status[label] = build(work, out_root / label)
        print(label, "build", status[label], flush=True)
    results: dict[str, list[dict]] = {label: [] for label in subs}
    SHM.mkdir(parents=True, exist_ok=True)
    for name in names:
        fixture = SHM / f"{name}-{os.getpid()}"
        cases.remove(bytes(fixture))
        t0 = time.time()
        cases.build(name, bytes(fixture), cases.HIDDEN_SEED, 1.0)
        gen_s = time.time() - t0
        try:
            if a.calibrate or not (EXPECTED / f"{name}.json.gz").exists():
                dump_expected(name, reffind.naive(bytes(fixture)))
            expected = load_expected(name)
            for label in subs:
                if not status[label]["ok"]:
                    r = {"case": name, "correct": False, "why": status[label]["why"], "seconds": 0.0}
                else:
                    base = baseline.get(name, {}).get("seconds", 60.0)
                    r = run_case(name, out_root / label / "code", fixture, out_root / label / "cases" / name, base, expected)
                if a.calibrate and label == "naive":
                    if not r["correct"]:
                        raise SystemExit(f"naive reference failed {name}: {r}")
                    baseline[name] = {"seconds": r["seconds"], "groups": len(expected),
                                      "paths": sum(len(g) for g in expected)}
                    BASELINE.write_text(json.dumps(baseline, indent=1))
                    r["naive_s"], r["speedup"] = r["seconds"], 1.0
                results[label].append(r)
                print(f"{name:16s} gen {gen_s:5.1f}s {label:12s} " + json.dumps({k: r.get(k) for k in ("correct", "seconds", "speedup", "why")}), flush=True)
        finally:
            cases.remove(bytes(fixture))
    for label in subs:
        g = {"summary": summarize(results[label]), "build": status[label], "cases": results[label]}
        (out_root / label / "grade.json").write_text(json.dumps(g, indent=1))
        print(label, json.dumps(g["summary"]), flush=True)


if __name__ == "__main__":
    main()
