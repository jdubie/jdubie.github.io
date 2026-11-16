"""Run coding agents on the black-box database task, each in its own sandbox, then (optionally) grade them.

  python3 run_agents.py --models opus --minutes 20 --tag opus20 [--seed 7101] [--no-grade]

Each run gets a private internal network holding only: the agent's container, one oracle for variant --seed
(alias `oracle`, port 7000) and an egress proxy that allows api.anthropic.com and nothing else. The agent
image has compilers and generic network tools, no database clients or documentation. Same session rules as
pgwire-db-env: one launch message; if the CLI session ends with more than RESUME_MIN_S left it is resumed with
a keep-going nudge. /work is snapshotted every SNAPSHOT_EVERY_S for the reward-over-time curve.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
TASK = ROOT / "task"
IMAGE, ORACLE, EGRESS_IMAGE = "bbx-sandbox:1", "bbx-oracle:2", "python:3.12-slim"  # the opus20 run used bbx-oracle:1
GEMINI_IMAGE = "bbx-gemini:1"
CLAUDE_BIN = Path.home() / ".local/share/claude/versions/2.1.322"
CODEX_VENDOR = Path("/home/robot/.nvm/versions/node/v24.7.0/lib/node_modules/@openai/codex/node_modules/"
                    "@openai/codex-linux-x64/vendor/x86_64-unknown-linux-musl")  # Codex CLI 0.167 from nvm
GEMINI_KEY_FILE = Path.home() / ".config/gemini"
CLAUDE_MODELS = {"opus": "claude-opus-5-5", "sonnet": "claude-sonnet-5-5", "haiku": "claude-haiku-5-5"}
GEMINI_MODELS = {"gemini": "gemini-4-argon"}
# OpenAI runs Sol/Terra through the Codex CLI with an explicit -m; never the CLI default model.
OPENAI_MODELS = {"sol": "gpt-6.1-sol", "terra": "gpt-6.1-terra"}


def api_hosts(model: str) -> str:
    """The one vendor API an agent's egress proxy lets through."""
    if model in CLAUDE_MODELS:
        return "api.anthropic.com"
    if model in OPENAI_MODELS:
        return "chatgpt.com,api.openai.com"
    return "generativelanguage.googleapis.com"


def agent_cmd(model: str, seg: int, text: str) -> tuple[str, list[str]]:
    """(image, argv) for one CLI session; seg > 0 resumes the previous session (as in pgwire-db-env)."""
    if model in CLAUDE_MODELS:
        argv = ["claude", "-p", text, "--model", CLAUDE_MODELS[model], "--dangerously-skip-permissions",
                "--output-format", "stream-json", "--verbose", "--disallowedTools", "WebSearch,WebFetch"]
        return IMAGE, argv + (["--continue"] if seg else [])
    if model in GEMINI_MODELS:
        argv = ["gemini", "-p", text, "-m", GEMINI_MODELS[model], "--approval-mode", "yolo", "--output-format", "stream-json"]
        return GEMINI_IMAGE, argv + (["--resume", "latest"] if seg else [])
    if model in OPENAI_MODELS:
        flags = ["--json", "--skip-git-repo-check", "--dangerously-bypass-approvals-and-sandbox", "-c", 'web_search="disabled"',
                 "-m", OPENAI_MODELS[model]]
        if seg:
            return IMAGE, ["/opt/codex/bin/codex", "exec", "resume", "--last", *flags, text]
        return IMAGE, ["/opt/codex/bin/codex", "exec", *flags, "-C", "/work", text]
    raise ValueError(f"unknown model {model}")
LAUNCH = "Read PROMPT.md in the current directory and do what it says."
NUDGE = "You have about {m} minutes left. Keep working on the task in PROMPT.md."
RESUME_MIN_S = 60
SNAPSHOT_EVERY_S = 120
TOKEN_MARGIN_MIN = 5


def docker(*a: str, check: bool = True) -> str:
    r = subprocess.run(["docker", *a], capture_output=True, text=True, timeout=300)
    if check and r.returncode:
        raise RuntimeError(f"docker {' '.join(a[:3])}: {r.stderr.strip()[-300:]}")
    return r.stdout.strip()


def claude_token(minutes: int) -> str:
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


class Env:
    """The run's private network, its oracle and its egress proxy."""

    def __init__(self, name: str, seed: int, allow: str = "api.anthropic.com"):
        self.net, self.oracle, self.egress = f"bbx-run-{name}", f"bbx-run-{name}-oracle", f"bbx-run-{name}-egress"
        self.vol = f"bbx-run-{name}-pg"
        docker("network", "create", "--internal", "--label", "bbx=1", self.net)
        docker("volume", "create", "--label", "bbx=1", self.vol)
        docker("run", "-d", "--name", self.oracle, "--label", "bbx=1", "--init", "--network", self.net, "--network-alias", "oracle",
               "-e", f"BBX_SEED={seed}", "--cpus", "4", "--memory", "6g", "--shm-size", "512m",
               "-v", f"{self.vol}:/var/lib/postgresql/data", ORACLE)
        docker("run", "-d", "--name", self.egress, "--label", "bbx=1", "-e", f"PROXY_ALLOW={allow}",
               "-v", f"{ROOT / 'docker/egress_proxy.py'}:/proxy.py:ro", EGRESS_IMAGE, "python", "-u", "/proxy.py")
        docker("network", "connect", "--alias", "egress", self.net, self.egress)
        for _ in range(300):
            r = subprocess.run(["docker", "logs", self.oracle], capture_output=True, text=True)
            if "on :7000" in r.stdout + r.stderr:
                return
            time.sleep(0.5)
        raise RuntimeError("oracle did not come up")

    def close(self, logdir: Path) -> None:
        for c in (self.oracle, self.egress):
            r = subprocess.run(["docker", "logs", c], capture_output=True, text=True)
            (logdir / f"{c.rsplit('-', 1)[1]}.log").write_text((r.stdout + r.stderr)[-200000:])
            docker("rm", "-f", c, check=False)
        docker("network", "rm", self.net, check=False)
        docker("volume", "rm", "-f", self.vol, check=False)


