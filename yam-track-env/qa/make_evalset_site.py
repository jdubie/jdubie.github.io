"""Render the eval-set page: python qa/make_evalset_site.py -> site/index.html (the first version's page is site/v0/)."""
import html
import json
import os
import re
import subprocess
import sys
from pathlib import Path

os.environ.setdefault("MUJOCO_GL", "egl")
import matplotlib  # noqa: E402

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import mujoco  # noqa: E402
import numpy as np  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "sim"))
sys.path.insert(0, str(ROOT / "qa"))
import core  # noqa: E402
import evalset  # noqa: E402
import sensitivity  # noqa: E402

SITE = ROOT / "site"
OUT = ROOT / "qa" / "out"
SERIES = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#6250d6"]
INK, MUTED, GRID = "#0b0b0b", "#52514e", "#e4e3df"
plt.rcParams.update({"font.size": 9, "axes.edgecolor": MUTED, "axes.labelcolor": INK, "xtick.color": MUTED,
                     "ytick.color": MUTED, "axes.spines.top": False, "axes.spines.right": False,
                     "axes.grid": True, "grid.color": GRID, "grid.linewidth": 0.6})

nominal = core.build_model(None)
EV, EV_SRC = evalset.build_set(nominal, "eval")
names = list(EV)
sens = json.loads((OUT / "sensitivity_eval.json").read_text())
sens_pr = json.loads((OUT / "sensitivity_practice.json").read_text())
ilc = json.loads((OUT / "ilc_robot100.json").read_text())["rms_mm"]
mean = lambda d: float(np.mean(list(d.values())))  # noqa: E731


def ref_grid() -> None:
    fig, axes = plt.subplots(5, 3, figsize=(12, 12), sharex=True)
    t = evalset.T_ALL
    for ax, name in zip(axes.flat, names):
        for j in range(6):
            ax.plot(t, EV[name][:, j] - EV[name][0, j], lw=1.2, color=SERIES[j], label=f"j{j + 1}")
        ax.set_title(f"{name}  ({evalset.CATEGORY.get(name, 'recorded')})", loc="left", fontsize=9)
    axes.flat[-1].axis("off")
    axes.flat[0].legend(ncol=6, fontsize=7, loc="upper left", frameon=False)
    for ax in axes[:, 0]:
        ax.set_ylabel("q - q(0)  (rad)")
    for ax in axes[-1]:
        ax.set_xlabel("time (s)")
    fig.tight_layout()
    fig.savefig(SITE / "refs_eval.png", dpi=110)


def heatmap(data: dict, path: Path, title: str) -> None:
    terms = sensitivity.TERMS
    rows = list(data)
    added = np.array([[mean(data[n][f"nominal_{t}"]) - mean(data[n]["oracle"]) for t in terms] for n in rows])
    fig, ax = plt.subplots(figsize=(7.5, 0.42 * len(rows) + 1.4))
    im = ax.imshow(added, cmap="Blues", aspect="auto", vmin=0)
    ax.grid(False)
    ax.set_xticks(range(len(terms)), [t.replace("_", " ") for t in terms])
    ax.set_yticks(range(len(rows)), rows)
    ax.xaxis.tick_top()
    for i in range(len(rows)):
        col_best = added[:, :].argmax(0)
        for j in range(len(terms)):
            v = added[i, j]
            bold = col_best[j] == i
            ax.text(j, i, f"{v:.2f}", ha="center", va="center", fontsize=8, fontweight="bold" if bold else "normal",
                    color="white" if v > 0.6 * added.max() else INK)
    fig.colorbar(im, ax=ax, label="mm added when this term is taken from the nominal model", shrink=0.8)
    ax.set_title(title, fontsize=9, loc="left", pad=28)
    fig.tight_layout()
    fig.savefig(path, dpi=110)


