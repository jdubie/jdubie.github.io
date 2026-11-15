"""ACID scenarios. Each takes a started box and a seed and returns (ok, detail).

Every scenario has a positive control (committed work must be visible), so a server that stores
nothing, or answers every query with an error, fails it.
"""

from __future__ import annotations

import random
import subprocess
import sys
import threading
import time
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.append(str(ROOT / "task/tools"))  # after grader/, so the kit's dbbox shim never shadows grader/dbbox.py
from pgwire import Conn  # noqa: E402

T = 10.0  # per-operation client timeout


class Fail(Exception):
    pass


def conn(box) -> Conn:
    return Conn(box.host, box.port, timeout=T)


def ex(c: Conn, sql: str):
    """Run one statement that must succeed; return its rows (or [])."""
    r = c.one(sql)
    if r.kind == "error":
        raise Fail(f"{sql[:80]!r} -> {r.sqlstate} {r.message[:120]}")
    return r.rows


def val(c: Conn, sql: str) -> str | None:
    rows = ex(c, sql)
    if len(rows) != 1 or len(rows[0]) != 1:
        raise Fail(f"{sql[:80]!r} expected one value, got {rows[:3]}")
    return rows[0][0]


def must_restart(box) -> None:
    if not box.restart():
        raise Fail("server did not come back after kill")


def check(cond: bool, msg: str) -> None:
    if not cond:
        raise Fail(msg)


# ---------------------------------------------------------------- atomicity

def atomic_rollback(box, rnd: random.Random) -> str:
    c = conn(box)
    ex(c, "CREATE TABLE a (id int PRIMARY KEY, v text)")
    n = rnd.randint(5, 40)
    ex(c, "BEGIN")
    for i in range(n):
        ex(c, f"INSERT INTO a VALUES ({i}, 'r{i}')")
    check(val(c, "SELECT count(*) FROM a") == str(n), "own uncommitted inserts not visible inside the transaction")
    ex(c, "ROLLBACK")
    check(val(c, "SELECT count(*) FROM a") == "0", "rolled-back inserts are visible")
    ex(c, "BEGIN")
    for i in range(n):
        ex(c, f"INSERT INTO a VALUES ({i}, 'r{i}')")
    ex(c, "COMMIT")
    check(val(c, "SELECT count(*) FROM a") == str(n), "committed inserts missing")
    return f"{n} rows"


def atomic_error_aborts(box, rnd: random.Random) -> str:
    c = conn(box)
    ex(c, "CREATE TABLE a (id int PRIMARY KEY)")
    ex(c, "INSERT INTO a VALUES (1)")
    ex(c, "BEGIN")
    ex(c, "INSERT INTO a VALUES (2)")
    check(c.one("INSERT INTO a VALUES (1)").kind == "error", "duplicate key accepted")
    check(c.one("INSERT INTO a VALUES (3)").kind == "error", "statements accepted in an aborted transaction")
    r = c.one("COMMIT")
    check(r.kind != "error" and r.tag == "ROLLBACK", f"COMMIT of an aborted transaction answered {r.tag or r.sqlstate!r}, not ROLLBACK")
    check(val(c, "SELECT count(*) FROM a") == "1", "part of a failed transaction was kept")
    return "ok"


def atomic_statement(box, rnd: random.Random) -> str:
    c = conn(box)
    ex(c, "CREATE TABLE a (id int PRIMARY KEY, v int NOT NULL)")
    ex(c, "INSERT INTO a VALUES (1, 1), (2, 2), (3, 3)")
    vals = ", ".join(f"({i}, {i})" for i in range(10, 60)) + ", (2, 0)"
    check(c.one(f"INSERT INTO a VALUES {vals}").kind == "error", "multi-row insert with a duplicate key accepted")
    check(c.one("UPDATE a SET v = CASE WHEN id = 3 THEN NULL ELSE v + 100 END").kind == "error", "NOT NULL violated by UPDATE")
    check(ex(c, "SELECT id, v FROM a ORDER BY id") == [["1", "1"], ["2", "2"], ["3", "3"]], "a failed statement left partial effects")
    return "ok"


