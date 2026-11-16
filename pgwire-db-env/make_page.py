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
MODEL_ORDER = ["opus", "sonnet", "haiku", "sol", "terra", "gemini"]
NAMES = {"opus": "Opus 5.5", "sonnet": "Sonnet 5.5", "haiku": "Haiku 5.5", "sol": "GPT-6.1 Sol", "terra": "GPT-6.1 Terra",
         "gemini": "Gemini 4 Argon"}
COMP_COLOR = {"protocol": "var(--c-p)", "clients": "var(--c-c)", "sql": "var(--c-s)", "acid": "var(--c-a)", "scale": "var(--c-x)"}


def _steps(pts, X, Y) -> str:
    d = f"M{X(0):.1f},{Y(0):.1f}"
    prev = 0.0
    for t, v in pts:
        d += f"H{X(t):.1f}V{Y(v):.1f}"
        prev = v
    return d + f"H{X(20):.1f}V{Y(prev):.1f}"


def _series(run: Path, key: str) -> list[tuple[float, float]]:
    tl = load(run / "timeline.json") or []
    return [(min(20.0, x["t_min"]), float(x.get(key, 0) or 0)) for x in tl]


def timeline_charts() -> str:
    runs = {p.name.split("__")[0]: p for p in (ROOT / "runs/v21").glob("*__r0") if (p / "timeline.json").exists()}
    if not runs:
        return "<p>Not graded yet.</p>"
    W, H, L, R, T, B = 640, 300, 34, 150, 14, 40
    ymax = max(0.1, max(v for r in runs.values() for _, v in _series(r, "reward")))
    ymax = min(1.0, (int(ymax * 10) + 1) / 10)
    X = lambda t: L + (W - L - R) * t / 20  # noqa: E731
    Y = lambda v: T + (H - T - B) * (1 - v / ymax)  # noqa: E731
    g = ""
    for i in range(int(round(ymax * 10)) + 1):
        v = i / 10
        g += f'<line x1="{L}" x2="{W - R}" y1="{Y(v):.1f}" y2="{Y(v):.1f}" stroke="var(--line)"/><text x="{L - 6}" y="{Y(v) + 4:.1f}" text-anchor="end">{v:.1f}</text>'
    for t in range(0, 21, 4):
        g += f'<text x="{X(t):.1f}" y="{H - B + 16}" text-anchor="middle">{t}</text>'
    g += f'<text x="{L}" y="{H - 6}">minutes into the run</text>'
    ends = []
    for m in [m for m in MODEL_ORDER if m in runs]:
        pts = _series(runs[m], "reward")
        g += f'<path d="{_steps(pts, X, Y)}" fill="none" stroke="var(--m-{m})" stroke-width="2.2" stroke-linejoin="round"/>'
        ends.append([Y(pts[-1][1] if pts else 0), m, pts[-1][1] if pts else 0])
    ends.sort()
    for i in range(1, len(ends)):  # keep end labels 14 px apart
        ends[i][0] = max(ends[i][0], ends[i - 1][0] + 14)
    for y, m, v in ends:
        g += f'<text x="{W - R + 6}" y="{y + 4:.1f}" class=n style="fill:var(--m-{m})">{E(NAMES[m])} {v:.3f}</text>'
    main = f'<div class=chart><svg viewBox="0 0 {W} {H}" role=img aria-label="Reward over 20 minutes per model">{g}</svg></div>'
    # one small panel per model: reward (thick) and the five components (thin)
    w, h, l, r, t, b = 300, 170, 30, 8, 22, 26
    panels = ""
    for m in [m for m in MODEL_ORDER if m in runs]:
        x = lambda tt: l + (w - l - r) * tt / 20  # noqa: E731
        y = lambda v: t + (h - t - b) * (1 - v)  # noqa: E731
        p = f'<text x="{l}" y="14" class=n>{E(NAMES[m])}</text>'
        for v in (0, 0.5, 1):
            p += f'<line x1="{l}" x2="{w - r}" y1="{y(v):.1f}" y2="{y(v):.1f}" stroke="var(--line)"/><text x="{l - 4}" y="{y(v) + 4:.1f}" text-anchor="end">{v:g}</text>'
        for tt in (0, 10, 20):
            p += f'<text x="{x(tt):.1f}" y="{h - 8}" text-anchor="middle">{tt}</text>'
        for c in COMPS:
            p += f'<path d="{_steps(_series(runs[m], c), x, y)}" fill="none" stroke="{COMP_COLOR[c]}" stroke-width="1.2" opacity=".85"/>'
        p += f'<path d="{_steps(_series(runs[m], "reward"), x, y)}" fill="none" stroke="var(--m-{m})" stroke-width="2.6"/>'
        panels += f'<svg viewBox="0 0 {w} {h}" role=img aria-label="{E(NAMES[m])}: reward and components over time">{p}</svg>'
    legend = " ".join(f'<span style="color:{COMP_COLOR[c]}">&#9644; {c}</span>' for c in COMPS)
    return main + f'<p class=sub>Per model: reward (thick, model colour) and each component score (thin): {legend}</p><div class=panels>{panels}</div>'

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
    labels = {"v21": ("v2.1, with docs", "agent run"), "smoke": ("v2.0, no docs", "comparison: no /work/docs")}
    for tag in ("v21", "smoke"):
        ver, note = labels[tag]
        for meta in sorted(ROOT.glob(f"runs/{tag}/*/meta.json"), key=lambda p: MODEL_ORDER.index(p.parent.name.split("__")[0])):
            m = json.loads(meta.read_text())
            g = load(meta.parent / "grade/grade.json")
            if not g:
                continue
            runs.append(row(f"{ver}: {NAMES.get(m['model'], m['model'])} ({m['minutes']} min, {len(m['segments'])} sessions)",
                            g["summary"], note))
    chart = timeline_charts()
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
:root{{--bg:#fbfbf9;--fg:#1d1d1b;--mut:#6b6b66;--line:#dddcd6;--head:#f0efea;--bad:#b3261e;--accent:#1f5f8b;
--m-opus:#c2410c;--m-sonnet:#7c3aed;--m-haiku:#0f766e;--m-sol:#1d4ed8;--m-terra:#a16207;--m-gemini:#be185d;
--c-p:#2563eb;--c-c:#9333ea;--c-s:#16a34a;--c-a:#dc2626;--c-x:#78716c}}
@media (prefers-color-scheme:dark){{:root:not([data-theme=light]){{--bg:#161615;--fg:#e8e7e2;--mut:#9a9993;--line:#34332f;--head:#22211f;--bad:#ff8a80;--accent:#8cc4ec;--m-opus:#fb923c;--m-sonnet:#a78bfa;--m-haiku:#2dd4bf;--m-sol:#60a5fa;--m-terra:#facc15;--m-gemini:#f472b6;--c-p:#60a5fa;--c-c:#c084fc;--c-s:#4ade80;--c-a:#f87171;--c-x:#a8a29e}}}}
:root[data-theme=dark]{{--bg:#161615;--fg:#e8e7e2;--mut:#9a9993;--line:#34332f;--head:#22211f;--bad:#ff8a80;--accent:#8cc4ec;--m-opus:#fb923c;--m-sonnet:#a78bfa;--m-haiku:#2dd4bf;--m-sol:#60a5fa;--m-terra:#facc15;--m-gemini:#f472b6;--c-p:#60a5fa;--c-c:#c084fc;--c-s:#4ade80;--c-a:#f87171;--c-x:#a8a29e}}
body{{background:var(--bg);color:var(--fg);font:15px/1.55 system-ui,sans-serif;max-width:1150px;margin:2em auto;padding:0 16px}}
h1{{font-size:26px;line-height:1.25}} h2{{margin-top:2.2em;font-size:20px}}
.lead{{font-size:17px;border-left:4px solid var(--accent);padding:.2em 0 .2em 1em}}
.wrap{{overflow-x:auto}} table{{border-collapse:collapse;margin:1em 0;font-size:13.5px}}
td,th{{border:1px solid var(--line);padding:5px 8px;text-align:left;vertical-align:top}} th{{background:var(--head)}}
td.n{{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}} .sub{{color:var(--mut);font-size:12px;font-weight:normal}}
svg text{{font:11px system-ui,sans-serif;fill:var(--mut)}} svg text.n{{font-weight:600;font-size:12.5px;fill:var(--fg)}}
.chart svg{{display:block;width:100%;max-width:720px}} .panels{{display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:8px 16px}}
code{{background:var(--head);padding:1px 4px;border-radius:3px}} a{{color:var(--accent)}} li{{margin:.25em 0}}
</style>
<h1>pgwire-db v2.1: a PostgreSQL 16 server, wire protocol and all, where every persisted file is empty</h1>
<p class=lead>{notes.get('conclusion', '')}</p>

<h2>Calibration and runs</h2>
<p>Reward = Σ weight × component, and 0 if the data directory ever held file content, an xattr, a symlink or a special file.
"raw" is the weighted sum before that rule.</p>
<div class=wrap><table>{head}{''.join(rows)}{''.join(runs)}</table></div>

<h2>Reward over time, v2.1 sweep (20 minutes each)</h2>
<p>Every 2-minute snapshot of each agent's /work graded with the full hidden grader (identical snapshots share a grade);
the last point is the final /work. A run is 0 until it has a server that starts.</p>
{chart}
{notes.get('sweep', '')}

<h2>v2.0 smoke runs (no docs, 20 minutes each)</h2>
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
