"""Load results/*.pkl and print summary numbers + write figs/<name>.svg for the post."""
import os, pickle, json
import numpy as np
from collections import defaultdict
from charts import line_chart, heatmap

os.makedirs("figs", exist_ok=True)
L = lambda n: pickle.load(open(f"results/{n}.pkl", "rb"))
NUM = {}


def agg(res):
    g = defaultdict(list)
    for name, seed, summ, tr in res:
        g[name].append(summ)
    return {k: {m: np.array([s[m] for s in v]) for m in v[0]} for k, v in g.items()}


def save(name, svg):
    open(f"figs/{name}.svg", "w").write(svg)


# ---------- drift ----------
r = L("drift")
ser = []
for m, col, nm in [("imu", "--c2", "IMU only"), ("fused", "--c1", "IMU + camera")]:
    rr = [x for x in r if x[0] == m]
    t = rr[0][3]["t"]
    pe = np.array([np.linalg.norm(x[3]["pos_err"], axis=1) for x in rr]) * 100
    med, lo, hi = np.median(pe, 0), np.percentile(pe, 10, 0), np.percentile(pe, 90, 0)
    keep = t >= 0.5
    ser.append(dict(name=nm, x=t[keep], y=med[keep], band=(lo[keep], hi[keep]), color=col))
    for tt in [1, 5, 10, 30, 60]:
        i = min(np.searchsorted(t, tt - 0.05), len(t) - 1)
        NUM[f"drift_{m}_{tt}s_cm_median"] = float(med[i])
save("drift", line_chart(ser, "time (s)", "position error (cm, log)", logy=True, ylim=(0.1, 1e5), label="Position error, IMU only vs fused",
                         hover_fmt="{:.3g} cm"))

# ---------- complementary ----------
r = L("complementary")
g = defaultdict(list)
for tau, seed, e in r:
    g[tau].append(e)
taus = sorted(g)
fin = [t for t in taus if np.isfinite(t) and t > 0]
NUM["cf"] = {str(t): float(np.mean(g[t])) for t in taus}
save("complementary", line_chart(
    [dict(name="complementary filter", x=fin, y=[np.mean(g[t]) for t in fin], color="--c1", markers=True, label=False)],
    "time constant τ (s, log)", "tilt error RMS (deg)", logx=True, ylim=(0, max(np.mean(g[0.0]), np.mean(g[np.inf])) * 1.15),
    hlines=[(np.mean(g[0.0]), f"accelerometer only {np.mean(g[0.0]):.2f}°"), (np.mean(g[np.inf]), f"gyro only {np.mean(g[np.inf]):.2f}°")],
    label="Complementary filter tilt error vs time constant", hover_fmt="{:.2f}°", xticks=[0.03, 0.1, 0.3, 1, 3, 10, 30, 100]))

# ---------- tuning ----------
if os.path.exists("results/tuning.pkl"):
    A = agg(L("tuning"))
    qs = [0.25, 0.5, 1, 2, 4, 8, 16]; rs = [0.25, 0.5, 1, 1.5, 2, 4, 8]
    rm = np.array([[np.mean(A[(q, r_)]["pos_rmse"]) * 1000 for r_ in rs] for q in qs])
    ne = np.array([[np.mean(A[(q, r_)]["anees15"]) for r_ in rs] for q in qs])
    ni = np.array([[np.mean(A[(q, r_)]["anis"]) for r_ in rs] for q in qs])
    bi = np.unravel_index(np.argmin(rm), rm.shape)
    bn = np.unravel_index(np.argmin(np.abs(np.log(ni))), ni.shape)
    be = np.unravel_index(np.argmin(np.abs(np.log(ne))), ne.shape)
    ds = (qs.index(1), rs.index(1))
    NUM["tuning"] = dict(best_rmse=(qs[bi[0]], rs[bi[1]], float(rm[bi])), best_nis=(qs[bn[0]], rs[bn[1]], float(rm[bn]), float(ni[bn])),
                         best_nees=(qs[be[0]], rs[be[1]], float(rm[be]), float(ne[be])),
                         datasheet=(float(rm[ds]), float(ne[ds]), float(ni[ds])),
                         truth=(float(rm[qs.index(2), rs.index(1.5)]), float(ne[qs.index(2), rs.index(1.5)]), float(ni[qs.index(2), rs.index(1.5)])),
                         rmse=rm.tolist(), nees=ne.tolist(), nis=ni.tolist())
    ql = [f"×{q:g}" for q in qs]; rl = [f"×{r_:g}" for r_ in rs]
    save("tuning_rmse", heatmap(ql, rl, rm, "IMU noise (Q) scale", "pixel noise (R) scale", lambda v: f"{v:.1f}", "Position RMSE (mm)", best=bi, mark=ds, height=240))
    save("tuning_nees", heatmap(ql, rl, ne, "IMU noise (Q) scale", "pixel noise (R) scale", lambda v: f"{v:.2g}", "ANEES", best=be, mark=ds, height=240))
    save("tuning_nis", heatmap(ql, rl, ni, "IMU noise (Q) scale", "pixel noise (R) scale", lambda v: f"{v:.2g}", "ANIS", best=bn, mark=ds, height=240))

