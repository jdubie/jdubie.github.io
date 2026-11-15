"""Reference trajectories: targeted tests, one family of dynamics each, plus windows of real right-arm motion.

Every generator returns a 12 s joint trajectory at PHYS_HZ, shape (TRAJ_S*PHYS_HZ + 1, 6), starting at rest.

  targeted   what it stresses                         how
  gravity_holds        g(q)                           slow moves between far-apart poses, long holds
  gravity_reach        g(q)                           long holds with the arm stretched out, where g(q) is largest
  friction_reversals   Coulomb, Stribeck, asymmetry   slow small sines: every joint keeps reversing direction
  friction_dither      stiction                       2-6 mrad sines, the arm barely leaves stick
  friction_speeds      viscous, cogging, asymmetry    constant-speed plateaus 0.08..1.6 rad/s, both directions
  inertia_proximal     M(q) incl. rotor inertia       quick short moves of joints 1-3, low speed
  inertia_wrist        M(q) incl. rotor inertia       quick short moves of joints 4-6
  coriolis_spin        C(q, qd)                       fast base spin with the arm stretched out, elbow pumping
  recorded_*           everything at once             12 s windows of the right arm in prod sv-pack rollouts

The eval set is fixed (EVAL_SEEDS, recorded windows from fr2-002); practice draws other seeds and windows
from fr2-001, so the two never share a trajectory.
"""

from __future__ import annotations

from pathlib import Path

import mujoco
import numpy as np
from scipy.interpolate import make_interp_spline
from scipy.signal import butter, filtfilt

from core import DT, PHYS_HZ, TAU_MAX, TRAJ_S, derivatives, inverse_dynamics

N = int(round(TRAJ_S * PHYS_HZ)) + 1
T_ALL = np.arange(N) * DT
BAGS = Path(__file__).resolve().parent / "data" / "bags_right.npz"
BOX = np.array([[-1.2, 1.2], [0.25, 2.5], [0.3, 2.6], [-1.1, 1.1], [-1.1, 1.1], [-1.6, 1.6]])
TARGETED = ["gravity_holds", "gravity_reach", "friction_reversals", "friction_dither", "friction_speeds",
            "inertia_proximal", "inertia_wrist", "coriolis_spin"]
CATEGORY = {"gravity_holds": "gravity", "gravity_reach": "gravity", "friction_reversals": "friction",
            "friction_dither": "friction", "friction_speeds": "friction", "inertia_proximal": "inertia",
            "inertia_wrist": "inertia", "coriolis_spin": "coriolis"}
N_RECORDED = 6
# Arm upright: the nominal gravity torque is zero on every joint here, and on the perturbed arms within 0.3 Nm
# (against 5-15 Nm stretched out). Friction and inertia tests stay near it so a wrong g(q) barely shows in them.
NULL_POSE = np.array([0.0, 1.571, 2.86, 0.123, 0.0, 0.0])
EVAL_SEED, PRACTICE_SEED = 20261114, 777


def _quintic(q0, q1, n):
    s = np.linspace(0, 1, n)[:, None]
    return q0 + (q1 - q0) * (10 * s**3 - 15 * s**4 + 6 * s**5)


def _smoothstep(t, T):
    s = np.clip(t / T, 0, 1)
    return 10 * s**3 - 15 * s**4 + 6 * s**5


def _fit(pieces: list[np.ndarray]) -> np.ndarray:
    traj = np.vstack(pieces)
    if len(traj) < N:
        traj = np.vstack([traj, np.repeat(traj[-1:], N - len(traj), 0)])
    return traj[:N]


def _sines(q0, amp, f, ramp=1.0):
    """q0 + envelope * sum of one sine per joint, starting from rest: (N, 6)."""
    t = T_ALL[:, None]
    ph = 0.0
    return q0 + _smoothstep(T_ALL, ramp)[:, None] * amp * np.sin(2 * np.pi * f * t + ph)


def _holds(rng, sample, n_poses, move_s, hold_s):
    q = sample()
    pieces = [np.repeat(q[None], int(hold_s * PHYS_HZ), 0)]
    for _ in range(n_poses - 1):
        tgt = sample()
        pieces += [_quintic(q, tgt, int(move_s * PHYS_HZ))[1:], np.repeat(tgt[None], int(hold_s * PHYS_HZ), 0)]
        q = tgt
    return _fit(pieces)


def gravity_holds(rng):
    """Six poses anywhere in the workspace, 1.2 s moves, 0.9 s holds (the arm is still 45% of the time)."""
    q = rng.uniform(BOX[:, 0], BOX[:, 1])

    def nxt():
        nonlocal q
        q = np.clip(q + rng.uniform(-0.8, 0.8, 6), BOX[:, 0], BOX[:, 1])
        return q
    return _holds(rng, nxt, 6, 1.2, 0.9)


_NOMINAL: list = []


