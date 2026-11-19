"""Error-state Kalman filter (ESKF) for IMU + camera (+ LiDAR), written from scratch in numpy.

Nominal state: p, v (world), R (body->world), ba, bg, camera extrinsic (R_bc, p_bc), camera time offset td.
Error state (22): dp, dv, dtheta (local, R_true = R Exp(dtheta)), dba, dbg, dphi_bc, dp_bc, dtd.
States that are not being estimated keep zero covariance, which pins them to their initial value.

Measurements are fused at their own timestamps: the filter keeps a short history of checkpoints, and a
late measurement rewinds to the checkpoint before its timestamp, is applied there, and the IMU samples
since are replayed ("rewind and replay"). mode='naive' instead applies every measurement on arrival.
"""
import numpy as np
from sim import G, skew, Exp, Log

IP, IV, ITH, IBA, IBG, IPHI, IPBC, ITD = 0, 3, 6, 9, 12, 15, 18, 21
NX = 22
CHI2_99 = {2: 9.21, 3: 11.34}


class State:
    __slots__ = ("p", "v", "R", "ba", "bg", "Rbc", "pbc", "td", "P")

    def copy(self):
        s = State()
        s.p, s.v, s.R, s.ba, s.bg = self.p.copy(), self.v.copy(), self.R.copy(), self.ba.copy(), self.bg.copy()
        s.Rbc, s.pbc, s.td, s.P = self.Rbc.copy(), self.pbc.copy(), self.td, self.P.copy()
        return s


def default_filter(**kw):
    f = dict(gyro_nd=3e-4, acc_nd=3e-3, gyro_rw=3e-5, acc_rw=3e-4, px_std=1.0, lidar_std=0.02,
             q_scale=1.0, r_scale=1.0,  # multipliers on the noise standard deviations (the tuning knobs)
             P0_p=0.3, P0_v=0.2, P0_th=np.deg2rad(3), P0_ba=0.1, P0_bg=0.01,
             est_extrinsic=False, P0_phi=np.deg2rad(3), P0_pbc=0.03, est_td=False, P0_td=0.03, td_start=0.0,
             init_err=True, zero_bias=False, gate=None, gate_guard=False, mode="replay", use_cam=True, use_lidar=True, window=0.3)
    f.update(kw)
    return f