def run_agent(tag: str, model: str, rep: int, minutes: int, seed: int, grade: bool) -> dict:
    run = ROOT / "runs" / tag / f"{model}__r{rep}"
    if (run / "meta.json").exists():
        return json.loads((run / "meta.json").read_text())
    if run.exists():
        shutil.rmtree(run)
    work, home, snaps = run / "work", run / "home", run / "snapshots"
    for d in (run, home, snaps):
        d.mkdir(parents=True)
    shutil.copytree(TASK, work, ignore=shutil.ignore_patterns("__pycache__"))
    (work / "PROMPT.md").write_text((TASK / "PROMPT.md").read_text().replace("{minutes}", str(minutes)))
    secret: dict[str, str] = {}
    mounts: list[str] = []
    if model in CLAUDE_MODELS:
        secret["CLAUDE_CODE_OAUTH_TOKEN"] = claude_token(minutes)
        mounts += ["-v", f"{CLAUDE_BIN}:/usr/local/bin/claude:ro", "-e", "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1",
                   "-e", "DISABLE_AUTOUPDATER=1"]
    elif model in GEMINI_MODELS:
        secret["GEMINI_API_KEY"] = GEMINI_KEY_FILE.read_text().strip()
        mounts += ["-e", "GEMINI_CLI_TRUST_WORKSPACE=true"]
    elif model in OPENAI_MODELS:
        auth = json.loads((Path.home() / ".codex/auth.json").read_text())
        auth["tokens"]["refresh_token"] = ""  # the container can use the session but never rotate it
        (home / "codex").mkdir()
        (home / "codex/auth.json").write_text(json.dumps(auth))
        (home / "codex/auth.json").chmod(0o600)
        mounts += ["-e", "CODEX_HOME=/home/agent/codex", "-v", f"{CODEX_VENDOR}:/opt/codex:ro"]
    passwd = run / "passwd"
    passwd.write_text(f"agent:x:{os.getuid()}:{os.getgid()}::/home/agent:/bin/sh\n")
    env = Env(f"{tag}-{model}-{rep}", seed, api_hosts(model))
    t0 = time.time()
    deadline = t0 + minutes * 60
    stop = threading.Event()

    def snapshotter() -> None:
        k = 1
        while not stop.wait(max(0.0, t0 + k * SNAPSHOT_EVERY_S - time.time())):
            subprocess.run(["cp", "-a", str(work), str(snaps / f"t{k * SNAPSHOT_EVERY_S // 60:03d}min")], capture_output=True)
            k += 1

    threading.Thread(target=snapshotter, daemon=True).start()
    segments = []
    try:
        for seg in range(500):
            left = deadline - time.time()
            if seg and left < RESUME_MIN_S:
                break
            name = f"bbx-agent-{tag}-{model}-{rep}-{seg}"
            text = LAUNCH if seg == 0 else NUDGE.format(m=round(left / 60))
            image, cmd = agent_cmd(model, seg, text)
            argv = ["docker", "run", "--rm", "--init", "--name", name, "--label", "bbx=1", "--cap-drop", "ALL",
                    "--security-opt", "no-new-privileges", "--read-only", "--user", f"{os.getuid()}:{os.getgid()}",
                    "--network", env.net, "--tmpfs", "/tmp:size=4g,exec", "--cpus", "4", "--memory", "8g", "--memory-swap", "8g",
                    "--pids-limit", "2048", "-v", f"{work.resolve()}:/work", "-v", f"{home.resolve()}:/home/agent",
                    "-v", f"{passwd.resolve()}:/etc/passwd:ro", "-w", "/work", "-e", "HOME=/home/agent"] + mounts
            for var in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
                argv += ["-e", f"{var}=http://egress:3128"]
            argv += [x for k in secret for x in ("-e", k)]  # secrets travel by name only
            argv += [image] + cmd
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
    finally:
        stop.set()
        env.close(run)
    meta = {"model": model, "rep": rep, "minutes": minutes, "seed": seed, "seconds": round(time.time() - t0, 1), "segments": segments}
    (run / "meta.json").write_text(json.dumps(meta, indent=1))
    if grade:
        subprocess.run([sys.executable, str(ROOT / "grader/grade.py"), "--work", str(work), str(run / "grade"), "--seed", str(seed)],
                       capture_output=True, timeout=4 * 3600)
    print(json.dumps({"model": model, "rep": rep, "seconds": meta["seconds"], "sessions": len(segments)}), flush=True)
    return meta


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", required=True)
    ap.add_argument("--minutes", type=int, default=20)
    ap.add_argument("--tag", required=True)
    ap.add_argument("--seed", type=int, default=7101)
    ap.add_argument("--concurrency", type=int, default=1)
    ap.add_argument("--no-grade", action="store_true")
    a = ap.parse_args()
    from concurrent.futures import ThreadPoolExecutor

    def one(m: str):
        try:
            return run_agent(a.tag, m, 0, a.minutes, a.seed, not a.no_grade)
        except Exception as e:  # one broken run must not end the sweep
            print("RUN FAILED", m, type(e).__name__, e, flush=True)

    with ThreadPoolExecutor(a.concurrency) as ex:
        list(ex.map(one, a.models.split(",")))


if __name__ == "__main__":
    main()
