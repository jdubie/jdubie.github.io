"""Render one terrain-bench run (runs/<tag>) as a static page at runs/<tag>/site/index.html.

  python report.py runs/<tag>
"""

from __future__ import annotations

import html
import json
import sys
from collections import defaultdict
from pathlib import Path

CREDIT = ('Car, courses, physics and score from the <a href="http://rednuht.org/genetic_cars_2/">HTML5 Genetic Algorithm 2D Car '
          'Thingy</a> by Rafael Matsunaga (read from <a href="https://santex.github.io/singularity/seed/idx.html">this copy</a>), '
          'loosely based on <a href="http://boxcar2d.com/">BoxCar2D</a>, on the <a href="https://box2d.org/">Box2D</a> engine.')


def load(run: Path) -> list[dict]:
    return [json.loads(line) for line in (run / "results.jsonl").read_text().splitlines()]


def practice_curve(run: Path, r: dict) -> list[float]:
    p = run / f"{r['model']}-{r['rep']}" / "practice.jsonl"
    return [json.loads(line)["score"] for line in p.read_text().splitlines()] if p.exists() else []


def spark(xs: list[float], w: int = 240, h: int = 40) -> str:
    if len(xs) < 2:
        return f"<span class=dim>{len(xs)} episode{'s' * (len(xs) != 1)}</span>"
    hi = max(xs) or 1
    pts = " ".join(f"{i * w / (len(xs) - 1):.1f},{h - x / hi * h:.1f}" for i, x in enumerate(xs))
    return f'<svg width={w} height={h} viewBox="0 0 {w} {h}"><polyline points="{pts}" fill=none stroke=currentColor stroke-width=1.5 /></svg>'


def main() -> None:
    run = Path(sys.argv[1])
    rs = load(run)
    seeds = json.loads((run / "seeds.json").read_text())
    by_model: dict[str, list[dict]] = defaultdict(list)
    for r in rs:
        by_model[r["model"]].append(r)
    order = sorted(by_model, key=lambda m: -sum(r["mean_score"] for r in by_model[m]) / len(by_model[m]))
    base = by_model.get("none")
    base_mean = sum(r["mean_score"] for r in base) / len(base) if base else None
    lead = order[0]
    lead_mean = sum(r["mean_score"] for r in by_model[lead]) / len(by_model[lead])
    headline = f"{html.escape(lead)} scored {lead_mean:.1f}"
    if base_mean is not None and lead != "none":
        headline += f", against {base_mean:.1f} for the default car with no agent"
    rows = []
    for m in order:
        for r in by_model[m]:
            cells = "".join(f"<td>{s:.1f}<br><span class=dim>{reason}</span></td>" for s, reason in zip(r["scores"], r["reasons"] + [""] * len(seeds)))
            rows.append(f"<tr><th>{html.escape(m)}</th><td><b>{r['mean_score']:.1f}</b></td>{cells}<td>{'yes' if r['has_drive'] else 'no'}</td>"
                        f"<td>{r['practice_episodes']}</td><td>{spark(practice_curve(run, r))}</td></tr>")
    head = "".join(f"<th>course {i + 1}</th>" for i in range(len(seeds)))
    page = f"""<!doctype html><html><head><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>terrain-bench {html.escape(run.name)}</title><style>
:root{{--bg:#fff;--fg:#111;--dim:#777;--line:#ddd}}
@media (prefers-color-scheme:dark){{:root{{--bg:#111;--fg:#eee;--dim:#999;--line:#333}}}}
body{{background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,sans-serif;max-width:1000px;margin:2em auto;padding:0 16px}}
a{{color:inherit}} .dim{{color:var(--dim);font-size:12px}} .wrap{{overflow-x:auto}}
table{{border-collapse:collapse}} th,td{{padding:6px 10px;border-bottom:1px solid var(--line);text-align:right;vertical-align:top}}
th:first-child{{text-align:left}}</style></head><body>
<h1>terrain-bench · {html.escape(run.name)}</h1>
<p><b>{headline}.</b> Score per course is cawro's: furthest x (m) plus average speed (m/s); higher is better. Every model drove the
same {len(seeds)} hidden courses.</p>
<div class=wrap><table><tr><th>model</th><th>mean</th>{head}<th>./drive</th><th>practice eps</th><th>practice score per episode</th></tr>
{''.join(rows)}</table></div>
<p class=dim>{CREDIT}</p></body></html>"""
    out = run / "site"
    out.mkdir(exist_ok=True)
    (out / "index.html").write_text(page)
    print(out / "index.html")


if __name__ == "__main__":
    main()