def baselines() -> dict:
    rows = {"PD only": "pd_only", "nominal model, frame-aware ff": "nominal_all", "true params, frame-aware ff (oracle)": "oracle"}
    vals = {lab: [mean(sens[n][c]) for n in names] for lab, c in rows.items()}
    vals["nominal ff + naive ILC, best of 15 (robot 100)"] = [min(ilc[n]) for n in names]
    order = ["PD only", "nominal model, frame-aware ff", "nominal ff + naive ILC, best of 15 (robot 100)", "true params, frame-aware ff (oracle)"]
    fig, ax = plt.subplots(figsize=(9, 7))
    y = np.arange(len(names))
    h = 0.2
    for i, lab in enumerate(order):
        ax.barh(y + (i - 1.5) * h, vals[lab], height=h - 0.03, color=SERIES[i], label=lab)
    ax.set_xscale("log")
    ax.set_yticks(y, names)
    ax.invert_yaxis()
    ax.set_xlabel("RMS gripper-point error (mm, log scale)")
    ax.legend(fontsize=8, loc="lower left", bbox_to_anchor=(0, 1.01), ncol=2, frameon=False)
    fig.tight_layout()
    fig.savefig(SITE / "baselines.png", dpi=110)
    return {lab: float(np.mean(v)) for lab, v in vals.items()}


def ilc_plot() -> None:
    fig, ax = plt.subplots(figsize=(8, 3.6))
    for n in names:
        lab = n if n in ("friction_speeds", "inertia_proximal", "gravity_reach") else None
        hi = n in ("friction_speeds", "inertia_proximal")
        col = {"friction_speeds": SERIES[1], "inertia_proximal": SERIES[3]}.get(n, "#9a9994")
        ax.plot(range(1, 16), ilc[n], lw=2 if hi else 1, color=col, label=lab, marker="o", ms=3)
    ax.plot(range(1, 16), np.mean([ilc[n] for n in names], 0), lw=2.5, color=SERIES[0], label="mean of 14")
    ax.set_yscale("log")
    ax.set_xlabel("trial of the same reference")
    ax.set_ylabel("RMS error (mm)")
    ax.legend(fontsize=8, frameon=False, ncol=2)
    fig.tight_layout()
    fig.savefig(SITE / "ilc.png", dpi=110)


def video(name: str, seed: int = 100) -> dict:
    traj = EV[name]
    conds = [("PD only", "pd_only"), ("nominal model", "nominal_all"), ("true params (oracle)", "oracle")]
    runs = {lab: sensitivity.simulate(seed, traj, name, c) for lab, c in conds}
    p = core.make_params(seed)
    mt = core.build_model(p)
    ref_pts = core.eef_points(mt, traj[1:])
    t = np.arange(1, len(traj)) / core.PHYS_HZ
    fig, ax = plt.subplots(figsize=(10, 3))
    for i, (lab, tr) in enumerate(runs.items()):
        d = np.linalg.norm(core.eef_points(mt, tr.true_q) - ref_pts, axis=2).mean(1) * 1000
        ax.plot(t, d, lw=1.1, color=SERIES[i], label=f"{lab}: RMS {tr.result()['rms_mm']:.2f} mm")
    ax.set_yscale("log")
    ax.set_xlabel("time (s)")
    ax.set_ylabel("mean point error (mm)")
    ax.legend(fontsize=8, frameon=False, loc="lower right")
    fig.tight_layout()
    fig.savefig(SITE / "video_err.png", dpi=110)
    # real arm (grey) against a green ghost of the reference, both on the true link lengths
    xml = (ROOT / "public" / "yam_nominal.xml").read_text()
    arm = re.search(r'(<body name="arm">.*</body>)\s*</worldbody>', xml, re.S).group(1)
    ghost = re.sub(r'name="([^"]+)"', r'name="\1_ref"', arm)
    ghost = re.sub(r'rgba="[^"]+"', 'rgba="0.2 0.85 0.3 0.35"', ghost).replace("<geom ", '<geom rgba="0.2 0.85 0.3 0.35" ')
    ghost = re.sub(r'(<geom rgba="[^"]+" )([^>]*)rgba="[^"]+"', r"\1\2", ghost)
    xml2 = xml.replace("</worldbody>", ghost + "\n</worldbody>").replace("<actuator>", "<!--").replace("</actuator>", "-->")
    xml2 = xml2.replace('<mujoco model="yam_nominal">', '<mujoco model="viz"><visual><global offwidth="1280" offheight="960"/></visual>')
    vm = mujoco.MjModel.from_xml_string(xml2)
    for i, b in enumerate(["link_2", "link_3", "link_4", "link_5", "link_6", "gripper"]):
        for suffix in ("", "_ref"):
            vm.body_pos[vm.body(b + suffix).id] *= p.length_scale[i]
    vd = mujoco.MjData(vm)
    W, H = 480, 420
    r = mujoco.Renderer(vm, H, W)
    cam = mujoco.MjvCamera()
    cam.azimuth, cam.elevation, cam.distance, cam.lookat[:] = 135, -15, 1.2, (0.1, 0.0, 0.3)
    ff = subprocess.Popen(["ffmpeg", "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W * 3}x{H}", "-r", "30",
                           "-i", "-", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "22", "-movflags", "+faststart",
                           str(SITE / "tracking.mp4")], stdin=subprocess.PIPE)
    for k in range(0, len(traj) - 1, core.SUB):
        tiles = []
        for tr in runs.values():
            vd.qpos[:6], vd.qpos[6:12] = tr.true_q[max(k - 1, 0)], traj[k]
            mujoco.mj_forward(vm, vd)
            r.update_scene(vd, cam)
            tiles.append(r.render())
        ff.stdin.write(np.hstack(tiles).tobytes())
    ff.stdin.close()
    ff.wait()
    return {lab: tr.result()["rms_mm"] for lab, tr in runs.items()}