def _gravity(q: np.ndarray) -> np.ndarray:
    if not _NOMINAL:
        m = mujoco.MjModel.from_xml_path(str(Path(__file__).resolve().parent.parent / "public" / "yam_nominal.xml"))
        _NOMINAL.extend([m, mujoco.MjData(m)])
    m, d = _NOMINAL
    d.qpos[:6], d.qvel[:], d.qacc[:] = q, 0, 0
    mujoco.mj_inverse(m, d)
    return d.qfrc_inverse[:6].copy()


def gravity_reach(rng):
    """Four poses where the shoulder holds at least 9 Nm of gravity (arm stretched out), 1.2 s moves, 2 s holds."""
    def nxt():
        while True:
            q = rng.uniform(BOX[:, 0], BOX[:, 1])
            if abs(_gravity(q)[1]) > 9:
                return q
    return _holds(rng, nxt, 4, 1.2, 2.0)


def _near_null(rng, r=0.15):
    return NULL_POSE + rng.uniform(-r, r, 6) * [6, 1, 1, 1, 1, 1]  # joint 1 carries no gravity: anywhere


def friction_reversals(rng):
    q0 = _near_null(rng)
    return _sines(q0, rng.uniform(0.015, 0.05, 6) * rng.choice([-1, 1], 6), rng.uniform(0.15, 0.5, 6))


def friction_dither(rng):
    q0 = _near_null(rng)
    return _sines(q0, rng.uniform(0.002, 0.006, 6) * rng.choice([-1, 1], 6), rng.uniform(0.5, 2.5, 6))


def friction_speeds(rng):
    """Per joint: plateaus at shuffled speeds, alternating direction, ~0.3 rad each, smoothed over 0.15 s."""
    q0 = _near_null(rng, 0.05)
    v = np.zeros((N, 6))
    for j in range(6):
        speeds = rng.permutation([0.08, 0.2, 0.45, 0.9, 1.6])
        t, k = 0.5 * PHYS_HZ, 0
        while t < N:
            s = speeds[k % len(speeds)]
            n = int(rng.uniform(0.2, 0.3) / s * PHYS_HZ)
            v[int(t) : int(t) + n, j] = s * (-1) ** k
            t, k = t + n + int(0.2 * PHYS_HZ), k + 1
    w = int(0.15 * PHYS_HZ)
    kern = np.hanning(w) / np.hanning(w).sum()
    v = np.stack([np.convolve(v[:, j], kern, "same") for j in range(6)], 1)
    q = q0 + np.cumsum(v, 0) * DT
    return q - (q.max(0) + q.min(0)) / 2 + q0  # centre the excursion on q0


def _jerks(rng, joints, dq, T):
    q0 = _near_null(rng, 0.05)
    q, pieces = q0.copy(), [q0[None]]
    while sum(map(len, pieces)) < N:
        js = rng.choice(joints, rng.integers(1, 3), replace=False)
        tgt = q.copy()
        tgt[js] = q0[js] + rng.uniform(-dq, dq, len(js))
        pieces += [_quintic(q, tgt, int(rng.uniform(*T) * PHYS_HZ))[1:], np.repeat(tgt[None], int(rng.uniform(0.25, 0.5) * PHYS_HZ), 0)]
        q = tgt
    return _fit(pieces)


def inertia_proximal(rng):
    return _jerks(rng, [0, 1, 2], 0.3, (0.32, 0.45))


def inertia_wrist(rng):
    return _jerks(rng, [3, 4, 5], 0.4, (0.25, 0.35))


def coriolis_spin(rng):
    q0 = np.array([0, rng.uniform(1.0, 1.4), rng.uniform(1.2, 1.6), 0, 0, 0]) + rng.uniform(-0.15, 0.15, 6)
    amp = np.array([rng.uniform(0.7, 0.9), *rng.uniform(0.3, 0.4, 2), *rng.uniform(0.15, 0.3, 3)])
    f = np.array([rng.uniform(0.5, 0.6), *rng.uniform(0.35, 0.55, 5)])
    return _sines(q0, amp * rng.choice([-1, 1], 6), f, ramp=1.5)


GENERATORS = {name: globals()[name] for name in TARGETED}


# ---------------------------------------------------------------- recorded right-arm windows

def _recorded_candidates(rig: str) -> list[tuple[str, int, float, float]]:
    """(episode, start frame, mean speed, mean stage) for every clean 12 s window of one rig, stride 2 s."""
    z = np.load(BAGS)
    b, a = butter(4, 6 / 15)
    out = []
    for key in sorted(k[:-2] for k in z.files if k.startswith(rig) and k.endswith("_q")):
        q = z[key + "_q"]
        if len(q) < 400:
            continue
        bad = np.abs(np.diff(q, axis=0)).max(1) > 0.2  # one-frame glitches in the recording
        qf = filtfilt(b, a, q, axis=0)
        sp = np.abs(np.gradient(qf, 1 / 30, axis=0)).sum(1)
        stage = z[key + "_stage"]
        for s0 in range(0, len(q) - 361, 60):
            if bad[s0 : s0 + 361].any():
                continue
            out.append((key, s0, float(sp[s0 : s0 + 361].mean()), float(np.nanmean(stage[s0 : s0 + 361]))))
    return out


