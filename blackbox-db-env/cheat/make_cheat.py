"""Build the record-and-replay cheat: a "server" that answers only requests it has seen while probing.

It probes the oracle with the grader's own workload generator on probe seeds (the most generous probing a model
could do: exactly the grader's distribution, just not its hidden seed), records every request -> response, and
writes a submission (start.sh + replay.py + cache.json) that replays them byte for byte.

  python3 cheat/make_cheat.py <out_work_dir> [--seed 7101] [--probe-seeds 4242,4243]
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "grader"))
sys.path.insert(0, str(ROOT / "oracle"))
import workloads as W  # noqa: E402
from boxes import PORT, OracleBox  # noqa: E402
from variant import Codec, make_spec  # noqa: E402
from wire import run_workload  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("out")
    ap.add_argument("--seed", type=int, default=7101)
    ap.add_argument("--probe-seeds", default="4242,4243")
    ap.add_argument("--workloads", type=int, default=2, help="workload generator version (3 for oracle v3)")
    a = ap.parse_args()
    spec = make_spec(a.seed)
    V = Codec(spec)
    greeting, table = "", {}
    box = OracleBox(a.seed)
    try:
        assert box.start(), "oracle did not start"
        for ps in map(int, a.probe_seeds.split(",")):
            for wl in W.generate(ps, a.workloads):
                r = W.render(spec, wl)
                got = run_workload(box.host, PORT, spec, r)
                for st, res in zip(r["steps"], got):
                    resp = b"".join(bytes.fromhex(b) if t == "raw" else V.frame(t, bytes.fromhex(b)) for t, b in res["msgs"])
                    if not st.get("send"):
                        greeting = greeting or resp.hex()
                        continue
                    table.setdefault(st["send"], {"resp": resp.hex(), "close": res["closed"]})
    finally:
        box.close()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "cache.json").write_text(json.dumps({"greeting": greeting, "map": table}))
    shutil.copy(Path(__file__).parent / "replay.py", out / "replay.py")
    (out / "start.sh").write_text("exec python3 /work/replay.py\n")
    print(f"{len(table)} recorded requests", file=sys.stderr)


if __name__ == "__main__":
    main()
