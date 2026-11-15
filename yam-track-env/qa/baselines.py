"""Baselines on several robots: python qa/baselines.py 101 102 ... -> qa/out/baselines.json"""
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import env  # noqa: E402

CTRL = ROOT / "qa" / "controllers"
out = {}
for seed in map(int, sys.argv[1:]):
    run = ROOT / "runs" / "qa" / f"robot{seed}"
    if not run.exists():
        env.reset(run, seed)
    os.environ["YAMTRACK_SIM"] = str(ROOT / "sim")
    row = {}
    for name, ctrl, params, mode in [("PD only", "pd", None, None), ("nominal model ff", "nominal_ff", None, None),
                                     ("nominal model, smart frames", "oracle", "nominal", "smart"),
                                     ("true params, naive", "oracle", "true", "naive"), ("true params, smart frames", "oracle", "true", "smart")]:
        if params:
            os.environ["YAMTRACK_PARAMS"] = "nominal" if params == "nominal" else str(run / "private" / "params.json")
            os.environ["MODE"] = mode
        r = env.grade(run, CTRL / f"{ctrl}.py", sandbox=False)
        row[name] = {"score_mrad": r["score_mrad"], "per_ref": [x["rms_mrad"] for x in r["runs"]],
                     "per_joint": [round(sum(x["per_joint_rms_mrad"][j] for x in r["runs"]) / len(r["runs"]), 2) for j in range(6)]}
        print(seed, name, r["score_mrad"], flush=True)
    out[seed] = row
(ROOT / "qa" / "out").mkdir(exist_ok=True)
(ROOT / "qa" / "out" / "baselines.json").write_text(json.dumps(out, indent=1))
