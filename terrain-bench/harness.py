"""terrain-bench: coding agents drive a 2D car over rough terrain through a unix socket, in real time.

The car and courses are a headless port of Rafael Matsunaga's "HTML5 Genetic Algorithm 2D Car Thingy" (cawro.js,
http://rednuht.org/genetic_cars_2/, read from https://santex.github.io/singularity/seed/idx.html), itself loosely based
on BoxCar2D (http://boxcar2d.com/). See README.md.

A run is one simulator and one agent for MINUTES of wall time. The simulator starts with the run and the default car
drives from the first second, whether or not the agent is connected. The agent changes the car and resets to new courses
whenever it likes. Reward = the furthest x any episode reached during the run; the curve of that over time is the
progress plot. Model `none` is no agent: the default car alone for the same time.

Each agent runs in its own container (the libbet-eval sandbox images, Dockerfiles in docker/), with /work holding
PROMPT.md, PROTOCOL.md and the simulator socket. Whenever its CLI session ends with time left it is resumed with the
same nudge; programs it started die with the session.

  python harness.py run --models none,opus,sonnet,haiku,sol,terra,gemini --minutes 30 --tag sweep1
  python harness.py summary runs/sweep1
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SERVER = ROOT / "sim" / "server.js"
IMAGE, GEMINI_IMAGE = "libbet-sandbox:1", "libbet-gemini:1"
NET, PROXY = "cadenv-agents", "cadenv-proxy"
CLAUDE_BIN = Path.home() / ".local/share/claude/versions/2.1.322"
CODEX_VENDOR = Path("/home/robot/.nvm/versions/node/v24.7.0/lib/node_modules/@openai/codex/node_modules/"
                    "@openai/codex-linux-x64/vendor/x86_64-unknown-linux-musl")
GEMINI_KEY_FILE = Path.home() / ".config/gemini"
CLAUDE_MODELS = {"opus": "claude-opus-5-5", "sonnet": "claude-sonnet-5-5", "haiku": "claude-haiku-5-5"}
GEMINI_MODELS = {"gemini": "gemini-4-argon"}
CODEX_MODELS = {"sol": "gpt-6.1-sol", "terra": "gpt-6.1-terra"}  # always an explicit model; never the CLI default
LAUNCH = "Read PROMPT.md in the current directory and do what it says."
NUDGE = "You have about {m} minutes left in the run. Keep working on the task in PROMPT.md."
RESUME_MIN_S = 60
TOKEN_MARGIN_MIN = 5

PROMPT = """\
# Drive a car as far as you can

A physics simulator is running now on the unix socket ./sim.sock: a 2D car on rough terrain, in real time at 60 Hz.
PROTOCOL.md describes it fully. In short: the car drives itself to the right; every tick you observe the chassis
position, angle, velocity and angular velocity plus one number, the height of the ground 2 m ahead; at any moment you
can change the car's chassis shape, wheel placement, wheel size and density, and each motor's speed and torque; and a
`reset` command starts a new random course from the beginning.

This run lasts {minutes} minutes from when you were started, whatever you do. Your reward is the furthest x that any of
your cars reaches at any moment during the run. Only that maximum counts: reset as often as you like, and failed
attempts cost nothing but time. The car keeps driving on its current settings whether or not you are connected.

