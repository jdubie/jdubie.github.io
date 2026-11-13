"""Render a terrain-bench sweep as one video: the race, then reward over time.

  python3 render.py runs/<tag>            -> runs/<tag>/site/terrain_bench.mp4

Part 1, the race: every model's furthest episode (sim/best.json), replayed side by side from the same start, one lane
each, drawn the way the original page draws its cars (grey tiles, outlined chassis triangles, wheels shaded by density,
a spoke for rotation) with the model's company logo on the chassis. Part 2: furthest distance so far against run time
(sim/progress.jsonl), drawn in.

Needs pycairo and ffmpeg (no numpy).
"""

from __future__ import annotations

import bisect
import html
import json
import math
import subprocess
import sys
from pathlib import Path

import cairo

ROOT = Path(__file__).resolve().parent
W, H, FPS = 1920, 1080, 30
RACE_TARGET_S = 30           # the race is sped up to fit roughly this long
GROUND_H = 0.15
MODELS = {  # key: (display name, colour, logo file or None)
    "opus": ("Opus 5.5", "#c2410c", "anthropic"),
    "sonnet": ("Sonnet 5.5", "#2563eb", "anthropic"),
    "haiku": ("Haiku 5.5", "#059669", "anthropic"),
    "sol": ("GPT-6.1 Sol", "#7c3aed", "openai"),
    "terra": ("GPT-6.1 Terra", "#db2777", "openai"),
    "gemini": ("Gemini 4 Argon", "#ca8a04", "google"),
    "none": ("Default car, no agent", "#6b7280", None),
}
CREDIT = ("Car, terrain and physics: HTML5 Genetic Algorithm 2D Car Thingy by Rafael Matsunaga (rednuht.org), "
          "after BoxCar2D, on Box2D.")
FONT = "DejaVu Sans"


