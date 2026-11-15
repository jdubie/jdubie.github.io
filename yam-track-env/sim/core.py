"""The hidden YAM arm, its drives, the reference trajectories and the tracking score.

Nothing in this file is given to the agent. The agent gets public/yam_nominal.xml (the unperturbed chain)
and talks to an Arm only through sim/server.py (practice) or env.py grade (scoring).

Timing: physics and the drives' PD loop run at 1500 Hz. A frame is 1/30 s = 50 physics steps. The agent
sees one observation per frame and sends one drive command per frame.
"""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass
from pathlib import Path

import mujoco
import numpy as np

NOMINAL = Path(__file__).resolve().parent.parent / "public" / "yam_nominal.xml"
PHYS_HZ = 1500
FRAME_HZ = 30
SUB = PHYS_HZ // FRAME_HZ  # 50 physics steps per frame
DT = 1.0 / PHYS_HZ
FRAME_DT = 1.0 / FRAME_HZ
TAU_MAX = np.array([28.0, 28.0, 28.0, 10.0, 10.0, 10.0])  # DM4340 x3, DM4310 x3 (teleop arms.yaml)
KP_MAX = np.array([120.0, 120.0, 120.0, 60.0, 15.0, 15.0])  # 1.5x the fleet's kp
KD_MAX = np.array([7.5, 7.5, 7.5, 2.25, 2.25, 2.25])  # 1.5x the fleet's kd
Q_NOISE, QD_NOISE, TAU_NOISE = 3e-4, 0.02, 0.03  # rad, rad/s, Nm; one sigma
TRAJ_S = 12.0
STRIBECK_EPS = 0.01  # rad/s, smoothing of sign(v) in the velocity-dependent friction terms
BODIES = ["link_1", "link_2", "link_3", "link_4", "link_5", "link_6", "gripper"]


@dataclass
class Params:
    """Everything that makes this arm differ from the nominal model. Per joint lists are length 6."""

    robot_seed: int
    mass_scale: list[float]  # per body in BODIES
    com_offset: list[list[float]]  # m, per body, added to the body-frame COM
    inertia_scale: list[list[float]]  # per body, per principal axis
    length_scale: list[float]  # per body in BODIES[1:], scales the body's offset from its parent
    armature: list[float]  # kg m^2, reflected rotor inertia
    coulomb: list[float]  # Nm
    stribeck_extra: list[float]  # Nm, extra friction near zero speed
    stribeck_v: list[float]  # rad/s
    viscous: list[float]  # Nm s/rad
    asym: list[float]  # Coulomb is coulomb*(1+asym) moving +, coulomb*(1-asym) moving -
    cog_amp: list[float]  # Nm
    cog_n: list[int]  # cycles per rad
    cog_phase: list[float]
    torque_const: list[float]  # delivered torque / commanded torque


def make_params(robot_seed: int) -> Params:
    rng = np.random.default_rng(robot_seed)
    wrist = np.array([1.0, 1.0, 1.0, 0.4, 0.25, 0.25])
    nb = len(BODIES)
    inertia = []
    for _ in range(nb):
        common = rng.uniform(0.7, 1.4)
        inertia.append((common * rng.uniform(0.88, 1.12, 3)).round(4).tolist())
    coulomb = rng.uniform(0.15, 0.6, 6) * wrist
    return Params(
        robot_seed=robot_seed,
        mass_scale=rng.uniform(0.75, 1.35, nb).round(4).tolist(),
        com_offset=np.clip(rng.normal(0, 0.008, (nb, 3)), -0.015, 0.015).round(5).tolist(),
        inertia_scale=inertia,
        length_scale=rng.uniform(0.94, 1.06, nb - 1).round(4).tolist(),
        armature=(0.02 * rng.uniform(0.6, 2.5, 6) * np.array([1, 1, 1, 0.6, 0.4, 0.4])).round(5).tolist(),
        coulomb=coulomb.round(4).tolist(),
        stribeck_extra=(coulomb * rng.uniform(0.2, 0.9, 6)).round(4).tolist(),
        stribeck_v=rng.uniform(0.02, 0.12, 6).round(4).tolist(),
        viscous=(rng.uniform(0.05, 0.4, 6) * wrist).round(4).tolist(),
        asym=rng.uniform(-0.25, 0.25, 6).round(4).tolist(),
        cog_amp=(rng.uniform(0.0, 0.12, 6) * wrist).round(4).tolist(),
        cog_n=rng.integers(15, 41, 6).tolist(),
        cog_phase=rng.uniform(0, 2 * math.pi, 6).round(4).tolist(),
        torque_const=rng.uniform(0.88, 1.12, 6).round(4).tolist(),
    )


