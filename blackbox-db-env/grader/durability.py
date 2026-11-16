"""Durability: SIGKILL the candidate mid-workload, restart it on the same data, compare its state with the oracle's.

Each scenario: setup statements, then phases of operations on one or more connections (one operation in flight per
connection), each phase ended by a SIGKILL either at a random moment or right after the last acknowledgement.
After the final restart the verify queries run on the candidate. The oracle then replays the setup and every
acknowledged committing operation, and for every subset of the operations that were in flight at the kill runs
the verify queries inside a transaction it rolls back. The candidate passes if its answers (rows, or the error's
SQLSTATE) equal the oracle's for one of those subsets. Verify queries are plain `SELECT * FROM t`; rows are
compared as a multiset and integers are canonicalised (the variant's `+5` / `1_000` styles), so neither ORDER BY,
casts nor value formatting are needed to pass: only lost, torn or phantom writes fail. The scenarios' own writes
use only CREATE TABLE, INSERT ... VALUES, UPDATE/DELETE with literals and `col op literal` (transfers add
`bal = bal +/- n`), BEGIN/COMMIT, plus CREATE INDEX / ALTER TABLE ADD COLUMN / DROP TABLE in the DDL scenario.
"""

from __future__ import annotations

import itertools
import random
import re
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "oracle"))
from variant import Codec, Dialect, Malformed  # noqa: E402

from boxes import PORT  # noqa: E402
from wire import Conn  # noqa: E402

SEEDS = 2


def _ins(t: str, i: int, rng: random.Random) -> str:
    return f"INSERT INTO {t} VALUES ({i}, 'v{rng.randint(0, 10**6)}', {rng.randint(-1000, 1000)})"


def _tbl(rng: random.Random, p: str) -> str:
    return p + "".join(rng.choice("qxjvwz") for _ in range(3)) + str(rng.randint(10, 99))


def sc_seq_insert(rng):
    t = _tbl(rng, "d")
    ops = [{"body": [_ins(t, i, rng)]} for i in range(1, 2001)]
    return {"setup": [f"CREATE TABLE {t} (id integer PRIMARY KEY, s text, n integer)"],
            "phases": [{"conns": [ops], "kill": "random"}],
            "verify": [f"SELECT * FROM {t}"]}


def sc_seq_mixed(rng):
    t = _tbl(rng, "m")
    nxt, ops = 51, []
    for _ in range(1500):
        r = rng.random()
        if r < 0.45:
            ops.append({"body": [_ins(t, nxt, rng)]})
            nxt += 1
        elif r < 0.8:
            ops.append({"body": [f"UPDATE {t} SET n = {rng.randint(-1000, 1000)}, s = 'u{rng.randint(0, 10**6)}' WHERE id = {rng.randint(1, nxt)}"]})
        else:
            ops.append({"body": [f"DELETE FROM {t} WHERE id = {rng.randint(1, nxt)}"]})
    return {"setup": [f"CREATE TABLE {t} (id integer PRIMARY KEY, s text, n integer)"] + [_ins(t, i, rng) for i in range(1, 51)],
            "phases": [{"conns": [ops], "kill": "random"}],
            "verify": [f"SELECT * FROM {t}"]}


def sc_txn_batches(rng):
    t = _tbl(rng, "b")
    nxt, ops = 1, []
    for _ in range(800):
        body = []
        for _ in range(rng.randint(2, 6)):
            if rng.random() < 0.7 or nxt < 3:
                body.append(_ins(t, nxt, rng))
                nxt += 1
            else:
                body.append(f"UPDATE {t} SET n = {rng.randint(-1000, 1000)} WHERE id = {rng.randint(1, nxt - 1)}")
        ops.append({"body": body, "txn": True})
    return {"setup": [f"CREATE TABLE {t} (id integer PRIMARY KEY, s text, n integer)"],
            "phases": [{"conns": [ops], "kill": "random"}],
            "verify": [f"SELECT * FROM {t}"]}


def sc_transfer(rng):
    a, g = _tbl(rng, "acct"), _tbl(rng, "led")
    conns = []
    for c in range(4):
        ops = []
        for k in range(600):
            x, y = sorted(rng.sample(range(1, 17), 2))
            amt = rng.randint(1, 50)
            if rng.random() < 0.5:
                x, y, amt = x, y, -amt
            ops.append({"body": [f"UPDATE {a} SET bal = bal - {amt} WHERE id = {x}", f"UPDATE {a} SET bal = bal + {amt} WHERE id = {y}",
                                 f"INSERT INTO {g} VALUES ({c * 10000 + k}, {x}, {y}, {amt})"], "txn": True})
        conns.append(ops)
    return {"setup": [f"CREATE TABLE {a} (id integer PRIMARY KEY, bal integer NOT NULL)",
                      f"CREATE TABLE {g} (id integer PRIMARY KEY, src integer, dst integer, amt integer)",
                      f"INSERT INTO {a} VALUES " + ", ".join(f"({i}, 1000)" for i in range(1, 17))],
            "phases": [{"conns": conns, "kill": "random"}],
            "verify": [f"SELECT * FROM {a}", f"SELECT * FROM {g}"]}