def recorded(key: str, s0: int) -> np.ndarray:
    """One window: low-pass at 6 Hz, quintic spline to PHYS_HZ, and a 0.5 s blend in from rest."""
    z = np.load(BAGS)
    b, a = butter(4, 6 / 15)
    qf = filtfilt(b, a, z[key + "_q"], axis=0)[s0 : s0 + 361]
    sp = make_interp_spline(np.arange(361) / 30, qf, k=5)
    q = sp(T_ALL)
    nb = int(0.5 * PHYS_HZ)
    v1, a1 = sp(0.5, 1), sp(0.5, 2)
    T = 0.5
    s = T_ALL[:nb, None]
    h = q[nb] - q[0]
    c3 = (20 * h - 8 * v1 * T + a1 * T**2) / (2 * T**3)
    c4 = (-30 * h + 14 * v1 * T - 2 * a1 * T**2) / (2 * T**4)
    c5 = (12 * h - 6 * v1 * T + a1 * T**2) / (2 * T**5)
    q[:nb] = q[0] + c3 * s**3 + c4 * s**4 + c5 * s**5
    return q


def pick_recorded(rig: str, n: int, seed: int) -> list[tuple[str, int]]:
    """n windows: half from the busiest third, the rest spread over SARM stages (bag pickup, filling, placing)."""
    rng = np.random.default_rng(seed)
    c = _recorded_candidates(rig)
    eps_used: set[str] = set()
    picked = []
    groups = [lambda st: st < 1.5, lambda st: 1.5 <= st < 16.5, lambda st: st >= 16.5]
    order = np.argsort([-x[2] for x in c])
    busy = [c[i] for i in order[: len(c) // 3]]
    pools = [busy, busy, busy] + [[x for x in c if g(x[3])] for g in groups]
    for i in range(n):
        pool = pools[i % len(pools)]
        pool = [x for x in pool if x[0] not in eps_used] or pool
        x = pool[rng.integers(len(pool))]
        eps_used.add(x[0])
        picked.append((x[0], x[1]))
    return picked


# ---------------------------------------------------------------- sets

def feasible(traj: np.ndarray, m: mujoco.MjModel, tau_frac: float = 0.6, floor: bool = True) -> bool:
    """Inside the joint ranges with margin, needing <= tau_frac of the torque limits on m, and (floor=True) above z=0.08.

    Recorded windows skip the floor test: the real rig is mounted above its table and reaches below its base."""
    lo, hi = m.jnt_range[:6, 0] + 0.05, m.jnt_range[:6, 1] - 0.05
    if np.any(traj < lo) or np.any(traj > hi):
        return False
    qd, qdd = derivatives(traj)
    idx = np.arange(0, len(traj), 15)
    if np.any(np.abs(inverse_dynamics(m, None, traj[idx], qd[idx], qdd[idx])) > tau_frac * TAU_MAX):
        return False
    if not floor:
        return True
    d = mujoco.MjData(m)
    tcp, l4 = m.site("tcp").id, m.body("link_4").id
    for q in traj[::150]:
        d.qpos[:6] = q
        mujoco.mj_kinematics(m, d)
        if d.site_xpos[tcp][2] < 0.08 or d.xpos[l4][2] < 0.08:
            return False
    return True


def build_set(m_nominal: mujoco.MjModel, which: str) -> tuple[dict[str, np.ndarray], dict[str, str]]:
    """The fixed eval set, or the practice set: every targeted kind once, plus N_RECORDED recorded windows.

    Returns the trajectories and, per name, where it came from (generator seed, or episode and start time)."""
    base, rig = (EVAL_SEED, "fr2-002") if which == "eval" else (PRACTICE_SEED, "fr2-001")
    out, src = {}, {}
    for i, name in enumerate(TARGETED):
        rng = np.random.default_rng(base * 100 + i)
        for _ in range(500):
            traj = GENERATORS[name](rng)
            if feasible(traj, m_nominal):
                out[name], src[name] = traj, f"{name} seed {base * 100 + i}"
                break
        else:
            raise RuntimeError(f"no feasible {name}")
    k = 0
    for key, s0 in pick_recorded(rig, 4 * N_RECORDED, base):
        traj = recorded(key, s0)
        if feasible(traj, m_nominal, floor=False):
            out[f"recorded_{k}"], src[f"recorded_{k}"] = traj, f"{key} from {s0 / 30:.0f} s"
            k += 1
        if k == N_RECORDED:
            break
    if k < N_RECORDED:
        raise RuntimeError(f"only {k} feasible recorded windows")
    return out, src
