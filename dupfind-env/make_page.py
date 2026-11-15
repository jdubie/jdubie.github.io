"""Render site/index.html from the calibration grades (ref/grades) and any agent runs (runs/<tag>/_grade)."""

from __future__ import annotations

import html
import json
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SITE = ROOT / "site"
sys.path.insert(0, str(ROOT / "grader"))
import cases  # noqa: E402

TAG = sys.argv[1] if len(sys.argv) > 1 else "sweep15v2"
REFS = [("naive", "naive baseline: walk + SHA-256 everything, 1 thread"),
        ("smart", "smart Python: size filter, inode dedup, head/tail probe, 8 threads"),
        ("sloppy", "sloppy Python: os.walk + isfile + size + first 64 KiB (wrong on purpose)")]


def load(p: Path) -> dict | None:
    return json.loads(p.read_text()) if p.exists() else None


def cell(r: dict | None) -> str:
    if r is None:
        return "<td class=na>–</td>"
    if not r["correct"]:
        why = html.escape(r.get("why", ""))
        return f"<td class=fail title=\"{why}\">FAIL<br><small>{why[:40]}</small></td>"
    sp = r.get("speedup", 1.0)
    cls = "gate" if r["case"] in cases.GATES else "good" if sp >= 2 else "ok" if sp >= 0.9 else "slow"
    return f"<td class={cls}>{sp:.2f}×<br><small>{r['seconds']:.3f}s</small></td>"


def agg(gs: list[dict]) -> dict:
    """Median speedup per case across reps; a case counts as failed if any rep failed it."""
    by: dict[str, list[dict]] = {}
    for g in gs:
        for r in g["cases"]:
            by.setdefault(r["case"], []).append(r)
    out = []
    for name, rs in by.items():
        bad = [r for r in rs if not r["correct"]]
        if bad:
            out.append({"case": name, "correct": False, "why": f"{len(bad)}/{len(rs)} reps failed: " + bad[0].get("why", "")})
        else:
            out.append({"case": name, "correct": True, "speedup": statistics.median(r["speedup"] for r in rs),
                        "seconds": statistics.median(r["seconds"] for r in rs)})
    rw = [g["summary"]["reward"] for g in gs]
    return {"cases": out, "summary": {"reward": statistics.median(rw), "correct": min(g["summary"]["correct"] for g in gs),
                                      "cases": gs[0]["summary"]["cases"]}, "reps": gs}


def model_table() -> str:
    rows = []
    runs = sorted((ROOT / "runs" / TAG).glob("*/meta.json"))
    by: dict[str, list[dict]] = {}
    for mp in runs:
        m = load(mp)
        if "reward" in m:
            by.setdefault(m["model"], []).append(m)
    for model in sorted(by, key=lambda k: -statistics.mean(x["reward"] for x in by[k])):
        ms = sorted(by[model], key=lambda x: x["rep"])
        rw = [x["reward"] for x in ms]
        fails = sorted({c for x in ms for c in x.get("failed", [])})
        lang = ""
        rows.append(f"<tr><td><b>{model}</b></td><td><b>{statistics.mean(rw):.2f}</b></td><td>{min(rw):.2f}–{max(rw):.2f}</td>"
                    f"<td>{' · '.join(f'{r:.2f}' for r in rw)}</td>"
                    f"<td>{' · '.join(str(x['correct']) + '/' + str(x['cases']) for x in ms)}</td>"
                    f"<td>{' · '.join(str(len(x.get('segments', []))) for x in ms)}</td>"
                    f"<td style='text-align:left'>{html.escape(', '.join(fails)) or '–'}</td></tr>")
    if not rows:
        return "<p>No graded runs for this sweep yet.</p>"
    return ("<table><tr><th>model</th><th>mean reward</th><th>range</th><th>per rep</th><th>correct per rep</th>"
            "<th>CLI sessions per rep</th><th>cases failed in any rep</th></tr>" + "".join(rows) + "</table>")


