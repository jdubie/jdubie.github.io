"""Ground truth + sensor simulation: a handheld-style rig (IMU, camera, LiDAR) in a 10 x 10 x 3 m room of known landmarks."""
import numpy as np

G = np.array([0.0, 0.0, -9.81])


def skew(v):
    return np.array([[0, -v[2], v[1]], [v[2], 0, -v[0]], [-v[1], v[0], 0]])


def Exp(w):
    th = np.linalg.norm(w)
    K = skew(w)
    if th < 1e-9:
        return np.eye(3) + K
    return np.eye(3) + np.sin(th) / th * K + (1 - np.cos(th)) / th**2 * K @ K


def Log(R):
    c = np.clip((np.trace(R) - 1) / 2, -1, 1)
    th = np.arccos(c)
    w = np.array([R[2, 1] - R[1, 2], R[0, 2] - R[2, 0], R[1, 0] - R[0, 1]]) / 2
    if th < 1e-7:
        return w
    return w * th / np.sin(th)


def euler_R(r, p, y):  # ZYX: R = Rz(y) Ry(p) Rx(r), body -> world
    cr, sr, cp, sp, cy, sy = np.cos(r), np.sin(r), np.cos(p), np.sin(p), np.cos(y), np.sin(y)
    return np.array([[cy * cp, cy * sp * sr - sy * cr, cy * sp * cr + sy * sr],
                     [sy * cp, sy * sp * sr + cy * cr, sy * sp * cr - cy * sr],
                     [-sp, cp * sr, cp * cr]])


class Trajectory:
    """Analytic trajectory: position and ZYX Euler angles are sums of sinusoids, so velocity, acceleration
    and body angular rate are exact. kind='rich' moves in 3D and rotates about all axes; kind='planar' is a
    constant-height circle with yaw-only rotation (a ground robot), the low-excitation case."""

    def __init__(self, kind="rich"):
        self.kind = kind

    def _terms(self, t):
        w0 = 2 * np.pi / 20
        if self.kind == "planar":
            pos = [(2.0, w0, 0.0), (2.0, w0, np.pi / 2)]  # x = 2 cos, y = 2 sin
            p = np.array([2.0 * np.cos(w0 * t), 2.0 * np.sin(w0 * t), 1.0])
            v = np.array([-2.0 * w0 * np.sin(w0 * t), 2.0 * w0 * np.cos(w0 * t), 0.0])
            a = np.array([-2.0 * w0**2 * np.cos(w0 * t), -2.0 * w0**2 * np.sin(w0 * t), 0.0])
            eul = np.array([0.0, 0.0, w0 * t + np.pi / 2])
            deul = np.array([0.0, 0.0, w0])
            return p, v, a, eul, deul
        # figure-eight in xy, bobbing in z, about 1.3 m/s peak
        A = np.array([2.5, 2.0, 0.3]); W = np.array([w0, 2 * w0, 3 * w0]); P0 = np.array([0.0, 0.0, 0.5])
        p = A * np.sin(W * t + P0) + np.array([0, 0, 1.2])
        v = A * W * np.cos(W * t + P0)
        a = -A * W**2 * np.sin(W * t + P0)
        # roll, pitch wobble; yaw turns slowly plus a sway: excites all three rotation axes
        r = 0.3 * np.sin(0.9 * t + 1.0); dr = 0.27 * np.cos(0.9 * t + 1.0)
        pi = 0.25 * np.sin(0.7 * t); dpi = 0.175 * np.cos(0.7 * t)
        y = 0.3 * t + 0.4 * np.sin(1.3 * t); dy = 0.3 + 0.52 * np.cos(1.3 * t)
        return p, v, a, np.array([r, pi, y]), np.array([dr, dpi, dy])

    def state(self, t):
        p, v, a, (r, pi, y), (dr, dpi, dy) = self._terms(t)
        R = euler_R(r, pi, y)
        # ZYX Euler rates -> body angular velocity
        w = np.array([dr - dy * np.sin(pi),
                      dpi * np.cos(r) + dy * np.cos(pi) * np.sin(r),
                      -dpi * np.sin(r) + dy * np.cos(pi) * np.cos(r)])
        return p, v, a, R, w


# camera frame: z forward (= body x), x right (= -body y), y down (= -body z)
R_BC0 = np.array([[0, 0, 1], [-1, 0, 0], [0, -1, 0]], float)


def default_config(**kw):
    c = dict(
        T=60.0, imu_rate=400.0, cam_rate=30.0, lidar_rate=10.0, traj="rich",
        # IMU (consumer MEMS, roughly a phone/BMI088-class part). densities: rad/s/sqrt(Hz), m/s^2/sqrt(Hz)
        gyro_nd=3e-4, acc_nd=3e-3, gyro_rw=3e-5, acc_rw=3e-4, bg0_std=0.005, ba0_std=0.05,
        # camera: 640x480, ~77 deg horizontal FOV
        fx=400.0, fy=400.0, cx=320.0, cy=240.0, W=640, H=480, px_std=1.0, max_feats=20,
        p_bc=np.array([0.05, -0.02, 0.03]), R_bc=R_BC0 @ Exp(np.array([0.01, -0.02, 0.015])),
        cam_latency=0.0, cam_td=0.0, outlier_frac=0.0, cam_on=True, cam_dropout=None,
        # LiDAR: matched landmark points in its own frame (a stand-in for scan matching)
        lidar_on=False, lidar_std=0.02, lidar_range=8.0, lidar_vfov=np.deg2rad(20), lidar_max=30,
        p_bl=np.array([0.0, 0.0, 0.10]), R_bl=np.eye(3), lidar_latency=0.0,
        n_landmarks=300, seed=0,
    )
    c.update(kw)
    return c


