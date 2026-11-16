"""Render site/index.html from calib/*/grade.json, specs/seed-*.json, the Opus run's graded snapshots, and site/notes.json.

  python3 make_page.py
notes.json: {"conclusion": html, "opus": html, "probing": html, "caveats": [html], "next": [html]}
"""

from __future__ import annotations

import html
import json
import re
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SITE = ROOT / "site"
RUN = ROOT / "runs/opus20/opus__r0"
E = html.escape
COMPS = [("sql", 0.45, "var(--s1)"), ("protocol", 0.25, "var(--s2)"), ("durability", 0.30, "var(--s3)")]
CALIB = [  # dir, label, what it shows
    ("oracle-7101", "The oracle itself (seed 7101)", "a fresh copy of the reference: the grader is deterministic"),
    ("postgres", "Stock PostgreSQL 16", "perfect memorisation of PostgreSQL, no reverse engineering"),
    ("abl-wire", "Oracle with only the wire layer", "PostgreSQL plus a perfectly reverse-engineered wire format, dialect and semantics as stock"),
    ("abl-wire-semantics", "Oracle with wire + semantics", "all but the renamed keywords, types and functions"),
    ("abl-wire-dialect", "Oracle with wire + dialect", "all but SQLSTATE, value formatting, tags, column case, dates"),
    ("cheat", "Record-and-replay cheat", "replays every request/response pair recorded by probing with the grader's own generator on 2 other seeds"),
    ("cheat-v0", "Same cheat, before the fix", "first version of the workloads, where logins were byte-identical across seeds (see below)"),
    ("cross-7102-on-7101", "Perfect seed-7102 server, graded on 7101", "a server built for another variant"),
    ("cross-7101-on-7102", "Perfect seed-7101 server, graded on 7102", "the same, the other way round"),
]


def load(p: Path):
    return json.loads(p.read_text()) if p.exists() else None


def f3(v) -> str:
    return "–" if v is None else f"{v:.3f}"


def calib_rows() -> str:
    out = []
    for d, label, what in CALIB:
        g = load(ROOT / "calib" / d / "grade.json")
        g1 = load(ROOT / "calib/v1" / d / "grade.json")
        s = g["summary"] if g else {}
        s1 = g1["summary"] if g1 else {}
        cells = "".join(f"<td class=n>{f3(s.get(k))}</td>" for k, _, _ in COMPS)
        detail = f"{s.get('sql_perfect', '–')} · {s.get('protocol_perfect', '–')} · {s.get('durability_runs', '–')}" if s else "–"
        out.append(f"<tr><td><b>{E(label)}</b><div class=sub>{E(what)}</div></td><td class=n><b>{f3(s.get('reward'))}</b></td>"
                   f"{cells}<td class=n>{detail}</td><td class=n>{f3(s1.get('reward'))}</td></tr>")
    return "".join(out)


def variant_rows() -> str:
    specs = [load(p)["summary"] for p in sorted((ROOT / "specs").glob("seed-*.json"))]
    keys = [("framing", "Framing"), ("type_bytes", "Type bytes"), ("handshake", "Handshake"), ("rowdesc_order", "Column-description field order"),
            ("keywords", "Keywords"), ("types", "Types"), ("functions", "Functions"), ("param_prefix", "Parameter marker"),
            ("sqlstate", "SQLSTATE classes"), ("values", "Value formatting"), ("tags_cols", "Tags, columns, severity"), ("product", "Name it reports")]
    head = "<tr><th></th>" + "".join(f"<th>seed {s['seed']}{' (the task)' if s['seed'] == 7101 else ''}</th>" for s in specs) + "</tr>"
    rows = "".join(f"<tr><th>{E(lbl)}</th>" + "".join(f"<td>{E(str(s[k]))}</td>" for s in specs) + "</tr>" for k, lbl in keys)
    return head + rows


def timeline(tag: str = "opus20") -> list[dict]:
    tl = load(ROOT / "results" / tag / "timeline.json") or []
    pts = [{"t": x["t_min"], "name": x["snapshot"], **{k: x.get(k, 0) for k in ("reward", "sql", "protocol", "durability")},
            "sqlp": x.get("sql_perfect"), "pp": x.get("protocol_perfect"), "dr": x.get("durability_runs")} for x in tl]
    pts.sort(key=lambda x: x["t"])
    return pts


