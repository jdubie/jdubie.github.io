"""A database under test: start it, SIGKILL it, restart it on the same storage, scan that storage.

AgentBox runs the agent's server with its storage on a tmpfs volume that can hold only 4 KB of file
content in total, under a seccomp profile that blocks xattrs, symlinks, device nodes, fallocate, io_uring
and non-zero truncate, with a read-only root. A holder container keeps the volume mounted across server
kills, so a kill loses exactly what a process crash would. PgBox runs real PostgreSQL 16 the same way
(no storage rule) to produce and check the reference answers. ClientBox is a long-lived container with
the pinned real clients (psql, pgbench, psycopg, node-postgres, pgJDBC) on the same internal network.

Everything here is named pgw-*; nothing touches efsdb-* objects.
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
IMAGE = "pgw-sandbox:1"
PG_IMAGE = "postgres:16-bookworm"
NET = "pgw-grade"
SECCOMP = ROOT / "docker/sandbox/seccomp.json"
CTL = ROOT / "grader/crashpoint"  # holds the static `supervise` binary, mounted read-only at /opt/pgwctl
PGINIT = ROOT / "grader/pginit"  # init script giving PostgreSQL the same roles and auth methods the task asks for
CTL_PORT = 5499
START_TIMEOUT_S = 30
INODE_CAP = 4_000_000  # protects the host; far above anything the suites need
DATA_OPTS = "size=4k,nr_inodes={cap},mode=0700,uid={uid},gid={gid}"
SERVER_MEM = "6g"


def docker(*args: str, check: bool = True, timeout: float = 180, input: str | None = None) -> str:
    r = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout, input=input)
    if check and r.returncode:
        raise RuntimeError(f"docker {' '.join(args[:3])}: {r.stderr.strip()[-400:]}")
    return r.stdout.strip()


def ensure_network() -> None:
    if docker("network", "ls", "-q", "-f", f"name=^{NET}$") == "":
        docker("network", "create", "--internal", NET)


def _ip(name: str) -> str:
    return json.loads(docker("inspect", name))[0]["NetworkSettings"]["Networks"][NET]["IPAddress"]


def handshake(host: str, port: int, timeout: float = 3) -> bool:
    """True if a trust StartupMessage as `postgres` gets as far as ReadyForQuery."""
    import struct

    try:
        s = socket.create_connection((host, port), timeout=timeout)
    except OSError:
        return False
    try:
        body = struct.pack("!I", 196608) + b"user\0postgres\0database\0regression\0\0"
        s.sendall(struct.pack("!I", len(body) + 4) + body)
        buf = b""
        while True:
            chunk = s.recv(65536)
            if not chunk:
                return False
            buf += chunk
            while len(buf) >= 5:
                t, n = buf[:1], struct.unpack("!I", buf[1:5])[0]
                if len(buf) < 1 + n:
                    break
                if t == b"Z":
                    s.sendall(b"X\0\0\0\4")
                    return True
                if t == b"E":
                    return False
                buf = buf[1 + n:]
    except OSError:
        return False
    finally:
        s.close()


def _wait_ready(host: str, port: int, deadline: float, alive) -> bool:
    while time.time() < deadline:
        if not alive():
            return False
        if handshake(host, port):
            return True
        time.sleep(0.2)
    return False


class AgentBox:
    port = 5432
    kind = "agent"

    def __init__(self, code: Path, tag: str | None = None, supervise: bool = False, mem: str = SERVER_MEM):
        ensure_network()
        self.code = code.resolve()
        self.supervise = supervise
        self.mem = mem
        self.tag = tag or uuid.uuid4().hex[:10]
        self.vol = f"pgw-{self.tag}"
        self.hold = f"pgw-{self.tag}-hold"
        self.srv = f"pgw-{self.tag}-srv"
        self.host = ""
        self.starts = 0
        self.logs: list[str] = []
        opts = DATA_OPTS.format(cap=INODE_CAP, uid=os.getuid(), gid=os.getgid())
        docker("volume", "create", "--driver", "local", "--opt", "type=tmpfs", "--opt", "device=tmpfs", "--opt", f"o={opts}",
               "--label", "pgw=1", self.vol)
        docker("run", "-d", "--name", self.hold, "--label", "pgw=1", "--network", "none", "--cap-drop", "ALL", "--read-only",
               "--user", f"{os.getuid()}:{os.getgid()}", "-v", f"{self.vol}:/data", IMAGE, "sleep", "infinity")

    def launch(self) -> None:
        self.stop_server()
        self.starts += 1
        cmd = ["sh", "/work/start.sh"]
        extra: list[str] = []
        if self.supervise:
            cmd = ["/opt/pgwctl/supervise", str(CTL_PORT), *cmd]
            extra = ["-v", f"{CTL}:/opt/pgwctl:ro"]
        docker("run", "-d", "--name", self.srv, "--label", "pgw=1", "--init", "--network", NET, "--read-only",
               "--tmpfs", "/tmp:size=64m", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
               "--security-opt", f"seccomp={SECCOMP}", "--cpus", "4", "--memory", self.mem, "--memory-swap", self.mem,
               "--pids-limit", "1024", "--user", f"{os.getuid()}:{os.getgid()}", "-e", "HOME=/tmp", "-e", "PGDATA_DIR=/data",
               *extra, "-v", f"{self.code}:/work:ro", "-v", f"{self.vol}:/data", "-w", "/work", IMAGE, *cmd)
        self.host = _ip(self.srv)

    def start(self, timeout: float = START_TIMEOUT_S) -> bool:
        """Start the server; True once it completes a trust handshake within `timeout`."""
        self.launch()
        return _wait_ready(self.host, self.port, time.time() + timeout, self.alive)

    def alive(self) -> bool:
        return docker("inspect", "-f", "{{.State.Running}}", self.srv, check=False) == "true"

    def kill(self) -> None:
        """SIGKILL every process in the server container (a crash), keeping the storage."""
        docker("kill", "-s", "KILL", self.srv, check=False)
        self.stop_server()

    def stop_server(self) -> None:
        out = docker("logs", "--tail", "60", self.srv, check=False)
        if out:
            self.logs.append(out[-4000:])
        docker("rm", "-f", self.srv, check=False)

    def restart(self, timeout: float = START_TIMEOUT_S) -> bool:
        self.kill()
        return self.start(timeout)

    def scan(self) -> dict:
        try:
            return json.loads(docker("exec", self.hold, "python3", "/opt/efs/scan.py", "/data", str(INODE_CAP), timeout=600))
        except Exception as e:  # noqa: BLE001 - a scan that can't run is reported, not fatal
            return {"ok": True, "violations": [], "entries": -1, "scan_error": str(e)[:200]}

    def close(self) -> None:
        self.stop_server()
        docker("rm", "-f", self.hold, check=False)
        docker("volume", "rm", "-f", self.vol, check=False)


class PgBox:
    """Real PostgreSQL 16 in the same harness: the reference, and the calibration target."""

    port = 5432
    kind = "postgres"

    def __init__(self, tag: str | None = None, supervise: bool = False, mem: str = SERVER_MEM, fsync: bool = True):
        ensure_network()
        self.supervise = supervise
        self.mem = mem
        self.fsync = fsync
        self.tag = tag or uuid.uuid4().hex[:10]
        self.vol = f"pgw-pg-{self.tag}"
        self.srv = f"pgw-pg-{self.tag}-srv"
        self.host = ""
        self.starts = 0
        self.logs: list[str] = []
        docker("volume", "create", "--label", "pgw=1", self.vol)

    def launch(self) -> None:
        self.stop_server()
        self.starts += 1
        sup: list[str] = []
        cmd = ["postgres", "-c", "max_connections=300", "-c", f"fsync={'on' if self.fsync else 'off'}",
               "-c", "max_prepared_transactions=0", "-c", "listen_addresses=*"]
        if self.supervise:  # wraps the image's entrypoint, so it sees initdb, gosu and every backend
            sup = ["-v", f"{CTL}:/opt/pgwctl:ro", "--entrypoint", "/opt/pgwctl/supervise"]
            cmd = [str(CTL_PORT), "docker-entrypoint.sh", *cmd]
        docker("run", "-d", "--name", self.srv, "--label", "pgw=1", "--network", NET,
               "-e", "POSTGRES_HOST_AUTH_METHOD=trust", "-e", "POSTGRES_USER=postgres", "-e", "POSTGRES_DB=regression",
               "-e", "POSTGRES_INITDB_ARGS=--locale=C --encoding=UTF8", "-v", f"{PGINIT}:/docker-entrypoint-initdb.d:ro",
               "--cpus", "4", "--memory", self.mem, "--shm-size", "1g", "-v", f"{self.vol}:/var/lib/postgresql/data", *sup,
               PG_IMAGE, *cmd)
        self.host = _ip(self.srv)

    def start(self, timeout: float = 90) -> bool:
        self.launch()
        deadline = time.time() + timeout
        # the image's init runs a temporary server first; wait until the real one answers twice
        ok = _wait_ready(self.host, self.port, deadline, self.alive)
        time.sleep(1.5)
        return ok and _wait_ready(self.host, self.port, deadline, self.alive)

    def alive(self) -> bool:
        return docker("inspect", "-f", "{{.State.Running}}", self.srv, check=False) == "true"

    def kill(self) -> None:
        docker("kill", "-s", "KILL", self.srv, check=False)
        self.stop_server()

    def stop_server(self) -> None:
        docker("rm", "-f", self.srv, check=False)

    def restart(self, timeout: float = 90) -> bool:
        self.kill()
        return self.start(timeout)

    def scan(self) -> dict:
        return {"ok": True, "violations": [], "entries": -1}

    def close(self) -> None:
        self.stop_server()
        docker("volume", "rm", "-f", self.vol, check=False)


class ClientBox:
    """Long-lived container with the pinned real clients; commands run via `docker exec`."""

    def __init__(self, tag: str | None = None):
        ensure_network()
        self.name = f"pgw-cli-{tag or uuid.uuid4().hex[:10]}"
        docker("run", "-d", "--name", self.name, "--label", "pgw=1", "--network", NET, "--read-only",
               "--tmpfs", "/tmp:size=2g,exec", "--cap-drop", "ALL", "--cpus", "4", "--memory", "4g",
               "--user", f"{os.getuid()}:{os.getgid()}", "-e", "HOME=/tmp",
               "-v", f"{ROOT / 'grader/clients'}:/clients:ro",
               "-v", f"{ROOT / 'pgsrc/postgresql-16.16/src/test/regress'}:/regress:ro",
               "-v", f"{ROOT / 'pgsrc/postgresql-16.16/src/test/regress'}:/work/public/regress:ro",  # the agent kit's path
               "-w", "/tmp", IMAGE, "sleep", "infinity")

    def run(self, argv: list[str], timeout: float, env: dict[str, str] | None = None, input: str | None = None) -> tuple[int, str, str]:
        """(exit status, stdout, stderr); status 124 on timeout (the process is killed inside the container)."""
        e = [x for k, v in (env or {}).items() for x in ("-e", f"{k}={v}")]
        full = ["docker", "exec", "-i", *e, self.name,
                "timeout", "-s", "KILL", str(int(timeout)), *argv]
        try:
            r = subprocess.run(full, capture_output=True, text=True, timeout=timeout + 30, input=input or "")
        except subprocess.TimeoutExpired:
            return 124, "", "harness timeout"
        return (124 if r.returncode in (124, 137) else r.returncode), r.stdout, r.stderr

    def popen(self, argv: list[str], env: dict[str, str] | None = None) -> subprocess.Popen:
        e = [x for k, v in (env or {}).items() for x in ("-e", f"{k}={v}")]
        return subprocess.Popen(["docker", "exec", "-i", *e, self.name, *argv], stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT)

    def close(self) -> None:
        docker("rm", "-f", self.name, check=False)


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


def cleanup_all() -> None:
    """Remove every pgw-labelled container and volume (after an interrupted run)."""
    ids = docker("ps", "-aq", "-f", "label=pgw=1", check=False).split()
    if ids:
        docker("rm", "-f", *ids, check=False)
    vols = docker("volume", "ls", "-q", "-f", "label=pgw=1", check=False).split()
    if vols:
        docker("volume", "rm", "-f", *vols, check=False)