# ---------- outliers ----------
if os.path.exists("results/outliers.pkl"):
    A = agg(L("outliers"))
    fr = [0, 0.02, 0.05, 0.1, 0.2, 0.3]
    NUM["outliers"] = {f"{f}_{g}": dict(median_mm=float(np.median(A[(f, g)]["pos_rmse"]) * 1000), mean_mm=float(np.mean(A[(f, g)]["pos_rmse"]) * 1000),
                                        lost=int(np.sum(A[(f, g)]["pos_rmse"] > 0.1)), runs=len(A[(f, g)]["pos_rmse"]),
                                        acc=int(np.sum(A[(f, g)]["outliers_accepted"])), out=int(np.sum(A[(f, g)]["outliers"])),
                                        inflate=int(np.sum(A[(f, g)]["inflate"])), feats=int(np.sum(A[(f, g)]["feats"]))) for f in fr for g in [0, 1, 2]}
    ser = []
    for g, c, nm in [(0, "--c2", "no gate"), (1, "--c4", "χ² gate"), (2, "--c1", "χ² gate + guard")]:
        ser.append(dict(name=nm, x=[f * 100 for f in fr], y=[np.sum(A[(f, g)]["pos_rmse"] > 0.1) for f in fr], color=c, markers=True))
    save("outliers", line_chart(ser, "features that are wrong matches (%)", "runs lost (of 16)", ylim=(0, 17),
                                label="Runs lost vs outlier rate", hover_fmt="{:.0f} of 16 lost", height=260))

# ---------- latency ----------
if os.path.exists("results/latency.pkl"):
    res = L("latency"); A = agg(res)
    Ls = [0, 0.01, 0.02, 0.03, 0.05, 0.075, 0.1]
    NUM["latency"] = {f"{l}_{m}": dict(rmse_mm=float(np.mean(A[(l, m)]["pos_rmse"]) * 1000), nees=float(np.mean(A[(l, m)]["anees15"])),
                                       rot=float(np.mean(A[(l, m)]["rot_rmse_deg"])), sec=float(np.mean(A[(l, m)]["sec"])))
                      for l in Ls for m in ["naive", "replay"]}
    NUM["lidar30"] = {m: dict(rmse_mm=float(np.mean(A[("lidar30", m)]["pos_rmse"]) * 1000), nees=float(np.mean(A[("lidar30", m)]["anees15"])),
                              sec=float(np.mean(A[("lidar30", m)]["sec"]))) for m in ["naive", "replay"]}
    save("latency", line_chart([
        dict(name="fuse on arrival", x=[l * 1000 for l in Ls], y=[np.mean(A[(l, "naive")]["pos_rmse"]) * 1000 for l in Ls], color="--c2", markers=True),
        dict(name="rewind + replay", x=[l * 1000 for l in Ls], y=[np.mean(A[(l, "replay")]["pos_rmse"]) * 1000 for l in Ls], color="--c1", markers=True)],
        "camera latency (ms)", "position RMSE (mm, log)", logy=True, ylim=(1, 1000), label="Latency", hover_fmt="{:.3g} mm"))

# ---------- td ----------
if os.path.exists("results/td.pkl"):
    res = L("td"); A = agg(res)
    tds = [-0.03, -0.015, 0, 0.015, 0.03]
    NUM["td"] = {f"{k[0]}_{k[1]}_{k[2]}": dict(rmse_mm=float(np.mean(v["pos_rmse"]) * 1000), med_mm=float(np.median(v["pos_rmse"]) * 1000),
                                              nees=float(np.mean(v["anees15"]))) for k, v in A.items()}
    fin = defaultdict(list); sd = defaultdict(list)
    for name, seed, summ, tr in res:
        if name[1]:
            fin[name].append(tr["td"][-1] * 1000); sd[name].append(np.sqrt(tr["Pd"][-1, 21]) * 1000)
            i = np.searchsorted(tr["t"], 10.0)
            fin[name + ("10s",)].append(tr["td"][i] * 1000)
    NUM["td_final_ms"] = {str(k): dict(mean=float(np.mean(v)), std=float(np.std(v)), min=float(np.min(v)), max=float(np.max(v)),
                                       filter_sd=float(np.mean(sd[k])) if k in sd else None) for k, v in fin.items()}
    ser = []
    cols = {-0.03: "--c2", 0.015: "--c1", 0.03: "--c3"}
    for td in [0.03, 0.015, -0.03]:
        tr = [x for x in res if x[0] == (td, 1, 400) and x[1] == 0][0][3]
        s3 = np.sqrt(tr["Pd"][:, 21]) * 1000 * 3
        ser.append(dict(name=f"true {td * 1000:+.0f} ms", x=tr["t"], y=tr["td"] * 1000, band=(tr["td"] * 1000 - s3, tr["td"] * 1000 + s3), color=cols[td]))
    save("td", line_chart(ser, "time (s)", "estimated camera time offset (ms)", ylim=(-50, 50), xlim=(0, 60), label="Time offset estimate",
                          hover_fmt="{:.1f} ms", hlines=[]))
    ser = [dict(name="ignored", x=[t * 1000 for t in tds], y=[np.mean(A[(t, 0, 400)]["pos_rmse"]) * 1000 for t in tds], color="--c2", markers=True),
           dict(name="estimated", x=[t * 1000 for t in tds], y=[np.mean(A[(t, 1, 400)]["pos_rmse"]) * 1000 for t in tds], color="--c1", markers=True)]
    save("td_rmse", line_chart(ser, "true camera time offset (ms)", "position RMSE (mm, log)", logy=True, ylim=(1, 300), label="Time offset RMSE",
                               hover_fmt="{:.3g} mm", height=260))