You may be stopped and resumed. Programs you start do not survive the end of your session.
"""


def claude_token(minutes: float) -> str:
    """An access token valid for the whole run; the refresh token never enters a container."""
    path = Path.home() / ".claude/.credentials.json"
    while True:
        o = json.loads(path.read_text())["claudeAiOauth"]
        left = (o["expiresAt"] / 1000 - time.time()) / 60
        if left >= minutes + TOKEN_MARGIN_MIN:
            return o["accessToken"]
        if left <= 0.5:
            with tempfile.TemporaryDirectory() as tmp:
                subprocess.run(["claude", "-p", "Reply with the single word ok", "--model", CLAUDE_MODELS["haiku"]],
                               cwd=tmp, capture_output=True, timeout=120)
        time.sleep(20)


def agent_cmd(model: str, seg: int, text: str) -> tuple[str, list[str]]:
    """(image, argv) for one CLI session; seg > 0 resumes the previous session."""
    if model in CLAUDE_MODELS:
        argv = ["claude", "-p", text, "--model", CLAUDE_MODELS[model], "--dangerously-skip-permissions",
                "--output-format", "stream-json", "--verbose", "--disallowedTools", "WebSearch,WebFetch"]
        return IMAGE, argv + (["--continue"] if seg else [])
    if model in GEMINI_MODELS:
        argv = ["gemini", "-p", text, "-m", GEMINI_MODELS[model], "--approval-mode", "yolo", "--output-format", "stream-json"]
        return GEMINI_IMAGE, argv + (["--resume", "latest"] if seg else [])
    if model in CODEX_MODELS:
        flags = ["--json", "-m", CODEX_MODELS[model], "--skip-git-repo-check", "--dangerously-bypass-approvals-and-sandbox", "-c", 'web_search="disabled"']
        if seg:
            return IMAGE, ["/opt/codex/bin/codex", "exec", "resume", "--last", *flags, text]
        return IMAGE, ["/opt/codex/bin/codex", "exec", *flags, "-C", "/work", text]
    raise ValueError(f"unknown model {model}")


def sandbox(model: str, run: Path, home: Path, minutes: float) -> tuple[dict[str, str], list[str]]:
    secret: dict[str, str] = {}
    mounts: list[str] = []
    if model in CLAUDE_MODELS:
        secret["CLAUDE_CODE_OAUTH_TOKEN"] = claude_token(minutes)
        mounts += ["-v", f"{CLAUDE_BIN}:/usr/local/bin/claude:ro", "-e", "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1", "-e", "DISABLE_AUTOUPDATER=1"]
    elif model in GEMINI_MODELS:
        secret["GEMINI_API_KEY"] = GEMINI_KEY_FILE.read_text().strip()
        mounts += ["-e", "GEMINI_CLI_TRUST_WORKSPACE=true"]
    elif model in CODEX_MODELS:
        auth = json.loads((Path.home() / ".codex/auth.json").read_text())
        auth["tokens"]["refresh_token"] = ""  # the container can use the session but never rotate it
        (home / "codex").mkdir()
        (home / "codex/auth.json").write_text(json.dumps(auth))
        (home / "codex/auth.json").chmod(0o600)
        mounts += ["-e", "CODEX_HOME=/home/agent/codex", "-v", f"{CODEX_VENDOR}:/opt/codex:ro"]
    passwd = run / "passwd"
    passwd.write_text(f"agent:x:{os.getuid()}:{os.getgid()}::/home/agent:/bin/sh\n")
    mounts += ["-v", f"{passwd.resolve()}:/etc/passwd:ro"]
    return secret, mounts


def run_agent(tag: str, model: str, minutes: float) -> dict:
    run = ROOT / "runs" / tag / model
    if (run / "meta.json").exists():
        return json.loads((run / "meta.json").read_text())
    if run.exists():
        shutil.rmtree(run)
    work, home, rec = run / "work", run / "home", run / "sim"
    for d in (work, home, rec):
        d.mkdir(parents=True)
    (work / "PROMPT.md").write_text(PROMPT.format(minutes=round(minutes)))
    shutil.copy(ROOT / "PROTOCOL.md", work / "PROTOCOL.md")
    secret, mounts = sandbox(model, run, home, minutes) if model != "none" else ({}, [])

    sock = work / "sim.sock"
    sim = subprocess.Popen(["node", str(SERVER), "--socket", str(sock), "--record", str(rec)], stderr=subprocess.DEVNULL, start_new_session=True)
    while not sock.exists():
        time.sleep(0.05)
    t0 = time.time()
    deadline = t0 + minutes * 60
    segments = []
    if model == "none":
        time.sleep(minutes * 60)
    for seg in range(30 if model != "none" else 0):
        left = deadline - time.time()
        if seg and left < RESUME_MIN_S:
            break
        name = f"terrain-{tag}-{model}-{seg}"
        image, cmd = agent_cmd(model, seg, LAUNCH if seg == 0 else NUDGE.format(m=round(left / 60)))
        argv = ["docker", "run", "--rm", "--init", "--name", name, "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
                "--read-only", "--user", f"{os.getuid()}:{os.getgid()}", "--network", NET, "--tmpfs", "/tmp:size=4g,exec",
                "--cpus", "4", "--memory", "8g", "--memory-swap", "8g", "--pids-limit", "2048",
                "-v", f"{work.resolve()}:/work", "-v", f"{home.resolve()}:/home/agent", "-w", "/work", "-e", "HOME=/home/agent"]
        for var in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
            argv += ["-e", f"{var}=http://{PROXY}:3128"]
        argv += [x for k in secret for x in ("-e", k)]  # secrets travel by name only, never on the command line
        argv += mounts + [image] + cmd
        start = time.time() - t0
        with open(run / "transcript.jsonl", "ab") as out, open(run / "stderr.log", "ab") as err:
            proc = subprocess.Popen(argv, stdout=out, stderr=err, env={**os.environ, **secret})
            try:
                proc.wait(timeout=max(1.0, deadline - time.time()))
            except subprocess.TimeoutExpired:
                subprocess.run(["docker", "kill", name], capture_output=True)
                proc.wait()
        segments.append({"segment": seg, "start_s": round(start, 1), "end_s": round(time.time() - t0, 1), "exit": proc.returncode})
        if time.time() >= deadline - 1:
            break
    time.sleep(max(0.0, deadline - time.time()))  # an agent that quit early still has its car driving to the end
    os.killpg(sim.pid, signal.SIGTERM)  # the simulator records the episode in progress, then exits
    sim.wait()
    eps = [json.loads(line) for line in (rec / "episodes.jsonl").read_text().splitlines()]
    meta = {"model": model, "minutes": minutes, "seconds": round(time.time() - t0, 1), "segments": segments,
            "episodes": len(eps), "best_distance": max(e["distance"] for e in eps)}
    (run / "meta.json").write_text(json.dumps(meta, indent=1))
    print(json.dumps({k: meta[k] for k in ("model", "best_distance", "episodes")} | {"sessions": len(segments)}), flush=True)
    return meta


def cmd_run(a: argparse.Namespace) -> None:
    jobs = [(a.tag, m, a.minutes) for m in a.models.split(",")]

    def guarded(job):
        try:
            return run_agent(*job)
        except Exception as e:  # one broken run must not end the sweep
            print("RUN FAILED", job, type(e).__name__, e, flush=True)

    with ThreadPoolExecutor(len(jobs)) as ex:  # all at once: every run is real time, and they share nothing
        list(ex.map(guarded, jobs))


def cmd_summary(a: argparse.Namespace) -> None:
    metas = [json.loads(p.read_text()) for p in sorted(Path(a.run).glob("*/meta.json"))]
    for m in sorted(metas, key=lambda m: -m["best_distance"]):
        print(f"{m['model']:>7} best {m['best_distance']:7.1f} m  episodes {m['episodes']:<5} sessions {len(m['segments'])}")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--models", default="none,opus,sonnet,haiku,sol,terra,gemini")
    r.add_argument("--minutes", type=float, default=30)
    r.add_argument("--tag", required=True)
    r.set_defaults(fn=cmd_run)
    s = sub.add_parser("summary")
    s.add_argument("run")
    s.set_defaults(fn=cmd_summary)
    a = p.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
