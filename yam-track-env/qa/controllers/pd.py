"""Baseline: max-gain PD on the reference, no feedforward."""
import numpy as np

KP = np.array([120, 120, 120, 60, 15, 15.0])
KD = np.array([7.5, 7.5, 7.5, 2.25, 2.25, 2.25])


def control(robot, ref):
    for k in range(len(ref["t"]) - 1):
        robot.step(ref["q"][k], ref["qd"][k], KP, KD)
