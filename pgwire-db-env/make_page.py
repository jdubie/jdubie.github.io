"""Render site/index.html from the calibration grades (ref/grade-*) and agent runs (runs/*/*/), plus site/notes.json.

  python3 make_page.py

notes.json holds the hand-written prose that depends on results: {"conclusion": html, "smoke": html,
"next": [html, ...]}.
"""

from __future__ import annotations

import html
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SITE = ROOT / "site"
sys.path.insert(0, str(ROOT / "grader"))
import regress as rg  # noqa: E402

COMPS = ["protocol", "clients", "sql", "acid", "scale"]
WEIGHTS = {"protocol": 0.20, "clients": 0.15, "sql": 0.30, "acid": 0.25, "scale": 0.10}
E = html.escape


def load(p: Path) -> dict | None:
    return json.loads(p.read_text()) if p.exists() else None


def f3(v) -> str:
    return f"{v:.3f}" if isinstance(v, (int, float)) else E(str(v if v is not None else "–"))


def row(label: str, s: dict, note: str) -> str:
    cells = "".join(f"<td class=n>{f3(s.get(k))}</td>" for k in COMPS)
    cg = s.get("clients_groups") or {}
    detail = (f"protocol {s.get('protocol_tests', '–')}; sql {s.get('sql_statements', '–')}; acid runs {s.get('acid_runs', '–')}, "
              f"crash points {s.get('crash_points', '–')}; clients " + ", ".join(f"{k} {v}" for k, v in cg.items()))
    viol = s.get("violations", 0)
    return (f"<tr><td>{E(label)}<div class=sub>{E(detail)}</div></td><td class=n><b>{f3(s.get('reward'))}</b></td>"
            f"<td class=n>{f3(s.get('raw_reward'))}</td>{cells}<td class=n{' style=color:var(--bad)' if viol else ''}>{viol}</td>"
            f"<td>{E(note)}</td></tr>")


