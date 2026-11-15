"""v1 reward over the run, one line per model (mean of its robots): python qa/make_reward_plot.py [TAG]
-> site/reward_v1.png (960x540) and site/thumb.jpg (480x270, the blog thumbnail).

reward = log10(baseline mm / score mm), per robot: 0 is the PD-only controller (no feedforward), +1 is 10x more accurate.
The score at each moment counts a reference never finished as the limp arm, as on the progress plot.
"""
import json
import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
from PIL import Image  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
OUT, SITE = ROOT / "qa" / "out", ROOT / "site"
MODELS = {"codexsol": ("Sol", "#2a78d6"), "codexterra": ("Terra", "#d6559a"), "opus": ("Opus", "#e0704f"),
          "grok": ("Grok", "#6250d6"), "sonnet": ("Sonnet", "#1baf7a"), "haiku": ("Haiku", "#c99400")}
BG, INK, MUTED, GRID = "#f3f4f3", "#1b1b1b", "#6b6b68", "#dcdedc"


def curve(run: Path, meta: dict, grid: np.ndarray, sens: dict) -> np.ndarray:
    seed = str(meta["seed"])
    base = np.mean([sens[n]["pd_only"][seed] for n in sens])
    best = json.loads((OUT / f"limp_{seed}.json").read_text())
    ts, ys = [0.0], [np.mean(list(best.values()))]
    for t, n, mm in json.loads((run / "grade.json").read_text()).get("progress", []):
        best[n] = min(best[n], mm)
        ts.append(t / 60)
        ys.append(np.mean(list(best.values())))
    idx = np.searchsorted(ts, grid, side="right") - 1
    return np.log10(base / np.array(ys)[idx])


def main() -> None:
    tag = sys.argv[1] if len(sys.argv) > 1 else "v1-sweep1"
    sens = json.loads((OUT / "sensitivity_eval.json").read_text())
    oracle = np.mean([np.log10(np.mean([sens[n]["pd_only"][s] for n in sens]) /
                               np.mean([sens[n]["oracle"][s] for n in sens])) for s in ("100", "101")])
    runs: dict[str, list] = {}
    for mp in sorted((ROOT / "runs" / "live" / tag).glob("*/meta.json")):
        m = json.loads(mp.read_text())
        runs.setdefault(m["model"], []).append((mp.parent, m))
    minutes = max(m["budget_min"] for rs in runs.values() for _, m in rs)
    grid = np.linspace(0, minutes, 961)
    fig, ax = plt.subplots(figsize=(9.6, 5.4), dpi=100)
    fig.patch.set_facecolor(BG)
    ax.set_facecolor(BG)
    ends = []
    for key, (lab, col) in MODELS.items():
        if key not in runs:
            continue
        y = np.mean([curve(r, m, grid, sens) for r, m in runs[key]], 0)
        ax.plot(grid, y, color=col, lw=3.2, solid_joinstyle="miter")
        ax.plot(grid[-1], y[-1], "o", color=col, ms=9)
        ends.append([y[-1], lab, col])
    ends.sort(key=lambda e: -e[0])
    ys = []
    for val, *_ in ends:  # keep end labels at least 0.32 apart, top down
        ys.append(val if not ys else min(val, ys[-1] - 0.32))
    for (val, lab, col), ypos in zip(ends, ys):
        ax.text(minutes + 0.25, ypos, f"{lab} {val:+.1f}", color=col, fontsize=17, fontweight="bold", va="center")
    ax.axhline(0, color=MUTED, lw=1.2, ls="--")
    ax.text(minutes - 0.1, 0.07, "baseline: PD only", color=MUTED, fontsize=13, va="bottom", ha="right")
    ax.axhline(oracle, color=MUTED, lw=1.2, ls=":")
    ax.text(0.1, oracle + 0.07, "knows the true dynamics", color=MUTED, fontsize=13, va="bottom")
    ax.set_xlim(0, minutes)
    ax.set_ylim(-1.8, 3.0)
    ax.set_xticks(range(0, int(minutes) + 1, 2), [f"{t}:00" for t in range(0, int(minutes) + 1, 2)])
    ax.tick_params(colors=MUTED, labelsize=14, length=0)
    ax.grid(axis="y", color=GRID, lw=1)
    for s in ax.spines.values():
        s.set_visible(False)
    ax.set_title("YAM tracking reward", loc="left", fontsize=24, fontweight="bold", color=INK, pad=14)
    fig.subplots_adjust(left=0.07, right=0.80, top=0.86, bottom=0.09)
    fig.savefig(SITE / "reward_v1.png", facecolor=BG)
    Image.open(SITE / "reward_v1.png").convert("RGB").resize((480, 270), Image.LANCZOS).save(SITE / "thumb.jpg", quality=92)


if __name__ == "__main__":
    main()