def sc_uncommitted(rng):
    t = _tbl(rng, "u")
    first = [{"body": [_ins(t, i, rng)]} for i in range(1, 61)]
    open_txn = [{"body": [_ins(t, i, rng) for i in range(100, 130)] + [f"UPDATE {t} SET n = 0"], "txn": True, "commit": False}]
    return {"setup": [f"CREATE TABLE {t} (id integer PRIMARY KEY, s text, n integer)"],
            "phases": [{"conns": [first], "kill": "after"}, {"conns": [open_txn], "kill": "after"}],
            "verify": [f"SELECT * FROM {t}"]}


def sc_ddl(rng):
    t, t2, gone = _tbl(rng, "k"), _tbl(rng, "k"), _tbl(rng, "g")
    ops = [{"body": [s]} for s in [
        f"CREATE TABLE {t2} (id integer PRIMARY KEY, s text)",
        f"INSERT INTO {t2} VALUES (1, 'a'), (2, 'b'), (3, 'c')",
        f"CREATE INDEX ON {t2} (s)",
        f"ALTER TABLE {t2} ADD COLUMN n integer DEFAULT {rng.randint(1, 9)}",
        f"UPDATE {t2} SET s = 'bx' WHERE id = 2",
        f"DROP TABLE {gone}",
        f"INSERT INTO {t} VALUES " + ", ".join(f"({i}, 'x{i}', {i * 3})" for i in range(1, rng.randint(20, 80) + 1)),
        f"DELETE FROM {t} WHERE id > {rng.randint(5, 15)}"]]
    return {"setup": [f"CREATE TABLE {t} (id integer PRIMARY KEY, s text, n integer)", f"CREATE TABLE {gone} (a integer)"],
            "phases": [{"conns": [ops], "kill": "after"}],
            "verify": [f"SELECT * FROM {t2}", f"SELECT * FROM {t}", f"SELECT * FROM {gone}"]}


def sc_two_crashes(rng):
    t = _tbl(rng, "c")
    p1 = [{"body": [_ins(t, i, rng)]} for i in range(1, 101)]
    p2 = [{"body": [_ins(t, i, rng)]} for i in range(101, 2001)]
    return {"setup": [f"CREATE TABLE {t} (id integer PRIMARY KEY, s text, n integer)"],
            "phases": [{"conns": [p1], "kill": "after"}, {"conns": [p2], "kill": "random"}],
            "verify": [f"SELECT * FROM {t}"]}


SCENARIOS = {f.__name__[3:]: f for f in (sc_seq_insert, sc_seq_mixed, sc_txn_batches, sc_transfer, sc_uncommitted, sc_ddl, sc_two_crashes)}


# ------------------------------------------------------------------ talking to a server

class Session:
    """A logged-in guest session in the variant; `q` returns (ok, rows-or-sqlstate, final ready status)."""

    def __init__(self, host: str, spec: dict, timeout: float = 10):
        self.V, self.d, self.spec = Codec(spec), Dialect(spec), spec
        w = spec["wire"]
        self.c = Conn(host, PORT, self.V, timeout)
        self.c.read("count", 1)
        body = self.V.u32(w["magic"]) + self.V.s(w["startup_keys"]["user"]) + self.V.s("guest") + \
            self.V.s(w["startup_keys"]["database"]) + self.V.s("main") + self.V.s("")
        self.c.send(self.V.frame_untyped(body))
        r = self.c.read("Z")
        self.ok = bool(r["msgs"]) and self.V.back_kind.get(r["msgs"][-1][0]) == "ready"

    def q(self, sql: str):
        self.c.send(self.V.encode("f:query", {"sql": self.d.to_variant(sql)}))
        r = self.c.read("Z")
        rows, err, ready = [], None, False
        for t, b in r["msgs"]:
            k = self.V.back_kind.get(t)
            try:
                if k == "datarow":
                    rows.append([None if v is None else v.decode("utf-8", "replace")
                                 for v in self.V.decode("b:datarow", bytes.fromhex(b))["values"]])
                elif k == "error" and err is None:
                    err = self.V.decode("b:error", bytes.fromhex(b))["fields"].get("C", "?")
                elif k == "ready":
                    ready = True
            except Malformed:
                err = err or "malformed"
        if not ready:
            return False, "no-reply", None
        return err is None, (rows if err is None else ["error", err]), True

    def close(self) -> None:
        try:
            self.c.send(self.V.encode("f:terminate", {}))
        except Exception:  # noqa: BLE001
            pass
        self.c.close()


def op_sql(op: dict) -> str:
    if op.get("txn"):
        return "BEGIN; " + "; ".join(op["body"]) + ("; COMMIT" if op.get("commit", True) else "")
    return op["body"][0]


def drive(host: str, spec: dict, ops: list, status: list, stop: threading.Event) -> None:
    """Send ops one at a time; status[i] becomes 'sent', then 'acked' or 'error'."""
    try:
        s = Session(host, spec, timeout=30)
    except Exception:  # noqa: BLE001
        return
    if not s.ok:
        return
    for i, op in enumerate(ops):
        if stop.is_set():
            break
        status[i] = "sent"
        ok, _, ready = s.q(op_sql(op))
        if not ready:
            break
        status[i] = "acked" if ok else "error"
    s.c.close()