def smoke_rows() -> str:
    rows = []
    for meta_path in sorted((ROOT / "runs" / "live").glob("v*-*/*/meta.json")):
        m = json.loads(meta_path.read_text())
        cats = m.get("per_category_mm") or {}
        rows.append(f"<tr><td>{m['version']}</td><td>{html.escape(meta_path.parent.parent.name)}</td><td>{m['model']}</td>"
                    f"<td>{m['budget_min']} min</td><td><b>{m['score_mm']}</b></td>"
                    + "".join(f"<td>{cats.get(c, '')}</td>" for c in ("gravity", "friction", "inertia", "coriolis", "recorded"))
                    + f"<td>{m.get('trials') or ''}</td><td>{', '.join(m.get('missing') or []) or '-'}</td></tr>")
    return "\n".join(rows) or '<tr><td colspan="11">no agent runs yet</td></tr>'


MODEL_ORDER = ["opus", "sonnet", "haiku", "codexsol", "codexterra", "grok"]
MODEL_LABEL = {"opus": "Opus 5.5", "sonnet": "Sonnet 5.5", "haiku": "Haiku 5.5", "codexsol": "Sol (gpt-6.1-sol)",
               "codexterra": "Terra (gpt-6.1-terra)", "grok": "Grok 4.7"}


def limp_all(run: Path) -> dict:
    """Limp-arm score of every eval reference on this run's robot, cached per robot seed."""
    seed = json.loads((run / "private" / "meta.json").read_text())["robot_seed"]
    path = OUT / f"limp_{seed}.json"
    if not path.exists():
        import env
        path.write_text(json.dumps({n: env.limp_mm(run, n) for n in names}))
    return json.loads(path.read_text())