def landmarks(rng, n):
    """Points on the walls, floor and ceiling of a 10 x 10 x 3 m room."""
    L = []
    for _ in range(n):
        f = rng.integers(6)
        u, v = rng.uniform(-5, 5), rng.uniform(-5, 5)
        h = rng.uniform(0, 3)
        L.append([[-5, u, h], [5, u, h], [u, -5, h], [u, 5, h], [u, v, 0], [u, v, 3]][f])
    return np.array(L, float)


def simulate(cfg):
    rng = np.random.default_rng(cfg["seed"])
    traj = Trajectory(cfg["traj"])
    lm = landmarks(rng, cfg["n_landmarks"])
    dt = 1 / cfg["imu_rate"]
    N = int(cfg["T"] * cfg["imu_rate"])
    t_imu = np.arange(N) * dt
    bg = rng.normal(0, cfg["bg0_std"], 3); ba = rng.normal(0, cfg["ba0_std"], 3)
    acc = np.zeros((N, 3)); gyr = np.zeros((N, 3)); BG = np.zeros((N, 3)); BA = np.zeros((N, 3))
    gt_p = np.zeros((N, 3)); gt_v = np.zeros((N, 3)); gt_R = np.zeros((N, 3, 3))
    for k, t in enumerate(t_imu):
        p, v, a, R, w = traj.state(t)
        gt_p[k], gt_v[k], gt_R[k] = p, v, R
        BG[k], BA[k] = bg, ba
        gyr[k] = w + bg + rng.normal(0, cfg["gyro_nd"] / np.sqrt(dt), 3)
        acc[k] = R.T @ (a - G) + ba + rng.normal(0, cfg["acc_nd"] / np.sqrt(dt), 3)
        bg = bg + rng.normal(0, cfg["gyro_rw"] * np.sqrt(dt), 3)
        ba = ba + rng.normal(0, cfg["acc_rw"] * np.sqrt(dt), 3)

    cams = []  # dicts: stamp (sensor clock), arrival, uv (n,2), ids
    if cfg["cam_on"]:
        t0 = rng.uniform(0, 1 / cfg["cam_rate"])
        for tc in np.arange(t0, cfg["T"] - 0.2, 1 / cfg["cam_rate"]):
            if cfg["cam_dropout"] and cfg["cam_dropout"][0] <= tc < cfg["cam_dropout"][1]:
                continue
            p, v, a, R, w = traj.state(tc)
            pc = (R @ cfg["R_bc"]).T @ (lm - p - R @ cfg["p_bc"]).T  # 3 x n in camera frame
            X, Y, Z = pc
            with np.errstate(divide="ignore", invalid="ignore"):
                u = cfg["fx"] * X / Z + cfg["cx"]; vv = cfg["fy"] * Y / Z + cfg["cy"]
            vis = np.where((Z > 0.3) & (u >= 0) & (u < cfg["W"]) & (vv >= 0) & (vv < cfg["H"]))[0]
            if len(vis) > cfg["max_feats"]:
                vis = rng.choice(vis, cfg["max_feats"], replace=False)
            uv = np.stack([u[vis], vv[vis]], 1) + rng.normal(0, cfg["px_std"], (len(vis), 2))
            out = rng.random(len(vis)) < cfg["outlier_frac"]  # wrong data association: a random pixel
            uv[out] = rng.uniform([0, 0], [cfg["W"], cfg["H"]], (out.sum(), 2))
            cams.append(dict(kind="cam", stamp=tc - cfg["cam_td"], arrival=tc + cfg["cam_latency"], z=uv, ids=vis, outlier=out))
    lids = []
    if cfg["lidar_on"]:
        t0 = rng.uniform(0, 1 / cfg["lidar_rate"])
        for tl in np.arange(t0, cfg["T"] - 0.2, 1 / cfg["lidar_rate"]):
            p, v, a, R, w = traj.state(tl)
            pl = (R @ cfg["R_bl"]).T @ (lm - p - R @ cfg["p_bl"]).T
            rng_ = np.linalg.norm(pl, axis=0)
            el = np.arcsin(np.clip(pl[2] / np.maximum(rng_, 1e-9), -1, 1))
            vis = np.where((rng_ < cfg["lidar_range"]) & (np.abs(el) < cfg["lidar_vfov"] / 2))[0]
            if len(vis) > cfg["lidar_max"]:
                vis = rng.choice(vis, cfg["lidar_max"], replace=False)
            z = pl[:, vis].T + rng.normal(0, cfg["lidar_std"], (len(vis), 3))
            lids.append(dict(kind="lidar", stamp=tl, arrival=tl + cfg["lidar_latency"], z=z, ids=vis))
    return dict(cfg=cfg, lm=lm, t=t_imu, acc=acc, gyr=gyr, gt_p=gt_p, gt_v=gt_v, gt_R=gt_R, gt_bg=BG, gt_ba=BA,
                meas=sorted(cams + lids, key=lambda m: m["arrival"]))
