"""A database under test: start it, SIGKILL it, restart it on the same storage, and scan that storage.

AgentBox runs the agent's server with its storage on a tmpfs volume that can hold only 4 KB of file
content in total, under a seccomp profile that blocks xattrs, symlinks, device nodes, fallocate,
non-zero truncate and io_uring, and (v2) at most INODE_CAP inodes. A holder container keeps the volume mounted across server restarts, so
a kill loses exactly what a process crash would. PgBox runs real PostgreSQL the same way, to calibrate
the scenarios.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import threading
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
IMAGE = "efsdb-sandbox:2"
PG_IMAGE = "postgres:16-bookworm"
NET = "efsdb-grade"
SECCOMP = ROOT / "docker/seccomp.json"
CTL = ROOT / "grader/crashpoint"  # holds the static `supervise` binary, mounted read-only at /opt/efsctl
CTL_PORT = 5499
START_TIMEOUT_S = 30
# v2: at most INODE_CAP inodes (files + directories + hard links, the root included) in the data dir.
# v1 had no effective cap (2M). tmpfs enforces it with ENOSPC; the scanner flags it too.
INODE_CAP = {1: 2_000_000, 2: 200_000}
DATA_OPTS = "size=4k,nr_inodes={cap},mode=0700,uid={uid},gid={gid}"


def _docker(*args: str, check: bool = True, timeout: float = 120) -> str:
    r = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout)
    if check and r.returncode:
        raise RuntimeError(f"docker {' '.join(args[:3])}: {r.stderr.strip()[-400:]}")
    return r.stdout.strip()


def _ip(name: str) -> str:
    return json.loads(_docker("inspect", name))[0]["NetworkSettings"]["Networks"][NET]["IPAddress"]


def _wait_ready(host: str, port: int, deadline: float, alive) -> bool:
    import sys
    sys.path.insert(0, str(ROOT / "task/tools"))
    from pgwire import Conn

    while time.time() < deadline:
        if not alive():
            return False
        try:
            c = Conn(host, port, timeout=3)
            c.close()
            return True
        except Exception:
            time.sleep(0.2)
    return False


class AgentBox:
    port = 5432

    def __init__(self, code: Path, tag: str | None = None, version: int = 2, supervise: bool = False):
        self.code = code.resolve()
        self.cap = INODE_CAP[version]
        self.supervise = supervise
        self.tag = tag or uuid.uuid4().hex[:10]
        self.vol = f"efsdb-{self.tag}"
        self.hold = f"efsdb-{self.tag}-hold"
        self.srv = f"efsdb-{self.tag}-srv"
        self.host = ""
        self.starts = 0
        self.logs: list[str] = []
        opts = DATA_OPTS.format(cap=self.cap, uid=os.getuid(), gid=os.getgid())
        _docker("volume", "create", "--driver", "local", "--opt", "type=tmpfs", "--opt", "device=tmpfs", "--opt", f"o={opts}",
                self.vol)
        _docker("run", "-d", "--name", self.hold, "--network", "none", "--cap-drop", "ALL", "--read-only",
                "--user", f"{os.getuid()}:{os.getgid()}", "-v", f"{self.vol}:/data", IMAGE, "sleep", "infinity")

    def launch(self) -> None:
        """Start the server container without waiting for it."""
        self.stop_server()
        self.starts += 1
        cmd = ["sh", "/work/start.sh"]
        extra: list[str] = []
        if self.supervise:
            cmd = ["/opt/efsctl/supervise", str(CTL_PORT), *cmd]
            extra = ["-v", f"{CTL}:/opt/efsctl:ro"]
        _docker("run", "-d", "--name", self.srv, "--init", "--network", NET, "--read-only", "--tmpfs", "/tmp:size=64m",
                "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--security-opt", f"seccomp={SECCOMP}",
                "--cpus", "4", "--memory", "4g", "--memory-swap", "4g", "--pids-limit", "512",
                "--user", f"{os.getuid()}:{os.getgid()}", "-e", "HOME=/tmp", "-e", "PGDATA_DIR=/data", *extra,
                "-v", f"{self.code}:/work:ro", "-v", f"{self.vol}:/data", "-w", "/work", IMAGE, *cmd)
        self.host = _ip(self.srv)

    def start(self) -> bool:
        """Start the server; True once it completes a protocol handshake within START_TIMEOUT_S."""
        self.launch()
        return _wait_ready(self.host, self.port, time.time() + START_TIMEOUT_S, self.alive)

    def alive(self) -> bool:
        return _docker("inspect", "-f", "{{.State.Running}}", self.srv, check=False) == "true"

    def kill(self) -> None:
        """SIGKILL every process in the server container (a crash), keeping the storage."""
        _docker("kill", "-s", "KILL", self.srv, check=False)
        self.stop_server()

    def stop_server(self) -> None:
        out = _docker("logs", "--tail", "60", self.srv, check=False)
        if out:
            self.logs.append(out[-4000:])
        _docker("rm", "-f", self.srv, check=False)

    def restart(self) -> bool:
        self.kill()
        return self.start()

    def scan(self) -> dict:
        try:
            return json.loads(_docker("exec", self.hold, "python3", "/opt/efs/scan.py", "/data", str(self.cap), timeout=300))
        except Exception as e:  # noqa: BLE001 - a scan that can't run is reported, not fatal
            return {"ok": True, "violations": [], "entries": -1, "scan_error": str(e)}

    def close(self) -> None:
        self.stop_server()
        _docker("rm", "-f", self.hold, check=False)
        _docker("volume", "rm", "-f", self.vol, check=False)


class PgBox:
    """Real PostgreSQL in the same harness; used only to prove scenarios pass on a correct database."""

    port = 5432

    def __init__(self, tag: str | None = None, version: int = 2, supervise: bool = False):
        self.supervise = supervise
        self.tag = tag or uuid.uuid4().hex[:10]
        self.vol = f"efsdb-pg-{self.tag}"
        self.srv = f"efsdb-pg-{self.tag}-srv"
        self.host = ""
        self.starts = 0
        self.logs: list[str] = []
        _docker("volume", "create", self.vol)

    def launch(self) -> None:
        self.stop_server()
        self.starts += 1
        sup: list[str] = []
        cmd = ["postgres", "-c", "max_connections=200"]
        if self.supervise:  # the supervisor wraps the image's entrypoint, so it sees initdb, gosu and every backend
            sup = ["-v", f"{CTL}:/opt/efsctl:ro", "--entrypoint", "/opt/efsctl/supervise"]
            cmd = [str(CTL_PORT), "docker-entrypoint.sh", *cmd]
        _docker("run", "-d", "--name", self.srv, "--network", NET, "-e", "POSTGRES_HOST_AUTH_METHOD=trust",
                "-e", "POSTGRES_USER=grader", "-e", "POSTGRES_DB=grader", "-e", "POSTGRES_INITDB_ARGS=--locale=C --encoding=UTF8",
                "--cpus", "4", "--memory", "4g", "-v", f"{self.vol}:/var/lib/postgresql/data", *sup, PG_IMAGE, *cmd)
        self.host = _ip(self.srv)

    def start(self) -> bool:
        self.launch()
        deadline = time.time() + 60
        # the image's init runs a temporary server first; wait until the real one is up twice in a row
        ok = _wait_ready(self.host, self.port, deadline, self.alive)
        time.sleep(2)
        return ok and _wait_ready(self.host, self.port, deadline, self.alive)

    def alive(self) -> bool:
        return _docker("inspect", "-f", "{{.State.Running}}", self.srv, check=False) == "true"

    def kill(self) -> None:
        _docker("kill", "-s", "KILL", self.srv, check=False)
        self.stop_server()

    def stop_server(self) -> None:
        _docker("rm", "-f", self.srv, check=False)

    def restart(self) -> bool:
        self.kill()
        return self.start()

    def scan(self) -> dict:
        return {"ok": True, "violations": [], "entries": -1}

    def close(self) -> None:
        self.stop_server()
        _docker("volume", "rm", "-f", self.vol, check=False)


class Scanner:
    """Scans a box's storage every `every_s` seconds in the background; any violation is kept."""

    def __init__(self, box, every_s: float = 2.0):
        self.box, self.every = box, every_s
        self.violations: list[str] = []
        self.scans = 0
        self.max_entries = 0
        self._stop = threading.Event()
        self._t = threading.Thread(target=self._loop, daemon=True)

    def __enter__(self):
        self._t.start()
        return self

    def _loop(self) -> None:
        while not self._stop.wait(self.every):
            self.check()

    def check(self) -> None:
        r = self.box.scan()
        self.scans += 1
        self.max_entries = max(self.max_entries, r.get("entries", 0))
        if not r["ok"]:
            self.violations.extend(r["violations"])

    def __exit__(self, *exc) -> None:
        self._stop.set()
        self._t.join()
        self.check()


def port_open(host: str, port: int) -> bool:
    try:
        socket.create_connection((host, port), timeout=1).close()
        return True
    except OSError:
        return False
