"""Render the results page: python qa/make_site.py -> site/"""
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
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "sim"))
import env  # noqa: E402
from core import SUB, Arm, Trial, build_model, load_params, make_trajectory, reference_for_controller  # noqa: E402

SITE = ROOT / "site"
SITE.mkdir(exist_ok=True)
RUN = ROOT / "runs" / "qa" / "robot100"
P = load_params(RUN / "private" / "params.json")
M = build_model(P)
traj = make_trajectory(json.loads((RUN / "private" / "eval_seeds.json").read_text())[0], M, P)
ref = reference_for_controller(traj)
os.environ["YAMTRACK_SIM"] = str(ROOT / "sim")
CASES = [("PD only", "pd", None, None), ("nominal model ff", "nominal_ff", None, None),
         ("true params, smart frames", "oracle", "true", "smart")]
runs = {}
for label, ctrl, params, mode in CASES:
    if params:
        os.environ["YAMTRACK_PARAMS"], os.environ["MODE"] = str(RUN / "private" / "params.json"), mode
    argv, tmp = env._controller_argv(ROOT / "qa" / "controllers" / f"{ctrl}.py", sandbox=False)
    with tmp:
        tr = Trial("eval0", traj)
        res = env.run_trial(Arm(P, 1000), tr, ref, argv)
    runs[label] = (tr.true_q.copy(), res)
    print(label, res["rms_mrad"], res["status"])

# error plot
t = np.arange(1, len(traj)) / 1500
fig, axes = plt.subplots(len(CASES), 1, figsize=(11, 7.5), sharex=True)
names = ["j1", "j2", "j3", "j4", "j5", "j6"]
for ax, (label, (q, res)) in zip(axes, runs.items()):
    err = (q - traj[1:]) * 1000
    for j in range(6):
        ax.plot(t, err[:, j], lw=0.9, label=names[j])
    ax.set_title(f"{label}: RMS {res['rms_mrad']:.2f} mrad", fontsize=10, loc="left")
    ax.set_ylabel("error (mrad)")
    ax.grid(alpha=0.3)
axes[0].legend(ncol=6, fontsize=8, loc="upper right")
axes[-1].set_xlabel("time (s)")
fig.tight_layout()
fig.savefig(SITE / "errors.png", dpi=110)
fig, ax = plt.subplots(figsize=(11, 3.2))
for j in range(6):
    ax.plot(t, traj[1:, j], lw=1, label=names[j])
ax.set_ylabel("reference (rad)"), ax.set_xlabel("time (s)"), ax.grid(alpha=0.3), ax.legend(ncol=6, fontsize=8)
fig.tight_layout()
fig.savefig(SITE / "reference.png", dpi=110)

# video: real arm (grey) against a green ghost of the reference
xml = (ROOT / "public" / "yam_nominal.xml").read_text()
arm = re.search(r'(<body name="arm">.*</body>)\s*</worldbody>', xml, re.S).group(1)
ghost = re.sub(r'name="([^"]+)"', r'name="\1_ref"', arm)
ghost = re.sub(r'rgba="[^"]+"', 'rgba="0.2 0.85 0.3 0.35"', ghost).replace("<geom ", '<geom rgba="0.2 0.85 0.3 0.35" ', ) 
ghost = re.sub(r'(<geom rgba="[^"]+" )([^>]*)rgba="[^"]+"', r"\1\2", ghost)
xml2 = xml.replace("</worldbody>", ghost + "\n</worldbody>").replace('<actuator>', '<!--').replace('</actuator>', '-->')
xml2 = xml2.replace('<visual>', '')
xml2 = xml2.replace('<mujoco model="yam_nominal">', '<mujoco model="viz"><visual><global offwidth="1280" offheight="960"/></visual>')
vm = mujoco.MjModel.from_xml_string(xml2)
for i, name in enumerate(["link_2", "link_3", "link_4", "link_5", "link_6", "gripper"]):
    for suffix in ("", "_ref"):  # both drawn with the true link lengths, so only joint error shows
        vm.body_pos[vm.body(name + suffix).id] *= P.length_scale[i]
vd = mujoco.MjData(vm)
W, H = 480, 400
r = mujoco.Renderer(vm, H, W)
cam = mujoco.MjvCamera()
cam.azimuth, cam.elevation, cam.distance, cam.lookat[:] = 135, -18, 1.05, (0.1, 0.0, 0.25)
out = SITE / "tracking.mp4"
ff = subprocess.Popen(["ffmpeg", "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W * 3}x{H}", "-r", "30", "-i", "-",
                       "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "22", "-movflags", "+faststart", str(out)], stdin=subprocess.PIPE)
for k in range(0, len(traj) - 1, SUB):
    tiles = []
    for label, (q, _) in runs.items():
        vd.qpos[:6] = q[max(k - 1, 0)]
        vd.qpos[6:12] = traj[k]
        mujoco.mj_forward(vm, vd)
        r.update_scene(vd, cam)
        tiles.append(r.render())
    ff.stdin.write(np.hstack(tiles).tobytes())
ff.stdin.close()
ff.wait()
json.dump({k: v[1] for k, v in runs.items()}, open(SITE / "demo_runs.json", "w"), indent=1)
print("wrote", out)