def chart(pts: list[dict], series: list | None = None, aria: str = "Opus 5.5 reward and components over 20 minutes") -> str:
    """series: (key or (run index, key), label, colour, width); default = one run's reward and components."""
    if not pts:
        return "<p>Not graded yet.</p>"
    W, H, L, R, T, B = 720, 320, 40, 150, 16, 42
    X = lambda t: L + (W - L - R) * t / 20  # noqa: E731
    Y = lambda v: T + (H - T - B) * (1 - v)  # noqa: E731
    g = ""
    for i in range(0, 11, 2):
        v = i / 10
        g += f'<line x1="{L}" x2="{W - R}" y1="{Y(v):.1f}" y2="{Y(v):.1f}" class=grid /><text x="{L - 6}" y="{Y(v) + 4:.1f}" text-anchor="end">{v:.1f}</text>'
    for t in range(0, 21, 4):
        g += f'<text x="{X(t):.1f}" y="{H - B + 18}" text-anchor="middle">{t}</text>'
    g += f'<text x="{(L + W - R) / 2:.0f}" y="{H - 6}" text-anchor="middle">minutes into the run</text>'
    runs = pts if pts and isinstance(pts[0], list) else [pts]
    series = series or [((0, "reward"), "reward", "var(--fg)", 2.6)] + [((0, k), k, c, 2) for k, _, c in COMPS]
    ends = []
    for (ri, key), label, col, wdt in series:
        rp = runs[ri]
        full = [{"t": 0, "reward": 0, "sql": 0, "protocol": 0, "durability": 0}] + rp
        d = " ".join(f"{'M' if i == 0 else 'L'}{X(p['t']):.1f},{Y(p[key]):.1f}" for i, p in enumerate(full))
        g += f'<path d="{d}" fill="none" stroke="{col}" stroke-width="{wdt}" stroke-linejoin="round"/>'
        for p in rp:
            g += f'<circle cx="{X(p["t"]):.1f}" cy="{Y(p[key]):.1f}" r="{4 if key == "reward" else 3}" fill="{col}" stroke="var(--bg)" stroke-width="1.5"/>'
        ends.append([Y(full[-1][key]), label, col, full[-1][key]])
    ends.sort()
    for i in range(1, len(ends)):
        ends[i][0] = max(ends[i][0], ends[i - 1][0] + 15)
    for y, label, col, v in ends:
        g += f'<line x1="{W - R + 2}" x2="{W - R + 14}" y1="{y:.1f}" y2="{y:.1f}" stroke="{col}" stroke-width="3"/>'
        g += f'<text x="{W - R + 18}" y="{y + 4:.1f}" class=n>{E(label)} {v:.3f}</text>'
    # hover targets: one column per snapshot (of the last run drawn)
    for p in runs[-1]:
        tip = f"{p['name']}: reward {p['reward']:.3f} · sql {p['sql']:.3f} ({p['sqlp']} exact) · protocol {p['protocol']:.3f} ({p['pp']}) · durability {p['durability']:.3f} ({p['dr']})"
        g += f'<rect x="{X(p["t"]) - 9:.1f}" y="{T}" width="18" height="{H - T - B}" class=hit><title>{E(tip)}</title></rect>'
    return f'<div class=chart><svg viewBox="0 0 {W} {H}" role=img aria-label="{E(aria)}">{g}</svg></div>'


def table(pts: list[dict]) -> str:
    if not pts:
        return ""
    rows = "".join(f"<tr><td>{E(p['name'])}</td><td class=n><b>{p['reward']:.3f}</b></td><td class=n>{p['sql']:.3f}</td><td class=n>{p['protocol']:.3f}</td>"
                   f"<td class=n>{p['durability']:.3f}</td><td class=n>{p['sqlp']} · {p['pp']} · {p['dr']}</td></tr>" for p in pts)
    return (f"<details><summary>Table view</summary><div class=wrap><table><tr><th>snapshot</th><th>reward</th><th>sql</th><th>protocol</th>"
            f"<th>durability</th><th>exact sessions · exact protocol · crash runs</th></tr>{rows}</table></div></details>")


