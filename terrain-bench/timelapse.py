"""Timelapse of a sweep: every model's car design as its run went on, beside the reward plot, on one clock.

  python3 timelapse.py runs/<tag>      -> runs/<tag>/site/timelapse.mp4

Left: one cell per model, drawing the body (chassis, wheel placement, wheel size, density shading) of the model's most
recent episode at that moment of its run, with the number of distinct bodies tried so far and the best distance.
Right: furthest distance so far against minutes into the run, drawn in up to the same moment.
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import cairo

from render import CREDIT, FPS, H, MODELS, W, Frames, badge, rgb, text

PLAY_S, HOLD_S = 40, 4
GEOMETRY = ("chassis", "wheel_vertex", "wheel_radius", "wheel_density")


def load(run: Path) -> list[dict]:
    out = []
    for key in MODELS:
        d = run / key
        if not (d / "meta.json").exists():
            continue
        eps = [json.loads(line) for line in (d / "sim" / "episodes.jsonl").read_text().splitlines()]
        eps = [e for e in eps if "wall_end" in e and "params" in e]
        prog = [json.loads(line) for line in (d / "sim" / "progress.jsonl").read_text().splitlines()]
        t0 = min(e["wall_start"] for e in eps)
        eps.sort(key=lambda e: e["wall_end"])
        timeline, seen = [], {}
        for e in eps:  # (minutes, body params, distinct bodies so far)
            g = json.dumps([e["params"][k] for k in GEOMETRY])
            seen.setdefault(g, len(seen) + 1)
            timeline.append(((e["wall_end"] - t0) / 60, e["params"], len(seen), seen[g]))
        out.append({"key": key, "timeline": timeline, "progress": [(p["t"] / 60, p["best"]) for p in prog],
                    "best": json.loads((d / "meta.json").read_text())["best_distance"]})
    out.sort(key=lambda m: -m["best"])
    return out


def at(seq: list, t: float, default=None):
    """The last element whose first field is <= t."""
    lo, hi = 0, len(seq)
    while lo < hi:
        mid = (lo + hi) // 2
        if seq[mid][0] <= t:
            lo = mid + 1
        else:
            hi = mid
    return seq[lo - 1] if lo else default


def draw_car(c: cairo.Context, p: dict, col: str, logo: str | None, cx: float, cy: float, zoom: float) -> None:
    """The body as render.py draws it in the race, level, chassis origin at (cx, cy)."""
    c.save()
    c.translate(cx, cy)
    c.scale(zoom, -zoom)
    for i in range(2):
        wx, wy = p["chassis"][p["wheel_vertex"][i]]
        r = p["wheel_radius"][i]
        g = round(255 - 255 * (p["wheel_density"][i] - 40) / 100) / 255
        c.new_path()
        c.arc(wx, wy, r, 0, 2 * math.pi)
        c.set_source_rgb(g, g, g)
        c.fill_preserve()
        c.set_source_rgb(0.27, 0.27, 0.27)
        c.set_line_width(1.5 / zoom)
        c.stroke()
    pts = p["chassis"]
    for i in range(8):
        c.move_to(0, 0)
        c.line_to(*pts[i])
        c.line_to(*pts[(i + 1) % 8])
        c.close_path()
    c.set_source_rgb(*rgb(col, 0.82))
    c.fill_preserve()
    c.set_source_rgb(*rgb(col))
    c.set_line_width(2 / zoom)
    c.stroke()
    c.restore()
    if logo:
        badge(c, logo, col, cx, cy, 0.3 * zoom)


def cell(c: cairo.Context, m: dict, t: float, box: tuple) -> None:
    x0, y0, w, h = box
    name, col, logo = MODELS[m["key"]]
    c.set_source_rgb(0.975, 0.975, 0.975)
    c.rectangle(x0 + 4, y0 + 4, w - 8, h - 8)
    c.fill()
    badge(c, logo, col, x0 + 30, y0 + 30, 16)
    text(c, name, x0 + 54, y0 + 38, 22, bold=True)
    cur = at(m["timeline"], t)
    if cur is None:
        text(c, "no episode yet", x0 + w / 2, y0 + h / 2 + 8, 18, col=(0.5, 0.5, 0.5), align="center")
        return
    _, p, n_seen, this = cur
    draw_car(c, p, col, logo, x0 + w / 2, y0 + h * 0.55, min(w, h) / 5.2)
    best = at(m["progress"], t, (0, 0))[1]
    text(c, f"body #{this} of {n_seen} tried", x0 + 16, y0 + h - 16, 17, col=(0.35, 0.35, 0.35))
    text(c, f"best {best:,.0f} m", x0 + w - 16, y0 + h - 16, 17, col=(0.2, 0.2, 0.2), bold=True, align="right")


def plot(c: cairo.Context, models: list[dict], t: float, minutes: float, box: tuple, ymax: float) -> None:
    x0, y0, w, h = box
    L, R, T, B = 80, 210, 50, 50
    pw, ph = w - L - R, h - T - B
    X = lambda m: x0 + L + pw * m / minutes
    Y = lambda d: y0 + T + ph * (1 - d / ymax)
    text(c, "Furthest distance so far", x0 + L, y0 + 26, 26, bold=True)
    c.set_line_width(1)
    step = 250 if ymax > 1000 else 100
    for d in range(0, int(ymax) + 1, step):
        c.set_source_rgb(0.9, 0.9, 0.9)
        c.move_to(X(0), Y(d))
        c.line_to(X(minutes), Y(d))
        c.stroke()
        text(c, f"{d:,} m", X(0) - 10, Y(d) + 6, 16, col=(0.45, 0.45, 0.45), align="right")
    for mi in range(0, int(minutes) + 1, 5 if minutes > 12 else 1):
        text(c, f"{mi} min", X(mi), y0 + h - 18, 16, col=(0.45, 0.45, 0.45), align="center")
    heads = []
    for m in models:
        name, col, logo = MODELS[m["key"]]
        pts = [(a, b) for a, b in m["progress"] if a <= t]
        if not pts:
            continue
        c.move_to(X(pts[0][0]), Y(pts[0][1]))
        for (a0, b0), (a1, b1) in zip(pts, pts[1:]):
            c.line_to(X(a1), Y(b0))
            c.line_to(X(a1), Y(b1))
        c.line_to(X(min(t, minutes)), Y(pts[-1][1]))
        c.set_source_rgb(*rgb(col))
        c.set_line_width(3.5)
        c.stroke()
        heads.append([Y(pts[-1][1]), pts[-1][1], m["key"]])
    heads.sort()
    for i in range(1, len(heads)):  # keep end labels apart
        heads[i][0] = max(heads[i][0], heads[i - 1][0] + 40)
    for ly, d, key in heads:
        name, col, logo = MODELS[key]
        badge(c, logo, col, X(min(t, minutes)), Y(d), 13)
        text(c, f"{name.split(',')[0]}", X(minutes) + 22, ly - 1, 16, bold=True)
        text(c, f"{d:,.0f} m", X(minutes) + 22, ly + 17, 15, col=(0.35, 0.35, 0.35))


def main() -> None:
    run = Path(sys.argv[1])
    models = load(run)
    # the clock runs until the last episode of the sweep ends (agents often spend their episode budget early)
    minutes = min(max(json.loads((run / m["key"] / "meta.json").read_text())["minutes"] for m in models),
                  math.ceil(max(m["timeline"][-1][0] for m in models if m["timeline"]) + 0.5))
    ymax = math.ceil(max(m["best"] for m in models) * 1.08 / 250) * 250
    out = run / "site"
    out.mkdir(exist_ok=True)
    fr = Frames(out / "timelapse.mp4")
    top, foot, left_w = 90, 40, 960
    cols, rows = 2, math.ceil(len(models) / 2)
    cw, ch = left_w / cols, (H - top - foot) / rows
    n = PLAY_S * FPS
    for f in range(n + HOLD_S * FPS):
        t = minutes * min(1.0, f / (n - 1))
        c = fr.ctx()
        text(c, "terrain-bench · each agent's car designs and reward over its run", 40, 58, 34, bold=True)
        text(c, f"{t:4.1f} min", W - 40, 58, 30, col=(0.35, 0.35, 0.35), align="right")
        for i, m in enumerate(models):
            cell(c, m, t, ((i % cols) * cw + 20, top + (i // cols) * ch, cw - 10, ch))
        plot(c, models, t, minutes, (left_w + 20, top, W - left_w - 40, H - top - foot), ymax)
        text(c, CREDIT, 40, H - 14, 18, col=(0.45, 0.45, 0.45))
        fr.emit()
    fr.close()
    print(out / "timelapse.mp4")


if __name__ == "__main__":
    main()
