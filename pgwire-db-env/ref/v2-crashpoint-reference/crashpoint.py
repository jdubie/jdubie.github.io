"""Commit-window crash points: SIGKILL the server at the Nth mutating syscall of a commit, then check.

The server runs under `crashpoint/supervise`, a seccomp user-notification supervisor that counts every
filesystem-mutating syscall (create, mkdir, link, rename, unlink, rmdir, chmod, utime*, truncate,
write/pwrite/writev, fsync/fdatasync/syncfs) across all threads and processes in one global order, and
SIGKILLs the whole container when the count reaches N, before syscall N runs.

For each scenario:
  1. control: arm with N = 0 (count only), run the commit window, read M = syscalls in the window
     (plus 0.5 s after it, so work done after the ack is covered), crash, restart, check the result;
  2. kill points: every N in 1..M, or MAX_POINTS evenly spaced ones when M is larger; each on a fresh
     server: set up, arm N, run the window, wait for the kill, restart, check.
A check passes when the state after recovery is exactly the before- or the after-state of the window
(for a sequence of commits: exactly a prefix of it), and every acknowledged commit is fully present.
score = mean over scenarios of passed / tried; a scenario whose control fails scores 0.
"""

from __future__ import annotations

import socket
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "task/tools"))
sys.path.insert(0, str(ROOT / "grader"))
from dbbox import CTL_PORT, Scanner  # noqa: E402
from pgwire import Conn  # noqa: E402

MAX_POINTS = 24
SETTLE_S = 0.5
KILL_WAIT_S = 8.0
T = 15.0


class Fail(Exception):
    pass


class Ctl:
    def __init__(self, host: str):
        self.s = socket.create_connection((host, CTL_PORT), timeout=5)
        self.f = self.s.makefile("rb")

    def cmd(self, line: str) -> str:
        self.s.sendall(line.encode() + b"\n")
        return self.f.readline().decode().strip()

    def close(self) -> None:
        self.s.close()


def ex(c: Conn, sql: str):
    r = c.one(sql)
    if r.kind == "error":
        raise Fail(f"{sql[:70]!r} -> {r.sqlstate} {r.message[:100]}")
    return r.rows


def pad(i: int, n: int = 60) -> str:
    return (f"row{i}-" + "abcdefghijklmnopqrstuvwxyz0123456789" * 8)[:n]


# ---------------------------------------------------------------- scenarios
# Each: setup(c) on a fresh server; window(c, acked: list) runs the commit(s) and appends to acked;
# states(acked) -> list of allowed final states; read(c) -> the state, comparable with ==.

class TxnMulti:
    """One transaction across two tables (updates, 200 inserts, a delete); kills land inside COMMIT."""

    name = "txn_multi"

    def setup(self, c):
        ex(c, "CREATE TABLE acct (id int PRIMARY KEY, bal int)")
        ex(c, "CREATE TABLE log (id int PRIMARY KEY, note text)")
        ex(c, "INSERT INTO acct VALUES " + ", ".join(f"({i}, 100)" for i in range(50)))
        ex(c, "BEGIN")
        ex(c, "UPDATE acct SET bal = bal - 1 WHERE id < 25")
        ex(c, "UPDATE acct SET bal = bal + 1 WHERE id >= 25")
        for lo in range(0, 200, 50):
            ex(c, "INSERT INTO log VALUES " + ", ".join(f"({j}, '{pad(j)}')" for j in range(lo, lo + 50)))
        ex(c, "DELETE FROM acct WHERE id = 49")

    def window(self, c, acked):
        if c.one("COMMIT").tag == "COMMIT":
            acked.append(1)

    def read(self, c):
        return (ex(c, "SELECT id, bal FROM acct ORDER BY id"), ex(c, "SELECT id, note FROM log ORDER BY id"))

    def states(self, acked):
        before = ([[str(i), "100"] for i in range(50)], [])
        after = ([[str(i), "99" if i < 25 else "101"] for i in range(49)], [[str(j), pad(j)] for j in range(200)])
        return [after] if acked else [before, after]


