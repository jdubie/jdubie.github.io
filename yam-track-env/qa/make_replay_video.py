"""Replay a v1 agent's first and last attempt at one reference on the true arm, one video each.

  python qa/make_replay_video.py RUN_DIR REFERENCE [--no-video]
  -> site/replay_<ref>_first.mp4, site/replay_<ref>_last.mp4

RUN_DIR is a v1 run whose agent saved its commands as work/runs/<ref>_latest.npz (qcmd, vcmd, ff at max gains), as
Sol's did. The first attempt is that agent's iteration 0: nominal inverse dynamics, recomputed exactly as its
track.py does. The camera is fixed, facing the gripper; red is where the reference puts the tool point, blue is where it is.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

os.environ.setdefault("MUJOCO_GL", "egl")
import mujoco  # noqa: E402
import numpy as np  # noqa: E402
from PIL import Image, ImageDraw, ImageFont  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "sim"))
import core  # noqa: E402

sys.path.insert(0, str(ROOT / "qa"))
import mesh_viz  # noqa: E402
import sensitivity  # noqa: E402

KP = np.array([120, 120, 120, 60, 15, 15.0])
KD = np.array([7.5, 7.5, 7.5, 2.25, 2.25, 2.25])
DT = 1 / 30
TCP = core.POINT_NAMES.index("tcp")
RED, BLUE = (230, 40, 40), (40, 110, 240)
W, H = 960, 720


def first_attempt(ref: dict) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Iteration 0 of the agent's track.py: frame-midpoint nominal inverse dynamics."""
    q, v, acc = (np.array(ref[k]) for k in ("q", "qd", "qdd"))
    n = len(q) - 1
    m = mujoco.MjModel.from_xml_path(str(ROOT / "public" / "yam_nominal.xml"))
    d = mujoco.MjData(m)
    qm = q[:-1] + acc[:-1] * DT * DT / (-12)
    vm = (q[1:] - q[:-1]) / DT
    ff = np.zeros((n, 6))
    for i in range(n):
        d.qpos[:] = q[i] + v[i] * DT / 2 + acc[i] * DT * DT / 8
        d.qvel[:] = v[i] + acc[i] * DT / 2
        d.qacc[:] = acc[i]
        mujoco.mj_inverse(m, d)
        ff[i] = d.qfrc_inverse
    return qm, vm, ff


def replay(p: core.Params, name: str, traj: np.ndarray, qcmd, vcmd, ff) -> core.Trial:
    arm = core.Arm(p, noise_seed=0)
    trial = core.Trial(name, traj, arm.m)
    arm.reset(traj[0])
    for i in range(trial.n_frames):
        cmd = core.Command.parse({"q": qcmd[i].tolist(), "qd": vcmd[i].tolist(), "kp": KP.tolist(),
                                  "kd": KD.tolist(), "tau": ff[i].tolist()})
        arm.frame(cmd, trial.log_slot())
        trial.advanced()
    return trial


def project(scn: mujoco.MjvScene, pts: np.ndarray, w: int, h: int) -> np.ndarray:
    """World points (K, 3) -> pixel (K, 2) through the scene's (averaged) camera."""
    c = scn.camera[0]
    pos, fwd, up = np.array(c.pos), np.array(c.forward), np.array(c.up)
    right = np.cross(fwd, up)
    rel = pts - pos
    x, y, z = rel @ right, rel @ up, rel @ fwd
    xs, ys = x * c.frustum_near / z, y * c.frustum_near / z
    half_w = c.frustum_width or (c.frustum_top - c.frustum_bottom) / 2 * w / h  # 0 means: from the viewport aspect
    u = (xs - c.frustum_center + half_w) / (2 * half_w) * w
    v = (c.frustum_top - ys) / (c.frustum_top - c.frustum_bottom) * h
    return np.stack([u, v], 1)


def render(mt: mujoco.MjModel, traj: np.ndarray, panels: dict[str, core.Trial], out: Path) -> None:
    """One video, one panel per trial side by side; each panel shows the arm and the tool point's target and actual."""
    vm = mesh_viz.mesh_model(mt, ROOT / "public" / "yam_nominal.xml", W, H)
    vd = mujoco.MjData(vm)
    r = mujoco.Renderer(vm, H, W)
    cam = mujoco.MjvCamera()  # fixed, beyond the gripper looking back at the whole arm
    cam.azimuth, cam.elevation, cam.distance, cam.lookat[:] = 215, -12, 0.72, (0.13, 0.13, 0.22)
    tgt = core.eef_points(mt, traj[1:])[:, TCP]  # (N, 3)
    acts = {lab: core.eef_points(mt, t.true_q)[:, TCP] for lab, t in panels.items()}
    font, small = ImageFont.load_default(size=26), ImageFont.load_default(size=20)
    ff = subprocess.Popen(["ffmpeg", "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s",
                           f"{W * len(panels)}x{H}", "-r", "30", "-i", "-", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                           "-crf", "20", "-movflags", "+faststart", str(out)], stdin=subprocess.PIPE)
    for k in range(core.SUB - 1, len(traj) - 1, core.SUB):
        tiles = []
        for lab, t in panels.items():
            vd.qpos[:6] = t.true_q[k]
            mujoco.mj_kinematics(vm, vd)
            r.update_scene(vd, cam)
            img = Image.fromarray(r.render())
            dr = ImageDraw.Draw(img)
            act = acts[lab][k]
            (tu, tv), (au, av) = project(r.scene, np.stack([tgt[k], act]), W, H)
            for (u, v), col, rad in (((tu, tv), RED, 8), ((au, av), BLUE, 4)):  # blue on top: centred on red = on target
                dr.ellipse([u - rad, v - rad, u + rad, v + rad], fill=col)
            err = np.linalg.norm(tgt[k] - act) * 1000
            dr.text((16, 12), f"{lab}: {t.result()['rms_mm']:.2f} mm RMS", fill=(255, 255, 255), font=font)
            dr.text((16, 48), f"tool point error now {err:5.2f} mm    t = {(k + 1) / core.PHYS_HZ:4.1f} s",
                    fill=(215, 215, 215), font=small)
            dr.ellipse([16, H - 60, 30, H - 46], fill=RED)
            dr.text((38, H - 64), "target", fill=(235, 235, 235), font=small)
            dr.ellipse([16, H - 32, 30, H - 18], fill=BLUE)
            dr.text((38, H - 36), "arm (actual)", fill=(235, 235, 235), font=small)
            tiles.append(np.asarray(img))
        frame = np.hstack(tiles)
        frame[:, W - 2 : W + 2] = 255
        ff.stdin.write(frame.tobytes())
    ff.stdin.close()
    ff.wait()


def main() -> None:
    run, name = Path(sys.argv[1]), sys.argv[2]
    seed = json.loads((run / "private" / "meta.json").read_text())["robot_seed"]
    p = core.load_params(run / "private" / "params.json")
    assert p == core.make_params(seed), "run params are not robot seed's"
    traj = dict(np.load(run / "private" / "eval.npz"))[name]
    best = np.load(run / "work" / "runs" / f"{name}_best.npz")
    panels = {"baseline (PD only)": sensitivity.simulate(seed, traj, name, "pd_only"),
              "Sol's best": replay(p, name, traj, best["qcmd"], best["vcmd"], best["ff"])}
    print(json.dumps({k: t.result()["rms_mm"] for k, t in panels.items()}))
    if "--no-video" in sys.argv:
        return
    render(core.build_model(p), traj, panels, ROOT / "site" / f"replay_{name}.mp4")


if __name__ == "__main__":
    main()
