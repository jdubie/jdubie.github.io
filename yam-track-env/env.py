"""Task lifecycle for YAM trajectory tracking, two versions on the same hidden arm and the same eval set.

  v1  online: the agent gets the eval references themselves and drives the arm through them over the socket, as
      often as it likes. Score per test = its best completed attempt within the budget (read from the server log).
  v2  held out: the agent gets practice references of the same kinds and delivers controller.py, which is run on
      the hidden eval references at the end.

  python env.py reset RUNDIR --robot-seed N --version v1|v2 [--minutes M]
  python env.py serve RUNDIR      the arm on RUNDIR/sock/sim.sock (foreground, in a container)
  python env.py grade RUNDIR      v1: best attempts from the server log; v2: run work/controller.py on the eval set

Score: RMS distance in mm of three gripper points (wrist centre, tool point, a point 6 cm off the tool axis) from
where the reference puts them, on the real arm's kinematics at 1500 Hz; mean over the eval set's tests.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent
PUBLIC, SIM = ROOT / "public", ROOT / "sim"
IMAGE = "yamstack-sandbox:1"  # python 3.12 + numpy, scipy, mujoco; no network inside
sys.path.insert(0, str(SIM))
from core import (KD_MAX, KP_MAX, TAU_MAX, Arm, Command, Trial, build_model, load_params, make_params,  # noqa: E402
                  reference_for_controller, save_params)
from evalset import CATEGORY, build_set  # noqa: E402

TOOLS = {"v1": ("yam_nominal.xml", "arm.py"), "v2": ("yam_nominal.xml", "arm.py", "run_controller.py", "practice.py")}
TRIAL_WALL_S = 300.0
GRACE_S = 30  # v1: a trial finishing this long after the budget still counts (the agent is killed at budget + 20 s)

ARM = """\
There is a YAM 6-DOF arm (gripper fixed shut). It is the same arm as in `tools/yam_nominal.xml`, but that file is
only nominal: the real arm's link masses, centres of mass, inertias, link lengths, rotor inertias, joint friction
and motor torque constants all differ from it, by amounts nobody has told you. The file has no friction at all.
There is no gravity compensation anywhere: if you do not hold the arm up, it falls.

## The arm

You reach the real arm only through the Unix socket `/sim/sim.sock` (stream, one JSON object per line each way;
`tools/arm.py` is a small client for it). It runs in lockstep: it advances only when you step it, one frame of
1/30 s at a time, so you can take as long as you like to think between frames. Every connection gets its own copy
of the same arm, so you can run experiments, and trials, in parallel.

On connect you receive `{"type":"hello", ...}` with the joint limits, gain limits, torque limits and the names of
the references you can run. Then send:

- `{"type":"reset","q":[6]}` places the arm at rest at those joint angles (rad). Reply: an observation.
- `{"type":"step","q":[6],"qd":[6],"kp":[6],"kd":[6],"tau":[6],"n":1}` holds this drive command for `n` frames
  (1 to 300). Omitted `qd`, `kp`, `kd`, `tau` are zero. Reply: `{"type":"obs","t","q","qd","tau"}` for n = 1,
  or `{"type":"obs_batch","frames":[...]}` with one observation per frame.
- `{"type":"trial","name":...}` starts a scored run of a reference (below).
- `{"type":"status"}` lists every trial finished so far on this arm, from any connection.

An observation is taken at the end of each frame: `q` joint angles (rad), `qd` joint speeds (rad/s), `tau` the
torque each drive commanded (Nm). All three are noisy. A bad request gets `{"type":"error","msg":...}`.

**The drives.** Each joint has a motor drive that runs its own loop at 1500 Hz. During a frame it applies

    tau_motor = clip( kp*(q + qd*s - q_meas) + kd*(qd - qd_meas) + tau , -tau_max, +tau_max )

where `s` is the time since the frame began (0 to 1/30 s): the position setpoint moves at `qd` within the frame.
`kp` is capped at {kp_max}, `kd` at {kd_max} (higher values are clipped), and `tau_max` is {tau_max} Nm.
The torque a motor actually delivers is not exactly `tau_motor`. Joint ranges are hard stops.

## References and trials

A reference is 12 s of joint angles, given as a JSON dict of arrays sampled at the frame times 0, 1/30, ..., 12 s:
`t` (N+1,), `q`, `qd`, `qdd` (N+1, 6). The reference itself is smooth between those samples.