class AutocommitBulk:
    """One autocommit INSERT of 300 wide rows."""

    name = "autocommit_bulk"

    def setup(self, c):
        ex(c, "CREATE TABLE t (id int PRIMARY KEY, v text)")
        ex(c, "INSERT INTO t VALUES " + ", ".join(f"({i}, 'base{i}')" for i in range(100)))

    def window(self, c, acked):
        r = c.one("INSERT INTO t VALUES " + ", ".join(f"({i}, '{pad(i, 200)}')" for i in range(100, 400)))
        if r.kind == "command" and r.tag == "INSERT 0 300":
            acked.append(1)

    def read(self, c):
        return ex(c, "SELECT id, v FROM t ORDER BY id")

    def states(self, acked):
        before = [[str(i), f"base{i}"] for i in range(100)]
        after = before + [[str(i), pad(i, 200)] for i in range(100, 400)]
        return [after] if acked else [before, after]


class CommitSequence:
    """40 autocommit statements from one client (inserts, every 5th an update); kills land anywhere in the run,
    including in any compaction or checkpoint they trigger. The result must be exactly a prefix."""

    name = "commit_sequence"
    N = 40

    def stmt(self, i: int) -> str:
        if i % 5 == 4:
            return f"UPDATE s SET k = k + 1 WHERE id < {i * 5}"
        return "INSERT INTO s VALUES " + ", ".join(f"({j}, {j}, '{pad(j, 80)}')" for j in range(i * 10, i * 10 + 10))

    def setup(self, c):
        ex(c, "CREATE TABLE s (id int PRIMARY KEY, k int, pad text)")

    def window(self, c, acked):
        for i in range(self.N):
            r = c.one(self.stmt(i))
            if r.kind != "command":
                return
            acked.append(i)

    def read(self, c):
        return ex(c, "SELECT id, k, pad FROM s ORDER BY id")

    def prefix(self, j: int):
        rows: dict[int, list] = {}
        for i in range(j):
            if i % 5 == 4:
                for k in rows:
                    if k < i * 5:
                        rows[k][1] += 1
            else:
                for k in range(i * 10, i * 10 + 10):
                    rows[k] = [k, k, pad(k, 80)]
        return [[str(a), str(b), p] for a, b, p in (rows[k] for k in sorted(rows))]

    def states(self, acked):
        j = len(acked)
        return [self.prefix(j)] + ([self.prefix(j + 1)] if j < self.N else [])


class DdlTxn:
    """CREATE TABLE + 100 inserts + an update of another table, in one transaction."""

    name = "ddl_txn"

    def setup(self, c):
        ex(c, "CREATE TABLE base (id int PRIMARY KEY, v int)")
        ex(c, "INSERT INTO base VALUES (1, 1)")
        ex(c, "BEGIN")
        ex(c, "CREATE TABLE fresh (id int PRIMARY KEY, v text NOT NULL)")
        ex(c, "INSERT INTO fresh VALUES " + ", ".join(f"({i}, '{pad(i, 40)}')" for i in range(100)))
        ex(c, "UPDATE base SET v = 2")

    def window(self, c, acked):
        if c.one("COMMIT").tag == "COMMIT":
            acked.append(1)

    def read(self, c):
        r = c.one("SELECT id, v FROM fresh ORDER BY id")
        fresh = None if r.kind == "error" else r.rows
        return (ex(c, "SELECT v FROM base"), fresh)

    def states(self, acked):
        before = ([["1"]], None)
        after = ([["2"]], [[str(i), pad(i, 40)] for i in range(100)])
        return [after] if acked else [before, after]


SCENARIOS = [TxnMulti(), AutocommitBulk(), CommitSequence(), DdlTxn()]


# ---------------------------------------------------------------- driver

