"""Run your /work/run.sh on the public cases and check its answers.

  python3 tools/check.py [name-filter] [-v] [--regen]

Fixtures are generated once under /tmp/dupfind-public (they persist until --regen or a restart).
The output format and correctness rule are exactly the grader's. Times are wall-clock for `sh run.sh ROOT`.
"""

from __future__ import annotations

import gzip
import json
import os
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import cases_public as cp  # noqa: E402

FIX = Path("/tmp/dupfind-public")
EXP = HERE.parent / "public/expected"


def parse(b: bytes):
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


def show(gs) -> str:
    return "\n".join("    " + " | ".join(repr(p)[2:-1][-70:] for p in sorted(g)[:4]) + (" ..." if len(g) > 4 else "")
                     for g in list(gs)[:5])


def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    verbose, regen = "-v" in sys.argv, "--regen" in sys.argv
    ok_all = True
    for name in cp.PUBLIC:
        if args and args[0] not in name:
            continue
        root = FIX / name
        if regen or not (FIX / f".{name}.done").exists():
            cp.remove(bytes(root))
            FIX.mkdir(exist_ok=True)
            cp.build(name, bytes(root), cp.PUBLIC_SEED, cp.PUBLIC_SCALE)
            (FIX / f".{name}.done").touch()
        with gzip.open(EXP / f"{name}.json.gz", "rt") as f:
            expected = {frozenset(bytes.fromhex(p) for p in g) for g in json.load(f)}
        t0 = time.perf_counter()
        try:
            r = subprocess.run(["sh", "/work/run.sh", str(root)], capture_output=True, timeout=120, cwd="/work")
            dt, out, err, code = time.perf_counter() - t0, r.stdout, r.stderr, r.returncode
        except subprocess.TimeoutExpired:
            dt, out, err, code = 120.0, b"", b"", "timeout"
        got = parse(out)
        ok = code == 0 and got == expected
        ok_all &= ok
        why = "" if ok else (f"exit {code}" if code != 0 else "malformed output" if got is None else
                             f"{len(expected - got)} groups missing, {len(got - expected)} wrong")
        print(f"{name:14s} {'PASS' if ok else 'FAIL'} {dt:8.3f}s  {len(expected)} groups  {why}")
        if not ok and verbose and got is not None:
            print("  missing:\n" + show(expected - got) + "\n  wrong:\n" + show(got - expected))
        if not ok and verbose and code != 0:
            print("  stderr:", err[-1500:].decode("utf-8", "replace"))
    sys.exit(0 if ok_all else 1)


if __name__ == "__main__":
    main()
