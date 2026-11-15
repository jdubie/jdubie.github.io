"""QA only, not a valid submission: reads the hidden params (YAMTRACK_PARAMS) and the sim code.

MODE=naive: true inverse dynamics + friction at each frame sample.
MODE=smart: also interpolates the reference inside each frame (quintic Hermite), fits the drive's linear
setpoint ramp to it, and sets tau to the frame-average of what perfect tracking needs from the drive.
Set YAMTRACK_PARAMS=nominal to use the nominal model with no friction, which is what an agent has
before any system identification.
"""
import os
import sys

import numpy as np

sys.path.insert(0, os.environ["YAMTRACK_SIM"])
import core  # noqa: E402

PATH = os.environ["YAMTRACK_PARAMS"]
P = None if PATH == "nominal" else core.load_params(core.Path(PATH))
M = core.build_model(P)
KP, KD = core.KP_MAX, core.KD_MAX
KT = np.ones(6) if P is None else np.asarray(P.torque_const)
MODE = os.environ.get("MODE", "smart")
S = 10  # samples per frame for the smart mode


def hermite5(q0, v0, a0, q1, v1, a1, T, s):
    """Quintic through (q, v, a) at both ends; s in [0, T]. Returns q, v, a at s, each (len(s), 6)."""
    s = s[:, None]
    h = q1 - q0
    c3 = (20 * h - (8 * v1 + 12 * v0) * T - (3 * a0 - a1) * T**2) / (2 * T**3)
    c4 = (-30 * h + (14 * v1 + 16 * v0) * T + (3 * a0 - 2 * a1) * T**2) / (2 * T**4)
    c5 = (12 * h - 6 * (v1 + v0) * T + (a1 - a0) * T**2) / (2 * T**5)
    q = q0 + v0 * s + a0 / 2 * s**2 + c3 * s**3 + c4 * s**4 + c5 * s**5
    v = v0 + a0 * s + 3 * c3 * s**2 + 4 * c4 * s**3 + 5 * c5 * s**4
    a = a0 + 6 * c3 * s + 12 * c4 * s**2 + 20 * c5 * s**3
    return q, v, a


def control(robot, ref):
    T = core.FRAME_DT
    N = len(ref["t"]) - 1
    if MODE == "naive":
        ff = core.inverse_dynamics(M, P, ref["q"][:N], ref["qd"][:N], ref["qdd"][:N]) / KT
        for k in range(N):
            robot.step(ref["q"][k], ref["qd"][k], KP, KD, ff[k])
        return
    s = (np.arange(S) + 0.5) * T / S
    A = np.stack([np.ones(S), s], 1)
    for k in range(N):
        q, v, a = hermite5(ref["q"][k], ref["qd"][k], ref["qdd"][k], ref["q"][k + 1], ref["qd"][k + 1], ref["qdd"][k + 1], T, s)
        coef = np.linalg.lstsq(A, q, rcond=None)[0]  # setpoint q0 + v0*s fitted to the curve
        q0, v0 = coef[0], coef[1]
        need = core.inverse_dynamics(M, P, q, v, a) / KT
        # if the arm tracks perfectly, the drive outputs kp*(q0+v0 s-q)+kd*(v0-v)+tau; make that average to need
        pd = KP * (q0 + v0 * s[:, None] - q) + KD * (v0 - v)
        robot.step(q0, v0, KP, KD, (need - pd).mean(0))
