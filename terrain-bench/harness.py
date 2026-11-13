"""terrain-bench: an agent tunes and drives a 2D car over rough terrain through a unix socket, in real time.

The car and courses are a headless port of Rafael Matsunaga's "HTML5 Genetic Algorithm 2D Car Thingy" (cawro.js,
http://rednuht.org/genetic_cars_2/, read from https://santex.github.io/singularity/seed/idx.html), itself loosely based
on BoxCar2D (http://boxcar2d.com/). See README.md.

Each run:
  1. practice  the agent (`claude -p`, Bash/Read/Write/Edit/Glob/Grep) gets BUDGET minutes in an empty dir with a live
               simulator at ./sim.sock on random courses, plus PROTOCOL.md. It must leave an executable ./drive.
  2. eval      a fresh simulator runs COURSES hidden courses (the same for every model in a tag); ./drive is started
               once with the socket path and drives them all. Score = mean cawro score (furthest x + average speed).
  Model `none` skips both and lets the default car drive the eval courses: the do-nothing baseline.

  python harness.py run --models none,haiku --budget 20
  python harness.py summary
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import shutil
import signal
import subprocess
import tempfile
import time
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent
RUNS = ROOT / "runs"
SERVER = ROOT / "sim" / "server.js"
MODELS = {"opus": "claude-opus-5-5", "sonnet": "claude-sonnet-5-5", "haiku": "claude-haiku-5-5", "fable": "claude-fable-5-1"}
MAX_EPISODE_S = 180

SYSTEM = "You are an AI agent taking part in a benchmark. You work in your current directory and must finish before the deadline."

PROMPT = """\
Drive a 2D car as far as you can over rough terrain.

A physics simulator is running now, in real time at 60 Hz, on the unix socket ./sim.sock. PROTOCOL.md (in this
directory) describes it fully. In short: the car drives itself to the right; you observe the chassis position, angle,
velocity and angular velocity plus one number, the height of the ground 2 m ahead, every tick; and at any moment you
can change the car's chassis shape, wheel placement, wheel size and density, and each motor's speed and torque. A
`reset` command starts a new random course from the beginning. Practise as much as you like.

Deadline: {deadline} ({budget} minutes from now). Your session ends then.

Deliverable: an executable file ./drive in this directory. After the deadline it is started once, as
`./drive <socket path>` from this directory, against a fresh simulator that runs {courses} courses you have not seen,
one after another (the next starts as soon as one ends; the socket closes after the last). It drives them all.
Your score is the mean over those courses of (furthest x reached + average speed). The car is reset to the default
params at the start of every course. If ./drive is missing or crashes, the default car drives on its own.

