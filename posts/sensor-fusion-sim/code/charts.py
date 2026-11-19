"""Tiny inline-SVG chart helpers. Colors are CSS variables (--c1.. --c4, --fg, --mut, --line, --bg) set by the page,
so light and dark mode come from the page's tokens. Hover: native <title> tooltips on wide hit strips/cells."""
import html, math
import numpy as np

W, H = 720, 300
ML, MR, MT, MB = 58, 120, 14, 40


def _ticks(lo, hi, log, n=5):
    if log:
        a, b = math.floor(math.log10(lo)), math.ceil(math.log10(hi))
        t = [10.0 ** k for k in range(a, b + 1)]
        if len(t) <= 3:
            t = sorted(set(t + [m * 10.0 ** k for k in range(a, b + 1) for m in (2, 5)]))
        return [x for x in t if lo * 0.999 <= x <= hi * 1.001]
    step = (hi - lo) / n
    mag = 10 ** math.floor(math.log10(step))
    step = min([m * mag for m in (1, 2, 2.5, 5, 10) if m * mag >= step])
    s = math.ceil(lo / step - 1e-9) * step
    return [round(v, 10) for v in np.arange(s, hi + step * 1e-6, step)]


def _fmt(v):
    if v == 0:
        return "0"
    if abs(v) >= 1000 or abs(v) < 0.01:
        return f"{v:g}"
    return f"{v:g}"


def line_chart(series, xlabel, ylabel, xlim=None, ylim=None, logx=False, logy=False, label="chart",
               hover_fmt="{:.3g}", xfmt=None, hlines=(), vspans=(), height=H, xticks=None):
    """series: list of dict(name, x, y, color='--c1', band=(lo, hi) or None, dash=False, label=True)."""
    xs = np.concatenate([np.asarray(s["x"], float) for s in series])
    ys = np.concatenate([np.asarray(s["y"], float) for s in series] + [np.asarray(s["band"][1], float) for s in series if s.get("band") is not None])
    x0, x1 = xlim or (np.nanmin(xs), np.nanmax(xs))
    y0, y1 = ylim or (0 if not logy else np.nanmin(ys[ys > 0]), np.nanmax(ys) * 1.05)
    pw, ph = W - ML - MR, height - MT - MB
    fx = (lambda v: ML + (math.log10(v) - math.log10(x0)) / (math.log10(x1) - math.log10(x0)) * pw) if logx else (lambda v: ML + (v - x0) / (x1 - x0) * pw)
    def fy(v):
        if logy:
            v = max(v, y0)
            return MT + ph - (math.log10(v) - math.log10(y0)) / (math.log10(y1) - math.log10(y0)) * ph
        return MT + ph - (min(max(v, y0), y1) - y0) / (y1 - y0) * ph
    o = [f'<svg viewBox="0 0 {W} {height}" role="img" aria-label="{html.escape(label)}">']
    for a, b, txt in vspans:
        o.append(f'<rect x="{fx(a):.1f}" y="{MT}" width="{fx(b) - fx(a):.1f}" height="{ph}" class="span"/>'
                 f'<text x="{(fx(a) + fx(b)) / 2:.1f}" y="{MT + 14}" text-anchor="middle">{html.escape(txt)}</text>')
    for t in _ticks(y0, y1, logy):
        o.append(f'<line x1="{ML}" x2="{ML + pw}" y1="{fy(t):.1f}" y2="{fy(t):.1f}" class="grid"/>'
                 f'<text x="{ML - 6}" y="{fy(t) + 4:.1f}" text-anchor="end">{_fmt(t)}</text>')
    for t in (xticks or _ticks(x0, x1, logx)):
        lab = xfmt(t) if xfmt else _fmt(t)
        o.append(f'<text x="{fx(t):.1f}" y="{MT + ph + 16}" text-anchor="middle">{lab}</text>')
    o.append(f'<line x1="{ML}" x2="{ML + pw}" y1="{MT + ph}" y2="{MT + ph}" class="axis"/>')
    o.append(f'<text x="{ML + pw / 2}" y="{height - 4}" text-anchor="middle">{html.escape(xlabel)}</text>')
    o.append(f'<text x="12" y="{MT + ph / 2}" text-anchor="middle" transform="rotate(-90 12 {MT + ph / 2})">{html.escape(ylabel)}</text>')
    for y, txt in hlines:
        o.append(f'<line x1="{ML}" x2="{ML + pw}" y1="{fy(y):.1f}" y2="{fy(y):.1f}" class="ref"/>'
                 f'<text x="{ML + pw - 4}" y="{fy(y) - 5:.1f}" text-anchor="end">{html.escape(txt)}</text>')
    ends = []
    for s in series:
        x, y = np.asarray(s["x"], float), np.asarray(s["y"], float)
        m = (x >= x0) & (x <= x1) & np.isfinite(y)
        x, y = x[m], y[m]
        if s.get("band") is not None:
            lo, hi = np.asarray(s["band"][0], float)[m], np.asarray(s["band"][1], float)[m]
            pts = [f"{fx(a):.1f},{fy(b):.1f}" for a, b in zip(x, hi)] + [f"{fx(a):.1f},{fy(b):.1f}" for a, b in zip(x[::-1], lo[::-1])]
            o.append(f'<polygon points="{" ".join(pts)}" style="fill:var({s["color"]});opacity:.16"/>')
        d = " ".join(f"{fx(a):.1f},{fy(b):.1f}" for a, b in zip(x, y))
        dash = ' stroke-dasharray="5 4"' if s.get("dash") else ""
        o.append(f'<polyline points="{d}" fill="none" style="stroke:var({s["color"]})" stroke-width="2" stroke-linejoin="round"{dash}/>')
        if s.get("markers"):
            for a, b in zip(x, y):
                o.append(f'<circle cx="{fx(a):.1f}" cy="{fy(b):.1f}" r="4" style="fill:var({s["color"]});stroke:var(--bg)" stroke-width="2"/>')
        if s.get("label", True) and len(x):
            ends.append([fy(y[-1]), s["name"], fx(x[-1])])
    ends.sort()
    for i in range(1, len(ends)):  # de-collide end labels
        ends[i][0] = max(ends[i][0], ends[i - 1][0] + 14)
    for yy, name, xx in ends:
        o.append(f'<text x="{xx + 6:.1f}" y="{yy + 4:.1f}" class="lab">{html.escape(name)}</text>')
    # hover strips: one per distinct x of the first series (subsampled to <= 60)
    xs0 = np.asarray(series[0]["x"], float)
    xs0 = xs0[(xs0 >= x0) & (xs0 <= x1)]
    idx = np.unique(np.linspace(0, len(xs0) - 1, min(len(xs0), 60)).astype(int))
    px = [fx(v) for v in xs0[idx]]
    for j, i in enumerate(idx):
        l = ML if j == 0 else (px[j - 1] + px[j]) / 2
        r = ML + pw if j == len(idx) - 1 else (px[j] + px[j + 1]) / 2
        xv = xs0[i]
        lines = [f"{xlabel}: {xfmt(xv) if xfmt else _fmt(round(xv, 4))}"]
        for s in series:
            sx, sy = np.asarray(s["x"], float), np.asarray(s["y"], float)
            k = int(np.argmin(np.abs(sx - xv)))
            if abs(sx[k] - xv) <= (abs(xv) * 1e-6 + 1e-9) or len(sx) > 60:
                lines.append(f"{s['name']}: {hover_fmt.format(sy[k])}")
        o.append(f'<rect x="{l:.1f}" y="{MT}" width="{max(r - l, 1):.1f}" height="{ph}" class="hit"><title>{html.escape(chr(10).join(lines))}</title></rect>')
    o.append("</svg>")
    return "".join(o)


