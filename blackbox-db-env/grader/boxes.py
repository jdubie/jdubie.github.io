"""Servers under test, all on the internal network bbx-grade, all listening on port 7000.

CandBox    a submission: `sh /work/start.sh` in bbx-sandbox, /work read-only, data in a volume at $DATA_DIR
OracleBox  bbx-oracle (PostgreSQL 16 + proxy) for a seed and set of layers: the reference, and calibrations
PgBox      stock PostgreSQL 16 on port 7000: the "memorised PostgreSQL" calibration
A SIGKILL (`kill`) keeps the data volume, so a restart sees exactly what a crash leaves. Everything is bbx-*.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
NET = "bbx-grade"
PORT = 7000
SANDBOX, PG = "bbx-sandbox:1", "postgres:16-bookworm"
# oracle image version: 1 = as Opus saw it (no discovery hints), 2 = errors name startup keys and renamed words
ORACLE_VERSION = int(os.environ.get("BBX_ORACLE_VERSION", "2"))
ORACLE = f"bbx-oracle:{ORACLE_VERSION}"
MEM = "6g"


def docker(*args: str, check: bool = True, timeout: float = 180) -> str:
    r = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout)
    if check and r.returncode:
        raise RuntimeError(f"docker {' '.join(args[:3])}: {r.stderr.strip()[-400:]}")
    return r.stdout.strip()


def ensure_network() -> None:
    if docker("network", "ls", "-q", "-f", f"name=^{NET}$") == "":
        docker("network", "create", "--internal", "--label", "bbx=1", NET)


def port_open(host: str, port: int = PORT) -> bool:
    try:
        socket.create_connection((host, port), timeout=1).close()
        return True
    except OSError:
        return False


class Box:
    kind = "box"
    start_timeout = 30.0

    def __init__(self, tag: str | None = None):
        ensure_network()
        self.tag = tag or uuid.uuid4().hex[:10]
        self.vol = f"bbx-{self.tag}"
        self.srv = f"bbx-{self.tag}-srv"
        self.host = ""
        self.starts = 0
        self.logs: list[str] = []
        docker("volume", "create", "--label", "bbx=1", self.vol)

    def run_args(self) -> list[str]:
        raise NotImplementedError

    def start(self) -> bool:
        self.stop_server()
        self.starts += 1
        docker("run", "-d", "--name", self.srv, "--label", "bbx=1", "--init", "--network", NET, *self.run_args())
        self.host = json.loads(docker("inspect", self.srv))[0]["NetworkSettings"]["Networks"][NET]["IPAddress"]
        deadline = time.time() + self.start_timeout
        while time.time() < deadline:
            if not self.alive():
                return False
            if port_open(self.host):
                return True
            time.sleep(0.2)
        return False

    def alive(self) -> bool:
        return docker("inspect", "-f", "{{.State.Running}}", self.srv, check=False) == "true"

    def kill(self) -> None:
        docker("kill", "-s", "KILL", self.srv, check=False)
        self.stop_server()

    def stop_server(self) -> None:
        out = docker("logs", "--tail", "40", self.srv, check=False)
        if out:
            self.logs.append(out[-3000:])
        docker("rm", "-f", self.srv, check=False)

    def restart(self) -> bool:
        self.kill()
        return self.start()

    def close(self) -> None:
        self.stop_server()
        docker("volume", "rm", "-f", self.vol, check=False)


class CandBox(Box):
    kind = "candidate"

    def __init__(self, code: Path, tag: str | None = None):
        self.code = code.resolve()
        super().__init__(tag)
        # the data volume belongs to the server's uid
        docker("run", "--rm", "--label", "bbx=1", "--network", "none", "-v", f"{self.vol}:/data", SANDBOX,
               "chown", f"{os.getuid()}:{os.getgid()}", "/data")

    def run_args(self) -> list[str]:
        return ["--read-only", "--tmpfs", "/tmp:size=256m", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
                "--cpus", "4", "--memory", MEM, "--memory-swap", MEM, "--pids-limit", "1024",
                "--user", f"{os.getuid()}:{os.getgid()}", "-e", "HOME=/tmp", "-e", "DATA_DIR=/data",
                "-v", f"{self.code}:/work:ro", "-v", f"{self.vol}:/data", "-w", "/work", SANDBOX, "sh", "/work/start.sh"]


class OracleBox(Box):
    kind = "oracle"
    start_timeout = 90.0

    def __init__(self, seed: int, layers: str = "wire,dialect,semantics", tag: str | None = None):
        self.seed, self.layers = seed, layers
        super().__init__(tag)

    def run_args(self) -> list[str]:
        return ["-e", f"BBX_SEED={self.seed}", "-e", f"BBX_LAYERS={self.layers}", "--cpus", "4", "--memory", MEM,
                "--shm-size", "512m", "-v", f"{self.vol}:/var/lib/postgresql/data", ORACLE]


class PgBox(Box):
    kind = "postgres"
    start_timeout = 90.0

    def run_args(self) -> list[str]:
        return ["-e", "POSTGRES_HOST_AUTH_METHOD=trust", "-e", "POSTGRES_DB=main", "-e", "POSTGRES_INITDB_ARGS=--locale=C --encoding=UTF8",
                "--cpus", "4", "--memory", MEM, "--shm-size", "512m", "-v", f"{self.vol}:/var/lib/postgresql/data", PG,
                "postgres", "-p", str(PORT), "-c", "listen_addresses=*"]

    def start(self) -> bool:
        ok = super().start()
        time.sleep(3)  # the image's init runs a temporary server first
        return ok and self.alive()


def cleanup_all() -> None:
    ids = docker("ps", "-aq", "-f", "label=bbx=1", check=False).split()
    if ids:
        docker("rm", "-f", *ids, check=False)
    vols = docker("volume", "ls", "-q", "-f", "label=bbx=1", check=False).split()
    if vols:
        docker("volume", "rm", "-f", *vols, check=False)