`{"type":"trial","name":...}` places the arm at rest at the reference's first sample and replies
`{"type":"trial_start","name","n_frames","ref","obs"}`. Step it one frame at a time (n = 1) through all
`n_frames`; each reply carries `"frame"`, and after the last one you also get
`{"type":"trial_result","rms_mm",...}`. Sending reset or another trial abandons the trial unscored.

**Error is in millimetres.** Three points fixed to the gripper are tracked: the wrist centre on joint 6's axis,
the tool point 11.5 cm out along it, and a point 6 cm off that axis (so wrist roll counts). A trial's error is the
RMS distance of those points from where the reference puts them, on the real arm's own geometry, at 1500 Hz over
the whole 12 s, so what happens inside a frame counts. Lower is better.

The references come in these kinds: `gravity_*` (slow moves and holds across the workspace), `friction_*` (slow
reversals, tiny dithers, constant-speed plateaus), `inertia_*` (quick short moves of the shoulder/elbow or of the
wrist), `coriolis_*` (fast base swings with the arm stretched out) and `recorded_*` (12 s windows of a real YAM
right arm's motion in production, up to about 6 rad/s and 50 rad/s^2 on the wrist).
"""

PROMPT_V1 = """\
# Make a YAM arm follow these trajectories as closely as you can

{arm}
## Your task

`references/` holds the {n} references you are scored on, one JSON file each; they are also the trial names.
**Score: for each reference, the lowest `rms_mm` of any trial of it that you finish; the final score is the mean
of those over all {n} references.** A reference you never finish a trial of counts as the arm left limp, which is
hundreds of mm. You may run each trial as often as you like, from as many connections at once as you like, and use
reset/step between trials to study the arm. Only finished trials count, and only those finished within your time.

Keep the scripts you write in this directory (not /tmp), so what you did can be read afterwards.

You have {minutes} minutes of wall-clock time. Finish a trial of every reference early, then improve them.
"""

PROMPT_V2 = """\
# Make a YAM arm follow trajectories as closely as you can

{arm}
## Your task

Deliver `controller.py` in this directory. It will be given {n} references it has never seen and must make the arm
follow them. **Score: the mean `rms_mm` over those hidden references.** They are of the same kinds as the {n}
practice references in `references/` (one of each `gravity_*`, `friction_*`, `inertia_*`, `coriolis_*` kind, and
recorded windows from other production episodes), on this same arm, but different trajectories.

`controller.py` must define `control(robot, ref)`. `ref` is the whole reference, given up front, as numpy arrays.
The arm starts at rest at `ref["q"][0]`. `robot.obs` is the latest observation (numpy arrays), and
`robot.step(q, qd=None, kp=None, kd=None, tau=None)` sends one frame's drive command and returns the next
observation. Call it once per frame; after the last frame it raises an exception that you should let propagate.
If the controller stops early or crashes, the drives go limp for the rest of the reference.

`controller.py` runs in its own process with Python 3, numpy, scipy and mujoco, and only `controller.py` is used
for scoring: copy anything you learn about the arm into it. It may load `tools/yam_nominal.xml`. It has no access
to the socket when scored and nothing else from this directory.

`python tools/practice.py <reference>` runs your `controller.py` on a practice reference against the real arm
exactly as scoring will, prints its error and writes everything that happened to `practice_runs/`.

You have {minutes} minutes of wall-clock time. Leave a working `controller.py` in place well before then.
"""


def prompt(version: str, minutes: int, n: int) -> str:
    fmt = lambda a: "[" + ", ".join(f"{x:g}" for x in a) + "]"  # noqa: E731
    arm = ARM.replace("{kp_max}", fmt(KP_MAX)).replace("{kd_max}", fmt(KD_MAX)).replace("{tau_max}", fmt(TAU_MAX))
    body = PROMPT_V1 if version == "v1" else PROMPT_V2
    return body.replace("{arm}", arm).replace("{minutes}", str(minutes)).replace("{n}", str(n))


def reset(run: Path, robot_seed: int, version: str, minutes: int) -> None:
    work, private = run / "work", run / "private"
    (work / "tools").mkdir(parents=True)
    (work / "references").mkdir()
    private.mkdir(parents=True)
    (run / "sock").mkdir()
    for name in TOOLS[version]:
        shutil.copyfile(PUBLIC / name, work / "tools" / name)
    nominal = build_model(None)
    ev, ev_src = build_set(nominal, "eval")
    served, src = (ev, ev_src) if version == "v1" else build_set(nominal, "practice")
    np.savez_compressed(private / "served.npz", **served)
    np.savez_compressed(private / "eval.npz", **ev)
    for name, traj in served.items():
        (work / "references" / f"{name}.json").write_text(json.dumps(reference_for_controller(traj)))
    (work / "PROMPT.md").write_text(prompt(version, minutes, len(served)))
    save_params(make_params(robot_seed), private / "params.json")
    (private / "meta.json").write_text(json.dumps({"version": version, "minutes": minutes, "robot_seed": robot_seed,
                                                   "served_sources": src, "eval_sources": ev_src}, indent=1))


def serve_argv(run: Path, name: str) -> list[str]:
    """The arm in a container that sees only the sim code, its private files and the socket directory."""
    argv = ["docker", "run", "--rm", "--init", "--name", name, "--network", "none", "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges", "--read-only", "--tmpfs", "/tmp", "--memory", "8g",
            "--user", f"{os.getuid()}:{os.getgid()}"]
    argv += ["-v", f"{SIM}:/app/sim:ro", "-v", f"{PUBLIC}:/app/public:ro", "-v", f"{run.resolve()}/private:/private",
             "-v", f"{run.resolve()}/sock:/sock", IMAGE]
    return argv + ["python", "/app/sim/server.py", "--sock", "/sock/sim.sock", "--private", "/private"]


def sandbox_argv(workdir: Path, cmd: list[str]) -> list[str]:
    argv = ["docker", "run", "--rm", "-i", "--init", "--network", "none", "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges", "--read-only", "--tmpfs", "/tmp:size=1g",
            "--memory", "4g", "--memory-swap", "4g", "--pids-limit", "256",
            "--user", f"{os.getuid()}:{os.getgid()}", "-v", f"{workdir}:/work:ro", "-w", "/work", "-e", "HOME=/tmp", IMAGE]
    return argv + cmd


def run_trial(arm: Arm, trial: Trial, ref: dict, argv: list[str]) -> dict:
    """Drive one reference with a controller process speaking run_controller.py's protocol on its stdin/stdout."""
    obs = arm.reset(trial.traj[0])
    proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)
    log: list[str] = []
    drain = threading.Thread(target=lambda: log.extend(proc.stderr), daemon=True)
    drain.start()
    t0, status, alive = time.time(), "ok", True
    try:
        proc.stdin.write(json.dumps({"type": "trial_start", "name": trial.name, "n_frames": trial.n_frames, "ref": ref, "obs": obs}) + "\n")
        proc.stdin.flush()
    except BrokenPipeError:
        alive, status = False, "controller_exited"
    while True:
        cmd = Command.limp()
        if alive:
            line = proc.stdout.readline()
            try:
                cmd = Command.parse(json.loads(line)) if line else None
            except (ValueError, TypeError, AttributeError) as e:
                cmd, status = None, f"bad_command:{e}"
            if cmd is None:
                alive, cmd = False, Command.limp()
                status = status if status != "ok" else "controller_exited"
            elif time.time() - t0 > TRIAL_WALL_S:
                alive, status = False, "wall_limit"
        obs = arm.frame(cmd, trial.log_slot())
        if trial.advanced():
            break
        if alive:
            try:
                proc.stdin.write(json.dumps({"type": "obs", **obs, "frame": trial.k}) + "\n")
                proc.stdin.flush()
            except BrokenPipeError:
                alive, status = False, "controller_exited"
    try:
        proc.stdin.close()
    except BrokenPipeError:
        pass
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
    drain.join(timeout=2)
    return {**trial.result(), "status": status, "wall_s": round(time.time() - t0, 1), "stderr_tail": "".join(log)[-1200:]}