def main() -> None:
    SITE.mkdir(exist_ok=True)
    base = load(ROOT / "grader/baseline.json") or {}
    cols: list[tuple[str, dict]] = []
    for key, label in REFS:
        g = load(ROOT / "ref/grades" / key / "grade.json")
        if g:
            cols.append((label, g))
    reps: dict[str, list[dict]] = {}
    for g_path in sorted((ROOT / "runs" / TAG / "_grade").glob("*/grade.json")):
        reps.setdefault(g_path.parent.name.split("__")[0], []).append(load(g_path))
    order = sorted(reps, key=lambda m: -statistics.mean(g["summary"]["reward"] for g in reps[m]))
    for m in order:
        cols.append((f"{m} (median of {len(reps[m])})", agg(reps[m])))
    head = "".join(f"<th>{html.escape(lbl)}</th>" for lbl, _ in cols)
    summary = "".join(f"<td><b>{g['summary']['reward']:.3f}</b><br><small>{g['summary']['correct']}/{g['summary']['cases']} correct</small></td>"
                      for _, g in cols)
    rows = []
    for name, fn in cases.HIDDEN.items():
        b = base.get(name, {})
        by = [{r["case"]: r for r in g["cases"]}.get(name) for _, g in cols]
        kind = "gate" if name in cases.GATES else "speed"
        pub = "public + hidden" if name in cases.PUBLIC else "hidden"
        doc = html.escape((fn.__doc__ or "").strip())
        rows.append(f"<tr><td><b>{name}</b><br><small>{kind}, {pub}</small></td><td class=doc>{doc}</td>"
                    f"<td>{b.get('seconds', 0):.3f}s</td><td>{b.get('groups', '')}</td><td>{b.get('paths', '')}</td>"
                    + "".join(cell(r) for r in by) + "</tr>")
    total_naive = sum(v["seconds"] for v in base.values())
    smart = next((g for lbl, g in cols if lbl.startswith("smart")), None)
    sloppy = next((g for lbl, g in cols if lbl.startswith("sloppy")), None)
    headline = "Calibration not finished yet."
    if smart and sloppy:
        headline = (f"The grader works: a reasonable Python finder scores <b>{smart['summary']['reward']:.2f}</b> "
                    f"({smart['summary']['correct']}/{smart['summary']['cases']} correct), and a sloppy one that gets "
                    f"{sloppy['summary']['cases'] - sloppy['summary']['correct']} trap cases wrong scores "
                    f"<b>{sloppy['summary']['reward']:.2f}</b>. No agent has run yet.")
        if len(cols) > len(REFS):
            best = max(cols[len(REFS):], key=lambda c: c[1]["summary"]["reward"])
            headline = (f"Best agent: <b>{html.escape(best[0])}</b> at <b>{best[1]['summary']['reward']:.2f}</b> "
                        f"({best[1]['summary']['correct']}/{best[1]['summary']['cases']} correct). "
                        f"References: smart Python {smart['summary']['reward']:.2f}, sloppy {sloppy['summary']['reward']:.2f}.")
    hl = SITE / "headline.html"
    if hl.exists():
        headline = hl.read_text()
    page = f"""<!doctype html><meta charset=utf-8><title>Find-duplicates agent benchmark</title>
<style>
body{{font:14px/1.45 system-ui,sans-serif;margin:24px;max-width:1500px;color:#1d1d1f}}
h1{{font-size:22px;margin:0 0 6px}} h2{{font-size:17px;margin:26px 0 8px}}
.lead{{font-size:16px;background:#f4f6fa;padding:12px 16px;border-radius:8px}}
table{{border-collapse:collapse;font-size:12.5px}} th,td{{border:1px solid #ddd;padding:4px 7px;text-align:center;vertical-align:top}}
th{{background:#fafafa;max-width:170px}} td.doc{{text-align:left;max-width:420px;color:#444}}
td.good{{background:#d6f5dc}} td.ok{{background:#eef7ee}} td.slow{{background:#fff4d6}} td.fail{{background:#ffd9d9;color:#900}}
td.gate{{background:#e8eefc}} td.na{{color:#aaa}} small{{color:#666}} code{{background:#f2f2f2;padding:1px 4px;border-radius:3px}}
ul{{margin:4px 0 4px 18px;padding:0}}
</style>
<h1>Find-duplicates: an agent speed benchmark with hidden trap cases</h1>
<p class=lead>{headline}</p>

<h2>Models: sweep <code>{TAG}</code></h2>
{model_table()}
<p><small>Each rep is a fresh 15-minute run with the same prompt. Reward is graded on all {len(cases.HIDDEN)} hidden cases.</small></p>

<h2>Task and reward</h2>
<ul>
<li>The agent writes <code>/work/run.sh ROOT</code>, which prints every group of byte-identical files (NUL-separated paths). It may use Python, C, Rust or Go, with no network. Prompt: <code>task/PROMPT.md</code>.</li>
<li>Rules it must get exactly right: symlinks are never followed; hard links to one inode are one file; empty, unreadable and special files are ignored; names are raw bytes; paths can exceed PATH_MAX.</li>
<li>{len(cases.HIDDEN)} hidden cases ({len(cases.HIDDEN) - len(cases.GATES)} timed, {len(cases.GATES)} correctness-only gates), each run in a sandbox with 8 CPUs and 8 GB and a read-only tmpfs fixture.</li>
<li>Per case: <b>speedup = naive time ÷ agent time</b> if exactly right, else 0.01. <b>Reward = geometric mean.</b> Naive = 1.00. The naive baseline takes {total_naive:.0f} s over all cases.</li>
</ul>

<h2>Per-case results</h2>
<p><small>Cells show speedup over naive and wall time (best of 3 when naive is under 3 s). Model columns show the median across reps; a case is FAIL if any rep failed it. Hover a FAIL for the reason.</small></p>
<table><tr><th>case</th><th>what it tests</th><th>naive</th><th>groups</th><th>paths</th>{head}</tr>
<tr><td colspan=5><b>reward</b></td>{summary}</tr>
{"".join(rows)}
</table>

<h2>Caveats and next steps</h2>
<ul>
<li>Fixtures live on tmpfs, so this measures CPU, syscalls and memory bandwidth, not disk. A cold-disk tier would reward I/O ordering instead.</li>
<li>"Circular hard links" can't be built: Linux forbids hard links to directories. The hard-link traps are many names for one inode instead, including one name hidden in an unlistable directory. The cycles come from symlinks (self, mutual, to an ancestor, to <code>/</code>).</li>
<li>v2 adds two mount traps, built on the host with <code>sudo mount</code> before the fixture is bind-mounted into the sandbox: <b>cross_device</b> (two fresh tmpfs mounts filled in the same order, so their inode numbers collide) and <b>bind_alias</b> (a directory bind-mounted at two more places, so one inode has several paths with link count 1). The prompt now says mounts can appear inside the tree.</li>
<li>The weak-hash trap uses CRC32 collisions with matching size, head and tail. No MD5/xxHash collision is planted: every v1 solution confirmed matches with <code>memcmp</code> or SHA-256, so one wouldn't have caught anybody.</li>
<li>Timing is wall clock on a shared 24-core workstation: other sessions' graders were running during calibration. Small cases take the best of 3, so speedups within about 5% are noise.</li>
<li>Sandboxes run with AppArmor unconfined. Docker's default profile returns ENAMETOOLONG past PATH_MAX even for <code>openat</code>, which made <code>deep_pathmax</code> unsolvable, and the naive reference itself failed it. Caps are dropped and default seccomp, no-new-privileges and no network stay on.</li>
<li>No fast compiled reference yet, so the ceiling is unknown. A C finder (getdents walk, size index, tail probe, memcmp of real duplicates, 8 threads) is the next calibration point.</li>
<li>The geometric mean rewards big wins on cases where the size index answers almost everything (flat_sizes, sparse files) as much as raw throughput on big_true_dups.</li>
</ul>
"""
    (SITE / "index.html").write_text(page)
    print("wrote", SITE / "index.html")


if __name__ == "__main__":
    main()
