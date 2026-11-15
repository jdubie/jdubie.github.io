"""Local stand-ins for the grader's sandbox objects, so the grader's own suite code runs unchanged here.

LocalBox runs `sh start.sh` from /work on a fresh data directory (no tmpfs limit, no seccomp, no supervisor);
LocalCli runs the client programs directly instead of in the grader's client container.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

WORK = Path(__file__).resolve().parent.parent
SCAN = "/opt/efs/scan.py"
CTL_PORT = 5499


def handshake(host: str, port: int, timeout: float = 3) -> bool:
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


class LocalBox:
    host, port = "127.0.0.1", 5432
    kind = "local"

    def __init__(self):
        self.data = Path(tempfile.mkdtemp(prefix="pgw-public-"))
        self.proc: subprocess.Popen | None = None
        self.starts = 0
        self.logpath = self.data.parent / f"{self.data.name}.log"
        self.log = open(self.logpath, "ab")
        self.cli = LocalCli()

    def launch(self) -> None:
        self.kill()
        self.starts += 1
        self.proc = subprocess.Popen(["sh", "start.sh"], cwd=WORK, env={**os.environ, "PGDATA_DIR": str(self.data)},
                                     stdout=self.log, stderr=self.log, start_new_session=True)

    def start(self, timeout: float = 30) -> bool:
        self.launch()
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.proc.poll() is not None:
                return False
            if handshake(self.host, self.port):
                return True
            time.sleep(0.2)
        return False

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def kill(self) -> None:
        if self.proc is not None:
            try:
                os.killpg(self.proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            self.proc.wait()
            self.proc = None
            time.sleep(0.1)

    def restart(self, timeout: float = 30) -> bool:
        return self.start(timeout)

    def scan(self) -> dict:
        r = subprocess.run([sys.executable, SCAN, str(self.data)], capture_output=True, text=True)
        return json.loads(r.stdout)

    def close(self) -> None:
        self.kill()
        shutil.rmtree(self.data, ignore_errors=True)


class LocalCli:
    def run(self, argv, timeout, env=None, input=None):
        try:
            r = subprocess.run(["timeout", "-s", "KILL", str(int(timeout)), *argv], capture_output=True, text=True,
                               env={**os.environ, **(env or {})}, input=input or "", timeout=timeout + 30, cwd="/tmp")
        except subprocess.TimeoutExpired:
            return 124, "", "timeout"
        return (124 if r.returncode in (124, 137) else r.returncode), r.stdout, r.stderr

    def popen(self, argv, env=None):
        return subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                env={**os.environ, **(env or {})}, cwd="/tmp")


class Scanner:
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

    def _loop(self):
        while not self._stop.wait(self.every):
            self.check()

    def check(self):
        r = self.box.scan()
        self.scans += 1
        self.max_entries = max(self.max_entries, r.get("entries", 0))
        if not r["ok"]:
            self.violations.extend(r["violations"])

    def __exit__(self, *exc):
        self._stop.set()
        self._t.join()
        self.check()