def save_params(p: Params, path: Path) -> None:
    path.write_text(json.dumps(asdict(p), indent=1))


def load_params(path: Path) -> Params:
    return Params(**json.loads(path.read_text()))


def build_model(p: Params | None) -> mujoco.MjModel:
    """The nominal model, or the hidden real one when given its Params."""
    m = mujoco.MjModel.from_xml_path(str(NOMINAL))
    m.opt.timestep = DT
    if p is None:
        return m
    for i, name in enumerate(BODIES):
        b = m.body(name).id
        m.body_mass[b] *= p.mass_scale[i]
        m.body_ipos[b] += np.array(p.com_offset[i])
        I = m.body_inertia[b] * np.array(p.inertia_scale[i]) * p.mass_scale[i]
        for k in range(3):  # keep it a physical inertia: no axis exceeds the sum of the other two
            I[k] = min(I[k], I[(k + 1) % 3] + I[(k + 2) % 3])
        m.body_inertia[b] = I
        if i > 0:
            s = p.length_scale[i - 1]
            m.body_pos[b] *= s
            m.body_ipos[b] *= s
    m.dof_armature[:6] = p.armature
    return m


def friction(p: Params, q: np.ndarray, v: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Joint friction torque and its derivative with respect to speed.

    Coulomb with a Stribeck hump near zero speed, smoothed over STRIBECK_EPS, different in each direction,
    plus viscous friction and position-periodic cogging.
    """
    c, s, vs = np.asarray(p.coulomb), np.asarray(p.stribeck_extra), np.asarray(p.stribeck_v)
    a, b = np.asarray(p.asym), np.asarray(p.viscous)
    sgn = np.tanh(v / STRIBECK_EPS)
    dsgn = (1.0 - sgn**2) / STRIBECK_EPS
    e = np.exp(-((v / vs) ** 2))
    mag = c * (1.0 + a * sgn) + s * e  # c*(1+a) moving +, c*(1-a) moving -
    dmag = c * a * dsgn - 2.0 * s * e * v / vs**2
    cog = np.asarray(p.cog_amp) * np.sin(np.asarray(p.cog_n) * q + np.asarray(p.cog_phase))
    tau = -mag * sgn - b * v + cog
    dtau = -(dmag * sgn + mag * dsgn) - b
    return tau, dtau


@dataclass
class Command:
    """One frame's drive command. The drive runs, at 1500 Hz:
    tau = clip(kp*(q + qd*s - q_meas) + kd*(qd - qd_meas) + tau_ff, +-TAU_MAX), s = time since the frame began."""

    q: np.ndarray
    qd: np.ndarray
    kp: np.ndarray
    kd: np.ndarray
    tau: np.ndarray

    @staticmethod
    def limp() -> "Command":
        z = np.zeros(6)
        return Command(z, z, z, z, z)

    @staticmethod
    def parse(msg: dict) -> "Command":
        """Validate a step message. Raises ValueError with a message meant for the agent."""
        out = {}
        for key in ("q", "qd", "kp", "kd", "tau"):
            v = msg.get(key)
            if v is None:
                if key == "q":
                    raise ValueError("step needs q (6 joint angles, rad)")
                v = [0.0] * 6
            a = np.asarray(v, dtype=float)
            if a.shape != (6,) or not np.all(np.isfinite(a)):
                raise ValueError(f"{key} must be 6 finite numbers")
            out[key] = a
        if np.any(out["kp"] < 0) or np.any(out["kd"] < 0):
            raise ValueError("kp and kd must be >= 0")
        out["kp"] = np.minimum(out["kp"], KP_MAX)
        out["kd"] = np.minimum(out["kd"], KD_MAX)
        out["tau"] = np.clip(out["tau"], -TAU_MAX, TAU_MAX)
        return Command(**out)


class Arm:
    """One physical copy of the hidden arm. Not thread-safe; give each client its own."""

    def __init__(self, p: Params, noise_seed: int):
        self.p = p
        self.m = build_model(p)
        self.d = mujoco.MjData(self.m)
        self.rng = np.random.default_rng(noise_seed)
        self.lo, self.hi = self.m.jnt_range[:6, 0].copy(), self.m.jnt_range[:6, 1].copy()
        self.kt = np.asarray(p.torque_const)
        self.tau_drive = np.zeros(6)
        self.frames = 0

    def reset(self, q: np.ndarray) -> dict:
        self.d.qpos[:] = np.clip(q, self.lo, self.hi)
        self.d.qvel[:] = 0.0
        self.d.qacc_warmstart[:] = 0.0
        self.d.time = 0.0
        self.tau_drive[:] = 0.0
        self.frames = 0
        mujoco.mj_forward(self.m, self.d)
        return self.obs()

    def frame(self, cmd: Command, log: np.ndarray | None = None) -> dict:
        """Advance 1/30 s under cmd. If log is given (shape (SUB, 6)), true joint angles go into it."""
        d = self.d
        for k in range(SUB):
            s = k * DT
            q, v = d.qpos[:6], d.qvel[:6]
            tau = cmd.kp * (cmd.q + cmd.qd * s - q) + cmd.kd * (cmd.qd - v) + cmd.tau
            self.tau_drive = np.clip(tau, -TAU_MAX, TAU_MAX)
            d.ctrl[:6] = self.kt * self.tau_drive
            # Friction, linearised about the current speed so its stiff part is integrated implicitly.
            f, df = friction(self.p, q, v)
            damp = np.maximum(-df, 0.0)
            self.m.dof_damping[:6] = damp
            d.qfrc_applied[:6] = f + damp * v
            mujoco.mj_step(self.m, d)  # joint ranges are hard limits in the model
            if log is not None:
                log[k] = d.qpos[:6]
        self.frames += 1
        return self.obs()

    def obs(self) -> dict:
        d, r = self.d, self.rng
        return {
            "t": round(self.frames * FRAME_DT, 6),
            "q": (d.qpos[:6] + r.normal(0, Q_NOISE, 6)).round(6).tolist(),
            "qd": (d.qvel[:6] + r.normal(0, QD_NOISE, 6)).round(5).tolist(),
            "tau": (self.tau_drive + r.normal(0, TAU_NOISE, 6)).round(4).tolist(),
        }


# ---------------------------------------------------------------- reference trajectories

Q_BOX = np.array([[-1.2, 1.2], [0.4, 2.2], [0.4, 2.2], [-1.0, 1.0], [-1.0, 1.0], [-1.5, 1.5]])


def _quintic(q0, v0, q1, v1, T, n):
    """Quintic with zero end accelerations, sampled at n points over [0, T)."""
    t = np.arange(n)[:, None] * DT
    a0, a1 = q0, v0
    h = q1 - q0 - v0 * T
    g = v1 - v0
    a3 = (10 * h - 4 * g * T) / T**3
    a4 = (-15 * h + 7 * g * T) / T**4
    a5 = (6 * h - 3 * g * T) / T**5
    return a0 + a1 * t + a3 * t**3 + a4 * t**4 + a5 * t**5


def make_trajectory(seed: int, m: mujoco.MjModel, p: Params) -> np.ndarray:
    """A 12 s joint trajectory at PHYS_HZ, shape (TRAJ_S*PHYS_HZ + 1, 6).

    Point-to-point moves, holds, slow creeps on a few joints and windowed multi-sine sweeps, joined
    smoothly. Rejected and redrawn until it stays in a safe box, above the floor, and within 70% of
    the real arm's torque limits.
    """
    rng = np.random.default_rng(seed)
    n_total = int(round(TRAJ_S * PHYS_HZ)) + 1
    for _ in range(200):
        q = rng.uniform(Q_BOX[:, 0], Q_BOX[:, 1])
        pieces = [q[None, :]]
        n = 1
        while n < n_total:
            kind = rng.choice(["move", "hold", "creep", "sine"], p=[0.4, 0.15, 0.2, 0.25])
            if kind == "move":
                T = rng.uniform(0.7, 2.0)
                tgt = rng.uniform(Q_BOX[:, 0], Q_BOX[:, 1])
                seg = _quintic(q, 0 * q, tgt, 0 * q, T, int(T * PHYS_HZ) + 1)[1:]
            elif kind == "hold":
                seg = np.repeat(q[None], int(rng.uniform(0.3, 0.8) * PHYS_HZ), 0)
            elif kind == "creep":
                v = np.zeros(6)
                js = rng.choice(6, rng.integers(1, 4), replace=False)
                v[js] = rng.uniform(0.02, 0.15, len(js)) * rng.choice([-1, 1], len(js))
                ta, tc = 0.3, rng.uniform(1.0, 2.0)
                acc = _quintic(q, 0 * q, q + v * ta / 2, v, ta, int(ta * PHYS_HZ) + 1)[1:]
                q1 = acc[-1]
                cru = q1 + v * (np.arange(1, int(tc * PHYS_HZ) + 1)[:, None] * DT)
                q2 = cru[-1]
                dec = _quintic(q2, v, q2 + v * ta / 2, 0 * v, ta, int(ta * PHYS_HZ) + 1)[1:]
                seg = np.vstack([acc, cru, dec])
            else:
                T = rng.uniform(1.5, 3.0)
                t = np.arange(1, int(T * PHYS_HZ) + 1)[:, None] * DT
                w = np.sin(np.pi * t / T) ** 4
                amp = rng.uniform(0.0, 0.25, 6) * np.array([1, 1, 1, 1.5, 1.5, 1.5])
                f = rng.uniform(0.4, 1.5, 6)
                ph = rng.uniform(0, 2 * np.pi, 6)
                seg = q + w * amp * (np.sin(2 * np.pi * f * t + ph) - np.sin(ph))
            pieces.append(seg)
            q = seg[-1].copy()
            n += len(seg)
        traj = np.vstack(pieces)[:n_total]
        if _feasible(traj, m, p):
            return traj
    raise RuntimeError(f"no feasible trajectory for seed {seed}")


def derivatives(traj: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    qd = np.gradient(traj, DT, axis=0, edge_order=2)
    qdd = np.gradient(qd, DT, axis=0, edge_order=2)
    return qd, qdd


def inverse_dynamics(m: mujoco.MjModel, p: Params | None, q, qd, qdd) -> np.ndarray:
    """Joint torque needed to follow (q, qd, qdd) on model m, plus friction if p is given. Shape (N, 6)."""
    d = mujoco.MjData(m)
    out = np.zeros((len(q), 6))
    for i in range(len(q)):
        d.qpos[:6], d.qvel[:6], d.qacc[:6] = q[i], qd[i], qdd[i]
        mujoco.mj_inverse(m, d)
        out[i] = d.qfrc_inverse[:6]
    if p is not None:
        out -= friction(p, q, qd)[0]
    return out


def _feasible(traj: np.ndarray, m: mujoco.MjModel, p: Params) -> bool:
    if np.any(traj < Q_BOX[:, 0] - 0.3) or np.any(traj > Q_BOX[:, 1] + 0.3):
        return False
    qd, qdd = derivatives(traj)
    if np.abs(qd).max() > 2.5 or np.abs(qdd).max() > 25:
        return False
    idx = np.arange(0, len(traj), 15)
    tau = inverse_dynamics(m, p, traj[idx], qd[idx], qdd[idx]) / np.asarray(p.torque_const)
    if np.any(np.abs(tau) > 0.7 * TAU_MAX):
        return False
    d = mujoco.MjData(m)
    tcp, l3 = m.site("tcp").id, m.body("link_4").id
    for q in traj[::150]:
        d.qpos[:6] = q
        mujoco.mj_kinematics(m, d)
        if d.site_xpos[tcp][2] < 0.06 or d.xpos[l3][2] < 0.06:
            return False
    return True


def reference_for_controller(traj: np.ndarray) -> dict:
    """What a controller is given: the reference at frame times 0, 1/30, ..., 12 s."""
    qd, qdd = derivatives(traj)
    idx = np.arange(0, len(traj), SUB)
    return {
        "t": (idx * DT).round(6).tolist(),
        "q": traj[idx].round(6).tolist(),
        "qd": qd[idx].round(6).tolist(),
        "qdd": qdd[idx].round(5).tolist(),
    }


# Three points fixed to the gripper (its body frame): the joint-6 axis at the wrist, the tool point, and a point
# 6 cm off the tool axis so that roll about joint 6 shows up as distance too.
EEF_POINTS = np.array([[0.0, 0.0, 0.0], [0.0, 0.0, 0.115], [0.0, 0.06, 0.08]])
POINT_NAMES = ["wrist", "tcp", "side"]


def eef_points(m: mujoco.MjModel, q: np.ndarray) -> np.ndarray:
    """World positions (m) of EEF_POINTS for each row of q (N, 6) on model m. Shape (N, 3, 3)."""
    d = mujoco.MjData(m)
    g = m.body("gripper").id
    out = np.zeros((len(q), len(EEF_POINTS), 3))
    for i, qi in enumerate(q):
        d.qpos[:6] = qi
        mujoco.mj_kinematics(m, d)
        out[i] = d.xpos[g] + EEF_POINTS @ d.xmat[g].reshape(3, 3).T
    return out


class Trial:
    """Scores one run of a reference: RMS distance (mm) of the three gripper points from where the reference puts
    them, on the real arm's own kinematics, at 1500 Hz over the whole trajectory."""

    def __init__(self, name: str, traj: np.ndarray, m: mujoco.MjModel, ref_points: np.ndarray | None = None):
        self.name = name
        self.traj = traj
        self.m = m
        self.ref_points = eef_points(m, traj[1:]) if ref_points is None else ref_points
        self.n_frames = (len(traj) - 1) // SUB
        self.true_q = np.zeros((self.n_frames * SUB, 6))
        self.k = 0

    def log_slot(self) -> np.ndarray:
        return self.true_q[self.k * SUB : (self.k + 1) * SUB]

    def advanced(self) -> bool:
        """Call after each frame. True when the trajectory is over."""
        self.k += 1
        return self.k >= self.n_frames

    def result(self) -> dict:
        assert self.k == self.n_frames, "score only a finished trial"
        dist = np.linalg.norm(eef_points(self.m, self.true_q) - self.ref_points, axis=2) * 1000  # (N, 3) mm
        return {
            "name": self.name,
            "rms_mm": round(float(np.sqrt((dist**2).mean())), 4),
            "per_point_rms_mm": dict(zip(POINT_NAMES, np.sqrt((dist**2).mean(0)).round(4).tolist())),
            "max_mm": round(float(dist.max()), 3),
            "frames": self.k,
        }
