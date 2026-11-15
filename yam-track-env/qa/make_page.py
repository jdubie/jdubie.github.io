"""Write site/index.html from qa/out/baselines.json and runs/live/*/*/meta.json."""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
B = json.loads((ROOT / "qa/out/baselines.json").read_text())
rows = list(next(iter(B.values())))
seeds = list(B)
mean = {r: sum(B[s][r]["score_mrad"] for s in seeds) / len(seeds) for r in rows}
tbl = "".join(f"<tr><td>{r}</td>" + "".join(f"<td>{B[s][r]['score_mrad']:.2f}</td>" for s in seeds) + f"<td><b>{mean[r]:.2f}</b></td></tr>" for r in rows)
metas = sorted((json.loads(p.read_text()) | {"tag": p.parent.parent.name} for p in ROOT.glob("runs/live/*/*/meta.json")), key=lambda m: (m["tag"], m["model"], m["rep"]))
agent = "".join(f"<tr><td>{m['tag']}</td><td>{m['model']} <span class=dim>{m['pinned']}</span></td><td>{m['seed']}</td><td>{m['budget_min']} min</td><td>{m['seconds']/60:.1f} min</td>"
                f"<td><b>{m['score_mrad']}</b></td><td>{mean_for if (mean_for := B.get(str(m['seed']), {}).get('nominal model ff', {}).get('score_mrad')) else '-'}</td><td>{', '.join(m['statuses'])}</td></tr>" for m in metas)
