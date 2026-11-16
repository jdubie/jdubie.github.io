"""Grade a run's final /work and every snapshot, at most --jobs at a time, for the reward-over-time curve.

  python3 grade_runs.py runs/<tag>/<model>__r0 [--jobs 2]
Writes <run>/grades/<name>/grade.json for name in t002min..t020min and final; skips ones already graded.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("run")
    ap.add_argument("--jobs", type=int, default=2)
    ap.add_argument("--seed", type=int, default=7101)
    a = ap.parse_args()
    run = Path(a.run)
    todo = [(p.name, p) for p in sorted((run / "snapshots").iterdir())] + [("final", run / "work")]

    def grade(item):
        name, work = item
        out = run / "grades" / name
        if (out / "grade.json").exists():
            return
        out.mkdir(parents=True, exist_ok=True)
        with open(out / "grade.log", "w") as log:
            subprocess.run([sys.executable, str(ROOT / "grader/grade.py"), "--work", str(work), str(out), "--seed", str(a.seed)],
                           stdout=log, stderr=log, timeout=4 * 3600)
        print(name, "done", flush=True)

    with ThreadPoolExecutor(a.jobs) as ex:
        list(ex.map(grade, todo))


if __name__ == "__main__":
    main()