def sweep(tag1: str, tag2: str) -> str:
    """Model x version table, v1-vs-v2 bars and v1 best-so-far curves. Returns the HTML section."""
    sys.path.insert(0, str(ROOT))
    runs = {}
    for tag in (tag1, tag2):
        for mp in sorted((ROOT / "runs" / "live" / tag).glob("*/meta.json")):
            m = json.loads(mp.read_text())
            runs.setdefault((m["version"], m["model"]), []).append((mp.parent, m))
    if not runs:
        return "<p>Sweep not finished yet.</p>"
    models = [m for m in MODEL_ORDER if any((v, m) in runs for v in ("v1", "v2"))]
    score = lambda v, m: [x[1]["score_mm"] for x in runs.get((v, m), []) if x[1]["score_mm"] is not None]  # noqa: E731
    # bars
    fig, ax = plt.subplots(figsize=(8, 3.8))
    x = np.arange(len(models))
    for i, v in enumerate(("v1", "v2")):
        vals = [np.mean(score(v, m)) if score(v, m) else np.nan for m in models]
        ax.bar(x + (i - 0.5) * 0.36, vals, 0.34, color=SERIES[i], label=f"{v} ({'8' if v == 'v1' else '16'} min)")
        for m_i, m in enumerate(models):
            for sc in score(v, m):
                ax.plot(x[m_i] + (i - 0.5) * 0.36, sc, "o", ms=4, color=INK, alpha=0.6)
    for lab, c in (("nominal model", "nominal_all"), ("oracle", "oracle")):
        val = float(np.mean([mean(sens[n][c]) for n in names]))
        ax.axhline(val, color=MUTED, lw=1, ls="--")
        ax.text(len(models) - 0.5, val, f" {lab} {val:.2f}", va="bottom", ha="right", fontsize=8, color=MUTED)
    ax.set_yscale("log")
    ax.set_xticks(x, [MODEL_LABEL[m] for m in models], fontsize=8)
    ax.set_ylabel("score: mean RMS mm (log)")
    ax.legend(frameon=False, fontsize=8)
    fig.tight_layout()
    fig.savefig(SITE / "sweep.png", dpi=110)
    # v1 curves: score as it stood at each moment (never-finished references count limp)
    fig, ax = plt.subplots(figsize=(8, 4))
    for i, m in enumerate(models):
        for k, (run, meta) in enumerate(runs.get(("v1", m), [])):
            g = json.loads((run / "grade.json").read_text())
            limp = limp_all(run)
            best = dict(limp)
            ts, ys = [0.0], [float(np.mean(list(best.values())))]
            for t, n, mm in g.get("progress", []):
                best[n] = min(best[n], mm)
                ts.append(t / 60)
                ys.append(float(np.mean(list(best.values()))))
            ts.append(meta["budget_min"])
            ys.append(ys[-1])
            ax.step(ts, ys, where="post", color=SERIES[i], lw=1.5, ls="-" if k == 0 else ":", label=MODEL_LABEL[m] if k == 0 else None)
    ax.set_yscale("log")
    ax.set_xlabel("minutes")
    ax.set_ylabel("score so far (mm, log)")
    ax.legend(frameon=False, fontsize=8, ncol=2)
    fig.tight_layout()
    fig.savefig(SITE / "v1_progress.png", dpi=110)
    rows = []
    for m in models:
        cells = []
        for v in ("v1", "v2"):
            sc = score(v, m)
            cats: dict = {}
            for _, meta in runs.get((v, m), []):
                for c, val in (meta.get("per_category_mm") or {}).items():
                    cats.setdefault(c, []).append(val)
            catcells = "".join(f"<td>{np.mean(cats[c]):.2f}</td>" if c in cats else "<td></td>"
                               for c in ("gravity", "friction", "inertia", "coriolis", "recorded"))
            note = ""
            for run, meta in runs.get((v, m), []):
                if meta.get("missing"):
                    note += f"r{meta['rep']} missed {len(meta['missing'])}; "
                if v == "v1":
                    note += f"r{meta['rep']} {meta.get('trials')} trials; "
                if meta.get("statuses") and meta["statuses"] != ["ok"]:
                    note += f"r{meta['rep']} {','.join(meta['statuses'])}; "
                if "cost_usd" in meta:
                    note += f"r{meta['rep']} ${meta['cost_usd']:.2f}; "
            cells.append(f"<td><b>{np.mean(sc):.2f}</b><br><span class=muted>{' / '.join(f'{x:.2f}' for x in sc)}</span></td>{catcells}<td class=muted>{note}</td>"
                         if sc else "<td>-</td>" + "<td></td>" * 6)
        rows.append(f"<tr><td>{MODEL_LABEL[m]}</td>{''.join(cells)}</tr>")
    head = "<th>score</th><th>grav</th><th>fric</th><th>iner</th><th>cor</th><th>rec</th><th>notes</th>"
    return f"""<img src="sweep.png" alt="sweep scores">
<table><tr><th rowspan=2>model</th><th colspan=7>v1: online, 8 min</th><th colspan=7>v2: held out, 16 min</th></tr>
<tr>{head}{head}</tr>{''.join(rows)}</table>
<p class="muted">Score = mean of 2 robots (seeds 100, 101; individual runs under it). Category columns are mean mm over that
category's references.</p>
<h3>v1: score over the run</h3><p>The score as it would have stood at each moment: a reference with no finished trial counts as a
limp arm, so a curve only drops below ~10 mm once every reference has been run. Solid = robot 100, dotted = 101.</p>
<img src="v1_progress.png" alt="v1 progress">"""