def heatmap(rows, cols, vals, rlabel, clabel, fmt, label, best=None, mark=None, good_low=True, logcolor=True, height=300):
    """vals[i][j] for row i, col j. Color: one hue (--c1), darker = worse (if good_low) on a log scale."""
    nr, nc = len(rows), len(cols)
    cw, ch = (W - 150 - 20) / nc, (height - 30 - 40) / nr
    v = np.asarray(vals, float)
    lv = np.log10(v) if logcolor else v
    lo, hi = np.nanmin(lv), np.nanmax(lv)
    o = [f'<svg viewBox="0 0 {W} {height}" role="img" aria-label="{html.escape(label)}">']
    for i in range(nr):
        for j in range(nc):
            x, y = 150 + j * cw, 10 + i * ch
            f = (lv[i, j] - lo) / (hi - lo + 1e-12)
            if not good_low:
                f = 1 - f
            pct = int(8 + 80 * f)
            dark = pct > 50
            o.append(f'<rect x="{x + 1:.1f}" y="{y + 1:.1f}" width="{cw - 2:.1f}" height="{ch - 2:.1f}" rx="3" '
                     f'style="fill:color-mix(in oklab, var(--c1) {pct}%, var(--bg))"><title>{html.escape(f"{rlabel} {rows[i]}, {clabel} {cols[j]}: {fmt(v[i, j])}")}</title></rect>')
            cls = "cell dk" if dark else "cell"
            wt = ' font-weight="700"' if best == (i, j) else ""
            o.append(f'<text x="{x + cw / 2:.1f}" y="{y + ch / 2 + 4:.1f}" text-anchor="middle" class="{cls}"{wt}>{fmt(v[i, j])}</text>')
            if mark == (i, j) or best == (i, j):
                o.append(f'<rect x="{x + 2.5:.1f}" y="{y + 2.5:.1f}" width="{cw - 5:.1f}" height="{ch - 5:.1f}" rx="3" fill="none" '
                         f'style="stroke:var(--fg)" stroke-width="{2 if best == (i, j) else 1.2}"{"" if best == (i, j) else " stroke-dasharray=\"3 3\""} pointer-events="none"/>')
        o.append(f'<text x="142" y="{10 + i * ch + ch / 2 + 4:.1f}" text-anchor="end">{rows[i]}</text>')
    for j in range(nc):
        o.append(f'<text x="{150 + j * cw + cw / 2:.1f}" y="{10 + nr * ch + 16:.1f}" text-anchor="middle">{cols[j]}</text>')
    o.append(f'<text x="{150 + nc * cw / 2:.1f}" y="{height - 4}" text-anchor="middle">{html.escape(clabel)}</text>')
    o.append(f'<text x="14" y="{10 + nr * ch / 2:.1f}" text-anchor="middle" transform="rotate(-90 14 {10 + nr * ch / 2:.1f})">{html.escape(rlabel)}</text>')
    o.append("</svg>")
    return "".join(o)
