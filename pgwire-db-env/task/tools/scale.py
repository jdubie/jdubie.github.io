"""Scale: pgbench at scale factor 10 (1,000,000 accounts) on the 4 KB tmpfs, with a restart time limit.

Five items, each worth 0..1 (score = mean):
  init                pgbench -i -s 10 (client-side COPY) finishes within INIT_LIMIT_S with the right row counts
  restart             SIGKILL, start again; time from container start to a correct
                      `SELECT count(*), sum(abalance) FROM pgbench_accounts`: full credit <= RESTART_FULL_S,
                      linear to 0 at RESTART_ZERO_S
  select_tps          pgbench -S (prepared) 4 clients, 15 s: min(1, tps / SELECT_TPS_FULL)
  tpcb_tps            pgbench TPC-B (prepared) 8 clients, 20 s, no failed transactions and the balance invariant:
                      min(1, tps / TPCB_TPS_FULL), else 0
  restart_after_writes  as restart, after the TPC-B run, with the invariant checked after recovery

Limits come from measured floors on the same 4 KB tmpfs (dev/meta_bench.py, Python 3.12, 4 CPUs): creating
1M empty files named by row takes 2.5 s, scanning and decoding all of them 0.8 s, 20k lookups 0.06 s. A
server that rebuilds its state from a full metadata scan restarts in a few seconds; one that replays every
statement it ever ran through its SQL engine needs minutes at this size.
"""

from __future__ import annotations

import re
import time

SCALE = 10
INIT_LIMIT_S = 600
RESTART_FULL_S = 10
RESTART_ZERO_S = 30
RESTART_GIVEUP_S = 60
SELECT_TPS_FULL = 500
TPCB_TPS_FULL = 100


def _sql(cli, host, q, timeout=120):
    code, so, se = cli.run(["psql", "-X", "-h", host, "-U", "postgres", "-d", "regression", "-Atc", q], timeout)
    return so.strip() if code == 0 else f"ERR {se.strip()[-160:]}"


def _tps(text: str) -> tuple[float, int, int]:
    m = re.search(r"number of transactions actually processed: (\d+)", text)
    f = re.search(r"number of failed transactions: (\d+)", text)
    t = re.search(r"tps = ([\d.]+) \(without initial connection time\)", text)
    return (float(t.group(1)) if t else 0.0), (int(m.group(1)) if m else -1), (int(f.group(1)) if f else 0)


def _restart(box, cli, expect: str) -> tuple[float, str]:
    """Seconds from container start until the server answers the check query correctly (inf if never)."""
    from dbbox import handshake

    box.kill()
    t0 = time.time()
    box.launch()
    got = ""
    while time.time() - t0 < RESTART_GIVEUP_S:
        if not box.alive():
            return float("inf"), "server exited"
        if handshake(box.host, box.port, timeout=2):
            got = _sql(cli, box.host, "SELECT count(*), sum(abalance) FROM pgbench_accounts", timeout=RESTART_GIVEUP_S)
            if got == expect or not got.startswith("ERR"):
                break
        time.sleep(0.2)
    dt = time.time() - t0
    return (dt if got == expect else float("inf")), got


def _restart_credit(dt: float) -> float:
    if dt <= RESTART_FULL_S:
        return 1.0
    return max(0.0, (RESTART_ZERO_S - dt) / (RESTART_ZERO_S - RESTART_FULL_S))


def run(box, cli) -> dict:
    from dbbox import Scanner

    items: dict[str, dict] = {k: {"credit": 0.0} for k in ("init", "restart", "select_tps", "tpcb_tps", "restart_after_writes")}
    if not box.start():
        items["init"]["why"] = "server did not start"
        return {"score": 0.0, "items": items, "violations": []}
    base = ["-h", box.host, "-U", "postgres"]
    with Scanner(box, every_s=30) as sc:
        t0 = time.time()
        code, so, se = cli.run(["pgbench", "-i", "-s", str(SCALE), "-q", *base, "regression"], INIT_LIMIT_S)
        dt = time.time() - t0
        counts = _sql(cli, box.host, "SELECT (SELECT count(*) FROM pgbench_accounts), (SELECT count(*) FROM pgbench_tellers), "
                                     "(SELECT count(*) FROM pgbench_branches)", 300)
        ok = code == 0 and counts == f"{SCALE * 100000}|{SCALE * 10}|{SCALE}"
        items["init"] = {"credit": float(ok), "seconds": round(dt, 1), "why": f"exit {code}, counts {counts}; {se.strip()[-200:]}"}
        if ok:
            expect = f"{SCALE * 100000}|0"
            rt, got = _restart(box, cli, expect)
            items["restart"] = {"credit": _restart_credit(rt), "seconds": None if rt == float("inf") else round(rt, 1), "got": got[:120]}
            if rt != float("inf"):
                code, so, se = cli.run(["pgbench", "-n", "-S", "-c", "4", "-j", "4", "-T", "15", "-M", "prepared", *base, "regression"], 200)
                tps, n, failed = _tps(so)
                items["select_tps"] = {"credit": min(1.0, tps / SELECT_TPS_FULL) if code == 0 and failed == 0 else 0.0,
                                       "tps": tps, "why": f"exit {code}, {n} tx, {failed} failed; {se.strip()[-160:]}"}
                code, so, se = cli.run(["pgbench", "-n", "-c", "8", "-j", "4", "-T", "20", "-M", "prepared", *base, "regression"], 300)
                tps, n, failed = _tps(so)
                inv = _sql(cli, box.host, "SELECT (SELECT sum(abalance) FROM pgbench_accounts) = (SELECT sum(bbalance) FROM pgbench_branches) "
                                          "AND (SELECT sum(tbalance) FROM pgbench_tellers) = (SELECT coalesce(sum(delta), 0) FROM pgbench_history) "
                                          "AND (SELECT sum(abalance) FROM pgbench_accounts) = (SELECT coalesce(sum(delta), 0) FROM pgbench_history), "
                                          "(SELECT sum(abalance) FROM pgbench_accounts)", 300)
                good = code == 0 and failed == 0 and inv.startswith("t|")
                items["tpcb_tps"] = {"credit": min(1.0, tps / TPCB_TPS_FULL) if good else 0.0, "tps": tps,
                                     "why": f"exit {code}, {n} tx, {failed} failed, invariant {inv[:60]}; {se.strip()[-160:]}"}
                if inv.startswith("t|"):
                    total = inv.split("|")[1]
                    rt, got = _restart(box, cli, f"{SCALE * 100000}|{total}")
                    inv2 = _sql(cli, box.host, "SELECT (SELECT sum(bbalance) FROM pgbench_branches) = " + total) if rt != float("inf") else ""
                    items["restart_after_writes"] = {"credit": _restart_credit(rt) if inv2 == "t" else 0.0,
                                                     "seconds": None if rt == float("inf") else round(rt, 1), "got": got[:120]}
    score = sum(v["credit"] for v in items.values()) / len(items)
    return {"score": score, "items": items, "violations": sc.violations[:20], "max_entries": sc.max_entries}