def main() -> None:
    old = SITE / "v0"
    if not old.exists():
        old.mkdir(parents=True)
        for f in list(SITE.iterdir()):
            if f.is_file():
                f.rename(old / f.name)
    ref_grid()
    heatmap(sens, SITE / "sens_eval.png", "Eval set: mm added per term (mean of 5 robots); bold = the test where that term costs most")
    heatmap(sens_pr, SITE / "sens_practice.png", "Practice set (v2): same check")
    base = baselines()
    ilc_plot()
    vid = video("recorded_0")
    z = np.load(evalset.BAGS)
    rec_rows = "".join(f"<tr><td>{n}</td><td>{html.escape(EV_SRC[n])}</td><td>{np.abs(core.derivatives(EV[n])[0]).max():.1f}</td>"
                       f"<td>{np.abs(core.derivatives(EV[n])[1]).max():.0f}</td></tr>" for n in names if n.startswith("recorded"))
    n_eps = len([k for k in z.files if k.endswith("_q")])
    desc = {
        "gravity_holds": "6 poses anywhere in the workspace; 1.2 s moves, 0.9 s holds",
        "gravity_reach": "4 poses where the shoulder holds &ge;9 Nm of gravity; 2 s holds",
        "friction_reversals": "15-50 mrad slow sines on every joint, so each keeps reversing direction; near the gravity-null pose",
        "friction_dither": "2-6 mrad sines at 0.5-2.5 Hz: the arm barely leaves stiction",
        "friction_speeds": "constant-speed plateaus 0.08 / 0.2 / 0.45 / 0.9 / 1.6 rad/s, both directions, every joint",
        "inertia_proximal": "quick 0.3 rad moves of joints 1-3 in 0.32-0.45 s (up to 14 rad/s&sup2;), near the gravity-null pose",
        "inertia_wrist": "quick moves of joints 4-6 in 0.25-0.35 s (up to 60 rad/s&sup2;)",
        "coriolis_spin": "base swinging &plusmn;0.8 rad at 0.55 Hz (2.5 rad/s) with the arm out and the elbow pumping",
    }
    targeted_rows = "".join(f"<tr><td>{n}</td><td>{evalset.CATEGORY[n]}</td><td>{desc[n]}</td></tr>" for n in evalset.TARGETED)
    page = f"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>YAM tracking eval set</title>
