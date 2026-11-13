"""terrain-bench: coding agents design and drive a 2D car over rough terrain through a unix socket.

The car and courses are a headless port of Rafael Matsunaga's "HTML5 Genetic Algorithm 2D Car Thingy" (cawro.js,
http://rednuht.org/genetic_cars_2/, read from https://santex.github.io/singularity/seed/idx.html), itself loosely based
on BoxCar2D (http://boxcar2d.com/). See README.md.

A run is one simulator and one agent for MINUTES of wall time. The simulator is stepped ("fake time"): nothing moves
unless the agent steps it, in up to 8 parallel environments, as fast as its 3 CPU cores allow, for at most EPISODES
episodes in all (every course driven counts, resets included). Reward = the furthest x
any episode reached during the run; the curve of that over time is the progress plot. Model `none` is no agent: a
script that runs the default car through course after course, in all 8 environments, until the budget is used.

Each agent runs in its own container (the libbet-eval sandbox images, Dockerfiles in docker/), with /work holding
PROMPT.md, PROTOCOL.md and the simulator socket. Whenever its CLI session ends with time left it is resumed with the
same nudge; programs it started die with the session.

  python harness.py run --models none,opus,sonnet,haiku,sol,terra,gemini --minutes 30 --tag sweep2
  python harness.py summary runs/sweep2
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
# xAI Grok CLI: a static binary mounted read-only into the sandbox image, egress through its own allowlist proxy
# (the setup ~/distributed-speedup-env uses). grok-4.7 list prices, $/M tokens, only to stop a run that overspends.
GROK_MODELS = {"grok": "grok-4.7"}
GROK_BIN, GROK_PROXY, GROK_KEY_FILE = Path.home() / ".grok/downloads/grok-linux-x86_64", "grok-proxy", Path.home() / ".grok-key"
GROK_PRICE = {"input": 2.0, "cache_read": 0.5, "output": 6.0}
GROK_RUN_CAP_USD = float(os.environ.get("GROK_RUN_CAP_USD", "12"))
LAUNCH = "Read PROMPT.md in the current directory and do what it says."
NUDGE = "You have about {m} minutes left in the run. Keep working on the task in PROMPT.md."
RESUME_MIN_S = 60
TOKEN_MARGIN_MIN = 5

TERRAIN = "v4"
EPISODES = 1000   # per run, across all environments: every course driven, including the first in each environment
SIM_CPUS = 3

PROMPT = """\
# Drive a car as far as you can

A physics simulator is running on the unix socket ./sim.sock: a 2D car on rough terrain. PROTOCOL.md describes it
fully. In short: the car's wheel motors push it to the right; you observe the chassis position, angle, velocity and
angular velocity plus one number, the height of the ground 2 m ahead; at any tick you can change the car's design and
its motors (PROTOCOL.md lists everything you can change). The car crashes if it tips past 90°, and stalls if it stops
making progress. Time is yours: nothing moves unless you step it, and you can run up to 8
environments in parallel, each on its own random course.

This run lasts {minutes} minutes of wall-clock time from when you were started, and allows {episodes} episodes in total:
every course you drive counts, whether it ends by crashing, stalling or a reset, including the first course of each
environment you make. Once they are used up nothing more can be driven. Your reward is the furthest x that any of your
cars reaches at any moment during the run, in any environment.

You may be stopped and resumed. Programs you start do not survive the end of your session.
"""


def budget_spent(rec: Path) -> bool:
    lines = (rec / "progress.jsonl").read_text().splitlines() if (rec / "progress.jsonl").exists() else []
    return bool(lines) and json.loads(lines[-1]).get("episodes", 0) >= EPISODES


def none_driver(sock: Path, deadline: float) -> None:
    """The baseline: the default car, never changed, stepped through course after course in every environment."""
    import socket as so
    s = so.socket(so.AF_UNIX)
    s.connect(str(sock))
    f = s.makefile("rw")
    envs = set()
    for _ in range(8):
        f.write(json.dumps({"cmd": "make"}) + "\n")
        f.flush()
        envs.add(json.loads(f.readline())["env"])
    while envs and time.time() < deadline:  # until the episode budget runs out
        for e in envs:
            f.write(json.dumps({"cmd": "step", "env": e, "n": 6000, "obs": "none", "id": e}) + "\n")
        f.flush()
        for _ in list(envs):
            r = json.loads(f.readline())
            if r["type"] == "error":
                envs.discard(r["id"])


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
    if model in GROK_MODELS:
        argv = ["grok", "-p", text, "-m", GROK_MODELS[model], "--always-approve", "--permission-mode", "bypassPermissions",
                "--disable-web-search", "--output-format", "streaming-json"]
        return IMAGE, argv + (["--continue"] if seg else [])
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
    elif model in GROK_MODELS:
        secret["XAI_API_KEY"] = GROK_KEY_FILE.read_text().strip()
        mounts += ["-v", f"{GROK_BIN}:/usr/local/bin/grok:ro"]   # its ~/.grok (sessions, usage) lands in the run's home/
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


def grok_cost(transcript: Path) -> float:
    """Dollars so far: the CLI's own total if a session ended cleanly, else priced from its per-call usage events."""
    total = 0.0
    if not transcript.exists():
        return total
    for line in transcript.read_text(errors="replace").splitlines():
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if ev.get("type") == "usage":
            u = ev["usage"]
            total += (u.get("input_tokens", 0) * GROK_PRICE["input"] + u.get("cache_read_input_tokens", 0) * GROK_PRICE["cache_read"]
                      + (u.get("output_tokens", 0) + u.get("reasoning_tokens", 0)) * GROK_PRICE["output"]) / 1e6
    return total


