"""Grade every run of a sweep: all finals first, then all snapshots, at most --jobs grades at once.

  python3 grade_sweep.py runs/<tag> --seed S --wseed W [--jobs 4]
Writes <run>/grades/<name>/grade.json; skips ones already graded.
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
    ap.add_argument("sweep")
    ap.add_argument("--seed", type=int, required=True)
    ap.add_argument("--wseed", type=int, required=True)
    ap.add_argument("--jobs", type=int, default=4)
    a = ap.parse_args()
    runs = sorted(p for p in Path(a.sweep).iterdir() if (p / "meta.json").exists())
    finals = [(r, "final", r / "work") for r in runs]
    snaps = [(r, s.name, s) for r in runs for s in sorted((r / "snapshots").iterdir())]

    def grade(item):
        run, name, work = item
        out = run / "grades" / name
        if (out / "grade.json").exists():
            return
        out.mkdir(parents=True, exist_ok=True)
        with open(out / "grade.log", "w") as log:
            subprocess.run([sys.executable, str(ROOT / "grader/grade.py"), "--work", str(work), str(out),
                            "--seed", str(a.seed), "--wseed", str(a.wseed)], stdout=log, stderr=log, timeout=4 * 3600)
        print(run.name, name, "done", flush=True)

    for batch in (finals, snaps):
        with ThreadPoolExecutor(a.jobs) as ex:
            list(ex.map(grade, batch))


if __name__ == "__main__":
    main()
