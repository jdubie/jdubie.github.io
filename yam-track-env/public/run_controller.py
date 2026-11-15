"""Host a controller: python run_controller.py controller.py

Speaks JSON lines on stdin/stdout with whatever runs the arm (tools/practice.py, or the grader).
Anything the controller prints goes to stderr.
"""

import json
import os
import runpy
import sys

import numpy as np

proto_in = os.fdopen(os.dup(0), "r", buffering=1)
proto_out = os.fdopen(os.dup(1), "w", buffering=1)
os.dup2(2, 1)  # the controller's stdout must never reach the protocol stream
sys.stdin = open(os.devnull)


class EpisodeOver(BaseException):
    """Raised by Robot.step once the reference has ended. Let it propagate."""


def _obs(msg: dict) -> dict:
    return {"t": msg["t"], "q": np.array(msg["q"]), "qd": np.array(msg["qd"]), "tau": np.array(msg["tau"])}


class Robot:
    """Given to control(robot, ref).

    obs: latest observation {"t": s, "q": (6,) rad, "qd": (6,) rad/s, "tau": (6,) Nm}, numpy arrays.
    step(q, qd=0, kp=0, kd=0, tau=0): one 1/30 s frame of drive command; returns the next obs.
    """

    def __init__(self, first_obs: dict, n_frames: int) -> None:
        self.obs = _obs(first_obs)
        self.n_frames = n_frames
        self.frame = 0

    def step(self, q, qd=None, kp=None, kd=None, tau=None) -> dict:
        msg = {"type": "step", "q": [float(x) for x in q]}
        for k, v in (("qd", qd), ("kp", kp), ("kd", kd), ("tau", tau)):
            if v is not None:
                msg[k] = [float(x) for x in v]
        proto_out.write(json.dumps(msg) + "\n")
        line = proto_in.readline()
        if not line:
            raise EpisodeOver()
        reply = json.loads(line)
        if reply.get("type") == "error":
            raise RuntimeError("arm rejected the command: " + reply["msg"])
        if reply.get("type") != "obs":
            raise EpisodeOver()
        self.obs = _obs(reply)
        self.frame += 1
        return self.obs


start = json.loads(proto_in.readline())
ref = {k: np.array(v) for k, v in start["ref"].items()}
ns = runpy.run_path(sys.argv[1], run_name="controller")
robot = Robot(start["obs"], start["n_frames"])
try:
    ns["control"](robot, ref)
except EpisodeOver:
    pass