def protocol_for(terrain: str) -> Path:
    return ROOT / ("PROTOCOL_FREE.md" if terrain.endswith("free") else "PROTOCOL.md")


def run_agent(tag: str, model: str, minutes: float, terrain: str = TERRAIN) -> dict:
    run = ROOT / "runs" / tag / model
    if (run / "meta.json").exists():
        return json.loads((run / "meta.json").read_text())
    if run.exists():
        shutil.rmtree(run)
    work, home, rec = run / "work", run / "home", run / "sim"
    for d in (work, home, rec):
        d.mkdir(parents=True)
    (work / "PROMPT.md").write_text(PROMPT.format(minutes=round(minutes), episodes=EPISODES))
    shutil.copy(protocol_for(terrain), work / "PROTOCOL.md")
    secret, mounts = sandbox(model, run, home, minutes) if model != "none" else ({}, [])

    sock = work / "sim.sock"
    # its own scope with a CPU cap, so runs in parallel get equal compute however many environments they use
    sim = subprocess.Popen(["systemd-run", "--user", "--scope", "-q", "-p", f"CPUQuota={SIM_CPUS * 100}%", "node", str(SERVER),
                            "--socket", str(sock), "--record", str(rec), "--terrain", terrain, "--episodes", str(EPISODES)], stderr=subprocess.DEVNULL, start_new_session=True)
    while not sock.exists():
        time.sleep(0.05)
    t0 = time.time()
    deadline = t0 + minutes * 60
    segments = []
    overspent = False
    if model == "none":
        none_driver(sock, deadline)
    for seg in range(30 if model != "none" else 0):
        left = deadline - time.time()
        if seg and left < RESUME_MIN_S:
            break
        if seg and budget_spent(rec):  # nothing left to drive: do not resume the agent just to idle
            break
        name = f"terrain-{tag}-{model}-{seg}"
        image, cmd = agent_cmd(model, seg, LAUNCH if seg == 0 else NUDGE.format(m=round(left / 60)))
        argv = ["docker", "run", "--rm", "--init", "--name", name, "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
                "--read-only", "--user", f"{os.getuid()}:{os.getgid()}", "--network", NET, "--tmpfs", "/tmp:size=4g,exec",
                "--cpus", "4", "--memory", "8g", "--memory-swap", "8g", "--pids-limit", "2048",
                "-v", f"{work.resolve()}:/work", "-v", f"{home.resolve()}:/home/agent", "-w", "/work", "-e", "HOME=/home/agent"]
        for var in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
            argv += ["-e", f"{var}=http://{GROK_PROXY if model in GROK_MODELS else PROXY}:3128"]
        argv += [x for k in secret for x in ("-e", k)]  # secrets travel by name only, never on the command line
        argv += mounts + [image] + cmd
        start = time.time() - t0
        with open(run / "transcript.jsonl", "ab") as out, open(run / "stderr.log", "ab") as err:
            proc = subprocess.Popen(argv, stdout=out, stderr=err, env={**os.environ, **secret})
            while proc.poll() is None:   # until it exits, the run ends, or (grok) it overspends
                over = model in GROK_MODELS and grok_cost(run / "transcript.jsonl") > GROK_RUN_CAP_USD
                if time.time() >= deadline or over:
                    subprocess.run(["docker", "kill", name], capture_output=True)
                    proc.wait()
                    if over:
                        overspent = True
                    break
                time.sleep(min(10.0, max(0.1, deadline - time.time())))
        if overspent:
            segments.append({"segment": seg, "start_s": round(start, 1), "end_s": round(time.time() - t0, 1), "exit": "cost_cap"})
            break
        segments.append({"segment": seg, "start_s": round(start, 1), "end_s": round(time.time() - t0, 1), "exit": proc.returncode})
        if time.time() >= deadline - 1:
            break
    time.sleep(max(0.0, deadline - time.time()))
    os.killpg(sim.pid, signal.SIGTERM)  # the simulator records the episode in progress, then exits
    try:
        sim.wait(timeout=10)
    except subprocess.TimeoutExpired:  # wedged inside a physics step (seen once, sweep1 gemini): it cannot handle SIGTERM
        os.killpg(sim.pid, signal.SIGKILL)
        sim.wait()
    eps = [json.loads(line) for line in (rec / "episodes.jsonl").read_text().splitlines()]
    meta = {"model": model, "minutes": minutes, "terrain": terrain,
            **({"cost_usd": round(grok_cost(run / "transcript.jsonl"), 3), "over_cost_cap": overspent} if model in GROK_MODELS else {}), "seconds": round(time.time() - t0, 1), "segments": segments,
            "episodes": len(eps), "best_distance": max(e["distance"] for e in eps)}
    (run / "meta.json").write_text(json.dumps(meta, indent=1))
    print(json.dumps({k: meta[k] for k in ("model", "best_distance", "episodes")} | {"sessions": len(segments)}), flush=True)
    return meta


def cmd_run(a: argparse.Namespace) -> None:
    jobs = [(a.tag, m, a.minutes, a.terrain) for m in a.models.split(",")]

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
    r.add_argument("--terrain", default=TERRAIN, help="a preset in sim/core.js TERRAINS; *free presets use PROTOCOL_FREE.md")
    r.set_defaults(fn=cmd_run)
    s = sub.add_parser("summary")
    s.add_argument("run")
    s.set_defaults(fn=cmd_summary)
    a = p.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