html = f"""<!doctype html><meta charset=utf-8><title>yam-track: hidden-dynamics tracking bench</title>
<meta name=viewport content="width=device-width,initial-scale=1">
<style>body{{font:15px/1.5 system-ui,sans-serif;max-width:1500px;margin:24px auto;padding:0 16px;color:#1d2330}}
h1{{font-size:24px;margin-bottom:4px}} h2{{font-size:18px;margin-top:32px;border-bottom:1px solid #dde;padding-bottom:4px}}
table{{border-collapse:collapse;font-variant-numeric:tabular-nums}} td,th{{padding:4px 10px;border-bottom:1px solid #eef;text-align:right}}
td:first-child,th:first-child{{text-align:left}} .dim{{color:#889;font-size:12px}} .lead{{font-size:17px;background:#f3f6fb;padding:12px 16px;border-radius:8px}}
video,img{{max-width:100%;border-radius:6px}} .lab{{display:grid;grid-template-columns:repeat(3,1fr);text-align:center;font-weight:600;max-width:1440px}}
code{{background:#f2f3f6;padding:1px 4px;border-radius:3px}} li{{margin:3px 0}}</style>
<h1>yam-track &mdash; make a YAM with hidden dynamics follow hidden trajectories</h1>
<p class=dim>~/yam-track-env &middot; sibling of the cube stacker (~/yam-stack-env) &middot; built 2026-11-13</p>
<p class=lead><b>The bench works and has a wide range:</b> on 5 hidden robots, plain PD scores <b>{mean['PD only']:.1f} mrad</b>, feedforward from the
nominal model <b>{mean['nominal model ff']:.1f}</b>, and an oracle that knows the true parameters and handles the 30&nbsp;Hz interface well
<b>{mean['true params, smart frames']:.2f}</b>, about 200&times; better than PD. Getting there takes both <i>system identification</i> (true params with naive use of the interface only reach {mean['true params, naive']:.1f})
and <i>using the interface well</i> (the nominal model used well only reaches {mean['nominal model, smart frames']:.1f}). One 8-minute Haiku smoke run proved the harness end to end.</p>

<h2>The task</h2>
<ul>
<li><b>Arm:</b> teleop's YAM chain (<code>assets/yam.xml</code>), gripper fixed. Hidden per robot seed: link masses &times;0.75&ndash;1.35, centres of mass &plusmn;15&nbsp;mm,
inertias &times;0.7&ndash;1.4, link lengths &times;0.94&ndash;1.06, rotor inertia (armature) &times;0.6&ndash;2.5, motor torque constants &times;0.88&ndash;1.12.
No gravity compensation.</li>
<li><b>Friction, per joint:</b> Coulomb (direction-asymmetric &plusmn;25%), a Stribeck hump near zero speed, viscous, and position-periodic cogging (15&ndash;40 cycles/rad).
Wrist joints get smaller values.</li>
<li><b>Interface:</b> lockstep JSON over a Unix socket, one frame = 1/30&nbsp;s. Each frame the agent sends a drive command <code>q, qd, kp, kd, tau</code> (MIT-mode style);
the drive runs PD at 1500&nbsp;Hz with the setpoint ramping at <code>qd</code> inside the frame. kp/kd capped at 1.5&times; the fleet's gains, torques at 28/10&nbsp;Nm.
Observations (noisy <code>q, qd, tau</code>) at 30&nbsp;Hz. Free probing: reset to any pose, step up to 300 frames per request, one independent copy of the arm per connection.</li>
<li><b>Deliverable:</b> <code>controller.py</code> with <code>control(robot, ref)</code>. It gets the whole 12&nbsp;s reference (q, qd, qdd at 30&nbsp;Hz) up front.</li>
<li><b>Score:</b> RMS joint error (mrad) of the <i>true</i> angles against the smooth reference at 1500&nbsp;Hz, mean over 8 hidden references on the same arm. Lower is better.
References mix point-to-point moves, holds, slow creeps (friction) and multi-sine sweeps; generated per robot and kept within 70% of torque limits.
3 practice references are scored live through the socket (<code>tools/practice.py</code>).</li>
<li>Agent prompt: <a href="prompt.md">prompt.md</a> &middot; example hidden parameters: <a href="params_robot100.json">params_robot100.json</a></li>
</ul>

<h2>Tracking one hidden reference (robot 100, eval0): real arm grey, reference ghost green</h2>
<div class=lab><div>PD only &mdash; {json.loads((ROOT/'site/demo_runs.json').read_text())['PD only']['rms_mrad']:.1f} mrad</div><div>nominal model ff &mdash; {json.loads((ROOT/'site/demo_runs.json').read_text())['nominal model ff']['rms_mrad']:.1f} mrad</div><div>true params, smart frames &mdash; {json.loads((ROOT/'site/demo_runs.json').read_text())['true params, smart frames']['rms_mrad']:.2f} mrad</div></div>
<video src="tracking.mp4" controls autoplay loop muted playsinline width=1440></video>
<img src="errors.png" alt="joint error over time"><img src="reference.png" alt="reference">

<h2>Baselines on 5 hidden robots (mean RMS mrad over 8 hidden references)</h2>
<table><tr><th>controller</th>{''.join(f'<th>robot {s}</th>' for s in seeds)}<th>mean</th></tr>{tbl}</table>
<p class=dim>"smart frames" = interpolate the reference inside each 1/30&nbsp;s frame, fit the drive's linear setpoint ramp to it, and set tau to the frame-average of what perfect tracking needs.
The oracle controllers read hidden parameters, so they are reference points, not valid submissions.</p>

<h2>Agent runs</h2>
<table><tr><th>tag</th><th>model</th><th>robot</th><th>budget</th><th>used</th><th>score mrad</th><th>nominal-ff on same robot</th><th>status</th></tr>{agent}</table>
<p>The smoke run's Haiku wrote nominal inverse dynamics plus PD plus an integral term, practised twice, and stopped after 24&nbsp;s with 7.6 of its 8 minutes unused.
It did no system identification. That is roughly where the interesting part of the bench starts.</p>

<h2>Caveats and next steps</h2>
<ul>
<li>Not yet run on any model beyond the one Haiku smoke run. Next: a sweep with opus, sonnet, haiku, codexsol and codexterra (Sol/Terra pinned), 2 robots each, 45&nbsp;min.</li>
<li>Lockstep, not real-time: the agent can think between frames. A real-time mode (the arm keeps moving at 30&nbsp;Hz whether you answer or not) is a small change if wanted.</li>
<li>Hidden parameter ranges are guesses, not measured on a YAM. The friction law is smooth (tanh over 0.01&nbsp;rad/s), so there is no true stick-slip.</li>
<li>The drive uses the true joint state internally; only the agent's 30&nbsp;Hz observations are noisy.</li>
<li>Gemini runs need the <code>yamstack-gemini:1</code> image rebuilt (it is no longer on this box).</li>
</ul>
"""
(ROOT / "site/index.html").write_text(html)
print("ok")