# ---------------------------------------------------------------- durability / crash atomicity

def crash_uncommitted(box, rnd: random.Random) -> str:
    c = conn(box)
    ex(c, "CREATE TABLE a (id int PRIMARY KEY, v text)")
    ex(c, "INSERT INTO a VALUES (0, 'committed')")
    ex(c, "BEGIN")
    for i in range(1, rnd.randint(10, 200)):
        ex(c, f"INSERT INTO a VALUES ({i}, '{'x' * rnd.randint(1, 300)}')")
    ex(c, "UPDATE a SET v = 'changed' WHERE id = 0")
    box.kill()
    must_restart(box)
    c = conn(box)
    check(ex(c, "SELECT id, v FROM a ORDER BY id") == [["0", "committed"]], "uncommitted work survived a crash, or committed work was lost")
    return "ok"


def crash_during_commit(box, rnd: random.Random) -> str:
    """Kill at a random moment around COMMIT of a large transaction: the result must be all or nothing."""
    c = conn(box)
    ex(c, "CREATE TABLE acct (id int PRIMARY KEY, bal int)")
    ex(c, "CREATE TABLE log (id int PRIMARY KEY, note text)")
    n = 50
    ex(c, "INSERT INTO acct VALUES " + ", ".join(f"({i}, 100)" for i in range(n)))
    m = rnd.randint(50, 400)
    ex(c, "BEGIN")
    ex(c, "UPDATE acct SET bal = bal - 1 WHERE id < 25")
    ex(c, "UPDATE acct SET bal = bal + 1 WHERE id >= 25")
    for i in range(0, m, 50):
        ex(c, "INSERT INTO log VALUES " + ", ".join(f"({j}, '{'n' * 40}{j}')" for j in range(i, min(m, i + 50))))
    ex(c, f"DELETE FROM acct WHERE id = {n - 1}")
    acked = threading.Event()

    def commit():
        try:
            if c.one("COMMIT").tag == "COMMIT":
                acked.set()
        except Exception:  # noqa: BLE001 - the kill cuts the connection
            pass

    th = threading.Thread(target=commit, daemon=True)
    th.start()
    time.sleep(rnd.uniform(0, 0.25))
    box.kill()
    th.join(5)
    must_restart(box)
    c = conn(box)
    cnt_a = val(c, "SELECT count(*) FROM acct")
    total = val(c, "SELECT sum(bal) FROM acct")
    cnt_l = val(c, "SELECT count(*) FROM log")
    before = (str(n), str(100 * n), "0")
    after = (str(n - 1), str(100 * n - 101 + 0), str(m))  # row 49 (bal 101 after +1) deleted
    state = (cnt_a, total, cnt_l)
    check(state in (before, after), f"torn commit: state {state}, expected {before} or {after}")
    if acked.is_set():
        check(state == after, "COMMIT was acknowledged but its effects were lost")
    return f"{'acked' if acked.is_set() else 'unacked'} -> {'after' if state == after else 'before'}"


