"""Run coding agents on the pgwire empty-files database task, each in its own sandbox container, then grade them.

  python3 run_agents.py --models opus,sonnet,haiku,sol,terra,gemini --minutes 60 --tag pilot [--concurrency 3]

Every agent gets the same rules: one launch message, and whenever its CLI session ends with more than
RESUME_MIN_S left, the session is resumed with the same keep-going nudge. /work is snapshotted every
SNAPSHOT_EVERY_S so progress over time can be graded afterwards.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent / "grader"))

ROOT = Path(__file__).resolve().parent
TASK = ROOT / "task"
IMAGE, GEMINI_IMAGE = "pgw-sandbox:1", "pgw-gemini:1"
NET, PROXY = "cadenv-agents", "cadenv-proxy"
CLAUDE_BIN = Path.home() / ".local/share/claude/versions/2.1.322"
CODEX_VENDOR = Path("/home/robot/.nvm/versions/node/v24.7.0/lib/node_modules/@openai/codex/node_modules/"
                    "@openai/codex-linux-x64/vendor/x86_64-unknown-linux-musl")
GEMINI_KEY_FILE = Path.home() / ".config/gemini"
CLAUDE_MODELS = {"opus": "claude-opus-5-5", "sonnet": "claude-sonnet-5-5", "haiku": "claude-haiku-5-5"}
GEMINI_MODELS = {"gemini": "gemini-4-argon"}
# OpenAI runs Sol/Terra through the Codex CLI with an explicit -m; never the CLI default model.
OPENAI_MODELS = {"sol": "gpt-6.1-sol", "terra": "gpt-6.1-terra"}
LAUNCH = "Read PROMPT.md in the current directory and do what it says."
NUDGE = "You have about {m} minutes left. Keep working on the task in PROMPT.md."
RESUME_MIN_S = 60
SNAPSHOT_EVERY_S = 900
TOKEN_MARGIN_MIN = 5


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


def make_workspace(work: Path, minutes: int) -> None:
    shutil.copytree(TASK, work, ignore=shutil.ignore_patterns("kit", "__pycache__"))
    (work / "PROMPT.md").write_text((TASK / "PROMPT.md").read_text().replace("{minutes}", str(minutes)))


def agent_cmd(model: str, seg: int, text: str) -> tuple[str, list[str]]:
    """(image, argv) for one CLI session; seg > 0 resumes the previous session."""
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


def run_agent(tag: str, model: str, rep: int, minutes: int) -> dict:
    run = ROOT / "runs" / tag / f"{model}__r{rep}"
    if (run / "meta.json").exists():
        return json.loads((run / "meta.json").read_text())
    if run.exists():
        shutil.rmtree(run)
    work, home, snaps = run / "work", run / "home", run / "snapshots"
    run.mkdir(parents=True)
    home.mkdir()
    snaps.mkdir()
    make_workspace(work, minutes)
    secret: dict[str, str] = {}
    mounts: list[str] = []
    if model in CLAUDE_MODELS:
        secret["CLAUDE_CODE_OAUTH_TOKEN"] = claude_token(minutes)
        mounts += ["-v", f"{CLAUDE_BIN}:/usr/local/bin/claude:ro", "-e", "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1", "-e", "DISABLE_AUTOUPDATER=1"]
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
    for seg in range(500):
        left = deadline - time.time()
        if seg and left < RESUME_MIN_S:
            break
        name = f"pgw-agent-{tag}-{model}-{rep}-{seg}"
        image, cmd = agent_cmd(model, seg, LAUNCH if seg == 0 else NUDGE.format(m=round(left / 60)))
        argv = ["docker", "run", "--rm", "--init", "--name", name, "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
                "--read-only", "--user", f"{os.getuid()}:{os.getgid()}", "--network", NET, "--tmpfs", "/tmp:size=4g,exec",
                "--cpus", "4", "--memory", "8g", "--memory-swap", "8g", "--pids-limit", "2048",
                "-v", f"{work.resolve()}:/work", "-v", f"{home.resolve()}:/home/agent", "-v", f"{passwd.resolve()}:/etc/passwd:ro",
                "-w", "/work", "-e", "HOME=/home/agent"]
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
    stop.set()
    meta = {"model": model, "rep": rep, "minutes": minutes, "seconds": round(time.time() - t0, 1), "segments": segments}
    subprocess.run([sys.executable, str(ROOT / "grader/grade.py"), str(work), str(run / "grade"), "--jobs", "4"],
                   capture_output=True, timeout=4 * 3600)
    g = json.loads((run / "grade/grade.json").read_text()) if (run / "grade/grade.json").exists() else {"summary": {"reward": 0.0}}
    meta |= {k: (round(v, 4) if isinstance(v, float) else v) for k, v in g["summary"].items()}
    (run / "meta.json").write_text(json.dumps(meta, indent=1))
    print(json.dumps({k: meta.get(k) for k in ("model", "rep", "seconds", "reward", "protocol", "clients", "sql", "acid", "scale", "violations")}
                     | {"sessions": len(segments)}), flush=True)
    return meta


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", required=True)
    ap.add_argument("--reps", type=int, default=1)
    ap.add_argument("--minutes", type=int, default=60)
    ap.add_argument("--concurrency", type=int, default=3)
    ap.add_argument("--tag", required=True)
    a = ap.parse_args()
    jobs = [(a.tag, m, k, a.minutes) for k in range(a.reps) for m in a.models.split(",")]
    print(f"{len(jobs)} runs, concurrency {a.concurrency}", flush=True)
    gate = threading.Semaphore(a.concurrency)

    def guarded(job):
        with gate:
            try:
                return run_agent(*job)
            except Exception as e:  # one broken run must not end the sweep
                print("RUN FAILED", job, type(e).__name__, e, flush=True)

    with ThreadPoolExecutor(a.concurrency) as ex:
        list(ex.map(guarded, jobs))


if __name__ == "__main__":
    main()