def main() -> None:
    notes = load(SITE / "notes.json") or {}
    pts = timeline()
    pts2 = timeline("opus20v2")
    cal2 = "".join(
        f"<tr><td>{E(lbl)}</td><td class=n><b>{f3(g['summary'].get('reward'))}</b></td>"
        + "".join(f"<td class=n>{f3(g['summary'].get(k))}</td>" for k, _, _ in COMPS)
        + f"<td class=n>{g['summary'].get('sql_perfect')} · {g['summary'].get('protocol_perfect')} · {g['summary'].get('durability_runs')}</td></tr>"
        for d, lbl in (("oracle", "The oracle itself"), ("postgres", "Stock PostgreSQL 16"))
        if (g := load(ROOT / "results/opus20v2/calib" / d / "summary.json")))
    compare = chart([pts, pts2], [((0, "reward"), "run 1 (v1)", "var(--mut)", 2), ((1, "reward"), "run 2 (v2)", "var(--fg)", 2.6)],
                    "Reward over time, both Opus runs") if pts2 else ""
    (SITE / "specs").mkdir(parents=True, exist_ok=True)
    for p in (ROOT / "specs").glob("seed-*.json"):
        shutil.copy(p, SITE / "specs" / p.name)
    shutil.copy(ROOT / "task/PROMPT.md", SITE / "PROMPT.md")
    lis = lambda k: "".join(f"<li>{x}</li>" for x in notes.get(k, []))  # noqa: E731
    page = f"""<!doctype html><html lang=en><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>Black-box database</title>
<style>
:root{{color-scheme:light;--bg:#fcfcfb;--fg:#0b0b0b;--mut:#52514e;--line:#e2e1dc;--head:#f1f0ec;--accent:#2a78d6;
--s1:#2a78d6;--s2:#eb6834;--s3:#1baf7a}}
@media (prefers-color-scheme:dark){{:root:not([data-theme=light]){{color-scheme:dark;--bg:#1a1a19;--fg:#f0efec;--mut:#c3c2b7;--line:#363532;--head:#252523;--accent:#3987e5;--s1:#3987e5;--s2:#d95926;--s3:#199e70}}}}
:root[data-theme=dark]{{color-scheme:dark;--bg:#1a1a19;--fg:#f0efec;--mut:#c3c2b7;--line:#363532;--head:#252523;--accent:#3987e5;--s1:#3987e5;--s2:#d95926;--s3:#199e70}}
body{{background:var(--bg);color:var(--fg);font:15px/1.55 system-ui,sans-serif;max-width:1100px;margin:2em auto;padding:0 16px}}
h1{{font-size:26px;line-height:1.25}} h2{{margin-top:2.2em;font-size:20px}}
.lead{{font-size:17px;border-left:4px solid var(--accent);padding:.2em 0 .2em 1em}}
.wrap{{overflow-x:auto}} table{{border-collapse:collapse;margin:1em 0;font-size:13.5px}}
td,th{{border:1px solid var(--line);padding:5px 8px;text-align:left;vertical-align:top}} th{{background:var(--head)}}
td.n{{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}} .sub{{color:var(--mut);font-size:12.5px;font-weight:normal}}
svg text{{font:11.5px system-ui,sans-serif;fill:var(--mut)}} svg text.n{{font-weight:600;font-size:12.5px;fill:var(--fg)}}
svg .grid{{stroke:var(--line)}} svg .hit{{fill:transparent}} svg .hit:hover{{fill:var(--line);opacity:.35}}
.chart svg{{display:block;width:100%;max-width:760px}}
code{{background:var(--head);padding:1px 4px;border-radius:3px;font-size:13px}} a{{color:var(--accent)}} li{{margin:.3em 0}}
</style>
<h1>Black-box database: rebuild a server you can only probe</h1>
<p class=lead>{notes.get('conclusion', '')}</p>

<h2>The task</h2>
<p>The model gets a TCP address (<code>oracle:7000</code>), two logins and a database name, and nothing else: no spec, no docs,
no client library, no internet. It must work out the protocol and SQL behaviour by experiment and build a server, from scratch,
that answers byte for byte the same and survives SIGKILL. The oracle is PostgreSQL 16.16 behind a seeded translating proxy, so
every reference answer is free and exact. The prompt deliberately <b>does not say</b> the server is derived from a well-known
open-source database: recognising it is part of the task, and saying so would hand over the prior that the perturbations exist to
devalue. Full prompt: <a href="PROMPT.md">PROMPT.md</a>.</p>

<h2>Calibration</h2>
<p>reward = 0.45 sql + 0.25 protocol + 0.30 durability. Last column: SQL sessions answered exactly (of 60) · protocol workloads
exactly (of 50) · crash runs passed (of 14). All graded on variant 7101 with the hidden workload seed unless the row says otherwise.
Columns are oracle v2 (the current benchmark); the last column is the same server graded against oracle v1, the version the Opus run saw.</p>
<div class=wrap><table><tr><th>server graded</th><th>reward</th><th>sql<div class=sub>×0.45</div></th><th>protocol<div class=sub>×0.25</div></th>
<th>durability<div class=sub>×0.30</div></th><th>exact / exact / passed</th><th>reward on oracle v1</th></tr>{calib_rows()}</table></div>
{notes.get('calibration', '')}

<h2>Opus 5.5, 20 minutes: two runs</h2>
<p>Both runs: same harness, same prompt, 20 minutes, every 2-minute snapshot of <code>/work</code> graded with the full hidden grader
(the last point is the final <code>/work</code>). Hover a column for the numbers.</p>
{compare}
<h3>Run 2: oracle v2 (discovery hints), fresh seeds</h3>
<p>A new variant seed and a new workload seed, drawn at random and kept off the repo (7101 and 90210 are public on the drafts branch).
The variant comes from the same generator as the table below, but its spec is not published. Calibration on these seeds, before
grading the run:</p>
<div class=wrap><table><tr><th>server graded</th><th>reward</th><th>sql</th><th>protocol</th><th>durability</th><th>exact / exact / passed</th></tr>{cal2}</table></div>
{notes.get('opus2', '<p>Running.</p>')}
{chart(pts2, aria="Opus 5.5 run 2 reward and components over 20 minutes")}
{table(pts2)}
{notes.get('probing2', '')}
<h3>Run 1: oracle v1 (no discovery channel), seed 7101</h3>
{notes.get('opus', '<p>Not run yet.</p>')}
{chart(pts, aria="Opus 5.5 run 1 reward and components over 20 minutes")}
{table(pts)}
<h4>How it probed</h4>
{notes.get('probing', '<p>–</p>')}

<h2>What each seed changes</h2>
<p>Every change is a pure function of the seed (<code>oracle/variant.py</code>) and applied exactly by the proxy, both ways.
The full hidden specs: <a href="specs/seed-7101.json">7101</a>, <a href="specs/seed-7102.json">7102</a>, <a href="specs/seed-7103.json">7103</a>.</p>
<div class=wrap><table>{variant_rows()}</table></div>
<p><b>Wire layer:</b> fresh type byte for every message in each direction; length as big- or little-endian u32 (counting itself or
not) or a varint, before or after the type byte; integers big- or little-endian; strings NUL-terminated or u16-prefixed; the server
speaks first with a greeting carrying the protocol magic; startup keys renamed; seeded codes for auth-ok, password request, cancel and
encryption requests; 7 to 10 of PostgreSQL's 14 startup ParameterStatus kept, renamed; column descriptions lose table OID and attribute
number, keep five fields in a seeded order; type OIDs remapped; error/notice field codes remapped and emitted in a seeded order (source
file/line/routine dropped); ReadyForQuery status bytes and Describe/Close sub-types remapped.
<b>Dialect layer:</b> 60% of 60 common keywords, 4 of 8 type families (all aliases), half of 18 functions plus <code>pg_sleep</code>
get invented names; the old spellings become ordinary identifiers (so <code>SELECT 1</code> is a syntax error); parameter marker
<code>$</code>, <code>?</code> or <code>:</code>; error positions mapped back to the client's text; PostgreSQL's spellings in tags,
messages and default column names translated back.
<b>Semantics layer:</b> SQLSTATE classes permuted; severity words renamed; booleans, integers (plain, <code>+5</code>, <code>1_000</code>),
floats (shortest, <code>%.6g</code>, <code>%.3f</code>, <code>%.4e</code>) and numerics (trailing zeros trimmed or not) reformatted;
DateStyle set at login; INSERT tags without the OID or count-first; column names upper-cased or not; product name and version.
Nothing needs a new engine: the proxy only rewrites what PostgreSQL computes.</p>

<h2>What is graded</h2>
<p>Grading never touches the oracle the model saw. A hidden workload seed generates fresh tables, values and statement mixes; the
workloads are rendered into the variant's bytes, played against a fresh oracle (transcripts cached per seed) and against the candidate,
and every reply message is compared byte for byte: 1 for identical, up to 0.5 for the right message type with some fields right, 0
otherwise; replies are aligned by message type within each step, and a step's denominator is the larger message count (plus one for a
connection the oracle closes). BackendKeyData is compared by shape only.</p>
<div class=wrap><table><tr><th>component</th><th>weight</th><th>what</th></tr>
<tr><td>sql</td><td class=n>0.45</td><td>60 sessions as <code>guest</code>, ~30 statements each, one request at a time: CREATE/ALTER/DROP, multi-row INSERT
with RETURNING, filters, ORDER BY/LIMIT/OFFSET, GROUP BY/HAVING, aggregates, DISTINCT, CASE, casts, joins, EXISTS/IN subqueries, UNION,
built-in functions, 15 kinds of error (unique, not-null, check, FK, division by zero, unknown column/table/function, bad input, syntax,
overflow...), transactions with rollback, savepoints, aborted-transaction errors, multi-statement requests. Scored after login; a session
that cannot log in scores 0.</td></tr>
<tr><td>protocol</td><td class=n>0.25</td><td>25 templates × 2: both logins, wrong password, unknown user/database, missing user, default database,
bad magic, unknown startup key, encryption request; the extended flow (parse/bind/describe/execute/sync, named statements, parameter
descriptions, NoData, row limits with suspension, errors skipping to sync, 4-sync pipelines, flush, close, bad sub-type); multi-statement and
empty requests; transaction status; unknown type, impossible length, malformed body; cancel of a running sleep from a second connection;
terminate; ParameterStatus after SET. Scored from the first byte.</td></tr>
<tr><td>durability</td><td class=n>0.30</td><td>7 scenarios × 2 seeds: 2,000 single inserts; 1,500 mixed insert/update/delete; 800 multi-statement
transactions; 4 concurrent connections of money transfers; an open uncommitted transaction; DDL (create, index, add column, drop);
two crashes in a row. SIGKILL at a random moment or right after the last acknowledgement, restart, query. Pass = the state equals the
oracle's after replaying the acknowledged commits plus some subset of the in-flight ones. Verified with plain <code>SELECT * FROM t</code>,
rows as a multiset, integers canonicalised, so only lost, torn or phantom writes fail; the writes themselves use only
<code>INSERT ... VALUES</code>, UPDATE/DELETE with literals (transfers <code>bal = bal ± n</code>), BEGIN/COMMIT, and in the DDL
scenario CREATE INDEX / ALTER TABLE ADD COLUMN / DROP TABLE.</td></tr></table></div>
<p><b>Why these weights.</b> SQL behaviour is most of what "behaves identically" means and gives the smoothest gradient, so it
gets the largest share. Durability is 0.30 because the brief makes persistence and crash safety first-class, and because it cannot be
faked by replay. Protocol is 0.25: the handshake and framing are the first wall a model hits, and the extended flow and error paths are
where a reverse-engineer who stops at "simple query works" leaves points. Every component is gated on the reverse-engineered protocol:
a server that cannot log in earns nothing anywhere, which is what makes memorising PostgreSQL worthless.</p>

<h2>Sandbox</h2>
<p>Per run, a private internal Docker network holds three containers: the agent (Python 3.12 without sqlite3, gcc, Rust, Go,
<code>nc</code>, <code>hexdump</code>, <code>strace</code>; no PostgreSQL client, library or documentation), one oracle (alias <code>oracle</code>), and an
egress proxy that allows CONNECT to <code>api.anthropic.com</code> only (checked: PyPI is refused). The oracle's PostgreSQL roles are
non-superusers and the proxy's code and seed are unreadable by the database user, so SQL cannot read the variant spec off disk.
Grading runs the candidate read-only with 4 CPUs and 6 GB, data in a Docker volume that survives SIGKILL.</p>

<h2>Caveats</h2>
<ul>{lis('caveats')}</ul>
<h2>Next steps</h2>
<ul>{lis('next')}</ul>
<p class=sub>Built from <code>~/blackbox-db-env</code> (drafts branch of jdubie.github.io). Regenerate with <code>python3 make_page.py</code>.</p>
</html>
"""
    (SITE / "index.html").write_text(page)
    print("wrote", SITE / "index.html")


if __name__ == "__main__":
    main()
