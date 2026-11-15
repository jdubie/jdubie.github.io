"""Floor costs of keeping 1M pgbench_accounts rows (scale 10) as empty-file names on the 4 KB tmpfs, in Python."""
import os, sys, time, random
root = "/data/bench"
mode = sys.argv[1]
N = int(sys.argv[2]) if len(sys.argv) > 2 else 1_000_000
def name(aid):  # aid|bid|abalance|filler(84 spaces) -> what pgbench rows carry; hex of a compact form ~ 2x
    return f"{aid}.{(aid - 1) // 100000 + 1}.0.{'x' * 40}"
if mode == "create":
    os.makedirs(root, exist_ok=True)
    t0 = time.time()
    for d in range(1000):
        os.mkdir(f"{root}/{d:03d}")
    for aid in range(1, N + 1):
        fd = os.open(f"{root}/{aid % 1000:03d}/{name(aid)}", os.O_CREAT | os.O_WRONLY, 0o644)
        os.close(fd)
    print("create", N, round(time.time() - t0, 2), "s")
elif mode == "scan":
    t0 = time.time()
    rows = {}
    for d in os.scandir(root):
        for e in os.scandir(d.path):
            aid, bid, bal, _ = e.name.split(".", 3)
            rows[int(aid)] = (int(bid), int(bal))
    print("scan+decode", len(rows), round(time.time() - t0, 2), "s")
elif mode == "stat":
    t0 = time.time()
    for _ in range(20000):
        aid = random.randint(1, N)
        os.stat(f"{root}/{aid % 1000:03d}/{name(aid)}")
    print("20k stats", round(time.time() - t0, 3), "s")
elif mode == "update":
    t0 = time.time()
    for _ in range(20000):
        aid = random.randint(1, N)
        p = f"{root}/{aid % 1000:03d}/{name(aid)}"
        os.rename(p, p + "~"); os.rename(p + "~", p)
    print("20k rename pairs", round(time.time() - t0, 3), "s")