def _controller_argv(controller: Path, sandbox: bool) -> tuple[list[str], tempfile.TemporaryDirectory]:
    tmp = tempfile.TemporaryDirectory()
    clean = Path(tmp.name).resolve()
    (clean / "tools").mkdir()
    shutil.copyfile(controller, clean / "controller.py")
    for name in ("yam_nominal.xml", "run_controller.py"):
        shutil.copyfile(PUBLIC / name, clean / "tools" / name)
    os.chmod(clean, 0o755)
    cmd = ["python", "tools/run_controller.py", "controller.py"]
    if sandbox:
        return sandbox_argv(clean, cmd), tmp
    return [sys.executable, str(clean / "tools/run_controller.py"), str(clean / "controller.py")], tmp


def limp_mm(run: Path, name: str) -> float:
    """What a never-finished v1 test scores: the arm left limp for the whole reference."""
    p = load_params(run / "private" / "params.json")
    traj = np.load(run / "private" / "eval.npz")[name]
    arm, trial = Arm(p, noise_seed=0), Trial(name, traj, build_model(p))
    arm.reset(traj[0])
    while True:
        arm.frame(Command.limp(), trial.log_slot())
        if trial.advanced():
            return trial.result()["rms_mm"]


def summarise(per_test: dict[str, float]) -> dict:
    cats: dict[str, list[float]] = {}
    for name, mm in per_test.items():
        cats.setdefault(CATEGORY.get(name, "recorded"), []).append(mm)
    return {"score_mm": round(statistics.mean(per_test.values()), 4),
            "per_category_mm": {c: round(statistics.mean(v), 4) for c, v in cats.items()},
            "per_test_mm": per_test}