def run(sim, fp, seed=0, record_every=4, extr_init=None, td_init=0.0):
    """Run the filter over a simulated sequence. Returns logged estimates, errors, NEES and NIS."""
    cfg = sim["cfg"]; rng = np.random.default_rng(10_000 + seed)
    dt = 1 / cfg["imu_rate"]; N = len(sim["t"])
    qs, rs = fp["q_scale"], fp["r_scale"]
    sa, sg = fp["acc_nd"] * qs, fp["gyro_nd"] * qs
    sba, sbg = fp["acc_rw"] * qs, fp["gyro_rw"] * qs
    px, sl = fp["px_std"] * rs, fp["lidar_std"] * rs
    Kcam = (cfg["fx"], cfg["fy"], cfg["cx"], cfg["cy"])

    # ---- initial state: truth plus an error drawn from P0 (so NEES is meaningful) ----
    P0 = np.zeros(NX)
    P0[IP:IP + 3] = fp["P0_p"] ** 2; P0[IV:IV + 3] = fp["P0_v"] ** 2; P0[ITH:ITH + 3] = fp["P0_th"] ** 2
    P0[IBA:IBA + 3] = fp["P0_ba"] ** 2; P0[IBG:IBG + 3] = fp["P0_bg"] ** 2
    if fp["est_extrinsic"]:
        P0[IPHI:IPHI + 3] = fp["P0_phi"] ** 2; P0[IPBC:IPBC + 3] = fp["P0_pbc"] ** 2
    if fp["est_td"] and fp["td_start"] <= 0:
        P0[ITD] = fp["P0_td"] ** 2
    td_pending = fp["est_td"] and fp["td_start"] > 0
    e0 = rng.normal(0, np.sqrt(P0[:15])) if fp["init_err"] else np.zeros(15)
    s = State()
    s.p = sim["gt_p"][0] + e0[0:3]; s.v = sim["gt_v"][0] + e0[3:6]; s.R = sim["gt_R"][0] @ Exp(e0[6:9])
    s.ba = sim["gt_ba"][0] + e0[9:12]; s.bg = sim["gt_bg"][0] + e0[12:15]
    if fp["zero_bias"]:
        s.ba, s.bg = np.zeros(3), np.zeros(3)
    if extr_init is None:
        s.Rbc, s.pbc = cfg["R_bc"].copy(), cfg["p_bc"].copy()
    else:
        s.Rbc, s.pbc = extr_init
    s.td = td_init
    s.P = np.diag(P0)
    Qc = np.zeros(NX)
    Qc[IV:IV + 3] = sa ** 2; Qc[ITH:ITH + 3] = sg ** 2; Qc[IBA:IBA + 3] = sba ** 2; Qc[IBG:IBG + 3] = sbg ** 2

    acc, gyr, lm = sim["acc"], sim["gyr"], sim["lm"]

    def propagate(s, am, wm, h):
        if h <= 0:
            return
        an = am - s.ba; w = wm - s.bg
        a = s.R @ an + G
        F = np.eye(NX)
        F[IP:IP + 3, IV:IV + 3] = np.eye(3) * h
        F[IV:IV + 3, ITH:ITH + 3] = -s.R @ skew(an) * h
        F[IV:IV + 3, IBA:IBA + 3] = -s.R * h
        dR = Exp(w * h)
        F[ITH:ITH + 3, ITH:ITH + 3] = dR.T
        F[ITH:ITH + 3, IBG:IBG + 3] = -np.eye(3) * h
        s.p = s.p + s.v * h + 0.5 * a * h * h
        s.v = s.v + a * h
        s.R = s.R @ dR
        P = s.P
        # F is identity outside rows 0..14: only the core block and its cross terms change
        Fc = F[:15, :15]
        P[:15, :15] = Fc @ P[:15, :15] @ Fc.T
        P[:15, 15:] = Fc @ P[:15, 15:]
        P[15:, :15] = P[:15, 15:].T
        P[np.arange(NX), np.arange(NX)] += Qc * h

    def inject(s, dx):
        s.p += dx[IP:IP + 3]; s.v += dx[IV:IV + 3]; s.R = s.R @ Exp(dx[ITH:ITH + 3])
        s.ba += dx[IBA:IBA + 3]; s.bg += dx[IBG:IBG + 3]
        s.Rbc = s.Rbc @ Exp(dx[IPHI:IPHI + 3]); s.pbc += dx[IPBC:IPBC + 3]; s.td += dx[ITD]

    def kf_update(s, r, H, Rd):
        P = s.P
        PHt = P @ H.T
        S = H @ PHt + np.diag(Rd)
        K = np.linalg.solve(S, PHt.T).T
        IKH = np.eye(NX) - K @ H
        Pn = IKH @ P @ IKH.T + (K * Rd) @ K.T
        s.P = 0.5 * (Pn + Pn.T)  # keep P symmetric: K H is badly conditioned when states are strongly correlated
        inject(s, K @ r)

    stats = dict(nis=[], n_inflate=0, n_rej=0, n_out_acc=0, n_feat=0, n_out=0)

    def cam_update(s, m, w_body, te):
        L = lm[m["ids"]]
        pb = (L - s.p) @ s.R            # rows: R^T (l - p)
        pc = (pb - s.pbc) @ s.Rbc       # rows: Rbc^T (pb - pbc)
        X, Y, Z = pc.T
        ok = Z > 0.1
        rs_, Hs, out_flags = [], [], m.get("outlier", np.zeros(len(L), bool))
        dpb_dt = -np.cross(w_body, pb) - s.v @ s.R  # d/dt of R^T(l - p)
        # the state sits at te, frozen when the frame arrived; the current model says the image was taken at
        # stamp + td. Predict the pixel at that time to first order so replays stay consistent as td moves.
        shift = (m["stamp"] + s.td - te) if fp["est_td"] else 0.0
        for i in np.where(ok)[0]:
            fx, fy, cx, cy = Kcam
            zhat = np.array([fx * X[i] / Z[i] + cx, fy * Y[i] / Z[i] + cy])
            Jp = np.array([[fx / Z[i], 0, -fx * X[i] / Z[i] ** 2], [0, fy / Z[i], -fy * Y[i] / Z[i] ** 2]])
            JR = Jp @ s.Rbc.T
            H = np.zeros((2, NX))
            H[:, IP:IP + 3] = -JR @ s.R.T
            H[:, ITH:ITH + 3] = JR @ skew(pb[i])
            H[:, IPHI:IPHI + 3] = Jp @ skew(pc[i])
            H[:, IPBC:IPBC + 3] = -JR
            H[:, ITD] = JR @ dpb_dt[i]
            zhat = zhat + H[:, ITD] * shift
            rs_.append(m["z"][i] - zhat); Hs.append(H)
        if not rs_:
            return
        flags = out_flags[np.where(ok)[0]]
        stats["n_feat"] += len(rs_); stats["n_out"] += int(flags.sum())
        keep = np.ones(len(rs_), bool)
        if fp["gate"] is not None:
            for attempt in range(4):
                nis = np.array([r @ np.linalg.solve(H @ s.P @ H.T + np.eye(2) * px ** 2, r) for r, H in zip(rs_, Hs)])
                keep = nis <= fp["gate"]
                # lock-out guard: if most of a frame fails the gate, the filter (not the features) is probably wrong
                if not fp["gate_guard"] or keep.mean() >= 0.5:
                    break
                s.P[:9, :9] *= 10.0
                stats["n_inflate"] += 1
            stats["n_rej"] += int((~keep).sum())
        else:
            nis = np.array([r @ np.linalg.solve(H @ s.P @ H.T + np.eye(2) * px ** 2, r) for r, H in zip(rs_, Hs)])
        stats["n_out_acc"] += int(flags[keep].sum())
        stats["nis"].extend(nis[keep].tolist())
        if keep.any():
            idx = np.where(keep)[0]
            kf_update(s, np.concatenate([rs_[i] for i in idx]), np.vstack([Hs[i] for i in idx]), np.full(2 * len(idx), px ** 2))

    def lidar_update(s, m):
        L = lm[m["ids"]]; Rbl, pbl = cfg["R_bl"], cfg["p_bl"]
        pb = (L - s.p) @ s.R
        zhat = (pb - pbl) @ Rbl
        rs_, Hs = [], []
        for i in range(len(L)):
            H = np.zeros((3, NX))
            H[:, IP:IP + 3] = -Rbl.T @ s.R.T
            H[:, ITH:ITH + 3] = Rbl.T @ skew(pb[i])
            r = m["z"][i] - zhat[i]
            S = H @ s.P @ H.T + np.eye(3) * sl ** 2
            nis = r @ np.linalg.solve(S, r)
            if fp["gate"] is not None and nis > CHI2_99[3] * 1.5:
                continue
            rs_.append(r); Hs.append(H)
        if rs_:
            kf_update(s, np.concatenate(rs_), np.vstack(Hs), np.full(3 * len(rs_), sl ** 2))

    def step(s, j, buf):
        """Propagate checkpoint at t_j to t_{j+1} with IMU sample j, applying buffered measurements in [t_j, t_j+dt)."""
        t = j * dt; tend = t + dt
        for te, _, m in buf:
            if t <= te < tend:
                propagate(s, acc[j], gyr[j], te - t); t = te
                if m["kind"] == "cam":
                    cam_update(s, m, gyr[j] - s.bg, te)
                else:
                    lidar_update(s, m)
        propagate(s, acc[j], gyr[j], tend - t)

    # ---- event loop ----
    meas = [m for m in sim["meas"] if (m["kind"] == "cam" and fp["use_cam"]) or (m["kind"] == "lidar" and fp["use_lidar"])]
    mi = 0
    ckpt = {}                    # j -> State at t_j (all measurements with t_eff < t_j applied)
    buf = []                     # (t_eff, seq, meas), sorted
    win = int(fp["window"] / dt) + 2
    log = {k: [] for k in ["t", "p", "v", "th_err", "ba", "bg", "phi_err", "pbc", "td", "Pd", "nees_pose", "nees15"]}
    for k in range(N):
        tk = k * dt
        if td_pending and tk >= fp["td_start"]:  # start estimating td once the pose has converged
            for st in [s] + list(ckpt.values()):  # checkpoints too, or the next rewind would undo it
                st.P[ITD, ITD] = fp["P0_td"] ** 2
            td_pending = False
        # measurements that have arrived by t_k
        arrived = []
        while mi < len(meas) and meas[mi]["arrival"] <= tk:
            arrived.append(meas[mi]); mi += 1
        if arrived:
            jmin = k
            for m in arrived:
                te = m["stamp"] + (s.td if m["kind"] == "cam" else 0.0)
                if fp["mode"] == "naive":
                    te = tk
                te = min(max(te, tk - (win - 2) * dt, 0.0), tk)
                buf.append((te, mi, m))
                jmin = min(jmin, int(np.floor(te / dt + 1e-9)))
            buf.sort(key=lambda x: (x[0], x[1]))
            if jmin < k:
                s = ckpt[jmin].copy()
                for j in range(jmin, k):
                    ckpt[j] = s.copy()
                    step(s, j, buf)
        # trim buffers
        buf = [b for b in buf if b[0] >= tk - fp["window"]]
        ckpt.pop(k - win, None)
        if k % record_every == 0:
            Rt = sim["gt_R"][k]
            e = np.concatenate([sim["gt_p"][k] - s.p, sim["gt_v"][k] - s.v, Log(s.R.T @ Rt),
                                sim["gt_ba"][k] - s.ba, sim["gt_bg"][k] - s.bg])
            Pc = s.P[:15, :15]
            ip = [0, 1, 2, 6, 7, 8]
            log["t"].append(tk); log["p"].append(s.p.copy()); log["v"].append(s.v.copy())
            log["th_err"].append(e[6:9]); log["ba"].append(s.ba.copy()); log["bg"].append(s.bg.copy())
            log["phi_err"].append(Log(s.Rbc.T @ cfg["R_bc"])); log["pbc"].append(s.pbc.copy()); log["td"].append(s.td)
            log["Pd"].append(np.diag(s.P).copy())
            log["nees_pose"].append(e[ip] @ np.linalg.solve(Pc[np.ix_(ip, ip)], e[ip]))
            log["nees15"].append(e @ np.linalg.solve(Pc, e))
        ckpt[k] = s.copy()
        step(s, k, buf)
    out = {k: np.array(v) for k, v in log.items()}
    idx = np.arange(0, N, record_every)
    out["pos_err"] = sim["gt_p"][idx] - out["p"]
    out["stats"] = stats
    return out


def summarize(sim, out, t_skip=5.0):
    m = out["t"] >= t_skip
    pe = np.linalg.norm(out["pos_err"][m], axis=1)
    te = np.linalg.norm(out["th_err"][m], axis=1)
    st = out["stats"]
    return dict(pos_rmse=float(np.sqrt(np.mean(pe ** 2))), rot_rmse_deg=float(np.rad2deg(np.sqrt(np.mean(te ** 2)))),
                pos_max=float(pe.max()), anees_pose=float(np.mean(out["nees_pose"][m]) / 6),
                anees15=float(np.mean(out["nees15"][m]) / 15),
                anis=float(np.mean(st["nis"]) / 2) if st["nis"] else float("nan"),
                rej=st["n_rej"], inflate=st["n_inflate"], feats=st["n_feat"], outliers=st["n_out"], outliers_accepted=st["n_out_acc"])
