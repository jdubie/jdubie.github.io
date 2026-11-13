"""Per-model reels of a sweep: how each agent's run unfolded, episode by episode.

  python3 reel.py runs/<tag>      -> runs/<tag>/site/reel_<model>.mp4 and reels.mp4 (every model, best first)

Each reel plays, in order, every episode that set a new personal best (at most MAX_CLIPS, evenly thinned, always
keeping the first and the last), each sped up to fit CLIP_S seconds, drawn as render.py draws the race. Under the
replay, a scatter of every episode of the run (minutes into the run vs distance) fills in as the reel advances, with
the best-so-far line and the episode on screen ringed. Needs the per-episode recordings (sim/episodes/*.json.gz).
"""

from __future__ import annotations

import gzip
import json
import math
import sys
from pathlib import Path

import cairo

from render import CREDIT, FPS, H, MODELS, W, Frames, badge, draw_lane, rgb, text

CLIP_S, HOLD_S, MAX_CLIPS = 3.5, 0.6, 12


def load_run(d: Path) -> tuple[list[dict], float]:
    """Every episode's summary in time order (reloaded lazily for replay), and the run's start time."""
    eps = [json.loads(line) for line in (d / "sim" / "episodes.jsonl").read_text().splitlines()]
    eps = [e for e in eps if "episode" in e and "wall_start" in e]
    eps.sort(key=lambda e: e["wall_end"])
    return eps, min(e["wall_start"] for e in eps)


def clips(eps: list[dict]) -> list[int]:
    best, idx = -1.0, []
    for i, e in enumerate(eps):
        if e["distance"] > best + 1e-6:
            best = e["distance"]
            idx.append(i)
    if len(idx) > MAX_CLIPS:
        keep = {idx[0], idx[-1]} | {idx[round(k * (len(idx) - 1) / (MAX_CLIPS - 1))] for k in range(MAX_CLIPS)}
        idx = sorted(keep)
    return idx


