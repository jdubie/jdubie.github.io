"""PD-only baseline vs a v1 agent's best attempt at one reference, replayed on the true arm, in one video.

  python qa/make_replay_video.py RUN_DIR REFERENCE [START_S DUR_S] [--no-video] [--thumb]
  -> site/replay_<ref>.mp4, or with --thumb a 960x540 close-up clip site/thumbclip_<ref>.mp4 for the blog thumbnail

RUN_DIR is a v1 run whose agent saved its commands as work/runs/<ref>_best.npz (qcmd, vcmd, ff at max gains), as
Sol's did. Two arm panels side by side (fixed camera facing the gripper; red = where the reference puts the tool point,
blue = where it is) above a strip plotting each one's tool-point error live, log scale. START_S/DUR_S trim the clip.
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
PLOT_H = 300
LINE = {0: (240, 130, 60), 1: (110, 180, 255)}


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


def marker(dr: ImageDraw.ImageDraw, tgt_uv, act_uv, ms: float) -> None:
    """Target = thick red ring, arm = solid blue disk with a white edge, on top. Disk inside the ring = on target."""
    (tu, tv), (au, av) = tgt_uv, act_uv
    rt, ra = 15 * ms, 8 * ms
    dr.ellipse([tu - rt, tv - rt, tu + rt, tv + rt], outline=RED, width=max(2, round(5 * ms)))
    dr.ellipse([au - ra, av - ra, au + ra, av + ra], fill=BLUE, outline=(255, 255, 255), width=max(1, round(2 * ms)))


def strip(errs: dict[str, np.ndarray], t: np.ndarray, t0: float, t1: float, now: float, font) -> np.ndarray:
    """Error-vs-time strip, (PLOT_H, 2W, 3): each curve drawn up to `now`, log mm axis."""
    img = Image.new("RGB", (2 * W, PLOT_H), (22, 23, 25))
    dr = ImageDraw.Draw(img)
    L, R, T, B = 110, 2 * W - 40, 24, PLOT_H - 48
    lo, hi = -2.0, 2.0  # log10 mm
    X = lambda tt: L + (tt - t0) / (t1 - t0) * (R - L)  # noqa: E731
    Y = lambda mm: B - (np.log10(np.clip(mm, 10**lo, 10**hi)) - lo) / (hi - lo) * (B - T)  # noqa: E731
    for e in range(int(lo), int(hi) + 1):
        y = Y(10.0**e)
        dr.line([(L, y), (R, y)], fill=(50, 52, 56), width=1)
        dr.text((L - 12, y), f"{10.0**e:g} mm", fill=(170, 170, 170), font=font, anchor="rm")
    for tt in range(int(np.ceil(t0)), int(t1) + 1):
        dr.text((X(tt), B + 10), f"{tt} s", fill=(170, 170, 170), font=font, anchor="mt")
    sel = (t >= t0) & (t <= now)
    for i, (lab, e) in enumerate(errs.items()):
        if sel.sum() > 1:
            pts = list(zip(X(t[sel]).tolist(), Y(e[sel]).tolist()))
            dr.line(pts, fill=LINE[i], width=3)
            x, y = pts[-1]
            dr.ellipse([x - 6, y - 6, x + 6, y + 6], fill=LINE[i])
        ly = Y(10**0.5) + (i - 0.5) * 30  # legend in the empty band between 1 and 10 mm
        dr.line([(R - 260, ly), (R - 220, ly)], fill=LINE[i], width=4)
        dr.text((R - 210, ly), lab, fill=LINE[i], font=font, anchor="lm")
    dr.text((L + 10, T - 30), "tool point error, log scale", fill=(170, 170, 170), font=font)
    return np.asarray(img)


def render(mt: mujoco.MjModel, traj: np.ndarray, panels: dict[str, core.Trial], out: Path, t0: float, t1: float,
           thumb: bool = False) -> None:
    """Arm panels side by side, with the live error strip below. thumb: close-up panels, big labels, no strip."""
    pw, ph = (960, 1080) if thumb else (W, H)
    vm = mesh_viz.mesh_model(mt, ROOT / "public" / "yam_nominal.xml", pw, ph)
    vd = mujoco.MjData(vm)
    r = mujoco.Renderer(vm, ph, pw)
    tgt = core.eef_points(mt, traj[1:])[:, TCP]  # (N, 3)
    k0, k1 = int(round(t0 * core.PHYS_HZ)), min(int(round(t1 * core.PHYS_HZ)), len(traj) - 1)
    cam = mujoco.MjvCamera()  # fixed, beyond the gripper looking back at the arm
    if thumb:
        cam.azimuth, cam.elevation, cam.distance = 215, -15, 0.45  # follows the target tool point (below)
    else:
        cam.azimuth, cam.elevation, cam.distance, cam.lookat[:] = 215, -12, 0.72, (0.13, 0.13, 0.22)
    acts = {lab: core.eef_points(mt, t.true_q)[:, TCP] for lab, t in panels.items()}
    errs = {lab: np.linalg.norm(a - tgt, axis=1) * 1000 for lab, a in acts.items()}  # (N,) mm at 1500 Hz
    tt = np.arange(1, len(traj)) / core.PHYS_HZ
    step = 5  # plot at 300 Hz
    errs_s, tt_s = {k: v[::step] for k, v in errs.items()}, tt[::step]
    font, small = ImageFont.load_default(size=26), ImageFont.load_default(size=20)
    big = ImageFont.load_default(size=72)
    size = f"{pw * len(panels) // 2}x{ph // 2}" if thumb else f"{W * len(panels)}x{H + PLOT_H}"
    ff = subprocess.Popen(["ffmpeg", "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s",
                           size, "-r", "30", "-i", "-", "-c:v", "libx264", "-pix_fmt",
                           "yuv420p", "-crf", "20", "-movflags", "+faststart", str(out)], stdin=subprocess.PIPE)
    for k in range(k0 + core.SUB - 1, k1, core.SUB):
        tiles = []
        for lab, t in panels.items():
            vd.qpos[:6] = t.true_q[k]
            mujoco.mj_kinematics(vm, vd)
            if thumb:  # same camera for both panels: it follows the target, smoothed over 0.2 s
                cam.lookat[:] = tgt[max(k - 150, 0) : k + 150].mean(0)
            r.update_scene(vd, cam)
            img = Image.fromarray(r.render())
            dr = ImageDraw.Draw(img)
            uv = project(r.scene, np.stack([tgt[k], acts[lab][k]]), pw, ph)
            marker(dr, uv[0], uv[1], 2.6 if thumb else 1.3)
            if thumb:
                short = "PD only" if "PD" in lab else "Sol"
                label = f"{short}  {t.result()['rms_mm']:.2g} mm"
                x0, y0, x1, y1 = dr.textbbox((36, 30), label, font=big)
                dr.rounded_rectangle([x0 - 18, y0 - 14, x1 + 18, y1 + 14], 14, fill=(15, 15, 17))
                dr.text((36, 30), label, fill=(255, 255, 255), font=big)
            else:
                dr.text((16, 12), f"{lab}: {t.result()['rms_mm']:.2f} mm RMS (whole 12 s)", fill=(255, 255, 255), font=font)
                dr.text((16, 48), f"tool point error now {errs[lab][k]:5.2f} mm    t = {tt[k]:4.1f} s",
                        fill=(215, 215, 215), font=small)
                dr.ellipse([18, H - 65, 42, H - 41], outline=RED, width=4)
                dr.text((52, H - 64), "target (red ring)", fill=(30, 30, 30), font=small)
                dr.ellipse([22, H - 33, 38, H - 17], fill=BLUE, outline=(255, 255, 255), width=2)
                dr.text((52, H - 36), "arm (blue disk)", fill=(30, 30, 30), font=small)
            tiles.append(np.asarray(img))
        top = np.hstack(tiles)
        top[:, pw - 3 : pw + 3] = 255
        if thumb:
            frame = np.asarray(Image.fromarray(top).resize((pw * len(panels) // 2, ph // 2), Image.LANCZOS))
        else:
            frame = np.vstack([top, strip(errs_s, tt_s, t0, t1, tt[k], small)])
        ff.stdin.write(np.ascontiguousarray(frame).tobytes())
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
    args = [a for a in sys.argv[3:] if not a.startswith("--")]
    t0, dur = (float(args[0]), float(args[1])) if len(args) == 2 else (0.0, 12.0)
    thumb = "--thumb" in sys.argv
    out = ROOT / "site" / (f"thumbclip_{name}.mp4" if thumb else f"replay_{name}.mp4")
    render(core.build_model(p), traj, panels, out, t0, t0 + dur, thumb)


if __name__ == "__main__":
    main()
