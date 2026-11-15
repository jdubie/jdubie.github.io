"""Render site/index.html from the calibration grades (ref/) and any agent runs (runs/)."""

from __future__ import annotations

import html
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SITE = ROOT / "site"


def load(p: Path) -> dict | None:
    return json.loads(p.read_text()) if p.exists() else None


def row(label: str, s: dict, note: str = "") -> str:
    def c(k):
        v = s.get(k, "")
        return f"{v:.3f}" if isinstance(v, float) else html.escape(str(v))
    return (f"<tr><td>{html.escape(label)}</td><td><b>{c('reward')}</b></td><td>{c('sql')}</td><td>{c('acid')}</td>"
            f"<td>{c('regress')}</td><td>{c('acid_runs')}</td><td>{c('sql_statements')}</td><td>{c('regress_statements')}</td>"
            f"<td>{c('violations')}</td><td>{html.escape(note)}</td></tr>")


def main() -> None:
    SITE.mkdir(exist_ok=True)
    rows = []
    for label, d, note in [("PostgreSQL 16 (calibration, storage rule not applied)", "postgres", "upper bound"),
                           ("mini baseline: tiny SQL subset, snapshot in file names", "grade-mini", "proves the sandbox allows a legit design"),
                           ("cheat baseline: same, plus data in file content", "grade-cheat", "must be 0")]:
        g = load(ROOT / "ref" / d / "grade.json")
        if g:
            rows.append(row(label, g["summary"], note))
    runs = []
    for meta in sorted(ROOT.glob("runs/*/*/meta.json")):
        m = json.loads(meta.read_text())
        runs.append(row(f"{meta.parent.parent.name}/{m['model']} r{m['rep']} ({m['minutes']} min, {len(m['segments'])} sessions)", m))
    pg = load(ROOT / "ref/postgres/grade.json")
    acid_rows = ""
    if pg:
        mini = load(ROOT / "ref/grade-mini/grade.json") or {"acid": []}
        names = sorted({a["scenario"] for a in pg["acid"]})
        for n in names:
            p = [a for a in pg["acid"] if a["scenario"] == n]
            mm = [a for a in mini["acid"] if a["scenario"] == n]
            acid_rows += (f"<tr><td>{n}</td><td>{len(p)}</td><td>{sum(a['ok'] for a in p)}/{len(p)}</td>"
                          f"<td>{sum(a['ok'] for a in mm)}/{len(mm)}</td><td>{html.escape(p[0]['why'][:80])}</td></tr>")
    head = "<tr><th>submission</th><th>reward</th><th>SQL</th><th>ACID</th><th>regress</th><th>ACID runs</th><th>SQL stmts</th><th>regress stmts</th><th>violations</th><th></th></tr>"
    page = f"""<!doctype html><meta charset=utf-8><title>emptyfs-db: an ACID database in empty files</title>
<style>body{{font:15px/1.5 system-ui,sans-serif;max-width:1100px;margin:2em auto;padding:0 1em;color:#222}}
table{{border-collapse:collapse;margin:1em 0;font-size:13px}}td,th{{border:1px solid #ccc;padding:4px 8px;text-align:left}}
th{{background:#f3f3f3}}code{{background:#f3f3f3;padding:1px 4px}}h2{{margin-top:2em}}.lead{{font-size:18px}}</style>
<h1>emptyfs-db: build an ACID, Postgres-compatible database where every file is empty</h1>
<p class=lead>The benchmark is built and calibrated. Real PostgreSQL scores 1.000 on every test, a cheat that stores
data in file contents scores 0, and a 250-line toy that keeps a snapshot in file names scores 0.195
(it only knows CREATE/INSERT/SELECT */DELETE, and has no isolation). <b>It is probably too easy.</b> In a 10-minute smoke run Haiku scored 0.703 legitimately (no storage violations).
It wrote a 1,700-line Python engine whose write-ahead log is hex-encoded SQL text in empty file names, replayed at startup.
ACID passed 27/33. It missed only FOR UPDATE row locks, READ COMMITTED re-reads, SERIALIZABLE write skew, and big values across restart.</p>

<h2>Scores</h2>
<table>{head}{''.join(rows)}</table>
<h3>Agent runs</h3>
<table>{head}{''.join(runs) or '<tr><td colspan=10>none yet</td></tr>'}</table>

<h2>The task</h2>
<p>Agents get <code>task/PROMPT.md</code>. They must write a server that speaks the PostgreSQL v3 wire protocol (simple
query flow) on :5432, from scratch, in Python (sqlite removed), C/C++, Rust (no crates) or Go, with no network. The rule:
<b>everything persisted must live in filesystem metadata</b>. That means file names (up to 255 bytes), the directory tree, timestamps, mode bits and
link counts. File contents are off limits.</p>
<p><b>Enforcement.</b> During grading the data directory is a tmpfs volume with <code>size=4k</code>, so only 4 KB of file content can exist at all.
A seccomp profile blocks xattrs, symlinks, mknod, fallocate, io_uring and non-zero truncate. The root filesystem is read-only, and <code>/tmp</code> is wiped on every restart.
A holder container keeps the volume mounted while the server container is SIGKILLed and restarted, so a kill loses exactly what a process crash would.
The storage is scanned every 2 s, and any non-empty file, xattr or special file at any moment makes the reward 0.</p>
<p><b>reward = 0.4·SQL + 0.4·ACID + 0.2·regress</b>, each compared with real PostgreSQL 16 (C collation):</p>
<ul><li><b>SQL</b>: 22 handwritten scripts, 459 statements. Basics through joins, subqueries, CTEs, windows, constraints, savepoints and a 20k-row table.
9 scripts are public, with their expected answers.</li>
<li><b>regress</b>: 34 files from PostgreSQL's own <code>src/test/regress</code>, about 6,800 statements. Files that depend on the shared
<code>test_setup</code> tables were dropped, as were statements whose answers changed between two Postgres runs.</li>
<li><b>ACID</b>: 16 scenarios, 33 seeded runs, with real crashes and real concurrency. 7 scenarios are public.</li></ul>
<p>Rows are compared in order when the query has a top-level ORDER BY, otherwise as a multiset. Commands must have identical tags.
Where Postgres raises an error, any error counts, but only if the last statement expected to succeed also passed. Without that gate a
server that rejects everything got credit for every error Postgres raises.</p>
<table><tr><th>ACID scenario</th><th>seeds</th><th>PostgreSQL</th><th>mini baseline</th><th>PostgreSQL detail</th></tr>{acid_rows}</table>

<h2>Caveats and next steps</h2>
<ul><li>Durability is tested against process crashes (container SIGKILL), not power loss: page-cache state survives, and fsync is not checked.</li>
<li>4 KB of content can exist transiently between scans. It can't hold anything durable, but a server could use it as scratch space.</li>
<li>The regress section is hard by design (Haiku 0.365), but SQL and ACID saturate fast. Hardening options: a scale tier (millions of rows, so a replay-everything log fails a 30 s restart budget and per-query timeouts); an <code>nr_inodes</code> cap that forces compaction; widening the crash window in crash_during_commit, because every kill so far landed after the ack; and more weight on regress.</li>
<li>The smoke run hit the harness's old 20-session cap, because Haiku kept ending its session. The cap is now 500.</li>
<li>Next: a real sweep (<code>run_agents.py --models opus,sonnet,haiku,sol,terra,gemini --minutes 60</code>). OpenAI runs use Sol/Terra via Codex with an explicit <code>-m</code>.
Not started; it's your call.</li></ul>
<p style="color:#888">~/emptyfs-db-env on the workstation</p>
"""
    (SITE / "index.html").write_text(page)
    print("wrote", SITE / "index.html")


if __name__ == "__main__":
    main()