def main() -> None:
    SITE.mkdir(exist_ok=True)
    notes = load(SITE / "notes.json") or {"conclusion": "", "smoke": "", "next": []}
    rows = []
    for label, d, note in [
        ("PostgreSQL 16.16, storage rule not applied", "grade-postgres-full", "upper bound: checks the grader's answers and limits"),
        ("cheat: v1 toy that also writes data into file contents", "grade-cheat", "must be 0"),
        ("v1's 250-line toy (snapshot in file names)", "grade-mini", "should be low"),
        ("v1 Haiku smoke-run server (scored 0.703 on v1)", "grade-v1-haiku", "should be low"),
    ]:
        g = load(ROOT / "ref" / d / "grade.json")
        if g:
            rows.append(row(label, g["summary"], note))
    runs = []
    for meta in sorted(ROOT.glob("runs/*/*/meta.json")):
        m = json.loads(meta.read_text())
        g = load(meta.parent / "grade/grade.json") or {"summary": m}
        runs.append(row(f"{meta.parent.parent.name}/{m['model']} r{m['rep']} ({m['minutes']} min, {len(m['segments'])} sessions)",
                        g["summary"], "agent run"))
    pgsql = load(ROOT / "grader/expected/regress.json") or {"stats": {}}
    st = pgsql.get("stats", {})
    excl_tests = "".join(f"<li><code>{E(t)}</code>: {E(w)}</li>" for t, w in rg.EXCLUDED_TESTS.items())
    excl_units = "".join(f"<li>{E(w)}: {n:,} units</li>" for w, n in sorted(st.get("excluded", {}).items(), key=lambda x: -x[1]))
    pg_acid = load(ROOT / "ref/grade-postgres-full/acid.json")
    cp_rows = ""
    if pg_acid:
        for sc in pg_acid["crashpoint"]["scenarios"]:
            cp_rows += (f"<tr><td>{E(sc['scenario'])}</td><td class=n>{sc.get('window_syscalls', '–')}</td>"
                        f"<td class=n>{sc['passed']}/{sc['tried']}</td></tr>")
    pg_scale = load(ROOT / "ref/grade-postgres-full/scale.json")
    scale_txt = ""
    if pg_scale:
        it = pg_scale["items"]
        scale_txt = (f"PostgreSQL itself: init {it['init'].get('seconds')} s, restart {it['restart'].get('seconds')} s, "
                     f"pgbench -S {it['select_tps'].get('tps', 0):,.0f} tps, TPC-B {it['tpcb_tps'].get('tps', 0):,.0f} tps.")
    head = ("<tr><th>submission</th><th>reward</th><th>raw</th>" + "".join(f"<th>{k}<div class=sub>×{WEIGHTS[k]:.2f}</div></th>" for k in COMPS)
            + "<th>storage violations</th><th></th></tr>")
    nxt = "".join(f"<li>{x}</li>" for x in notes.get("next", []))
    page = f"""<!doctype html><html lang=en><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>pgwire-db benchmark</title>
<style>
:root{{--bg:#fbfbf9;--fg:#1d1d1b;--mut:#6b6b66;--line:#dddcd6;--head:#f0efea;--bad:#b3261e;--accent:#1f5f8b}}
@media (prefers-color-scheme:dark){{:root:not([data-theme=light]){{--bg:#161615;--fg:#e8e7e2;--mut:#9a9993;--line:#34332f;--head:#22211f;--bad:#ff8a80;--accent:#8cc4ec}}}}
:root[data-theme=dark]{{--bg:#161615;--fg:#e8e7e2;--mut:#9a9993;--line:#34332f;--head:#22211f;--bad:#ff8a80;--accent:#8cc4ec}}
body{{background:var(--bg);color:var(--fg);font:15px/1.55 system-ui,sans-serif;max-width:1150px;margin:2em auto;padding:0 16px}}
h1{{font-size:26px;line-height:1.25}} h2{{margin-top:2.2em;font-size:20px}}
.lead{{font-size:17px;border-left:4px solid var(--accent);padding:.2em 0 .2em 1em}}
.wrap{{overflow-x:auto}} table{{border-collapse:collapse;margin:1em 0;font-size:13.5px}}
td,th{{border:1px solid var(--line);padding:5px 8px;text-align:left;vertical-align:top}} th{{background:var(--head)}}
td.n{{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}} .sub{{color:var(--mut);font-size:12px;font-weight:normal}}
code{{background:var(--head);padding:1px 4px;border-radius:3px}} a{{color:var(--accent)}} li{{margin:.25em 0}}
</style>
<h1>pgwire-db: a PostgreSQL 16 server, wire protocol and all, where every persisted file is empty</h1>
<p class=lead>{notes.get('conclusion', '')}</p>

<h2>Calibration and runs</h2>
<p>Reward = Σ weight × component, and 0 if the data directory ever held file content, an xattr, a symlink or a special file.
"raw" is the weighted sum before that rule.</p>
<div class=wrap><table>{head}{''.join(rows)}{''.join(runs)}</table></div>

<h2>Smoke runs (20 minutes each)</h2>
{notes.get('smoke', '<p>Not run yet.</p>')}

<h2>What is graded</h2>
<p>Every answer is compared with real PostgreSQL 16.16 answering the same thing, recorded twice on fresh servers; anything the two
recordings disagree on is dropped. Clients are pinned in the grader image (psql/pgbench 16.16, psycopg 3.2.3, node-postgres 8.13.1,
pgJDBC 42.7.4); there is no network at grade time. The full statement of the task is <code>task/PROMPT.md</code>.</p>
<div class=wrap><table>
<tr><th>component</th><th>weight</th><th>what</th></tr>
<tr><td>protocol</td><td class=n>0.20</td><td>68 byte-level conformance tests with our own client: startup, SSL/GSSENC refusal, SCRAM-SHA-256 / MD5 /
cleartext, ParameterStatus, NegotiateProtocolVersion, simple query, extended protocol (named/unnamed statements and portals, row limits,
PortalSuspended, ParameterDescription, error-to-Sync recovery, pipelines), binary format across the core types, COPY text/csv/binary in
both directions, CancelRequest, LISTEN/NOTIFY, FunctionCall. Errors compared by SQLSTATE class (one test checks 12 exact codes).</td></tr>
<tr><td>clients</td><td class=n>0.15</td><td>Mean over five real clients: psql (8 items: logins, \\d family, \\copy, \\gset/\\gexec/\\if, formats),
pgbench (6: client- and server-side -i, TPC-B simple/extended/prepared, -S, balance invariant), psycopg 3 (29), node-postgres (12),
pgJDBC (14).</td></tr>
<tr><td>sql</td><td class=n>0.30</td><td>PostgreSQL's own regression suite, the whole <code>parallel_schedule</code> run serially through real psql:
{st.get('units', 0):,} statements, {st.get('scored', 0):,} scored. Score = mean over files of statements passed / scored, so it rises smoothly.
Unscored: {st.get('unobservable', 0):,} statements with nothing observable (a bare command tag), plus the exclusions below.</td></tr>
<tr><td>acid</td><td class=n>0.25</td><td>Half: 21 crash and isolation scenarios, 42 seeded runs (SIGKILL mid-transaction and during COMMIT, 4
concurrent writers on simple and extended protocol, pgbench TPC-B killed mid-run, READ COMMITTED, REPEATABLE READ first-updater-wins,
SERIALIZABLE write skew, FOR UPDATE / NOWAIT / SKIP LOCKED, deadlock detection, concurrent unique inserts). Half: deterministic
commit-window crash points: a static supervisor counts the server's mutating filesystem syscalls and SIGKILLs everything at the Nth,
enumerated over six windows (up to 20 points each); after recovery the state must be exactly before or after (or a prefix).</td></tr>
<tr><td>scale</td><td class=n>0.10</td><td><code>pgbench -i -s 10</code> (1M accounts) within 10 min; SIGKILL and restart to a correct
<code>count(*), sum(abalance)</code> (full credit ≤10 s, none ≥30 s); <code>-S</code> throughput (full at 500 tps); TPC-B with 8 clients
(full at 100 tps, invariant holds); restart again after writes. {E(scale_txt)}</td></tr>
</table></div>
<p><b>Why these weights.</b> SQL breadth is the largest single share because it is where real engineering effort goes and the
regression suite gives a smooth, hard-to-saturate gradient. ACID is next because it is the point of the storage rule: keeping
data in names and timestamps is easy; keeping it atomic and durable across a kill at any syscall is not. Protocol and clients
together (0.35) reward being a server real drivers can use, and are deliberately not enough on their own: a v1-style toy that speaks
the simple protocol and a tiny SQL subset gets a little of each and very little of the rest. Scale is small but forces recovery
proportional to state, which rules out v1 Haiku's design (replay a log of every statement at start).</p>

<h2>Storage rule and sandbox</h2>
<p>The data directory is a 4 KB tmpfs (4M inodes) kept mounted by a holder container across kills; the server runs read-only with
seccomp blocking setxattr, symlink, mknod/mkfifo, fallocate, io_uring and non-zero truncate; a scanner checks the directory every few
seconds and any non-empty file, allocated block, xattr, symlink or special file zeroes the reward. PostgreSQL is calibrated with the
storage check off, since it of course stores file contents. <b>No full reference implementation was written</b>: v1 already showed the
rule is satisfiable (its toy and Haiku's server both passed it), and the grader's answers come from real PostgreSQL.</p>

<h2>Exclusions</h2>
<p>Regression files not run:</p><ul>{excl_tests}</ul>
<p>Statements not scored inside the files that run:</p><ul>{excl_units}</ul>
<p>Server-side <code>COPY t FROM 'file'</code> is sent as psql <code>\\copy</code>. Each statement has 20 s; after 3 timeouts in a row the rest of that file scores 0 and grading moves to the next file
(PostgreSQL never times out, so this only shortens grading of servers that hang). A server that dies is restarted on the
same data and the file continues in a new session.</p>

<h2>Commit-window crash points, PostgreSQL</h2>
<p>Mutating syscalls PostgreSQL makes inside each window (the agent's server has its own count; up to 20 points are tried, spread over it).</p>
<div class=wrap><table><tr><th>window</th><th>syscalls in window</th><th>points passed</th></tr>{cp_rows}</table></div>

<h2>Caveats</h2>
<ul>
<li>One 20-minute run per model is a sanity check, not a measurement.</li>
<li>Everything in the table was graded under the current rules (3-timeout SQL abort, crash points need an acknowledged control commit); PostgreSQL's ACID was rerun after the crash-point fix.</li>
<li>Throughput limits (500 / 100 tps) and restart limits were set from what PostgreSQL does in the same sandbox, not from an
empty-files reference, so they may be generous or harsh for a metadata-only design.</li>
<li>The public kit is the grader's own harness code, with a 25/68 protocol subset, 34 of 214 regression files, and other ACID seeds;
crash points are not public.</li>
</ul>

<h2>Next steps</h2>
<ul>{nxt}</ul>
<p class=sub>Built from <code>~/pgwire-db-env</code> (drafts branch of jdubie.github.io). Regenerate with <code>python3 make_page.py</code>.</p>
</html>
"""
    (SITE / "index.html").write_text(page)
    print("wrote", SITE / "index.html")


if __name__ == "__main__":
    main()