def episode(d: Path, e: dict) -> dict:
    b = json.loads(gzip.open(d / "sim" / "episodes" / f"{e['episode']}.json.gz").read())
    s = b["stride"]
    b["changes"] = [[t // s, p] for t, p in b["changes"]]  # ticks -> track rows
    return b


def scatter(c: cairo.Context, eps: list[dict], t0: float, upto: int, cur: int, key: str, box: tuple, minutes: float, ymax: float) -> None:
    x0, y0, w, h = box
    L, B = 90, 46
    pw, ph = w - L - 30, h - B - 20
    X = lambda m: x0 + L + pw * m / minutes
    Y = lambda d: y0 + 10 + ph * (1 - d / ymax)
    col = rgb(MODELS[key][1])
    c.set_line_width(1)
    step = 250 if ymax > 1000 else 100
    for dv in range(0, int(ymax) + 1, step):
        c.set_source_rgb(0.9, 0.9, 0.9)
        c.move_to(X(0), Y(dv))
        c.line_to(X(minutes), Y(dv))
        c.stroke()
        text(c, f"{dv} m", X(0) - 10, Y(dv) + 6, 17, col=(0.45, 0.45, 0.45), align="right")
    for mi in range(0, int(minutes) + 1, 5):
        text(c, f"{mi} min", X(mi), y0 + h - 14, 17, col=(0.45, 0.45, 0.45), align="center")
    best, pts = 0.0, []
    for i, e in enumerate(eps[:upto + 1]):
        m = (e["wall_end"] - t0) / 60
        c.arc(X(m), Y(e["distance"]), 2.6, 0, 2 * math.pi)
        c.set_source_rgba(*col, 0.35 if e["reason"] != "reset" else 0.15)
        c.fill()
        if e["distance"] > best:
            best = e["distance"]
            pts.append((m, best))
    c.set_source_rgb(*col)
    c.set_line_width(2.5)
    if pts:
        c.move_to(X(pts[0][0]), Y(pts[0][1]))
        for (m0, b0), (m1, b1) in zip(pts, pts[1:]):
            c.line_to(X(m1), Y(b0))
            c.line_to(X(m1), Y(b1))
        c.line_to(X((eps[upto]["wall_end"] - t0) / 60), Y(pts[-1][1]))
        c.stroke()
    e = eps[cur]
    c.arc(X((e["wall_end"] - t0) / 60), Y(e["distance"]), 9, 0, 2 * math.pi)
    c.set_source_rgb(0.1, 0.1, 0.1)
    c.set_line_width(2.5)
    c.stroke()
    text(c, f"every episode of the run ({len(eps)}): distance vs minutes into the run", X(0), y0 + 2, 18, col=(0.35, 0.35, 0.35))


def reel(fr: Frames, d: Path, key: str, minutes: float, ymax: float) -> None:
    eps, t0 = load_run(d)
    name, colhex, logo = MODELS[key]
    top, foot, split = 90, 40, 620
    lane = (340, top, W - 340, split - top)
    zoom = (split - top) / 6.0
    picks = clips(eps)
    for n, i in enumerate(picks):
        e = eps[i]
        ep = episode(d, e)
        rows = len(ep["track"])
        frames = round(CLIP_S * FPS)
        cam = [ep["track"][0][0], ep["track"][0][1]]
        m = {"key": key, "best": ep}
        # smoothing only when the clip is near real time; sped up a lot, the car moves metres per frame and a lagging
        # camera loses it
        follow = (1.0, 1.0) if ep["sim_s"] / CLIP_S > 10 else (0.35, 0.2)
        for f in range(frames + round(HOLD_S * FPS)):
            k = min(rows - 1, round(f * (rows - 1) / max(1, frames - 1)))
            cam[0] += (ep["track"][k][0] - cam[0]) * (1 if f == 0 else follow[0])
            cam[1] += (ep["track"][k][1] - cam[1]) * (1 if f == 0 else follow[1])
            c = fr.ctx()
            draw_lane(c, m, k, cam, lane, zoom, max(1.0, ymax))
            text(c, f"episode {i + 1} of {len(eps)}", 84, top + 330, 20, col=(0.3, 0.3, 0.3))
            text(c, f"{(e['wall_start'] - t0) / 60:.1f} min into the run", 84, top + 360, 20, col=(0.3, 0.3, 0.3))
            text(c, f"new best {n + 1} of {len(picks)} shown", 84, top + 390, 20, col=(0.3, 0.3, 0.3))
            text(c, f"{ep['sim_s']:.0f} s simulated, ×{ep['sim_s'] / CLIP_S:.0f}", 84, top + 420, 20, col=(0.3, 0.3, 0.3))
            c.set_source_rgb(1, 1, 1)
            c.rectangle(0, 0, W, top)
            c.fill()
            badge(c, logo, colhex, 50, 46, 26)
            text(c, f"{name} · how its run unfolded", 92, 58, 36, bold=True)
            text(c, "each new personal best, in order", W - 40, 58, 24, col=(0.4, 0.4, 0.4), align="right")
            c.set_source_rgb(0.8, 0.8, 0.8)
            c.move_to(0, split + 0.5)
            c.line_to(W, split + 0.5)
            c.stroke()
            scatter(c, eps, t0, i, i, key, (0, split + 8, W, H - split - foot - 8), minutes, ymax)
            text(c, CREDIT, 40, H - 14, 18, col=(0.45, 0.45, 0.45))
            fr.emit()


def main() -> None:
    run = Path(sys.argv[1])
    keys = [k for k in MODELS if (run / k / "sim" / "episodes").is_dir() and (run / k / "meta.json").exists()]
    metas = {k: json.loads((run / k / "meta.json").read_text()) for k in keys}
    keys.sort(key=lambda k: -metas[k]["best_distance"])
    minutes = max(m["minutes"] for m in metas.values())
    ymax = math.ceil(max(m["best_distance"] for m in metas.values()) * 1.08 / 250) * 250
    out = run / "site"
    out.mkdir(exist_ok=True)
    for k in keys:
        fr = Frames(out / f"reel_{k}.mp4")
        reel(fr, run / k, k, minutes, ymax)
        fr.close()
        print(out / f"reel_{k}.mp4", flush=True)
    lst = out / "reels.txt"
    lst.write_text("".join(f"file 'reel_{k}.mp4'\n" for k in keys))
    import subprocess
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(lst), "-c", "copy", str(out / "reels.mp4")], check=True)
    lst.unlink()
    print(out / "reels.mp4")


if __name__ == "__main__":
    main()
