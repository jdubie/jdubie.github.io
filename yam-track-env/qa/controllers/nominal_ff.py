"""Max-gain PD plus inverse dynamics of the NOMINAL model at each frame sample."""
from pathlib import Path

import mujoco
import numpy as np

KP = np.array([120, 120, 120, 60, 15, 15.0])
KD = np.array([7.5, 7.5, 7.5, 2.25, 2.25, 2.25])
m = mujoco.MjModel.from_xml_path(str(Path(__file__).parent / "tools" / "yam_nominal.xml"))
d = mujoco.MjData(m)


def control(robot, ref):
    for k in range(len(ref["t"]) - 1):
        d.qpos[:], d.qvel[:], d.qacc[:] = ref["q"][k], ref["qd"][k], ref["qdd"][k]
        mujoco.mj_inverse(m, d)
        robot.step(ref["q"][k], ref["qd"][k], KP, KD, d.qfrc_inverse.copy())