def run_candidate(box, spec: dict, sc: dict, rng: random.Random) -> dict:
    """Run a scenario on a fresh candidate box. Returns its verify answers and every op's status."""
    if not box.start():
        return {"ok": False, "why": "did not start"}
    s = Session(box.host, spec)
    if not s.ok:
        return {"ok": False, "why": "login failed"}
    for q in sc["setup"]:
        ok, res, _ = s.q(q)
        if not ok:
            return {"ok": False, "why": f"setup failed: {res}"}
    s.close()
    statuses = []
    for ph in sc["phases"]:
        st = [[None] * len(ops) for ops in ph["conns"]]
        stop = threading.Event()
        threads = [threading.Thread(target=drive, args=(box.host, spec, ops, st[i], stop), daemon=True) for i, ops in enumerate(ph["conns"])]
        for th in threads:
            th.start()
        if ph["kill"] == "random":
            time.sleep(rng.uniform(0.15, 1.2))
        else:
            for th in threads:
                th.join(timeout=300)
        stop.set()
        box.kill()
        for th in threads:
            th.join(timeout=60)
        statuses.append(st)
        if not box.start():
            return {"ok": False, "why": "did not restart after kill", "statuses": statuses}
    s = None
    for _ in range(20):  # a server may accept TCP before it can serve
        s = Session(box.host, spec)
        if s.ok:
            break
        time.sleep(0.5)
    if not s or not s.ok:
        return {"ok": False, "why": "login failed after restart", "statuses": statuses}
    answers = [s.q(q)[1] for q in sc["verify"]]
    s.close()
    return {"ok": True, "answers": answers, "statuses": statuses}


def allowed_answers(oracle_host: str, spec: dict, sc: dict, statuses: list) -> list:
    """The oracle's verify answers for every state the candidate may legitimately be in."""
    s = Session(oracle_host, spec, timeout=60)
    for q in sc["setup"]:
        s.q(q)
    inflight = []
    for pi, (ph, st) in enumerate(zip(sc["phases"], statuses)):
        for ci, ops in enumerate(ph["conns"]):
            for i, op in enumerate(ops):
                if st[ci][i] == "acked" and op.get("commit", True):
                    s.q("BEGIN")
                    for b in op["body"]:
                        s.q(b)
                    s.q("COMMIT")
                elif st[ci][i] == "sent" and op.get("commit", True):
                    if pi == len(sc["phases"]) - 1:
                        inflight.append(op)
    out = []
    for r in range(len(inflight) + 1):
        for sub in itertools.combinations(inflight, r):
            s.q("BEGIN")
            for op in sub:
                for b in op["body"]:
                    s.q(b)
            out.append([s.q(q)[1] for q in sc["verify"]])
            s.q("ROLLBACK")
    s.close()
    return out


_INT = re.compile(r"[+-]?[0-9][0-9_]*")


def canon(answers: list) -> list:
    """Order-free, formatting-free form of verify answers: rows sorted, integer strings as plain ints."""
    out = []
    for a in answers:
        if isinstance(a, list) and a[:1] == ["error"]:
            out.append(a)
            continue
        rows = [[str(int(v.replace("_", ""))) if isinstance(v, str) and _INT.fullmatch(v) else v for v in r] for r in a]
        out.append(sorted(rows, key=repr))
    return out


def run_all(make_box, oracle_host: str, spec: dict, wseed: int, log=print) -> dict:
    runs = []
    for name, fn in SCENARIOS.items():
        for k in range(SEEDS):
            rng = random.Random(f"bbx-dur-{wseed}-{name}-{k}")
            sc = fn(rng)
            box = make_box()
            try:
                c = run_candidate(box, spec, sc, rng)
            except Exception as e:  # noqa: BLE001 - harness trouble on one run must not sink the rest
                c = {"ok": False, "why": f"harness: {type(e).__name__}: {e}"[:300]}
            finally:
                box.close()
            rec = {"scenario": name, "seed": k, "ok": False}
            if c.get("statuses"):
                st = c["statuses"]
                rec["acked"] = sum(x == "acked" for ph in st for conn in ph for x in conn)
                rec["inflight"] = sum(x == "sent" for ph in st for conn in ph for x in conn)
            if c["ok"]:
                allowed = allowed_answers(oracle_host, spec, sc, c["statuses"])
                rec["ok"] = canon(c["answers"]) in [canon(x) for x in allowed]
                rec["states_allowed"] = len(allowed)
                if not rec["ok"]:
                    rec["got"] = str(c["answers"])[:600]
                    rec["want_one_of"] = str(allowed[:2])[:600]
            else:
                rec["why"] = c.get("why")
            log(f"durability {name}#{k}: {'pass' if rec['ok'] else 'FAIL'} {rec.get('why', '')} acked={rec.get('acked')}")
            runs.append(rec)
    passed = sum(r["ok"] for r in runs)
    return {"score": passed / len(runs), "passed": passed, "total": len(runs), "runs": runs}
