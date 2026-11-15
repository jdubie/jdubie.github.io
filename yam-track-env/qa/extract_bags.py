"""Right-arm joint angles from the 2026-09-25 sv-pack-br-15ct prod rollouts -> sim/data/bags_right.npz.

  python qa/extract_bags.py /home/robot/bagcount/ds

lr_yam observation.state is 15 wide: left j1-6, left gripper, right j1-6, right gripper, one spare.
Saved per episode: q (n, 6) at 30 Hz, stage (n,) SARM stage estimate, rig, episode index.
"""
import glob
import sys
from pathlib import Path

import numpy as np
import pyarrow.parquet as pq

ROOT = Path(__file__).resolve().parent.parent
out: dict[str, np.ndarray] = {}
for ds in sorted(glob.glob(f"{sys.argv[1]}/*rollout-448")):
    rig = "fr2-001" if "fr2-001" in ds else "fr2-002"
    for f in sorted(glob.glob(f"{ds}/data/*/*.parquet")):
        t = pq.read_table(f, columns=["observation.state", "stage_estimate", "episode_index", "frame_index"])
        s = np.stack(t.column("observation.state").to_numpy(zero_copy_only=False))
        st = np.asarray(t.column("stage_estimate").to_pylist(), float).reshape(-1)
        ep = np.asarray(t.column("episode_index").to_pylist()).reshape(-1)
        fi = np.asarray(t.column("frame_index").to_pylist()).reshape(-1)
        for e in np.unique(ep):
            k = ep == e
            o = np.argsort(fi[k])
            key = f"{rig}_ep{int(e):03d}"
            out[key + "_q"] = s[k][o][:, 7:13].astype(np.float64)
            out[key + "_stage"] = st[k][o]
np.savez_compressed(ROOT / "sim" / "data" / "bags_right.npz", **out)
eps = [k[:-2] for k in out if k.endswith("_q")]
print(len(eps), "episodes", sum(len(out[e + "_q"]) for e in eps) / 30 / 60, "minutes")
