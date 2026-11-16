"""Grade every run of a tag, final /work and every 2-minute snapshot, at most JOBS grades at once.

  python3 grade_runs.py <tag> [--jobs 4]

Each grade is its own transient systemd unit (pgw-g-<tag>-<model>-<what>). Snapshots whose files are identical
to an earlier-graded tree of the same run are not regraded; they reuse that result. Writes, per run,
grade/ (the final), snapgrades/<tNNNmin>/ and timeline.json, and folds the final summary into meta.json.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SKIP = {"__pycache__", "docs", "data", ".git", "node_modules", "target"}


def digest(work: Path) -> str:
    h = hashlib.sha256()
    for p in sorted(work.rglob("*")):
        if any(part in SKIP for part in p.relative_to(work).parts) or not p.is_file() or p.is_symlink():
            continue
        try:
            h.update(str(p.relative_to(work)).encode() + b"\0" + p.read_bytes() + b"\0")
        except OSError:
            pass
    return h.hexdigest()


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def grade(unit: str, work: Path, out: Path) -> dict:
    if (out / "grade.json").exists():
        return json.loads((out / "grade.json").read_text())["summary"]
    out.mkdir(parents=True, exist_ok=True)
    subprocess.run(["systemctl", "--user", "reset-failed", unit], capture_output=True)
    cmd = ["systemd-run", "--user", "--wait", "--collect", f"--unit={unit}", "-p", "MemoryMax=40G", "-p", "MemorySwapMax=0",
           "-E", f"HOME={os.environ['HOME']}", "-E", f"PATH={os.environ['PATH']}", f"--working-directory={ROOT}",
           "sh", "-c", f"exec {sys.executable} grader/grade.py '{work}' '{out}' --jobs 4 > '{out}/grade.log' 2>&1"]
    t0 = time.time()
    subprocess.run(cmd, capture_output=True)
    g = json.loads((out / "grade.json").read_text())["summary"] if (out / "grade.json").exists() else {"reward": 0.0, "why": "no grade.json"}
    log(unit, g.get("reward"), f"{time.time() - t0:.0f} s")
    return g


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("tag")
    ap.add_argument("--jobs", type=int, default=4)
    a = ap.parse_args()
    runs = sorted(p.parent for p in (ROOT / "runs" / a.tag).glob("*/meta.json"))
    items = []  # (run, label, t_min, work, out)
    for run in runs:
        m = json.loads((run / "meta.json").read_text())
        items.append((run, "final", m["seconds"] / 60, run / "work", run / "grade"))
        for s in sorted((run / "snapshots").glob("t*min")):
            items.append((run, s.name, int(re.search(r"\d+", s.name).group()), s, run / "snapgrades" / s.name))
    reps, dup = {}, {}
    for it in items:
        key = (it[0], digest(it[3]))
        if key in reps:
            dup[id(it)] = reps[key]
        else:
            reps[key] = it
    todo = list(reps.values())
    log(f"{len(items)} trees, {len(todo)} distinct to grade, {args_jobs(a)} at once")

    def do(it):
        run, label, _, work, out = it
        model = run.name.split("__")[0]
        return id(it), grade(f"pgw-g-{a.tag}-{model}-{label}", work, out)

    with ThreadPoolExecutor(a.jobs) as ex:
        res = dict(ex.map(do, todo))
    for run in runs:
        tl = []
        for it in items:
            if it[0] != run:
                continue
            src = dup.get(id(it), it)
            tl.append({"label": it[1], "t_min": round(it[2], 2), "same_as": src[1] if src is not it else None, **res[id(src)]})
        tl.sort(key=lambda x: x["t_min"])
        (run / "timeline.json").write_text(json.dumps(tl, indent=1))
        final = next(x for x in tl if x["label"] == "final")
        m = json.loads((run / "meta.json").read_text())
        m |= {k: v for k, v in final.items() if k not in ("label", "t_min", "same_as")}
        (run / "meta.json").write_text(json.dumps(m, indent=1))
        if final["same_as"] and not (run / "grade/grade.json").exists():  # keep the usual grade/ layout for the page
            shutil.copytree(run / "snapgrades" / final["same_as"], run / "grade", dirs_exist_ok=True)
        log(run.name, "final", final.get("reward"))


def args_jobs(a) -> int:
    return a.jobs


if __name__ == "__main__":
    main()