<style>
:root{{--bg:#fcfcfb;--ink:#0b0b0b;--muted:#52514e;--line:#e4e3df;--card:#fff}}
@media (prefers-color-scheme:dark){{:root{{--bg:#1a1a19;--ink:#f0efec;--muted:#c3c2b7;--line:#3a3936;--card:#232322}} img{{background:#fff}}}}
body{{background:var(--bg);color:var(--ink);font:15px/1.5 system-ui,sans-serif;margin:0;padding:24px 16px}}
main{{max-width:1080px;margin:auto}} h1{{font-size:24px;margin:0 0 6px}} h2{{font-size:18px;margin:34px 0 8px;border-top:1px solid var(--line);padding-top:18px}}
.lead{{font-size:17px}} .muted{{color:var(--muted)}} img,video{{max-width:100%;border-radius:6px}}
table{{border-collapse:collapse;font-size:13px;margin:8px 0;display:block;overflow-x:auto}} td,th{{border-bottom:1px solid var(--line);padding:4px 10px;text-align:left;vertical-align:top}}
code{{font-size:13px}} .two{{display:grid;grid-template-columns:1fr 1fr;gap:16px}} @media(max-width:760px){{.two{{grid-template-columns:1fr}}}}
.card{{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:12px 16px}}
</style></head><body><main>
<h1>YAM tracking: eval set and two task versions</h1>
<p class="lead">14 fixed 12 s references: 8 targeted tests, each isolating one dynamics term (gravity, friction, inertia,
Coriolis), and 6 windows of the real right arm from prod sv-pack bag rollouts. Error is now <b>mm</b>: RMS distance of three
gripper points from where the reference puts them. Nominal-model feedforward scores <b>{base['nominal model, frame-aware ff']:.1f} mm</b>,
the true-parameter oracle <b>{base['true params, frame-aware ff (oracle)']:.2f} mm</b>, and PD alone <b>{base['PD only']:.0f} mm</b>.</p>
<p class="muted">Built 2026-11-14 in <code>~/yam-track-env</code>. Older single-version page: <a href="v0/">v0/</a>.</p>

<h2>The two versions</h2>
<div class="two">
<div class="card"><b>v1: online, references given</b><br>The agent gets all 14 eval references and the arm socket. It runs
<code>trial</code>s of them, as often as it likes, in parallel connections. Score per reference = its best finished trial within
the budget (10-30 min); final = mean over the 14. Every trial lands in the server log with a timestamp, so progress is a curve
we watch, and nothing has to be delivered at the end. Never finishing a reference scores it limp (~1,000 mm).</div>
<div class="card"><b>v2: practice, then held out</b><br>The agent gets 14 <i>practice</i> references of the same kinds (other
generator seeds, recorded windows from the other rig, fr2-001) and the socket, and must deliver <code>controller.py</code>. At the
end it is run, sandboxed, on the 14 hidden eval references, the same set v1 sees. So v1 and v2 scores are directly comparable:
the gap is the price of not seeing the test.</div>
</div>
<p>Both: same hidden arm per robot seed (masses, COMs, inertias, link lengths, rotor inertia, Coulomb/Stribeck/viscous/asymmetric
friction, cogging, torque constants), lockstep 30 Hz socket, drives at 1500 Hz with gains capped at 1.5&times; fleet.</p>

<h2>Error metric: mm at three gripper points</h2>
<p>Points fixed to the gripper: the wrist centre on joint 6's axis, the tool point 11.5 cm out, and a point 6 cm off the tool
axis, so wrist roll registers. Both the arm's and the reference's points come from the <i>real</i> arm's kinematics
(perturbed link lengths), sampled at 1500 Hz, so intra-frame error counts. Score = RMS over time and the three points.</p>

<h2>Does each test measure what it says?</h2>
<p>The oracle controller with exactly one term (g(q), M(q)q&#776;, C(q,q&#775;)q&#775;, friction, torque constant) taken from the nominal
model instead of the truth. Cells are mm added over the full oracle. In every column the targeted test is the top cell, or within
a few percent of it. Gravity and friction errors still show everywhere: with capped gains any static torque error becomes a
position offset, so no test can be pure. Inertia and Coriolis are second order on this arm (&le;1.4 mm against 2-5 mm), which is
why their targeted tests are fast.</p>
<img src="sens_eval.png" alt="sensitivity heatmap, eval set">
<details><summary>Same check on the v2 practice set</summary><img src="sens_practice.png" alt="sensitivity heatmap, practice set"></details>

<h2>The eval set</h2>
<table><tr><th>targeted test</th><th>term</th><th>what it does</th></tr>{targeted_rows}</table>
<p>Friction and inertia tests sit near the upright pose q = (0, 1.57, 2.86, 0.12, 0, 0), where nominal gravity torque is zero
(&le;0.3 Nm on the hidden arms, against 5-15 Nm stretched out), so a wrong g(q) barely shows in them.</p>
<table><tr><th>recorded</th><th>source (2026-09-25 sv-pack-br-15ct rollouts, right arm)</th><th>max |q&#775;| rad/s</th><th>max |q&#776;| rad/s&sup2;</th></tr>{rec_rows}</table>
<p class="muted">{n_eps} episodes, 219 min of right-arm motion. Windows: low-passed at 6 Hz, splined to 1500 Hz, blended in from rest over
0.5 s; windows with one-frame glitches (&gt;0.2 rad jumps, present in the recordings) are skipped. Half are drawn from the busiest
third of windows, half spread across SARM stages (pickup, filling, placing). Real motion is fast: p99 speed 2-5 rad/s, p99
acceleration 10-35 rad/s&sup2;, beyond what the old synthetic references used.</p>
<img src="refs_eval.png" alt="eval references">

<h2>Baselines</h2>
<img src="baselines.png" alt="baselines per test">
<table><tr><th>controller</th><th>mean mm</th></tr>{''.join(f"<tr><td>{k}</td><td>{v:.3f}</td></tr>" for k, v in base.items())}</table>
<p><b>v1 is not trivially saturated by retrying.</b> A stand-in v1 agent (nominal ff, then plain P-type learning on each
reference's last error, 15 trials, all 14 in parallel, 145 s) gets the mean from 4.4 to 1.1 mm, then <i>diverges</i> on
friction_speeds and inertia_proximal, whose sharp content its learning loop amplifies. Reaching the oracle needs a filtered
learning law or real system identification.</p>
<img src="ilc.png" alt="ILC curves">

<h2>What it looks like: recorded_0 on robot 100</h2>
<p>Grey = the arm, green = the reference, both drawn with the real link lengths. Left to right: {', '.join(f'{k} {v:.2f} mm' for k, v in vid.items())}.
Millimetre errors are invisible at this scale; the plot below shows them.</p>
<video src="tracking.mp4" controls muted loop playsinline></video>
<img src="video_err.png" alt="error over time">

<h2>Sweep: six models, two versions</h2>
{sweep("v1-sweep1", "v2-sweep1")}
<h2>All agent runs</h2>
<table><tr><th>ver</th><th>tag</th><th>model</th><th>budget</th><th>score mm</th><th>gravity</th><th>friction</th><th>inertia</th><th>coriolis</th><th>recorded</th><th>trials</th><th>missing</th></tr>
{smoke_rows()}</table>

<h2>Caveats and next</h2>
<ul>
<li>Parameter ranges are guesses, not measured on a real YAM; friction is smoothed (no true stick-slip).</li>
<li>The recorded windows are joint positions as the encoders saw them, not commands; they include whatever tracking error the
real arm had, which is fine for a reference.</li>
<li>In v1 the agent sees exact trial scores and can retry, so v1 rewards learning-style control as much as modelling.</li>
<li>Smoke runs (Haiku, 10 min): v1 1.26 mm in 107 s by retrying references with a learning loop; v2 stopped after
19 s at plain nominal feedforward, 4.38 mm. Both prove the wiring, neither is a measurement.</li>
<li>Next: a sweep (opus, sonnet, haiku, Sol, Terra; 2 robots each), v1 at 20 min and v2 at 30 min.</li>
</ul>
</main></body></html>"""
    (SITE / "index.html").write_text(page)
    print("wrote", SITE / "index.html")


if __name__ == "__main__":
    main()
