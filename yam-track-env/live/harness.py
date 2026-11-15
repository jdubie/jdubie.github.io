"""Run coding agents on the tracking task: the practice arm in one container, the agent in another.

  python live/harness.py run --version v1 --models opus,sonnet,haiku,codexsol,codexterra --reps 2 --budget-min 20 --tag v1-sweep1

Each run: env.py reset (robot seed 100+rep, so every model on rep r faces the same arm), start the arm, give the
agent the workspace and the socket, stop it at the budget, then grade: v1 from the best trial of each eval
reference in the server log, v2 by running work/controller.py on the hidden eval references.
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
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import env as task  # noqa: E402

IMAGE = "yamstack-sandbox:1"
NET, PROXY = "cadenv-agents", "cadenv-proxy"
CLAUDE_BIN = Path.home() / ".local/share/claude/versions/2.1.317"
MODELS = {"opus": "claude-opus-5-5", "sonnet": "claude-sonnet-5-5", "haiku": "claude-haiku-5-5"}
GEMINI_MODELS = {"gemini": "gemini-4-argon"}
GEMINI_IMAGE = "yamstack-gemini:1"
GEMINI_KEY_FILE = Path.home() / ".config/gemini"
# The whole vendor tree: codex finds codex-code-mode-host (its command tool), rg and bwrap beside itself.
CODEX_VENDOR = Path(
    "/home/robot/.nvm/versions/node/v24.7.0/lib/node_modules/@openai/codex/node_modules/"
    "@openai/codex-linux-x64/vendor/x86_64-unknown-linux-musl"
)
# Pinned model and reasoning effort, so every Codex run says exactly what it ran. Never the CLI's unpinned default.
CODEX_MODELS = {"codexsol": ("gpt-6.1-sol", "high"), "codexterra": ("gpt-6.1-terra", "high")}
# Grok CLI is one static binary. high is its default effort for grok-4.7; pinned so the run says what it ran.
GROK_MODELS = {"grok": ("grok-4.7", "high")}
GROK_BIN = (Path.home() / ".grok/bin/grok").resolve()
GROK_KEY_FILE = Path.home() / ".grok-key"
GROK_PROXY = "yamstack-grok-proxy"  # same CONNECT-only proxy, allowlist {api.x.ai}: docker/grok/proxy.py
# Stop a Grok run whose estimated spend passes this. Rates fitted to its own reported cost: $/token uncached, cached, output.
GROK_RUN_CAP_USD = 10.0
GROK_RATES = (2e-6, 0.5e-6, 10e-6)
LAUNCH = "Read PROMPT.md in the current directory and do what it says."
TOKEN_MARGIN_MIN = 5



def docker_base(name: str) -> list[str]:
    return ["docker", "run", "--rm", "--init", "--name", name, "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
            "--read-only", "--user", f"{os.getuid()}:{os.getgid()}"]


def start_sim(run: Path, name: str) -> subprocess.Popen:
    log = open(run / "sim.log", "wb")
    proc = subprocess.Popen(task.serve_argv(run, name), stdout=log, stderr=log)
    for _ in range(300):
        if (run / "sock" / "sim.sock").exists():
            return proc
        if proc.poll() is not None:
            raise RuntimeError("sim exited at startup: " + (run / "sim.log").read_text()[-500:])
        time.sleep(0.1)
    raise RuntimeError("sim did not open its socket")


def stop_sim(name: str, proc: subprocess.Popen) -> None:
    subprocess.run(["docker", "stop", "-t", "5", name], capture_output=True)
    proc.wait(timeout=30)


def client_argv(name: str, work: Path, run: Path, network: str = "none") -> list[str]:
    """Container for whatever drives the robot: no sim code, no private output, just the socket."""
    argv = docker_base(name) + ["--network", network, "--tmpfs", "/tmp:size=4g,exec", "--memory", "8g", "--memory-swap", "8g", "--pids-limit", "2048"]
    return argv + ["-v", f"{work.resolve()}:/work", "-v", f"{run.resolve()}/sock:/sim", "-w", "/work", "-e", "HOME=/tmp"]


def claude_token(budget_min: int) -> str:
    """Current access token, valid for the whole run. The refresh token never enters a container.

    If it is about to expire, wait for that and let the Claude CLI on the host renew it in the normal way.
    """
    path = Path.home() / ".claude/.credentials.json"
    needed = budget_min + TOKEN_MARGIN_MIN
    while True:
        o = json.loads(path.read_text())["claudeAiOauth"]
        left = (o["expiresAt"] / 1000 - time.time()) / 60
        if left >= needed:
            return o["accessToken"]
        if left <= 0.5:
            with tempfile.TemporaryDirectory() as tmp:
                subprocess.run(["claude", "-p", "Reply with the single word ok", "--model", MODELS["haiku"]], cwd=tmp, capture_output=True, timeout=120)
        time.sleep(20)


def grok_cost(transcript: Path, home: Path | None = None) -> tuple[float, str]:
    """Grok's spend in USD and where the number came from.

    Prefers the CLI's own total_cost_usd (the stream's end event), then its persisted usage.json,
    and otherwise estimates from the per-call usage events with GROK_RATES.
    """
    est = 0.0
    reported = None
    try:
        lines = transcript.read_bytes().splitlines()
    except FileNotFoundError:
        lines = []
    for line in lines:
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if d.get("type") == "usage":
            u = d["usage"]
            est += u["input_tokens"] * GROK_RATES[0] + u.get("cache_read_input_tokens", 0) * GROK_RATES[1] + u["output_tokens"] * GROK_RATES[2]
        elif d.get("type") == "end" and "total_cost_usd" in d:
            reported = d["total_cost_usd"]
    if reported is not None:
        return reported, "reported"
    if home is not None:
        ticks = sum(json.loads(p.read_text())["session"]["costUsdTicks"] for p in home.glob(".grok/sessions/*/*/usage.json"))
        if ticks:
            return ticks / 1e10, "usage.json"
    return est, "estimated"


def run_agent(tag: str, version: str, model: str, rep: int, budget_min: int) -> dict:
    run = ROOT / "runs" / "live" / tag / f"{model}__r{rep}"
    if (run / "meta.json").exists():
        return json.loads((run / "meta.json").read_text())
    if run.exists():
        shutil.rmtree(run)
    run.parent.mkdir(parents=True, exist_ok=True)
    seed = 100 + rep
    task.reset(run, seed, version, budget_min)
    work = run / "work"
    sim_name, agent_name = f"yamtrack-sim-{tag}-{model}-{rep}", f"yamtrack-agent-{tag}-{model}-{rep}"
    secret = {}
    if model in MODELS:
        secret["CLAUDE_CODE_OAUTH_TOKEN"] = claude_token(budget_min)
    elif model in GEMINI_MODELS:
        secret["GEMINI_API_KEY"] = GEMINI_KEY_FILE.read_text().strip()
    elif model in GROK_MODELS:
        secret["XAI_API_KEY"] = GROK_KEY_FILE.read_text().strip()
    sim = start_sim(run, sim_name)
    t0 = time.time()
    with tempfile.TemporaryDirectory(dir=run) as tmp:
        passwd = Path(tmp).resolve() / "passwd"
        passwd.write_text(f"agent:x:{os.getuid()}:{os.getgid()}::/tmp:/bin/sh\n")
        proxy = f"http://{GROK_PROXY if model in GROK_MODELS else PROXY}:3128"
        argv = client_argv(agent_name, work, run, NET) + ["-v", f"{passwd}:/etc/passwd:ro"]
        for var in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
            argv += ["-e", f"{var}={proxy}"]
        argv += [x for k in secret for x in ("-e", k)]  # secrets travel by name only, never on the command line
        if model in MODELS:
            argv += ["-v", f"{CLAUDE_BIN}:/usr/local/bin/claude:ro", "-e", "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1", "-e", "DISABLE_AUTOUPDATER=1", IMAGE]
            argv += ["claude", "-p", LAUNCH, "--model", MODELS[model], "--dangerously-skip-permissions"]
            argv += ["--output-format", "stream-json", "--verbose", "--disallowedTools", "WebSearch,WebFetch"]
        elif model in GEMINI_MODELS:
            argv += ["-e", "GEMINI_CLI_TRUST_WORKSPACE=true", GEMINI_IMAGE, "gemini", "-p", LAUNCH, "-m", GEMINI_MODELS[model]]
            argv += ["--approval-mode", "yolo", "--output-format", "stream-json"]
        elif model in CODEX_MODELS:
            auth = json.loads((Path.home() / ".codex/auth.json").read_text())
            auth["tokens"]["refresh_token"] = ""  # the container can use the session but never rotate it
            codex_home = Path(tmp).resolve() / "codex"
            codex_home.mkdir()
            (codex_home / "auth.json").write_text(json.dumps(auth))
            (codex_home / "auth.json").chmod(0o600)
            argv += ["-e", "CODEX_HOME=/tmp/codex", "-v", f"{codex_home}:/tmp/codex", "-v", f"{CODEX_VENDOR}:/opt/codex:ro", IMAGE]
            argv += ["/opt/codex/bin/codex", "exec", "--json", "--skip-git-repo-check", "--dangerously-bypass-approvals-and-sandbox"]
            name_, effort = CODEX_MODELS[model]
            argv += ["-m", name_, "-c", f'model_reasoning_effort="{effort}"']
            argv += ["-c", 'web_search="disabled"', "-C", "/work", LAUNCH]
        elif model in GROK_MODELS:
            # HOME persists in the run dir so usage.json (its own cost record) survives a killed run. The key is never written there.
            grok_home = run / "grokhome"
            grok_home.mkdir()
            name_, effort = GROK_MODELS[model]
            argv += ["-v", f"{GROK_BIN}:/usr/local/bin/grok:ro", "-v", f"{grok_home.resolve()}:/tmp/grokhome", "-e", "HOME=/tmp/grokhome"]
            argv += ["-e", "DISABLE_TELEMETRY=1", "-e", "DISABLE_ERROR_REPORTING=1", IMAGE]
            argv += ["grok", "-p", LAUNCH, "-m", name_, "--reasoning-effort", effort, "--always-approve"]
            argv += ["--permission-mode", "bypassPermissions", "--disable-web-search", "--output-format", "streaming-json"]
        else:
            raise ValueError(f"unknown model {model}")
        env = {**os.environ, **secret}
        with open(run / "transcript.jsonl", "wb") as out, open(run / "stderr.log", "wb") as err:
            proc = subprocess.Popen(argv, stdout=out, stderr=err, env=env)
            killed = over_cap = False
            while proc.poll() is None:
                if model in GROK_MODELS and not killed and grok_cost(run / "transcript.jsonl")[0] > GROK_RUN_CAP_USD:
                    over_cap = True
                if (time.time() - t0 > budget_min * 60 + 20 or over_cap) and not killed:
                    # SIGTERM first for Grok, so it can still write its final cost; then kill.
                    subprocess.run(["docker", "stop", "-t", "15", agent_name] if model in GROK_MODELS else ["docker", "kill", agent_name], capture_output=True)
                    killed = True
                time.sleep(1)
    seconds = round(time.time() - t0, 1)
    stop_sim(sim_name, sim)
    result = task.grade(run)
    (run / "grade.json").write_text(json.dumps(result, indent=1))
    result = {k: result.get(k) for k in ("score_mm", "per_category_mm", "per_test_mm", "missing", "trials")} | {
        "statuses": sorted({r["status"] for r in result.get("runs", [])})}
    pinned = CODEX_MODELS.get(model) or GEMINI_MODELS.get(model) or GROK_MODELS.get(model) or MODELS.get(model)
    meta = {"model": model, "version": version, "pinned": pinned, "rep": rep, "seed": seed, "budget_min": budget_min, "seconds": seconds, "exit": proc.returncode, "timed_out": killed, **result}
    if model in GROK_MODELS:
        meta["cost_usd"], meta["cost_source"] = grok_cost(run / "transcript.jsonl", run / "grokhome")
        meta["over_cap"] = over_cap
    (run / "meta.json").write_text(json.dumps(meta, indent=1))
    print(json.dumps({k: meta[k] for k in ("model", "version", "rep", "seed", "seconds", "exit", "timed_out", "score_mm", "missing", "statuses", "cost_usd") if k in meta}), flush=True)
    return meta


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--version", choices=["v1", "v2"], required=True)
    r.add_argument("--models", required=True)
    r.add_argument("--reps", type=int, default=3)
    r.add_argument("--budget-min", type=int, default=45)
    r.add_argument("--concurrency", type=int, default=3)
    r.add_argument("--tag", required=True)
    a = ap.parse_args()
    jobs = [(a.tag, a.version, m, k, a.budget_min) for k in range(a.reps) for m in a.models.split(",")]
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