def rgb(hexcol: str, mix_white: float = 0.0) -> tuple[float, float, float]:
    c = [int(hexcol[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    return tuple(x + (1 - x) * mix_white for x in c)


def load(run: Path) -> list[dict]:
    out = []
    for key in MODELS:
        d = run / key / "sim"
        if not (d / "best.json").exists() or not (run / key / "meta.json").exists():  # finished runs only
            continue
        best = json.loads((d / "best.json").read_text())
        prog = [json.loads(line) for line in (d / "progress.jsonl").read_text().splitlines()]
        out.append({"key": key, "best": best, "progress": prog})
    out.sort(key=lambda m: -m["best"]["distance"])
    return out


class Frames:
    def __init__(self, path: Path):
        self.proc = subprocess.Popen(["ffmpeg", "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "bgra", "-s", f"{W}x{H}",
                                      "-r", str(FPS), "-i", "-", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "20",
                                      "-movflags", "+faststart", str(path)], stdin=subprocess.PIPE)
        self.surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, W, H)

    def ctx(self) -> cairo.Context:
        c = cairo.Context(self.surface)
        c.set_source_rgb(1, 1, 1)
        c.paint()
        return c

    def emit(self, n: int = 1) -> None:
        self.surface.flush()
        data = bytes(self.surface.get_data())
        for _ in range(n):
            self.proc.stdin.write(data)

    def close(self) -> None:
        self.proc.stdin.close()
        self.proc.wait()


def text(c: cairo.Context, s: str, x: float, y: float, size: float, col=(0.1, 0.1, 0.1), bold=False, align="left") -> None:
    c.select_font_face(FONT, cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD if bold else cairo.FONT_WEIGHT_NORMAL)
    c.set_font_size(size)
    w = c.text_extents(s).x_advance
    c.move_to(x - (w if align == "right" else w / 2 if align == "center" else 0), y)
    c.set_source_rgb(*col)
    c.show_text(s)


LOGOS: dict[str, cairo.ImageSurface] = {}


def badge(c: cairo.Context, logo: str | None, col: str, x: float, y: float, r: float, angle: float = 0.0) -> None:
    """White disc with a coloured ring and the company logo, centred at (x, y) in device units."""
    c.save()
    c.new_path()
    c.translate(x, y)
    c.rotate(angle)
    c.arc(0, 0, r, 0, 2 * math.pi)
    c.set_source_rgb(1, 1, 1)
    c.fill_preserve()
    c.set_source_rgb(*rgb(col))
    c.set_line_width(max(1.5, r * 0.12))
    c.stroke()
    if logo:
        img = LOGOS.setdefault(logo, cairo.ImageSurface.create_from_png(str(ROOT / "assets" / "logos" / f"{logo}.png")))
        s = 1.25 * r / img.get_width()
        c.scale(s, s)
        c.set_source_surface(img, -img.get_width() / 2, -img.get_height() / 2)
        c.paint()
    c.restore()


def params_at(changes: list, tick: int) -> dict:
    p = changes[0][1]
    for t, q in changes:
        if t > tick:
            break
        p = q
    return p


def draw_lane(c: cairo.Context, m: dict, tick: int, cam: list[float], box: tuple[float, float, float, float], zoom: float, lead: float) -> None:
    x0, y0, w, h = box
    key = m["key"]
    name, col, logo = MODELS[key]
    best = m["best"]
    track = best["track"]
    k = min(tick, len(track) - 1)
    cx, cy, ca, *wheels = track[k]
    p = params_at(best["changes"], k)

    # road view
    c.save()
    c.rectangle(x0, y0, w, h)
    c.clip()
    ox, oy = x0 + w * 0.35, y0 + h * 0.5
    c.translate(ox - cam[0] * zoom, oy + cam[1] * zoom)
    c.scale(zoom, -zoom)
    c.set_line_width(1 / zoom)
    lo, hi = cam[0] - w * 0.35 / zoom - 2, cam[0] + w * 0.65 / zoom + 2
    surf = best["surface"]
    if "_x0" not in best:  # start x of each tile; tiles never fold back on these terrains, so it is sorted
        best["_x0"] = [min(a[0], b[0]) for a, b in surf]
    for (ax, ay), (bx, by) in surf[max(0, bisect.bisect_left(best["_x0"], lo) - 3):bisect.bisect_right(best["_x0"], hi) + 1]:
        if max(ax, bx) < lo or min(ax, bx) > hi:
            continue
        ang = math.atan2(by - ay, bx - ax)
        dx, dy = GROUND_H * math.sin(ang), -GROUND_H * math.cos(ang)
        c.move_to(ax, ay)
        c.line_to(ax + dx, ay + dy)
        c.line_to(bx + dx, by + dy)
        c.line_to(bx, by)
        c.close_path()
    c.set_source_rgb(0x77 / 255, 0x77 / 255, 0x77 / 255)
    c.fill_preserve()
    c.set_source_rgb(0, 0, 0)
    c.stroke()
    for i in range(2):  # wheels: shaded by density (cw_drawCars), with a spoke showing rotation
        wx, wy, wa = wheels[3 * i:3 * i + 3]
        r = p["wheel_radius"][i]
        g = round(255 - 255 * (p["wheel_density"][i] - 40) / 100) / 255
        c.arc(wx, wy, r, 0, 2 * math.pi)
        c.set_source_rgb(g, g, g)
        c.fill_preserve()
        c.move_to(wx, wy)
        c.line_to(wx + r * math.cos(wa), wy + r * math.sin(wa))
        c.set_source_rgb(0x44 / 255, 0x44 / 255, 0x44 / 255)
        c.set_line_width(1.5 / zoom)
        c.stroke()
    cos, sin = math.cos(ca), math.sin(ca)
    pts = [(cx + vx * cos - vy * sin, cy + vx * sin + vy * cos) for vx, vy in p["chassis"]]
    for i in range(8):  # the chassis is 8 triangles fanned from its origin; the original outlines each one
        c.move_to(cx, cy)
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
        badge(c, logo, col, ox + (cx - cam[0]) * zoom, oy - (cy - cam[1]) * zoom, 0.34 * zoom, -ca)

    # label panel
    c.set_source_rgb(0.97, 0.97, 0.97)
    c.rectangle(0, y0, x0, h)
    c.fill()
    badge(c, logo, col, 44, y0 + h * 0.42, 26)
    text(c, name, 84, y0 + h * 0.36, 25, bold=True)
    dist = max(0.0, max(t[0] for t in track[:k + 1]))
    done = k >= len(track) - 1
    status = f"{dist:6.1f} m" + (f"  · {best['reason']}" if done else "")
    text(c, status, 84, y0 + h * 0.36 + 34, 22, col=(0.3, 0.3, 0.3))
    bw = x0 - 104
    c.set_source_rgb(0.88, 0.88, 0.88)
    c.rectangle(84, y0 + h * 0.36 + 50, bw, 8)
    c.fill()
    c.set_source_rgb(*rgb(col))
    c.rectangle(84, y0 + h * 0.36 + 50, bw * min(1.0, dist / lead), 8)
    c.fill()


def race(fr: Frames, models: list[dict], tag: str) -> None:
    lead = models[0]["best"]["distance"]
    longest = max(len(m["best"]["track"]) for m in models) / 60
    speed = max(1, math.ceil(longest / RACE_TARGET_S))
    n_frames = math.ceil(longest / speed * FPS)
    top, foot, panel = 96, 46, 340
    lane_h = (H - top - foot) / len(models)
    zoom = lane_h / 4.2
    cams = [[m["best"]["track"][0][0], m["best"]["track"][0][1]] for m in models]
    for f in range(n_frames + 3 * FPS):
        tick = round(min(f, n_frames) / FPS * speed * 60)
        c = fr.ctx()
        for i, m in enumerate(models):
            tr = m["best"]["track"]
            k = min(tick, len(tr) - 1)
            cams[i][0] += (tr[k][0] - cams[i][0]) * (1 if f == 0 else 0.25)
            cams[i][1] += (tr[k][1] - cams[i][1]) * (1 if f == 0 else 0.12)
            draw_lane(c, m, tick, cams[i], (panel, top + i * lane_h, W - panel, lane_h), zoom, lead)
            c.set_source_rgb(0.8, 0.8, 0.8)
            c.set_line_width(1)
            c.move_to(0, top + i * lane_h + 0.5)
            c.line_to(W, top + i * lane_h + 0.5)
            c.stroke()
        c.set_source_rgb(1, 1, 1)
        c.rectangle(0, 0, W, top)
        c.fill()
        text(c, "terrain-bench · each agent's furthest run", 40, 60, 38, bold=True)
        text(c, f"t = {min(f, n_frames) / FPS * speed:5.1f} s   ×{speed} speed", W - 40, 60, 28, col=(0.35, 0.35, 0.35), align="right")
        c.set_source_rgb(1, 1, 1)
        c.rectangle(0, H - foot, W, foot)
        c.fill()
        text(c, CREDIT, 40, H - 16, 19, col=(0.45, 0.45, 0.45))
        fr.emit()


def chart(fr: Frames, models: list[dict], minutes: float) -> None:
    L, R, T, B = 150, 420, 150, 150
    pw, ph = W - L - R, H - T - B
    top = max(m["best"]["distance"] for m in models)
    ymax = max(50, math.ceil(top * 1.1 / 50) * 50)
    tmax = minutes * 60
    X = lambda t: L + pw * t / tmax
    Y = lambda d: T + ph * (1 - d / ymax)
    draw_s, hold_s = 7, 5
    for f in range((draw_s + hold_s) * FPS):
        now = tmax * min(1.0, f / (draw_s * FPS))
        c = fr.ctx()
        text(c, "Furthest distance so far", L, 80, 40, bold=True)
        text(c, f"over a {minutes:g}-minute run; each agent may reset to a new course whenever it likes", L, 118, 24, col=(0.4, 0.4, 0.4))
        c.set_line_width(1)
        for d in range(0, ymax + 1, 50 if ymax <= 400 else 100):
            c.set_source_rgb(0.9, 0.9, 0.9)
            c.move_to(L, Y(d))
            c.line_to(L + pw, Y(d))
            c.stroke()
            text(c, f"{d} m", L - 14, Y(d) + 7, 20, col=(0.4, 0.4, 0.4), align="right")
        step = 5 if minutes > 10 else 1
        for mi in range(0, int(minutes) + 1, step):
            text(c, f"{mi} min", X(mi * 60), T + ph + 34, 20, col=(0.4, 0.4, 0.4), align="center")
        c.set_source_rgb(0.3, 0.3, 0.3)
        c.move_to(L, T)
        c.line_to(L, T + ph)
        c.line_to(L + pw, T + ph)
        c.stroke()
        heads = []
        for m in models:
            name, col, logo = MODELS[m["key"]]
            pts = [(p["t"], p["best"]) for p in m["progress"] if p["t"] <= now]
            if not pts:
                continue
            c.move_to(X(pts[0][0]), Y(pts[0][1]))
            for (t0, d0), (t1, d1) in zip(pts, pts[1:]):
                c.line_to(X(t1), Y(d0))
                c.line_to(X(t1), Y(d1))
            c.line_to(X(now), Y(pts[-1][1]))
            c.set_source_rgb(*rgb(col))
            c.set_line_width(4)
            c.stroke()
            heads.append([Y(pts[-1][1]), X(now), pts[-1][1], m["key"]])
        heads.sort()
        for i in range(1, len(heads)):  # keep end labels from overlapping
            heads[i][0] = max(heads[i][0], heads[i - 1][0] + 56)
        for ly, hx, d, key in heads:
            name, col, logo = MODELS[key]
            badge(c, logo, col, hx, Y(d), 20)
            badge(c, logo, col, L + pw + 76, ly, 22)
            text(c, name, L + pw + 108, ly - 2, 22, bold=True)
            text(c, f"{d:.1f} m", L + pw + 108, ly + 24, 20, col=(0.35, 0.35, 0.35))
        text(c, CREDIT, 40, H - 16, 19, col=(0.45, 0.45, 0.45))
        fr.emit()


def main() -> None:
    run = Path(sys.argv[1])
    models = load(run)
    minutes = max(json.loads((run / m["key"] / "meta.json").read_text())["minutes"] for m in models)
    out = run / "site"
    out.mkdir(exist_ok=True)
    fr = Frames(out / "terrain_bench.mp4")
    race(fr, models, run.name)
    chart(fr, models, minutes)
    fr.close()
    for m in models:  # one full-height clip per model: its furthest run on its own
        solo = Frames(out / f"best_{m['key']}.mp4")
        race(solo, [m], run.name)
        solo.close()
    page(run, models, minutes)
    print(out / "terrain_bench.mp4")


def page(run: Path, models: list[dict], minutes: float) -> None:
    """runs/<tag>/site/index.html: the headline, the video, and one row per model."""
    rows = []
    for m in models:
        name, col, _ = MODELS[m["key"]]
        meta = json.loads((run / m["key"] / "meta.json").read_text())
        b = m["best"]
        p = b["params"]
        car = (f"wheels r={p['wheel_radius'][0]:g}/{p['wheel_radius'][1]:g} m on vertices {p['wheel_vertex'][0]}/{p['wheel_vertex'][1]}, "
               f"motor {p['motor_speed'][0]:g}/{p['motor_speed'][1]:g} rad/s, torque x{p['torque_scale'][0]:g}/{p['torque_scale'][1]:g}")
        t_best = next((q for q in m["progress"] if q["best"] >= b["distance"] - 1e-3), m["progress"][-1])["t"]
        rows.append(f"<tr><th><span class=dot style='background:{col}'></span>{html.escape(name)}</th><td><b>{b['distance']:.1f} m</b></td>"
                    f"<td>{t_best / 60:.1f} min</td><td>{meta['episodes']}</td><td>{len(meta['segments'])}</td>"
                    f"<td>{b['reason']}, {b['sim_s']:.0f} s, {len(b['changes']) - 1} mid-run changes</td><td class=dim>{html.escape(car)}</td></tr>")
    lead, base = models[0], next((m for m in models if m["key"] == "none"), None)
    head = f"{MODELS[lead['key']][0]} drove furthest: {lead['best']['distance']:.1f} m"
    if base and lead is not base:
        head += f", against {base['best']['distance']:.1f} m for the default car with no agent"
    (run / "site" / "index.html").write_text(f"""<!doctype html><html><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1"><title>terrain-bench {html.escape(run.name)}</title><style>
:root{{--bg:#fff;--fg:#111;--dim:#777;--line:#ddd}}
@media (prefers-color-scheme:dark){{:root{{--bg:#111;--fg:#eee;--dim:#999;--line:#333}}}}
body{{background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,sans-serif;max-width:1100px;margin:2em auto;padding:0 16px}}
a{{color:inherit}} video{{width:100%;border:1px solid var(--line)}} .dim{{color:var(--dim);font-size:13px}} .wrap{{overflow-x:auto}}
table{{border-collapse:collapse;margin:1em 0}} th,td{{padding:6px 10px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}}
.dot{{display:inline-block;width:10px;height:10px;border-radius:5px;margin-right:8px}}</style></head><body>
<h1>terrain-bench · {html.escape(run.name)}</h1>
<p><b>{html.escape(head)}.</b> Each agent had {minutes:g} minutes in its own sandbox with a live simulator, resetting to new courses
whenever it liked; the reward is the furthest x any of its cars reached.</p>
<video src="terrain_bench.mp4" controls autoplay muted loop playsinline></video>
<h2>Each agent's furthest run</h2>
{''.join(f"<h3>{html.escape(MODELS[m['key']][0])} · {m['best']['distance']:.1f} m</h3><video src='best_{m['key']}.mp4' controls muted loop playsinline preload=metadata></video>" for m in models)}
<div class=wrap><table><tr><th>model</th><th>furthest</th><th>reached at</th><th>episodes</th><th>sessions</th><th>that episode</th><th>car at its end</th></tr>
{''.join(rows)}</table></div>
<p class=dim>Every reset draws a new random course, so lanes in the race are different courses. "Sessions" counts CLI sessions;
an agent that stopped early was resumed with a nudge. Car, terrain and physics: <a href="http://rednuht.org/genetic_cars_2/">HTML5
Genetic Algorithm 2D Car Thingy</a> by Rafael Matsunaga (read from <a href="https://santex.github.io/singularity/seed/idx.html">this
copy</a>), after <a href="http://boxcar2d.com/">BoxCar2D</a>, on <a href="https://box2d.org/">Box2D</a>.</p></body></html>""")


if __name__ == "__main__":
    main()