def trial(box_factory, sc, n: int | None) -> dict:
    """n=None: the control (count only). Returns ok, why, count (syscalls in the window), acked."""
    box = box_factory()
    t0 = time.time()
    try:
        if not box.start():
            return {"n": n, "ok": False, "why": "server did not start", "count": 0, "violations": []}
        with Scanner(box) as scn:
            try:
                c = Conn(box.host, box.port, timeout=T)
                sc.setup(c)
                ctl = Ctl(box.host)
                if ctl.cmd(f"arm {n or 0}") != "ok":
                    raise Fail("supervisor did not arm")
                acked: list = []

                def run():
                    try:
                        sc.window(c, acked)
                    except Exception:  # noqa: BLE001 - the kill cuts the connection
                        pass

                th = threading.Thread(target=run, daemon=True)
                th.start()
                th.join(60)
                count = -1
                if n is None:
                    time.sleep(SETTLE_S)
                    count = int(ctl.cmd("count"))
                else:
                    deadline = time.time() + KILL_WAIT_S
                    while box.alive() and time.time() < deadline:
                        time.sleep(0.05)
                ctl.close()
                killed_by_supervisor = n is not None and not box.alive()
                box.kill()  # the control and any point past the end of the window crash here instead
                if not box.start():
                    raise Fail("server did not come back after the kill")
                c2 = Conn(box.host, box.port, timeout=T)
                got = sc.read(c2)
                c2.close()
                allowed = sc.states(acked)
                if got not in allowed:
                    what = "acked commit lost or torn" if acked and n is not None else "torn state"
                    raise Fail(f"{what}: acked={len(acked)}, state matches none of {len(allowed)} allowed")
                ok, why = True, f"acked={len(acked)}" + ("" if n is None else f" killed={killed_by_supervisor}")
            except Fail as e:
                ok, why, count = False, str(e), locals().get("count", -1)
            except Exception as e:  # noqa: BLE001
                ok, why, count = False, f"{type(e).__name__}: {e}"[:200], locals().get("count", -1)
        return {"n": n, "ok": ok, "why": why, "count": count, "seconds": round(time.time() - t0, 1),
                "violations": scn.violations[:10]}
    finally:
        box.close()


def points(m: int) -> list[int]:
    if m <= MAX_POINTS:
        return list(range(1, m + 1))
    return sorted({round(1 + (m - 1) * i / (MAX_POINTS - 1)) for i in range(MAX_POINTS)})


def run_scenario(box_factory, sc, pool) -> dict:
    ctrl = trial(box_factory, sc, None)
    res = {"scenario": sc.name, "control": ctrl, "window_syscalls": ctrl["count"], "trials": []}
    if not ctrl["ok"]:
        res |= {"passed": 0, "tried": 1, "score": 0.0}
        return res
    pts = points(max(0, ctrl["count"]))
    res["trials"] = list(pool.map(lambda n: trial(box_factory, sc, n), pts))
    passed = 1 + sum(t["ok"] for t in res["trials"])
    res |= {"passed": passed, "tried": 1 + len(pts), "score": passed / (1 + len(pts))}
    return res


def run_all(box_factory, jobs: int = 4) -> dict:
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(jobs) as pool, ThreadPoolExecutor(len(SCENARIOS)) as outer:
        per = list(outer.map(lambda sc: run_scenario(box_factory, sc, pool), SCENARIOS))
    violations = [v for s in per for t in [s["control"], *s["trials"]] for v in t.get("violations", [])]
    return {"score": sum(s["score"] for s in per) / len(per), "scenarios": per, "violations": violations[:20],
            "points": f"{sum(s['passed'] for s in per)}/{sum(s['tried'] for s in per)}"}


if __name__ == "__main__":
    import json

    from dbbox import AgentBox, PgBox

    if sys.argv[1] == "--postgres":
        r = run_all(lambda: PgBox(supervise=True), jobs=int(sys.argv[3]) if len(sys.argv) > 3 else 4)
    else:
        code = Path(sys.argv[1])
        r = run_all(lambda: AgentBox(code, supervise=True), jobs=int(sys.argv[3]) if len(sys.argv) > 3 else 4)
    Path(sys.argv[2]).write_text(json.dumps(r, indent=1))
    print(json.dumps({"score": r["score"], "points": r["points"],
                      "per": {s["scenario"]: [s["passed"], s["tried"], s["window_syscalls"]] for s in r["scenarios"]}}))