# ---------- extrinsic ----------
if os.path.exists("results/extrinsic.pkl"):
    res = L("extrinsic"); A = agg(res)
    NUM["extrinsic"] = {f"{k[0]}_{k[1]}": dict(rmse_mm=float(np.mean(v["pos_rmse"]) * 1000), rot=float(np.mean(v["rot_rmse_deg"])),
                                               nees=float(np.mean(v["anees15"]))) for k, v in A.items()}
    true_pbc = np.array([0.05, -0.02, 0.03])
    for tr_kind in ["rich", "planar"]:
        rr = [x for x in res if x[0] == (tr_kind, 1)]
        t = rr[0][3]["t"]
        perr = np.array([np.abs(x[3]["pbc"] - true_pbc) * 1000 for x in rr])      # seeds x T x 3 (mm)
        rerr = np.array([np.abs(np.rad2deg(x[3]["phi_err"])) for x in rr])
        psd = np.array([np.sqrt(x[3]["Pd"][:, 18:21]) * 1000 for x in rr])
        rsd = np.array([np.rad2deg(np.sqrt(x[3]["Pd"][:, 15:18])) for x in rr])
        NUM[f"extr_{tr_kind}"] = dict(pbc_err_mm=np.median(perr[:, -1], 0).tolist(), pbc_sd_mm=np.median(psd[:, -1], 0).tolist(),
                                      rot_err_deg=np.median(rerr[:, -1], 0).tolist(), rot_sd_deg=np.median(rsd[:, -1], 0).tolist())
        ser = [dict(name=f"{a} error", x=t, y=np.median(perr[:, :, i], 0), color=c) for i, (a, c) in enumerate(zip("xyz", ["--c1", "--c2", "--c3"]))]
        ser += [dict(name=f"{a} 1σ", x=t, y=np.median(psd[:, :, i], 0), color=c, dash=True, label=False) for i, (a, c) in enumerate(zip("xyz", ["--c1", "--c2", "--c3"]))]
        save(f"extr_{tr_kind}", line_chart(ser, "time (s)", "lever-arm error (mm, log)", logy=True, ylim=(0.1, 60), xlim=(0, 60), height=240,
                                           label=f"Extrinsic translation, {tr_kind} motion", hover_fmt="{:.2g} mm"))

# ---------- dropout ----------
if os.path.exists("results/dropout.pkl"):
    res = L("dropout"); A = agg(res)
    ser = []
    for m, c, nm in [("cam", "--c2", "camera only"), ("cam+lidar", "--c1", "camera + LiDAR")]:
        rr = [x for x in res if x[0] == m]
        t = rr[0][3]["t"]
        pe = np.median([np.linalg.norm(x[3]["pos_err"], axis=1) * 100 for x in rr], 0)
        sd = np.median([np.sqrt(x[3]["Pd"][:, :3].sum(1)) * 100 for x in rr], 0)
        ser.append(dict(name=nm, x=t, y=pe, color=c))
        ser.append(dict(name=f"{nm} 3σ", x=t, y=3 * sd, color=c, dash=True, label=False))
        i = np.searchsorted(t, 29.95)
        NUM[f"dropout_{m}"] = dict(err_at_30_cm=float(pe[i]), sd3_at_30_cm=float(3 * sd[i]), max_cm=float(np.median([x[2]["pos_max"] for x in rr]) * 100))
    save("dropout", line_chart(ser, "time (s)", "position error (cm, log)", logy=True, ylim=(0.05, 1000), xlim=(5, 40),
                               vspans=[(20, 30, "camera dark")], label="Camera dropout", hover_fmt="{:.3g} cm"))

json.dump(NUM, open("figs/numbers.json", "w"), indent=1, default=float)
print(json.dumps(NUM, indent=1, default=float)[:6000])
