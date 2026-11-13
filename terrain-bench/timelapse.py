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

import gzip

from render import CREDIT, FPS, H, MODELS, W, Frames, badge, ballast_r, draw_lane, rgb, shade, text, wheel_list

CLIP_SPEED = 8   # the best-run clips in each cell play at this many times real time

PLAY_S, HOLD_S = 40, 4
GEOMETRY = ("chassis", "wheel_vertex", "wheel_radius", "wheel_density")


def body(p: dict) -> str:
    """Identity of a car body: everything but motor settings and where the ballast currently is."""
    if "wheels" in p:
        return json.dumps([p["chassis"], p.get("chassis_density"), p["ballast"]["mass"],
                           [[w["vertex"], w["radius"], w["density"], w["friction"], w["suspension"]] for w in p["wheels"]]])
    return json.dumps([p[k] for k in GEOMETRY])


def axles(p: dict) -> list[tuple[float, float]]:
    if "wheels" in p:
        return [tuple(p["chassis"][w["vertex"]]) for w in p["wheels"]]
    return [tuple(p["chassis"][v]) for v in p["wheel_vertex"]]


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
            g = body(e["params"])
            seen.setdefault(g, len(seen) + 1)
            timeline.append(((e["wall_end"] - t0) / 60, e["params"], len(seen), seen[g]))
        bests, top = [], -1.0   # (minutes, episode id) each time the run's best episode changed, from the recordings
        for e in eps:
            if e["distance"] > top and (d / "sim" / "episodes" / f"{e['episode']}.json.gz").exists():
                top = e["distance"]
                bests.append(((e["wall_end"] - t0) / 60, e["episode"]))
        out.append({"key": key, "timeline": timeline, "bests": bests, "dir": d, "clip": None, "progress": [(p["t"] / 60, p["best"]) for p in prog],
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
    for (wx, wy), (r, dens) in zip(axles(p), wheel_list(p)):
        g = shade(dens, p)
        c.new_path()
        c.arc(wx, wy, r, 0, 2 * math.pi)
        c.set_source_rgb(g, g, g)
        c.fill_preserve()
        c.set_source_rgb(0.27, 0.27, 0.27)
        c.set_line_width(1.5 / zoom)
        c.stroke()
    pts = p["chassis"]
    for i in range(len(pts)):
        c.move_to(0, 0)
        c.line_to(*pts[i])
        c.line_to(*pts[(i + 1) % len(pts)])
        c.close_path()
    c.set_source_rgb(*rgb(col, 0.82))
    c.fill_preserve()
    c.set_source_rgb(*rgb(col))
    c.set_line_width(2 / zoom)
    c.stroke()
    for (wx, wy), w in zip(axles(p), p.get("wheels", [])):   # suspension: the axle's travel, as a line
        s = w.get("suspension")
        if s:
            n = math.hypot(*s["axis"])
            dx, dy = s["axis"][0] / n * s["travel"], s["axis"][1] / n * s["travel"]
            c.move_to(wx - dx, wy - dy)
            c.line_to(wx + dx, wy + dy)
            c.set_source_rgb(0.85, 0.1, 0.1)
            c.set_line_width(3 / zoom)
            c.stroke()
    if "ballast" in p and p["ballast"]["mass"] > 0:
        c.new_path()
        c.arc(*p["ballast"]["pos"], ballast_r(p), 0, 2 * math.pi)
        c.set_source_rgb(0.15, 0.15, 0.15)
        c.fill()
    c.restore()
    if logo:
        badge(c, logo, col, cx, cy, 0.3 * zoom)


def clip(c: cairo.Context, m: dict, t: float, box: tuple) -> None:
    """The model's best episode so far, looping at CLIP_SPEED, camera following the car."""
    cur = at(m["bests"], t)
    if cur is None:
        return
    st = m["clip"]
    if st is None or st["episode"] != cur[1]:   # a new best: load it and start it from the beginning
        ep = json.loads(gzip.open(m["dir"] / "sim" / "episodes" / f"{cur[1]}.json.gz").read())
        ep["changes"] = [[k // ep["stride"], p] for k, p in ep["changes"]]
        st = m["clip"] = {"episode": cur[1], "ep": ep, "row": 0.0, "cam": [ep["track"][0][0], ep["track"][0][1]]}
    ep = st["ep"]
    rows = len(ep["track"])
    k = int(st["row"]) % rows
    st["row"] += CLIP_SPEED * 60 / ep["stride"] / FPS
    if k < st["row"] - CLIP_SPEED * 60 / ep["stride"] / FPS and int(st["row"]) % rows < k:  # looped: snap the camera back
        st["cam"] = [ep["track"][0][0], ep["track"][0][1]]
    tr = ep["track"][k]
    st["cam"][0] += (tr[0] - st["cam"][0]) * 0.5
    st["cam"][1] += (tr[1] - st["cam"][1]) * 0.3
    x0, y0, w, h = box
    c.set_source_rgb(1, 1, 1)
    c.rectangle(x0, y0, w, h)
    c.fill()
    draw_lane(c, {"key": m["key"], "best": ep}, k, st["cam"], box, min(h, w * 0.6) / 6.0, 1.0, label=False)
    run_x = max(r[0] for r in ep["track"][:k + 1])
    text(c, f"best run so far · {run_x:,.0f} m", x0 + 8, y0 + 20, 15, col=(0.3, 0.3, 0.3))


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
    side = min(w * 0.36, h)   # left: the body being tried now; right: the best run so far, driving
    draw_car(c, p, col, logo, x0 + side / 2 + 6, y0 + h * 0.55, min(side, h) / 5.0)
    text(c, "trying now", x0 + side / 2 + 6, y0 + h * 0.55 + min(side, h) * 0.42, 14, col=(0.45, 0.45, 0.45), align="center")
    clip(c, m, t, (x0 + side + 12, y0 + 48, w - side - 20, h - 80))
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
    # the clock stops a little after the last time any model's best improved: after that nothing changes on screen
    last_gain = max(next(t for t, b in m["progress"] if b >= m["progress"][-1][1] - 1e-6) for m in models if m["progress"])
    minutes = min(max(json.loads((run / m["key"] / "meta.json").read_text())["minutes"] for m in models),
                  math.ceil(last_gain * 1.08 + 0.5))
    ymax = math.ceil(max(m["best"] for m in models) * 1.08 / 250) * 250
    out = run / "site"
    out.mkdir(exist_ok=True)
    fr = Frames(out / "timelapse.mp4")
    top, foot, left_w = 90, 40, 960
    cols, rows = 2, math.ceil(len(models) / 2)
    cw, ch = left_w / cols, min((H - top - foot) / rows, 320)   # a lone model gets a normal-sized cell, not the whole column
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
