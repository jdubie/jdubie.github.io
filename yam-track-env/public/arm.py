"""Minimal client for the arm socket. One Arm = one connection = one private copy of the arm.

    from arm import Arm
    a = Arm()                                # a.hello: limits and reference names
    obs = a.reset([0, 1.57, 2.86, 0.12, 0, 0])
    obs = a.step(q, qd=None, kp=None, kd=None, tau=None)          # one frame
    frames = a.step(q, kp=..., n=30)                              # 30 frames, list of observations
    start = a.trial("gravity_holds")         # start["ref"], start["n_frames"], start["obs"]
    obs = a.step(...)                        # n_frames times; obs["frame"] counts up
    a.result                                 # set after the last frame: {"rms_mm": ...}
    a.status()                               # every trial finished so far on this arm
"""

import json
import socket


class Arm:
    def __init__(self, path: str = "/sim/sim.sock"):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect(path)
        self.f = self.sock.makefile("rw", buffering=1)
        self.hello = self._recv()
        self.result = None
        self._in_trial = False

    def _recv(self) -> dict:
        msg = json.loads(self.f.readline())
        if msg.get("type") == "error":
            raise RuntimeError(msg["msg"])
        return msg

    def _send(self, msg: dict) -> dict:
        self.f.write(json.dumps(msg) + "\n")
        return self._recv()

    def reset(self, q) -> dict:
        self._in_trial = False
        return self._send({"type": "reset", "q": [float(x) for x in q]})

    def trial(self, name: str) -> dict:
        self.result, self._in_trial = None, True
        start = self._send({"type": "trial", "name": name})
        self._n_frames = start["n_frames"]
        return start

    def status(self) -> dict:
        return self._send({"type": "status"})

    def step(self, q, qd=None, kp=None, kd=None, tau=None, n: int = 1):
        msg = {"type": "step", "q": [float(x) for x in q], "n": n}
        for k, v in (("qd", qd), ("kp", kp), ("kd", kd), ("tau", tau)):
            if v is not None:
                msg[k] = [float(x) for x in v]
        reply = self._send(msg)
        if self._in_trial and reply.get("frame", 0) >= self._n_frames:
            self.result, self._in_trial = self._recv(), False
        return reply["frames"] if reply["type"] == "obs_batch" else reply

    def close(self) -> None:
        self.sock.close()
