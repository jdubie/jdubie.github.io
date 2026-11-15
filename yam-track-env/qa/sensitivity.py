"""Does each test measure what its name says? Run the true-parameter controller with ONE dynamics term taken from the
nominal model instead, on every test, and see which tests get worse.

  python qa/sensitivity.py --robots 100 101 102 103 104 --which eval   -> qa/out/sensitivity_eval.json

The controller is the oracle's frame-aware one (qa/controllers/oracle.py, smart mode), in process: per frame it
fits the drive's setpoint ramp to the reference and feeds forward
    tau = [g(q) + M(q) qdd + C(q, qd) - friction(q, qd)] / kt
with each term computed on the true or the nominal model. Inverse dynamics is exactly additive in those terms.
"""

from __future__ import annotations

import argparse
import json
import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import mujoco
import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "sim"))
import core  # noqa: E402
import evalset  # noqa: E402

TERMS = ["gravity", "inertia", "coriolis", "friction", "torque_const"]
CONDITIONS = ["oracle", *[f"nominal_{t}" for t in TERMS], "nominal_all", "pd_only"]
S = 10


def hermite5(q0, v0, a0, q1, v1, a1, T, s):
    s = s[:, None]
    h = q1 - q0
    c3 = (20 * h - (8 * v1 + 12 * v0) * T - (3 * a0 - a1) * T**2) / (2 * T**3)
    c4 = (-30 * h + (14 * v1 + 16 * v0) * T + (3 * a0 - 2 * a1) * T**2) / (2 * T**4)
    c5 = (12 * h - 6 * (v1 + v0) * T + (a1 - a0) * T**2) / (2 * T**5)
    q = q0 + v0 * s + a0 / 2 * s**2 + c3 * s**3 + c4 * s**4 + c5 * s**5
    v = v0 + a0 * s + 3 * c3 * s**2 + 4 * c4 * s**3 + 5 * c5 * s**4
    a = a0 + 6 * c3 * s + 12 * c4 * s**2 + 20 * c5 * s**3
    return q, v, a


class Terms:
    def __init__(self, m: mujoco.MjModel):
        self.m, self.d = m, mujoco.MjData(m)

    def _id(self, q, v, a):
        self.d.qpos[:6], self.d.qvel[:6], self.d.qacc[:6] = q, v, a
        mujoco.mj_inverse(self.m, self.d)
        return self.d.qfrc_inverse[:6].copy()

    def split(self, q, v, a):
        z = np.zeros(6)
        g = self._id(q, z, z)
        return {"gravity": g, "inertia": self._id(q, z, a) - g, "coriolis": self._id(q, v, z) - g}


def run_one(args) -> tuple:
    seed, which, name, cond = args
    return seed, which, name, cond, simulate(seed, evalset_cache(which)[name], name, cond).result()["rms_mm"]


def simulate(seed: int, traj: np.ndarray, name: str, cond: str) -> core.Trial:
    """One trial of traj on robot `seed` under a CONDITIONS controller; the finished Trial (true_q, result())."""
    p = core.make_params(seed)
    mt = core.build_model(p)
    true_terms, nom_terms = Terms(mt), Terms(core.build_model(None))
    nominal = {t for t in TERMS if cond in (f"nominal_{t}", "nominal_all")}
    kt = np.ones(6) if "torque_const" in nominal else np.asarray(p.torque_const)
    arm, trial = core.Arm(p, noise_seed=1), core.Trial(name, traj, mt)
    ref = core.reference_for_controller(traj)
    rq, rv, ra = (np.asarray(ref[k]) for k in ("q", "qd", "qdd"))
    arm.reset(traj[0])
    T = core.FRAME_DT
    s = (np.arange(S) + 0.5) * T / S
    A = np.stack([np.ones(S), s], 1)
    KP, KD = core.KP_MAX, core.KD_MAX
    for k in range(trial.n_frames):
        if cond == "pd_only":
            cmd = core.Command(rq[k], rv[k], KP, KD, np.zeros(6))
        else:
            q, v, a = hermite5(rq[k], rv[k], ra[k], rq[k + 1], rv[k + 1], ra[k + 1], T, s)
            q0, v0 = np.linalg.lstsq(A, q, rcond=None)[0]
            need = np.zeros((S, 6))
            for i in range(S):
                tt, tn = true_terms.split(q[i], v[i], a[i]), nom_terms.split(q[i], v[i], a[i]) if nominal else None
                need[i] = sum((tn if t in nominal else tt)[t] for t in ("gravity", "inertia", "coriolis"))
            if "friction" not in nominal:
                need -= core.friction(p, q, v)[0]
            need /= kt
            pd = KP * (q0 + v0 * s[:, None] - q) + KD * (v0 - v)
            cmd = core.Command(q0, v0, KP, KD, np.clip((need - pd).mean(0), -core.TAU_MAX, core.TAU_MAX))
        arm.frame(cmd, trial.log_slot())
        trial.advanced()
    return trial


_CACHE: dict = {}


def evalset_cache(which: str) -> dict:
    if which not in _CACHE:
        _CACHE[which] = evalset.build_set(core.build_model(None), which)[0]
    return _CACHE[which]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--robots", type=int, nargs="+", default=[100, 101, 102, 103, 104])
    ap.add_argument("--which", default="eval")
    ap.add_argument("--workers", type=int, default=16)
    a = ap.parse_args()
    names = list(evalset_cache(a.which))
    jobs = [(r, a.which, n, c) for r in a.robots for n in names for c in CONDITIONS]
    out: dict = {}
    with ProcessPoolExecutor(a.workers) as ex:
        for seed, _, name, cond, mm in ex.map(run_one, jobs):
            out.setdefault(name, {}).setdefault(cond, {})[seed] = mm
    (ROOT / "qa" / "out").mkdir(exist_ok=True)
    (ROOT / "qa" / "out" / f"sensitivity_{a.which}.json").write_text(json.dumps(out, indent=1))
    print(f"{'test':20s}" + "".join(f"{c[:13]:>14s}" for c in CONDITIONS))
    for name in names:
        print(f"{name:20s}" + "".join(f"{np.mean(list(out[name][c].values())):14.3f}" for c in CONDITIONS))


if __name__ == "__main__":
    main()