def grade_v1(run: Path) -> dict:
    meta = json.loads((run / "private" / "meta.json").read_text())
    deadline = meta["minutes"] * 60 + GRACE_S
    names = list(np.load(run / "private" / "eval.npz").files)
    best: dict[str, float] = {}
    progress = []  # (wall_s, name, rms_mm) of every finished trial
    log = run / "private" / "server_log.jsonl"
    for line in log.read_text().splitlines() if log.exists() else []:
        e = json.loads(line)
        if e.get("event") == "trial_result" and e["wall_s"] <= deadline:
            progress.append((e["wall_s"], e["name"], e["rms_mm"]))
            best[e["name"]] = min(best.get(e["name"], np.inf), e["rms_mm"])
    missing = [n for n in names if n not in best]
    per_test = {n: best[n] if n in best else limp_mm(run, n) for n in names}
    return {**summarise(per_test), "missing": missing, "trials": len(progress), "progress": progress}


def grade_v2(run: Path, controller: Path | None = None, sandbox: bool = True) -> dict:
    controller = controller or run / "work" / "controller.py"
    if not controller.exists():
        return {"score_mm": None, "error": "no controller.py"}
    p = load_params(run / "private" / "params.json")
    m = build_model(p)
    ev = np.load(run / "private" / "eval.npz")
    names = list(ev.files)

    def one(i: int) -> dict:
        traj = ev[names[i]]
        argv, tmp = _controller_argv(controller, sandbox)
        with tmp:
            return run_trial(Arm(p, noise_seed=1000 + i), Trial(names[i], traj, m), reference_for_controller(traj), argv)

    with ThreadPoolExecutor(8) as ex:
        runs = list(ex.map(one, range(len(names))))
    return {**summarise({r["name"]: r["rms_mm"] for r in runs}), "runs": runs}


def grade(run: Path, controller: Path | None = None, sandbox: bool = True) -> dict:
    version = json.loads((run / "private" / "meta.json").read_text())["version"]
    return grade_v1(run) if version == "v1" else grade_v2(run, controller, sandbox)


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("reset")
    r.add_argument("run", type=Path)
    r.add_argument("--robot-seed", type=int, required=True)
    r.add_argument("--version", choices=["v1", "v2"], required=True)
    r.add_argument("--minutes", type=int, required=True)
    sub.add_parser("serve").add_argument("run", type=Path)
    g = sub.add_parser("grade")
    g.add_argument("run", type=Path)
    g.add_argument("--controller", type=Path)
    g.add_argument("--no-sandbox", action="store_true")
    a = ap.parse_args()
    if a.cmd == "reset":
        reset(a.run, a.robot_seed, a.version, a.minutes)
    elif a.cmd == "serve":
        return subprocess.call(serve_argv(a.run, f"yamtrack-sim-{a.run.resolve().name}"))
    else:
        res = grade(a.run, a.controller, not a.no_sandbox)
        res.pop("progress", None)
        print(json.dumps({**res, "runs": [{k: v for k, v in x.items() if k != "stderr_tail" or v} for x in res.get("runs", [])]}, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
