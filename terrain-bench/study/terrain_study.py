"""Plots of cawro's terrain: what the courses look like and where they stop being driveable.

  node study/dump_terrain.js 1000 > runs/terrain/cawro.json
  uv run --with matplotlib --with numpy python study/terrain_study.py runs/terrain [runs/sweep1]
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402

COL = {"opus": "#c2410c", "sonnet": "#2563eb", "haiku": "#059669", "gemini": "#ca8a04", "none": "#6b7280", "codex": "#7c3aed",
       "sol": "#7c3aed", "terra": "#db2777"}


def first_over(c: dict, deg: float) -> float | None:
    for k, a in enumerate(c["angles"]):
        if math.degrees(a) > deg:
            return c["pts"][k][0]
    return None


def main() -> None:
    out = Path(sys.argv[1])
    sweep = Path(sys.argv[2]) if len(sys.argv) > 2 else None
    d = json.loads((out / "cawro.json").read_text())
    plt.rcParams.update({"font.size": 11, "axes.spines.top": False, "axes.spines.right": False})

    # 1. whole courses, true aspect ratio
    fig, axes = plt.subplots(6, 1, figsize=(16, 13), sharex=True)
    for ax, c in zip(axes, d[:6]):
        p = np.array(c["pts"])
        for k in range(len(p) - 1):
            a = math.degrees(c["angles"][k])
            col = "#777" if abs(a) < 45 else "#e08a00" if abs(a) < 75 else "#c0392b"
            ax.plot(p[k:k + 2, 0], p[k:k + 2, 1], color=col, lw=2.5, solid_capstyle="round")
        ax.set_aspect("equal")
        ax.set_ylabel(f"seed {c['seed']}\ny (m)")
        ax.axvline(p[:, 0].max(), color="#999", ls=":", lw=1)
    axes[-1].set_xlabel("x (m)")
    fig.suptitle("Six cawro courses at true scale (grey <45°, orange 45–75°, red >75° from horizontal)", x=0.01, ha="left")
    fig.tight_layout()
    fig.savefig(out / "courses.png", dpi=110)
    plt.close(fig)

    # 2. close-up of the end of a course
    c = d[0]
    p = np.array(c["pts"])
    fig, ax = plt.subplots(figsize=(14, 6))
    sel = p[:, 0] > 130
    ax.fill_between(p[sel, 0], p[sel, 1], p[sel, 1].min() - 5, color="#ddd")
    ax.plot(p[sel, 0], p[sel, 1], color="#333", lw=2)
    ax.set_aspect("equal")
    ax.set_xlabel("x (m)")
    ax.set_ylabel("y (m)")
    ax.set_title("Seed 0 from x = 130 m to the end: tiles near vertical, some folding back on themselves", loc="left")
    fig.tight_layout()
    fig.savefig(out / "endgame.png", dpi=110)
    plt.close(fig)

    # 3. slope of every tile against x, with the envelope cawro draws from
    fig, ax = plt.subplots(figsize=(14, 6))
    xs = np.concatenate([np.array(c["pts"])[:-1, 0] for c in d[:300]])
    an = np.degrees(np.concatenate([c["angles"] for c in d[:300]]))
    ax.scatter(xs, an, s=2, alpha=0.15, color="#444", lw=0)
    for deg in (30, 45, 60, 90):
        ax.axhline(deg, color="#c0392b", lw=0.8, ls="--")
        ax.axhline(-deg, color="#2563eb", lw=0.8, ls="--")
        ax.text(-4, deg + 2, f"{deg}° up", color="#c0392b", fontsize=9)
    ax.set_xlabel("x where the tile starts (m)")
    ax.set_ylabel("tile slope (degrees, + is uphill)")
    ax.set_title("Every tile of 300 courses: slope range grows linearly with tile index to ±129° at the last tile", loc="left")
    fig.tight_layout()
    fig.savefig(out / "slopes.png", dpi=110)
    plt.close(fig)

    # 4. survival: fraction of courses with no uphill tile steeper than θ before x
    fig, ax = plt.subplots(figsize=(14, 6))
    grid = np.linspace(0, 250, 501)
    for deg, col in ((30, "#9ca3af"), (45, "#e08a00"), (60, "#c0392b"), (75, "#7f1d1d")):
        f = np.array([first_over(c, deg) or 1e9 for c in d])
        ax.plot(grid, [(f > g).mean() for g in grid], color=col, lw=2.5, label=f"no uphill tile > {deg}° yet")
    ends = np.array([np.array(c["pts"])[:, 0].max() for c in d])
    ax.plot(grid, [(ends > g).mean() for g in grid], color="#111", lw=2, ls=":", label="course has not ended yet")
    if sweep:
        for m in sorted(p.name for p in sweep.iterdir() if (p / "meta.json").exists()):
            best = json.loads((sweep / m / "meta.json").read_text())["best_distance"]
            ax.axvline(best, color=COL.get(m, "#000"), lw=1.5, alpha=0.8)
            ax.text(best, 1.02, m, color=COL.get(m, "#000"), rotation=60, fontsize=9, ha="left", va="bottom")
    ax.set_xlabel("x (m)")
    ax.set_ylabel(f"fraction of {len(d)} courses")
    ax.set_ylim(0, 1.0)
    ax.legend(loc="lower left")
    ax.set_title("How far a course stays climbable (vertical lines: sweep1's best distance per agent)", loc="left", pad=40)
    fig.tight_layout()
    fig.savefig(out / "survival.png", dpi=110)
    plt.close(fig)

    # 5. where sweep1's episodes ended
    if sweep:
        fig, ax = plt.subplots(figsize=(14, 5))
        bins = np.arange(0, 250, 5)
        for m in ("none", "haiku", "opus", "sonnet", "gemini"):
            f = sweep / m / "sim" / "episodes.jsonl"
            if not f.exists():
                continue
            ds = [e["distance"] for e in map(json.loads, f.read_text().splitlines()) if e["reason"] == "stalled"]
            ax.hist(ds, bins=bins, histtype="step", lw=2, color=COL[m], density=True, label=f"{m} ({len(ds)} stalls)")
        ax.set_xlabel("distance at stall (m)")
        ax.set_ylabel("density")
        ax.legend()
        ax.set_title("Where sweep1's cars stalled", loc="left")
        fig.tight_layout()
        fig.savefig(out / "stalls.png", dpi=110)
        plt.close(fig)

    stats = {deg: np.median([first_over(c, deg) for c in d]) for deg in (30, 45, 60, 75)}
    (out / "stats.json").write_text(json.dumps({"first_uphill_median_x": stats, "course_end_median_x": float(np.median(ends))}))


if __name__ == "__main__":
    main()