def durability_acked(box, rnd: random.Random) -> str:
    """Concurrent autocommit writers; kill at a random time. Every acknowledged row must survive, intact."""
    c = conn(box)
    ex(c, "CREATE TABLE d (id int PRIMARY KEY, w int, chk text)")
    acked: set[int] = set()
    sent: set[int] = set()
    lock = threading.Lock()
    stop = threading.Event()

    def writer(w: int):
        try:
            cw = conn(box)
            i = 0
            while not stop.is_set():
                rid = w * 1_000_000 + i
                with lock:
                    sent.add(rid)
                r = cw.one(f"INSERT INTO d VALUES ({rid}, {w}, 'c{rid * 7 % 1009}')")
                if r.kind == "command":
                    with lock:
                        acked.add(rid)
                i += 1
        except Exception:  # noqa: BLE001
            pass

    ths = [threading.Thread(target=writer, args=(w,), daemon=True) for w in range(4)]
    for t in ths:
        t.start()
    time.sleep(rnd.uniform(1.0, 3.0))
    box.kill()
    stop.set()
    for t in ths:
        t.join(5)
    check(len(acked) >= 5, f"only {len(acked)} inserts acknowledged before the kill")
    must_restart(box)
    c = conn(box)
    rows = ex(c, "SELECT id, w, chk FROM d")
    got = {int(r[0]): r for r in rows}
    lost = acked - got.keys()
    check(not lost, f"{len(lost)} of {len(acked)} acknowledged rows lost")
    phantom = got.keys() - sent
    check(not phantom, f"{len(phantom)} rows nobody inserted")
    bad = [r for r in rows if r[2] != f"c{int(r[0]) * 7 % 1009}" or r[1] != str(int(r[0]) // 1_000_000)]
    check(not bad, f"{len(bad)} torn rows, e.g. {bad[:2]}")
    ex(c, f"INSERT INTO d VALUES ({max(got) + 1}, 0, 'after')")
    check(c.one(f"INSERT INTO d VALUES ({max(got)}, 0, 'dup')").kind == "error", "primary key not enforced after recovery")
    return f"{len(acked)} acked, {len(got)} present"


def restart_everything(box, rnd: random.Random) -> str:
    """Schema, constraints, wide values and a sequence all survive two crashes."""
    c = conn(box)
    big = "".join(rnd.choice("abcdefghijklmnopqrstuvwxyz0123456789 /.'") for _ in range(rnd.randint(2000, 30000)))
    ex(c, "CREATE TABLE p (id serial PRIMARY KEY, name text UNIQUE, body text, n bigint, ok boolean)")
    for i in range(30):
        ex(c, f"INSERT INTO p (name, body, n, ok) VALUES ('name{i}', '{big[:i * 50].replace(chr(39), chr(39) * 2)}', {i * 10**12}, {str(i % 2 == 0).lower()})")
    ex(c, f"INSERT INTO p (name, body, n, ok) VALUES ('big', '{big.replace(chr(39), chr(39) * 2)}', -1, NULL)")
    q = "SELECT id, name, length(body), md5(body), n, ok FROM p ORDER BY id"
    before = ex(c, q)
    for _ in range(2):
        box.kill()
        must_restart(box)
    c = conn(box)
    check(ex(c, q) == before, "data differs after restart")
    check(c.one("INSERT INTO p (name) VALUES ('name3')").kind == "error", "UNIQUE not enforced after restart")
    nid = val(c, "INSERT INTO p (name) VALUES ('new') RETURNING id")
    check(nid is not None and int(nid) > 31, f"serial reused an id after restart: {nid}")
    return f"{len(big)} byte value"


# ---------------------------------------------------------------- isolation

def no_dirty_read(box, rnd: random.Random) -> str:
    a, b = conn(box), conn(box)
    ex(a, "CREATE TABLE x (id int PRIMARY KEY, v int)")
    ex(a, "INSERT INTO x VALUES (1, 1)")
    ex(a, "BEGIN")
    ex(a, "UPDATE x SET v = 2 WHERE id = 1")
    ex(a, "INSERT INTO x VALUES (2, 2)")
    check(val(b, "SELECT v FROM x WHERE id = 1") == "1", "dirty read: saw an uncommitted update")
    check(val(b, "SELECT count(*) FROM x") == "1", "dirty read: saw an uncommitted insert")
    ex(a, "COMMIT")
    check(val(b, "SELECT v FROM x WHERE id = 1") == "2", "committed update not visible to another session")
    check(val(b, "SELECT count(*) FROM x") == "2", "committed insert not visible to another session")
    return "ok"


def no_lost_update(box, rnd: random.Random) -> str:
    c = conn(box)
    ex(c, "CREATE TABLE ctr (id int PRIMARY KEY, v int)")
    ex(c, "INSERT INTO ctr VALUES (1, 0)")
    nw, per = 8, 40
    errs: list[str] = []

    def w():
        try:
            cw = conn(box)
            for _ in range(per):
                ex(cw, "UPDATE ctr SET v = v + 1 WHERE id = 1")
        except Exception as e:  # noqa: BLE001
            errs.append(str(e)[:120])

    ths = [threading.Thread(target=w) for _ in range(nw)]
    for t in ths:
        t.start()
    for t in ths:
        t.join(120)
    check(not errs, f"increments failed: {errs[:2]}")
    got = val(c, "SELECT v FROM ctr WHERE id = 1")
    check(got == str(nw * per), f"lost updates: {got} != {nw * per}")
    return f"{nw}x{per}"


def select_for_update(box, rnd: random.Random) -> str:
    """Read-modify-write in explicit transactions with SELECT ... FOR UPDATE must not lose increments."""
    c = conn(box)
    ex(c, "CREATE TABLE ctr (id int PRIMARY KEY, v int)")
    ex(c, "INSERT INTO ctr VALUES (1, 0)")
    nw, per = 4, 15
    errs: list[str] = []

    def w():
        try:
            cw = conn(box)
            for _ in range(per):
                ex(cw, "BEGIN")
                v = int(val(cw, "SELECT v FROM ctr WHERE id = 1 FOR UPDATE"))
                time.sleep(0.002)
                ex(cw, f"UPDATE ctr SET v = {v + 1} WHERE id = 1")
                ex(cw, "COMMIT")
        except Exception as e:  # noqa: BLE001
            errs.append(str(e)[:120])

    ths = [threading.Thread(target=w) for _ in range(nw)]
    for t in ths:
        t.start()
    for t in ths:
        t.join(120)
    check(not errs, f"transactions failed: {errs[:2]}")
    got = val(c, "SELECT v FROM ctr WHERE id = 1")
    check(got == str(nw * per), f"lost updates under FOR UPDATE: {got} != {nw * per}")
    return "ok"


def bank_invariant(box, rnd: random.Random) -> str:
    """Concurrent transfers; every statement-level and repeatable-read snapshot must see the same total."""
    c = conn(box)
    n, start = 10, 1000
    ex(c, "CREATE TABLE acct (id int PRIMARY KEY, bal int)")
    ex(c, "INSERT INTO acct VALUES " + ", ".join(f"({i}, {start})" for i in range(n)))
    total = str(n * start)
    stop = threading.Event()
    bad: list[str] = []
    done = [0, 0]

    def mover(seed: int):
        r = random.Random(seed)
        try:
            cw = conn(box)
            while not stop.is_set():
                a, b = sorted(r.sample(range(n), 2))
                amt = r.randint(1, 50) * r.choice((1, -1))
                ex(cw, "BEGIN")
                ex(cw, f"UPDATE acct SET bal = bal - {amt} WHERE id = {a}")
                ex(cw, f"UPDATE acct SET bal = bal + {amt} WHERE id = {b}")
                ex(cw, "COMMIT")
                done[0] += 1
        except Exception as e:  # noqa: BLE001
            bad.append(f"mover: {str(e)[:120]}")

    def reader(rr: bool):
        try:
            cr = conn(box)
            while not stop.is_set():
                if rr:
                    ex(cr, "BEGIN ISOLATION LEVEL REPEATABLE READ")
                    s1 = val(cr, "SELECT sum(bal) FROM acct")
                    rows = ex(cr, "SELECT bal FROM acct")
                    ex(cr, "COMMIT")
                    s2 = str(sum(int(x[0]) for x in rows))
                    if s1 != total or s2 != total:
                        bad.append(f"repeatable-read snapshot totals {s1}, {s2} != {total}")
                else:
                    s = val(cr, "SELECT sum(bal) FROM acct")
                    if s != total:
                        bad.append(f"statement saw total {s} != {total}")
                done[1] += 1
        except Exception as e:  # noqa: BLE001
            bad.append(f"reader: {str(e)[:120]}")

    ths = [threading.Thread(target=mover, args=(rnd.random(),)) for _ in range(5)]
    ths += [threading.Thread(target=reader, args=(k % 2 == 1,)) for k in range(4)]
    for t in ths:
        t.start()
    time.sleep(4)
    stop.set()
    for t in ths:
        t.join(30)
    check(not bad, bad[0] if bad else "")
    check(done[0] >= 10 and done[1] >= 10, f"too little progress: {done[0]} transfers, {done[1]} reads in 4 s")
    check(val(c, "SELECT sum(bal) FROM acct") == total, "money created or destroyed")
    return f"{done[0]} transfers, {done[1]} reads"


def repeatable_read(box, rnd: random.Random) -> str:
    a, b = conn(box), conn(box)
    ex(a, "CREATE TABLE x (id int PRIMARY KEY, v int)")
    ex(a, "INSERT INTO x VALUES (1, 10), (2, 20)")
    ex(a, "BEGIN ISOLATION LEVEL REPEATABLE READ")
    check(val(a, "SELECT sum(v) FROM x") == "30", "wrong initial read")
    ex(b, "UPDATE x SET v = 100 WHERE id = 1")
    ex(b, "INSERT INTO x VALUES (3, 1)")
    check(val(a, "SELECT sum(v) FROM x") == "30", "non-repeatable read inside REPEATABLE READ")
    check(val(a, "SELECT count(*) FROM x") == "2", "phantom inside REPEATABLE READ")
    ex(a, "COMMIT")
    check(val(a, "SELECT sum(v) FROM x") == "121", "new snapshot after COMMIT missing committed changes")
    return "ok"


def read_committed_sees_commits(box, rnd: random.Random) -> str:
    a, b = conn(box), conn(box)
    ex(a, "CREATE TABLE x (id int PRIMARY KEY, v int)")
    ex(a, "INSERT INTO x VALUES (1, 1)")
    ex(a, "BEGIN")
    check(val(a, "SELECT v FROM x WHERE id = 1") == "1", "wrong initial read")
    ex(b, "UPDATE x SET v = 5 WHERE id = 1")
    check(val(a, "SELECT v FROM x WHERE id = 1") == "5", "READ COMMITTED transaction did not see a later commit")
    ex(a, "COMMIT")
    return "ok"


def write_skew_serializable(box, rnd: random.Random) -> str:
    """Two doctors on call; each SERIALIZABLE transaction takes itself off if the other is on. One must fail."""
    a, b = conn(box), conn(box)
    ex(a, "CREATE TABLE oncall (doc text PRIMARY KEY, on_call boolean)")
    ex(a, "INSERT INTO oncall VALUES ('alice', true), ('bob', true)")
    for c in (a, b):
        ex(c, "BEGIN ISOLATION LEVEL SERIALIZABLE")
    check(val(a, "SELECT count(*) FROM oncall WHERE on_call") == "2", "wrong read")
    check(val(b, "SELECT count(*) FROM oncall WHERE on_call") == "2", "wrong read")
    outcomes = []
    for c, who in ((a, "alice"), (b, "bob")):
        r = c.one(f"UPDATE oncall SET on_call = false WHERE doc = '{who}'")
        rc = c.one("COMMIT" if r.kind != "error" else "ROLLBACK")
        outcomes.append(r.kind != "error" and rc.kind != "error" and rc.tag == "COMMIT")
    left = val(conn(box), "SELECT count(*) FROM oncall WHERE on_call")
    check(left != "0", "write skew: both SERIALIZABLE transactions committed")
    check(any(outcomes), "neither transaction committed")
    return f"commits={outcomes}"


def concurrent_unique(box, rnd: random.Random) -> str:
    c = conn(box)
    ex(c, "CREATE TABLE u (id int PRIMARY KEY, who int)")
    wins: list[int] = []
    errs: list[str] = []
    barrier = threading.Barrier(8)

    def w(k: int):
        try:
            cw = conn(box)
            barrier.wait(10)
            r = cw.one(f"INSERT INTO u VALUES (42, {k})")
            if r.kind == "command":
                wins.append(k)
            elif r.sqlstate not in ("23505", "40001"):
                errs.append(f"{r.sqlstate} {r.message[:80]}")
        except Exception as e:  # noqa: BLE001
            errs.append(str(e)[:120])

    ths = [threading.Thread(target=w, args=(k,)) for k in range(8)]
    for t in ths:
        t.start()
    for t in ths:
        t.join(60)
    check(not errs, f"unexpected errors: {errs[:2]}")
    check(len(wins) == 1, f"{len(wins)} concurrent inserts of the same key succeeded")
    check(val(c, "SELECT count(*) FROM u") == "1", "duplicate rows stored")
    return "ok"


def no_hang_on_idle_txn(box, rnd: random.Random) -> str:
    """An open transaction holding a lock must block only conflicting writers, not readers or other rows."""
    a, b = conn(box), conn(box)
    ex(a, "CREATE TABLE x (id int PRIMARY KEY, v int)")
    ex(a, "INSERT INTO x VALUES (1, 1), (2, 2)")
    ex(a, "BEGIN")
    ex(a, "UPDATE x SET v = 10 WHERE id = 1")
    b.settimeout(3)
    t0 = time.time()
    check(val(b, "SELECT sum(v) FROM x") == "3", "reader saw uncommitted data")
    ex(b, "UPDATE x SET v = 20 WHERE id = 2")
    check(time.time() - t0 < 3, "readers or writers of other rows were blocked by an open transaction")
    ex(a, "COMMIT")
    check(val(b, "SELECT sum(v) FROM x") == "30", "wrong final state")
    return "ok"


def rr_first_updater_wins(box, rnd: random.Random) -> str:
    """REPEATABLE READ: updating a row another transaction changed and committed since the snapshot fails with 40001."""
    a, b = conn(box), conn(box)
    ex(a, "CREATE TABLE x (id int PRIMARY KEY, v int)")
    ex(a, "INSERT INTO x VALUES (1, 10)")
    ex(a, "BEGIN ISOLATION LEVEL REPEATABLE READ")
    check(val(a, "SELECT v FROM x WHERE id = 1") == "10", "wrong initial read")
    ex(b, "UPDATE x SET v = 20 WHERE id = 1")
    r = a.one("UPDATE x SET v = v + 1 WHERE id = 1")
    check(r.kind == "error" and r.sqlstate == "40001", f"concurrent update under REPEATABLE READ gave {r.kind} {r.sqlstate}, not 40001")
    ex(a, "ROLLBACK")
    check(val(a, "SELECT v FROM x WHERE id = 1") == "20", "committed update lost")
    return "ok"


def lock_nowait_skip_locked(box, rnd: random.Random) -> str:
    a, b = conn(box), conn(box)
    ex(a, "CREATE TABLE q (id int PRIMARY KEY, job text)")
    ex(a, "INSERT INTO q VALUES (1, 'a'), (2, 'b'), (3, 'c')")
    ex(a, "BEGIN")
    check(ex(a, "SELECT id FROM q ORDER BY id LIMIT 1 FOR UPDATE") == [["1"]], "FOR UPDATE returned the wrong row")
    ex(b, "BEGIN")
    r = b.one("SELECT id FROM q WHERE id = 1 FOR UPDATE NOWAIT")
    check(r.kind == "error" and r.sqlstate == "55P03", f"FOR UPDATE NOWAIT on a locked row gave {r.kind} {r.sqlstate}, not 55P03")
    ex(b, "ROLLBACK")
    ex(b, "BEGIN")
    check(ex(b, "SELECT id FROM q ORDER BY id LIMIT 2 FOR UPDATE SKIP LOCKED") == [["2"], ["3"]], "SKIP LOCKED did not skip the locked row")
    ex(b, "COMMIT")
    ex(a, "COMMIT")
    return "ok"


def deadlock_detected(box, rnd: random.Random) -> str:
    """Two transactions lock rows in opposite orders: one must fail with 40P01 within a few seconds, the other commits."""
    a, b = conn(box), conn(box)
    ex(a, "CREATE TABLE d (id int PRIMARY KEY, v int)")
    ex(a, "INSERT INTO d VALUES (1, 0), (2, 0)")
    ex(a, "BEGIN")
    ex(b, "BEGIN")
    ex(a, "UPDATE d SET v = 1 WHERE id = 1")
    ex(b, "UPDATE d SET v = 2 WHERE id = 2")
    res = {}

    def go(c, name, row):
        try:
            r = c.one(f"UPDATE d SET v = v + 10 WHERE id = {row}")
            res[name] = r.sqlstate if r.kind == "error" else "ok"
        except Exception as e:  # noqa: BLE001
            res[name] = type(e).__name__

    ta = threading.Thread(target=go, args=(a, "a", 2))
    ta.start()
    time.sleep(0.3)
    tb = threading.Thread(target=go, args=(b, "b", 1))
    tb.start()
    ta.join(8)
    tb.join(8)
    check(sorted(res.values()) == ["40P01", "ok"], f"deadlock outcome {res}, expected one 40P01 and one success")
    for c, name in ((a, "a"), (b, "b")):
        ex(c, "COMMIT" if res[name] == "ok" else "ROLLBACK")
    return str(res)


def durability_extended(box, rnd: random.Random) -> str:
    """Like durability_acked, but writers use the extended protocol with a named statement; kill at a random time."""
    import pgproto as pp

    c = conn(box)
    ex(c, "CREATE TABLE d (id int PRIMARY KEY, w int, chk text)")
    acked: set[int] = set()
    sent: set[int] = set()
    lock = threading.Lock()
    stop = threading.Event()

    def writer(w: int):
        try:
            s = pp.Session.connect(box.host, box.port, timeout=T)
            s.send(pp.parse("ins", "INSERT INTO d VALUES ($1, $2, $3)"), pp.sync())
            s.until_ready()
            i = 0
            while not stop.is_set():
                rid = w * 1_000_000 + i
                with lock:
                    sent.add(rid)
                s.send(pp.bind("", "ins", [str(rid).encode(), str(w).encode(), f"c{rid * 7 % 1009}".encode()]), pp.execute(""), pp.sync())
                out = s.until_ready()
                if any(t == b"C" for t, _ in out) and not any(t == b"E" for t, _ in out):
                    with lock:
                        acked.add(rid)
                i += 1
        except Exception:  # noqa: BLE001
            pass

    ths = [threading.Thread(target=writer, args=(w,), daemon=True) for w in range(4)]
    for t in ths:
        t.start()
    time.sleep(rnd.uniform(1.0, 3.0))
    box.kill()
    stop.set()
    for t in ths:
        t.join(5)
    check(len(acked) >= 5, f"only {len(acked)} inserts acknowledged before the kill")
    must_restart(box)
    c = conn(box)
    rows = ex(c, "SELECT id, w, chk FROM d")
    got = {int(r[0]): r for r in rows}
    lost = acked - got.keys()
    check(not lost, f"{len(lost)} of {len(acked)} acknowledged rows lost")
    check(not (got.keys() - sent), "rows nobody inserted")
    bad = [r for r in rows if r[2] != f"c{int(r[0]) * 7 % 1009}" or r[1] != str(int(r[0]) // 1_000_000)]
    check(not bad, f"{len(bad)} torn rows")
    return f"{len(acked)} acked, {len(got)} present"


def pgbench_crash(box, rnd: random.Random) -> str:
    """pgbench TPC-B with 8 clients (prepared protocol); SIGKILL the server mid-run. After recovery the balance
    invariant must hold, and every transaction pgbench logged as completed must be in pgbench_history."""
    cli = box.cli
    tag = f"pgbc{rnd.randrange(10**9)}"
    base = ["-h", box.host, "-U", "postgres"]
    code, so, se = cli.run(["pgbench", "-i", "-s", "1", "-q", *base, "regression"], 300)
    check(code == 0, f"pgbench -i failed: {se.strip()[-200:]}")
    p = cli.popen(["sh", "-c", f"mkdir -p /tmp/{tag} && cd /tmp/{tag} && exec pgbench -n -c 8 -j 4 -T 60 -M prepared -l "
                               f"-h {box.host} -U postgres regression"])
    time.sleep(rnd.uniform(3.0, 8.0))
    box.kill()
    try:
        p.wait(30)
    except subprocess.TimeoutExpired:
        p.kill()
    code, logged, _ = cli.run(["sh", "-c", f"cat /tmp/{tag}/pgbench_log.* 2>/dev/null | wc -l"], 30)
    logged_n = int(logged.strip() or 0)
    check(logged_n >= 20, f"only {logged_n} transactions completed before the kill")
    must_restart(box)
    c = conn(box)
    a = val(c, "SELECT sum(abalance) FROM pgbench_accounts")
    b = val(c, "SELECT sum(bbalance) FROM pgbench_branches")
    t = val(c, "SELECT sum(tbalance) FROM pgbench_tellers")
    d = val(c, "SELECT coalesce(sum(delta), 0) FROM pgbench_history")
    n = int(val(c, "SELECT count(*) FROM pgbench_history"))
    check(a == b == t == d, f"balance invariant broken after crash: accounts {a}, branches {b}, tellers {t}, history {d}")
    check(n >= logged_n, f"{logged_n} transactions completed but only {n} history rows survived")
    check(n <= logged_n + 8, f"{n} history rows for {logged_n} completed transactions and 8 clients")
    return f"{logged_n} completed, {n} in history"


SCENARIOS = {
    # name: (function, seeds)
    "atomic_rollback": (atomic_rollback, 2),
    "atomic_error_aborts": (atomic_error_aborts, 1),
    "atomic_statement": (atomic_statement, 1),
    "crash_uncommitted": (crash_uncommitted, 3),
    "crash_during_commit": (crash_during_commit, 6),
    "durability_acked": (durability_acked, 4),
    "restart_everything": (restart_everything, 2),
    "no_dirty_read": (no_dirty_read, 1),
    "read_committed_sees_commits": (read_committed_sees_commits, 1),
    "repeatable_read": (repeatable_read, 1),
    "no_lost_update": (no_lost_update, 2),
    "select_for_update": (select_for_update, 2),
    "concurrent_unique": (concurrent_unique, 2),
    "bank_invariant": (bank_invariant, 3),
    "no_hang_on_idle_txn": (no_hang_on_idle_txn, 1),
    "write_skew_serializable": (write_skew_serializable, 1),
    "rr_first_updater_wins": (rr_first_updater_wins, 1),
    "lock_nowait_skip_locked": (lock_nowait_skip_locked, 1),
    "deadlock_detected": (deadlock_detected, 1),
    "durability_extended": (durability_extended, 3),
    "pgbench_crash": (pgbench_crash, 3),
}


def run_one(box_factory, name: str, seed: int, cli=None) -> dict:
    from dbbox import Scanner

    fn, _ = SCENARIOS[name]
    box = box_factory()
    box.cli = cli
    t0 = time.time()
    try:
        if not box.start():
            return {"scenario": name, "seed": seed, "ok": False, "why": "server did not start", "violations": []}
        with Scanner(box) as sc:
            try:
                detail = fn(box, random.Random(seed * 7919 + zlib.crc32(name.encode())))
                ok, why = True, detail
            except Fail as e:
                ok, why = False, str(e)
            except Exception as e:  # noqa: BLE001 - protocol errors, timeouts, closed sockets
                ok, why = False, f"{type(e).__name__}: {e}"[:300]
        return {"scenario": name, "seed": seed, "ok": ok, "why": why, "seconds": round(time.time() - t0, 1),
                "violations": sc.violations[:20], "restarts": box.starts - 1}
    finally:
        box.close()
