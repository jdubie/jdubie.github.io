"""Practice server: the hidden arm behind a Unix socket, newline-delimited JSON, lockstep at 30 Hz.

Every connection gets its own copy of the same arm. The arm only moves when the client steps it.
  client -> server
    {"type":"reset","q":[6]}                               place the arm at rest at q
    {"type":"step","q","qd","kp","kd","tau","n":1}         hold this drive command for n frames (n <= 300)
    {"type":"trial","name":...}                            start a scored run of one served reference
    {"type":"status"}                                      every trial finished on this arm so far
  server -> client
    {"type":"hello",...} on connect, then one reply per request:
    {"type":"obs","t","q","qd","tau"} | {"type":"obs_batch","frames":[obs...]} | {"type":"trial_start",...}
    | {"type":"error","msg"}; the last obs of a trial is followed by {"type":"trial_result",...}
"""

from __future__ import annotations

import argparse
import json
import os
import socketserver
import threading
import time
from pathlib import Path

import numpy as np

from core import (FRAME_HZ, KD_MAX, KP_MAX, TAU_MAX, Arm, Command, Trial, build_model, eef_points, load_params,
                  reference_for_controller)

MAX_N = 300
MAX_LINE = 1 << 20


class State:
    def __init__(self, private: Path):
        self.p = load_params(private / "params.json")
        self.m = build_model(self.p)
        z = np.load(private / "served.npz")
        self.served = {k: z[k] for k in z.files}
        self.refs = {k: reference_for_controller(v) for k, v in self.served.items()}
        self.points = {k: eef_points(self.m, v[1:]) for k, v in self.served.items()}
        self.results: list[dict] = []
        self.log = open(private / "server_log.jsonl", "a", buffering=1)
        self.lock = threading.Lock()
        self.conn_id = 0
        self.frames = 0
        self.t0 = time.time()

    def record(self, **kw) -> None:
        with self.lock:
            self.log.write(json.dumps({"wall_s": round(time.time() - self.t0, 2), "frames_total": self.frames, **kw}) + "\n")


class Handler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        st: State = self.server.state  # type: ignore[attr-defined]
        with st.lock:
            st.conn_id += 1
            cid = st.conn_id
        arm = Arm(st.p, noise_seed=cid)
        arm.reset(np.array(next(iter(st.served.values()))[0]))
        lo, hi = arm.lo, arm.hi
        trial: Trial | None = None
        self.send({"type": "hello", "frame_hz": FRAME_HZ, "kp_max": KP_MAX.tolist(), "kd_max": KD_MAX.tolist(),
                   "tau_max": TAU_MAX.tolist(), "q_min": lo.round(5).tolist(), "q_max": hi.round(5).tolist(),
                   "references": sorted(st.refs)})
        while True:
            line = self.rfile.readline(MAX_LINE)
            if not line:
                return
            try:
                msg = json.loads(line)
                kind = msg.get("type")
                if kind == "reset":
                    q = np.asarray(msg.get("q"), float)
                    if q.shape != (6,) or not np.all(np.isfinite(q)):
                        raise ValueError("reset needs q: 6 finite joint angles")
                    trial = None
                    self.send({"type": "obs", **arm.reset(q)})
                elif kind == "trial":
                    name = msg.get("name")
                    if name not in st.served:
                        raise ValueError(f"unknown trial {name!r}; have {sorted(st.served)}")
                    trial = Trial(name, st.served[name], st.m, st.points[name])
                    obs = arm.reset(st.served[name][0])
                    st.record(conn=cid, event="trial_start", name=name)
                    self.send({"type": "trial_start", "name": name, "n_frames": trial.n_frames, "ref": st.refs[name], "obs": obs})
                elif kind == "step":
                    cmd = Command.parse(msg)
                    n = int(msg.get("n", 1))
                    if not 1 <= n <= MAX_N:
                        raise ValueError(f"n must be 1..{MAX_N}")
                    if trial is not None:
                        if n != 1:
                            raise ValueError("during a trial, step one frame at a time (n=1)")
                        obs = arm.frame(cmd, trial.log_slot())
                        with st.lock:
                            st.frames += 1
                        done = trial.advanced()
                        self.send({"type": "obs", **obs, "frame": trial.k})
                        if done:
                            res = trial.result()
                            st.record(conn=cid, event="trial_result", **res)
                            with st.lock:
                                st.results.append({"wall_s": round(time.time() - st.t0, 1), "name": res["name"], "rms_mm": res["rms_mm"]})
                            self.send({"type": "trial_result", **res})
                            trial = None
                    else:
                        frames = [arm.frame(cmd) for _ in range(n)]
                        with st.lock:
                            st.frames += n
                        self.send({"type": "obs", **frames[0]} if n == 1 else {"type": "obs_batch", "frames": frames})
                elif kind == "status":
                    with st.lock:
                        done = list(st.results)
                    best: dict = {}
                    for r in done:
                        best[r["name"]] = min(best.get(r["name"], 1e9), r["rms_mm"])
                    self.send({"type": "status", "elapsed_s": round(time.time() - st.t0, 1), "best_mm": best, "trials": done})
                else:
                    raise ValueError("type must be reset, step, trial or status")
            except (ValueError, TypeError, KeyError, json.JSONDecodeError) as e:
                self.send({"type": "error", "msg": str(e)})

    def send(self, obj: dict) -> None:
        self.wfile.write((json.dumps(obj, separators=(",", ":")) + "\n").encode())


class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sock", required=True)
    ap.add_argument("--private", type=Path, required=True)
    a = ap.parse_args()
    if os.path.exists(a.sock):
        os.unlink(a.sock)
    srv = Server(a.sock, Handler)
    srv.state = State(a.private)  # type: ignore[attr-defined]
    os.chmod(a.sock, 0o777)
    srv.serve_forever()


if __name__ == "__main__":
    main()