Write ./drive early and improve it; whatever is there at the deadline is what runs.
"""


def start_server(sock: Path, log: Path, seeds: list[str] | None = None) -> subprocess.Popen:
    argv = ["node", str(SERVER), "--socket", str(sock), "--log", str(log), "--max-s", str(MAX_EPISODE_S)]
    if seeds:
        argv += ["--seeds", ",".join(seeds)]
    proc = subprocess.Popen(argv, stderr=subprocess.DEVNULL, start_new_session=True)
    for _ in range(100):
        if sock.exists():
            return proc
        time.sleep(0.05)
    raise RuntimeError("simulator did not open its socket")


def kill(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        os.killpg(proc.pid, signal.SIGKILL)
    proc.wait()


def practice(model: str, work: Path, out: Path, budget_min: float, courses: int) -> dict:
    deadline = datetime.now() + timedelta(minutes=budget_min)
    shutil.copy(ROOT / "PROTOCOL.md", work / "PROTOCOL.md")
    text = PROMPT.format(deadline=deadline.strftime("%H:%M:%S"), budget=budget_min, courses=courses)
    sim = start_server(work / "sim.sock", out / "practice.jsonl")
    argv = ["claude", "-p", text, "--model", MODELS[model], "--system-prompt", SYSTEM, "--tools", "Bash,Read,Write,Edit,Glob,Grep",
            "--permission-mode", "bypassPermissions", "--setting-sources", "", "--strict-mcp-config", "--output-format", "json"]
    start = time.monotonic()
    with (out / "claude.json").open("wb") as so, (out / "stderr.txt").open("wb") as se:
        agent = subprocess.Popen(argv, cwd=work, stdout=so, stderr=se, start_new_session=True)
        try:
            agent.wait(timeout=budget_min * 60)
            timed_out = False
        except subprocess.TimeoutExpired:
            timed_out = True
    elapsed = time.monotonic() - start
    kill(agent)  # also takes down anything it left running in its process group
    kill(sim)
    (out / "prompt.txt").write_text(text)
    return {"agent_s": round(elapsed, 1), "agent_timed_out": timed_out}


def evaluate(work: Path, out: Path, seeds: list[str]) -> dict:
    sock = work / "sim.sock"
    sock.unlink(missing_ok=True)
    sim = start_server(sock, out / "eval.jsonl", seeds)
    drive = work / "drive"
    has_drive = drive.is_file() and os.access(drive, os.X_OK)
    drv = None
    if has_drive:
        with (out / "drive.log").open("wb") as log:
            drv = subprocess.Popen([str(drive), str(sock)], cwd=work, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    try:
        sim.wait(timeout=len(seeds) * MAX_EPISODE_S + 60)
    except subprocess.TimeoutExpired:
        pass
    drive_rc = drv.poll() if drv else None
    if drv:
        kill(drv)
    kill(sim)
    eps = [json.loads(line) for line in (out / "eval.jsonl").read_text().splitlines()] if (out / "eval.jsonl").exists() else []
    scores = [e["score"] for e in eps] + [0.0] * (len(seeds) - len(eps))
    return {"has_drive": has_drive, "drive_exit": drive_rc, "scores": scores, "distances": [e["distance"] for e in eps],
            "reasons": [e["reason"] for e in eps], "mean_score": sum(scores) / len(seeds)}


def run_one(model: str, tag: str, rep: int, seeds: list[str], budget_min: float) -> dict:
    out = RUNS / tag / f"{model}-{rep}"
    out.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="tb-", dir="/tmp"))  # short path: unix socket paths are capped at 108 bytes
    try:
        info = {"agent_s": 0, "agent_timed_out": False}
        if model != "none":
            info = practice(model, work, out, budget_min, len(seeds))
        res = evaluate(work, out, seeds)
        (work / "sim.sock").unlink(missing_ok=True)
        shutil.copytree(work, out / "workdir", dirs_exist_ok=True, ignore=shutil.ignore_patterns("node_modules", ".venv", "__pycache__"))
    finally:
        shutil.rmtree(work, ignore_errors=True)
    practice_eps = 0
    if (out / "practice.jsonl").exists():
        practice_eps = len((out / "practice.jsonl").read_text().splitlines())
    return {"model": model, "rep": rep, "budget_min": budget_min, "practice_episodes": practice_eps, **info, **res}


def cmd_run(args: argparse.Namespace) -> None:
    tag = args.tag or datetime.now().strftime("%Y%m%d-%H%M%S")
    seeds_file = RUNS / tag / "seeds.json"
    seeds_file.parent.mkdir(parents=True, exist_ok=True)
    if not seeds_file.exists():  # hidden eval courses, shared by every model and rep in this tag
        seeds_file.write_text(json.dumps([secrets.token_hex(4) for _ in range(args.courses)]))
    seeds = json.loads(seeds_file.read_text())
    for rep in range(args.reps):
        for model in args.models.split(","):
            r = run_one(model, tag, rep, seeds, args.budget)
            with (RUNS / tag / "results.jsonl").open("a") as f:
                f.write(json.dumps(r) + "\n")
            print(f"{model:>6} rep {rep}: mean {r['mean_score']:.1f}  scores {[round(s, 1) for s in r['scores']]}  "
                  f"drive={r['has_drive']} practice_eps={r['practice_episodes']} agent {r['agent_s']}s", flush=True)


def cmd_summary(_: argparse.Namespace) -> None:
    groups: dict[str, list[dict]] = defaultdict(list)
    for path in sorted(RUNS.glob("*/results.jsonl")):
        for line in path.read_text().splitlines():
            r = json.loads(line)
            groups[r["model"]].append(r)
    for model, rs in sorted(groups.items(), key=lambda kv: -sum(r["mean_score"] for r in kv[1]) / len(kv[1])):
        m = [r["mean_score"] for r in rs]
        print(f"{model:>6} n={len(rs):<3} mean={sum(m) / len(m):7.1f}  best={max(m):7.1f}  drive={sum(r['has_drive'] for r in rs)}/{len(rs)}")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--models", default="none,haiku")
    r.add_argument("--reps", type=int, default=1)
    r.add_argument("--budget", type=float, default=30, help="practice minutes per agent")
    r.add_argument("--courses", type=int, default=5)
    r.add_argument("--tag")
    r.set_defaults(fn=cmd_run)
    s = sub.add_parser("summary")
    s.set_defaults(fn=cmd_summary)
    args = p.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
