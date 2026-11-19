"""All Monte Carlo experiments for the post. Usage: python3 experiments.py [name ...] (default: all). Writes results/<name>.npz"""
import os, sys, time, pickle
import numpy as np
from multiprocessing import Pool
from sim import simulate, default_config, Exp, Log, Trajectory, G
from eskf import run, default_filter, summarize

OUT = "results"
SEEDS = int(os.environ.get("SEEDS", 8))
EXTR_ERR = (np.deg2rad([2.0, -2.0, 2.0]), np.array([0.03, -0.03, 0.02]))


def job(args):
    name, cfgkw, fkw, rkw, seed, keep = args
    cfg = default_config(seed=seed, **cfgkw)
    sim = simulate(cfg)
    if rkw.get("extr_err"):
        rkw = dict(rkw); del rkw["extr_err"]
        rkw["extr_init"] = (cfg["R_bc"] @ Exp(EXTR_ERR[0]), cfg["p_bc"] + EXTR_ERR[1])
    t0 = time.time()
    out = run(sim, default_filter(**fkw), seed=seed, **rkw)
    summ = summarize(sim, out); summ["sec"] = time.time() - t0
    tr = {k: out[k][::10] for k in keep}  # 10 Hz traces
    if keep:
        tr["t"] = out["t"][::10]
    return name, seed, summ, tr


def complementary(seed, tau, T=60.0):
    """Tilt-only complementary filter: integrate the gyro, pull roll/pitch toward the accelerometer's gravity direction
    with time constant tau. Returns tilt RMSE in degrees (after 5 s)."""
    sim = simulate(default_config(seed=seed, T=T, cam_on=False))
    dt = 1 / 400; R = sim["gt_R"][0].copy(); err = []
    for k in range(len(sim["t"])):
        if tau < np.inf:
            gb = sim["acc"][k] / np.linalg.norm(sim["acc"][k])  # "up" in body frame, if not accelerating
            gw_est = R.T @ np.array([0, 0, 1.0])
            corr = np.cross(gw_est, gb)           # small rotation that aligns estimate with measurement
            alpha = dt / (tau + dt) if tau > 0 else 1.0
            ang = np.arcsin(np.clip(np.linalg.norm(corr), 0, 1))
            if ang > 0:
                R = R @ Exp(-corr / np.linalg.norm(corr) * ang * alpha)
        R = R @ Exp(sim["gyr"][k] * dt)
        if k % 4 == 0 and sim["t"][k] > 5:
            zt = sim["gt_R"][k][2]; ze = R[2]       # world z expressed in body frame: tilt only, yaw-free
            err.append(np.arccos(np.clip(zt @ ze, -1, 1)))
    return np.rad2deg(np.sqrt(np.mean(np.square(err))))


def cf_job(a):
    seed, tau = a
    return tau, seed, complementary(seed, tau)


def experiments():
    E = {}
    S = range(SEEDS)
    # 1. dead reckoning vs fusion: biases start at zero (unknown), pose starts exact
    E["drift"] = [(m, dict(T=60), dict(use_cam=(m == "fused"), init_err=False, zero_bias=True), {}, s, ["pos_err", "th_err", "Pd"])
                  for m in ["imu", "fused"] for s in range(2 * SEEDS)]
    # 2. tuning grid. The real sensors are noisier than their datasheets (2x IMU noise from vibration, 1.5 px features);
    #    the filter is told the datasheet values times (q_scale, r_scale).
    real = dict(gyro_nd=6e-4, acc_nd=6e-3, gyro_rw=6e-5, acc_rw=6e-4, px_std=1.5, T=30)
    E["tuning"] = [((q, r), real, dict(q_scale=q, r_scale=r), {}, s, [])
                   for q in [0.25, 0.5, 1, 2, 4, 8, 16] for r in [0.25, 0.5, 1, 1.5, 2, 4, 8] for s in S]
    # 3. outliers (wrong data association) with and without a chi-square gate
    E["outliers"] = [((f, g), dict(outlier_frac=f, T=30), dict(gate=9.21 if g else None, gate_guard=(g == 2)), {}, s, [])
                     for f in [0, 0.02, 0.05, 0.1, 0.2, 0.3] for g in [0, 1, 2] for s in range(2 * SEEDS)]
    # 4. latency: naive (fuse on arrival) vs rewind-and-replay
    E["latency"] = [((L, m), dict(cam_latency=L, T=30), dict(mode=m), {}, s, ["pos_err"] if L == 0.03 and s == 0 else [])
                    for L in [0, 0.01, 0.02, 0.03, 0.05, 0.075, 0.1] for m in ["naive", "replay"] for s in S]
    E["latency"] += [(("lidar30", m), dict(lidar_on=True, lidar_latency=0.03, T=30), dict(mode=m), {}, s, [])
                     for m in ["naive", "replay"] for s in S]
    # 5. unknown camera time offset
    E["td"] = [((td, e, 400), dict(cam_td=td, cam_latency=0.05, T=60), dict(est_td=bool(e), td_start=2.0), {}, s, ["td", "Pd"] if e else [])
               for td in [-0.03, -0.015, 0, 0.015, 0.03] for e in [0, 1] for s in S]
    E["td"] += [((0.0, 1, 800), dict(cam_td=0.0, cam_latency=0.05, T=60, imu_rate=800), dict(est_td=True, td_start=2.0), dict(record_every=8), s, ["td", "Pd"])
                for s in S]
    # 6. online camera-IMU extrinsic calibration: rich vs planar motion
    E["extrinsic"] = [((tr, e), dict(traj=tr, T=60), dict(est_extrinsic=bool(e)), dict(extr_err=True), s,
                       ["phi_err", "pbc", "Pd", "pos_err"])
                      for tr in ["rich", "planar"] for e in [0, 1] for s in S]
    # 7. camera dropout 20-30 s (lights off): camera-only vs camera + LiDAR
    E["dropout"] = [(m, dict(lidar_on=True, cam_dropout=(20, 30), T=40), dict(use_lidar=(m == "cam+lidar")), {}, s, ["pos_err", "Pd"])
                    for m in ["cam", "cam+lidar"] for s in S]
    return E


if __name__ == "__main__":
    os.makedirs(OUT, exist_ok=True)
    E = experiments()
    names = sys.argv[1:] or list(E) + ["complementary"]
    with Pool(int(os.environ.get("PROCS", os.cpu_count()))) as pool:
        for n in names:
            t0 = time.time()
            if n == "complementary":
                taus = [0.0, 0.03, 0.1, 0.3, 1, 3, 10, 30, 100, np.inf]
                res = pool.map(cf_job, [(s, tau) for tau in taus for s in range(SEEDS)])
            else:
                res = pool.map(job, E[n], chunksize=1)
            pickle.dump(res, open(f"{OUT}/{n}.pkl", "wb"))
            print(n, len(res), "runs", round(time.time() - t0), "s", flush=True)
