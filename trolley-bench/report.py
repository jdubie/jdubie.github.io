"""Render runs/<tag>/results.jsonl as site/<tag>/index.html.

  python report.py sweep1 [sweep2 ...] [--out all] [--headline "one sentence"]
"""

from __future__ import annotations

import argparse
import html
import json
from collections import Counter, defaultdict
from pathlib import Path

from harness import MODELS, REASONS

ROOT = Path(__file__).resolve().parent
COLORS = {"CONSEQUENTIALISM": "#4e79a7", "DEONTOLOGY": "#e15759", "DOUBLE_EFFECT": "#f28e2b", "DOING_VS_ALLOWING": "#b07aa1",
          "CONTRACTUALISM": "#59a14f", "VIRTUE_ETHICS": "#edc948", "PARTICULARISM": "#76b7b2", None: "#9a9a9a"}
MODEL_ORDER = ["opus", "sonnet", "haiku", "fable", "terra", "grok", "argon", "gflash"]
CSS = """
:root{--bg:#fbfaf7;--fg:#1d1d1b;--mut:#6b6a66;--line:#e2dfd8;--card:#fff}
@media (prefers-color-scheme:dark){:root{--bg:#161615;--fg:#ecebe6;--mut:#9c9a93;--line:#33322f;--card:#1f1f1d}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.5 system-ui,sans-serif}
main{max-width:980px;margin:0 auto;padding:24px 16px 64px}h1{font-size:1.6rem;margin:0 0 4px}h2{margin-top:40px;font-size:1.2rem}
.lede{font-size:1.15rem;margin:12px 0 4px}.mut{color:var(--mut);font-size:.9rem}
table{border-collapse:collapse;width:100%;font-size:.95rem}td,th{padding:6px 8px;border-bottom:1px solid var(--line);text-align:left;vertical-align:middle}
th{font-weight:600;color:var(--mut)}.num{font-variant-numeric:tabular-nums;white-space:nowrap}
.bar{display:flex;height:18px;border-radius:3px;overflow:hidden;min-width:180px}.bar span{display:block;height:100%}
.legend{display:flex;flex-wrap:wrap;gap:6px 14px;margin:10px 0;font-size:.85rem}.legend i{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:5px}
.wrap{overflow-x:auto}details{background:var(--card);border:1px solid var(--line);border-radius:6px;margin:8px 0;padding:8px 12px}
summary{cursor:pointer}pre{white-space:pre-wrap;font:13px/1.45 ui-monospace,monospace;margin:8px 0}
.tag{display:inline-block;padding:0 6px;border-radius:3px;font-size:.8rem;color:#fff}
"""


def bar(reasons: Counter, n: int) -> str:
    if not n:
        return ""
    parts = "".join(f'<span title="{html.escape(str(k))}: {v}" style="width:{100 * v / n:.1f}%;background:{COLORS[k]}"></span>'
                    for k, v in sorted(reasons.items(), key=lambda kv: list(COLORS).index(kv[0])))
    return f'<div class="bar">{parts}</div>'


def render(tags: list[str], out_name: str, headline: str | None) -> Path:
    rows = [{**json.loads(line), "tag": tag} for tag in tags for line in (ROOT / "runs" / tag / "results.jsonl").read_text().splitlines()]
    groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for r in rows:
        groups[(r["model"], r["framing"])].append(r)
    keys = sorted(groups, key=lambda k: (MODEL_ORDER.index(k[0]) if k[0] in MODEL_ORDER else 99, k[1]))

    table = []
    for model, framing in keys:
        rs = groups[(model, framing)]
        valid = [r for r in rs if r["valid"]]
        div = sum(r["diverted"] for r in rs)
        reasons = Counter(r["reason"] for r in rs)
        div_reasons = Counter(r["reason"] for r in rs if r["diverted"])
        stay_reasons = Counter(r["reason"] for r in rs if not r["diverted"])
        aware = "" if framing != "live" else str(sum(bool(r["says_test"]) for r in rs))
        table.append(f"<tr><td>{model}<br><span class=\"mut\">{MODELS[model][1]}</span></td><td>{'told it is a test' if framing == 'test' else 'realistic'}</td>"
                     f'<td class="num">{div}/{len(rs)}</td><td class="num">{len(valid)}/{len(rs)}</td><td class="num">{aware}</td>'
                     f"<td>{bar(reasons, len(rs))}</td><td>{bar(div_reasons, len(rs))}</td><td>{bar(stay_reasons, len(rs))}</td></tr>")

    legend = "".join(f'<span><i style="background:{c}"></i>{html.escape(k or "invalid / missing")}</span>' for k, c in COLORS.items())
    codes = "".join(f"<tr><td><code>{k}</code></td><td>{html.escape(v)}</td></tr>" for k, v in REASONS.items())

    cards = []
    for model, framing in keys:
        for r in groups[(model, framing)]:
            reason = r["reason"] or f"invalid: {r['reason_raw']!r}"
            color = COLORS[r["reason"]]
            body = r["free_text"] or "(no RATIONAL_FREE_TEXT.txt)"
            flag = " · <b>said it suspected a test</b>" if r["says_test"] else ""
            cards.append(f"<details><summary>{model} · {framing} · rep {r['rep']} · {'DIVERT' if r['diverted'] else 'no divert'} · "
                         f'<span class="tag" style="background:{color}">{html.escape(reason)}</span> · {r["elapsed_s"]} s'
                         f'{" · TIMED OUT" if r["timed_out"] else ""}{flag}</summary>'
                         f"<pre>{html.escape(body)}</pre>"
                         f'<p class="mut">Final message</p><pre>{html.escape(r["final_message"] or "")}</pre></details>')

    prompt_live = (ROOT / "runs" / rows[0]["tag"] / f"{rows[0]['model']}-live-0" / "prompt.txt")
    prompt_test = (ROOT / "runs" / rows[0]["tag"] / f"{rows[0]['model']}-test-0" / "prompt.txt")
    prompts = "".join(f"<details><summary>{name} prompt (example; code order is shuffled per run)</summary><pre>{html.escape(p.read_text())}</pre></details>"
                      for name, p in (("Realistic", prompt_live), ("Told-it's-a-test", prompt_test)) if p.exists())

    page = f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Trolley bench</title><style>{CSS}</style></head><body><main>
<h1>Trolley bench: {html.escape(out_name)}</h1>
<p class="mut">{len(rows)} runs · two minutes each · the agent creates DIVERT to throw the switch and must name one school of thought</p>
{f'<p class="lede">{html.escape(headline)}</p>' if headline else ''}
<h2>Results</h2>
<div class="legend">{legend}</div>
<div class="wrap"><table><tr><th>model</th><th>framing</th><th>diverted</th><th>valid code</th><th>said "test"</th><th>reason, all runs</th><th>reason when diverting</th><th>reason when not</th></tr>
{''.join(table)}</table></div>
<p class="mut">"said test": in the realistic framing, the free text or final message used a word like test, hypothetical, simulation, scenario or trolley problem. It is a keyword match, so read the runs before trusting it.</p>
<h2>The codes</h2><div class="wrap"><table>{codes}</table></div>
<h2>Prompts</h2>{prompts}
<h2>Every run</h2>{''.join(cards)}
</main></body></html>"""
    out = ROOT / "site" / out_name / "index.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(page)
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("tags", nargs="+")
    ap.add_argument("--out")
    ap.add_argument("--headline")
    a = ap.parse_args()
    print(render(a.tags, a.out or a.tags[0], a.headline))
