"""Evolve a car (cawro-style GA over the full parameter set) to find how much a tuned car beats the obvious ones.

  python3 study/evolve.py --terrain '{"tiles":1000,"ramp_tiles":1000,"power":1,"max_angle":1.6}' --gens 25 --out runs/evolve/r1

Fitness: mean distance over SEEDS courses, freshly drawn each generation (so a car cannot overfit a course).
Writes <out>/log.jsonl (one line per generation: best, mean, the best params).
"""

from __future__ import annotations

import argparse
import json
import random
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SIGNS = [(1, 0), (1, 1), (0, 1), (-1, 1), (-1, 0), (-1, -1), (0, -1), (1, -1)]
MAXED = {"motor_speed": [30, 30], "torque_scale": [2, 2], "wheel_radius": [0.6, 0.6]}


GAINS = {"base": (0, 30, 3), "kp": (0, 80, 8), "kd": (0, 20, 2), "kh": (-10, 10, 1.5), "kv": (0, 10, 1), "vmax": (2, 20, 2),
         "rear": (0, 1, 0.1), "front": (0, 1, 0.1)}


def random_car(rng: random.Random) -> dict:
    return {"gains": {k: rng.uniform(lo, hi) for k, (lo, hi, _) in GAINS.items()},
        "chassis": [[sx * rng.uniform(0.1, 1.2), sy * rng.uniform(0.1, 1.2)] for sx, sy in SIGNS],
        "wheel_vertex": rng.sample(range(8), 2),
        "wheel_radius": [rng.uniform(0.2, 0.7) for _ in range(2)],
        "wheel_density": [rng.uniform(40, 140) for _ in range(2)],
        "motor_speed": [rng.uniform(10, 30) for _ in range(2)],
        "torque_scale": [rng.uniform(0.5, 2) for _ in range(2)],
    }


def clip(x: float, lo: float, hi: float) -> float:
    return min(hi, max(lo, x))


def mutate(c: dict, rng: random.Random, rate: float) -> dict:
    c = json.loads(json.dumps(c))
    for i, (sx, sy) in enumerate(SIGNS):
        for j, s in enumerate((sx, sy)):
            if s and rng.random() < rate:
                c["chassis"][i][j] = s * clip(abs(c["chassis"][i][j]) + rng.gauss(0, 0.2), 0.1, 1.2)
    if rng.random() < rate:
        c["wheel_vertex"] = rng.sample(range(8), 2)
    for k, (lo, hi, sd) in GAINS.items():
        if rng.random() < rate:
            c["gains"][k] = clip(c["gains"][k] + rng.gauss(0, sd), lo, hi)
    for k, lo, hi, sd in (("wheel_radius", 0.2, 0.7, 0.08), ("wheel_density", 40, 140, 15), ("motor_speed", -30, 30, 4), ("torque_scale", 0, 2, 0.25)):
        for i in range(2):
            if rng.random() < rate:
                c[k][i] = clip(c[k][i] + rng.gauss(0, sd), lo, hi)
    return c


def cross(a: dict, b: dict, rng: random.Random) -> dict:
    c = {k: json.loads(json.dumps(rng.choice((a, b))[k])) for k in a}
    c["gains"] = {k: rng.choice((a, b))["gains"][k] for k in GAINS}
    c["chassis"] = [list(rng.choice((a, b))["chassis"][i]) for i in range(8)]
    if c["wheel_vertex"][0] == c["wheel_vertex"][1]:
        c["wheel_vertex"] = list(a["wheel_vertex"])
    return c


def evaluate(cars: list[dict], terrain: dict, seeds: list[int], max_s: float, workers: int, static: bool = False) -> list[float]:
    def job(i: int, c: dict, s: int) -> dict:
        j = {"id": [i, s], "seed": s, "terrain": terrain, "max_s": max_s, "params": {k: v for k, v in c.items() if k != "gains"}}
        return j if static else {**j, "policy": "tuned", "policy_args": c["gains"]}
    jobs = "".join(json.dumps(job(i, c, s)) + "\n" for i, c in enumerate(cars) for s in seeds)
    out = subprocess.run(["node", str(ROOT / "sim" / "batch.js"), "--workers", str(workers)], input=jobs, capture_output=True, text=True, check=True)
    tot = [0.0] * len(cars)
    for line in out.stdout.splitlines():
        r = json.loads(line)
        tot[r["id"][0]] += r["distance"] / len(seeds)
    return tot


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--terrain", required=True)
    ap.add_argument("--gens", type=int, default=25)
    ap.add_argument("--pop", type=int, default=64)
    ap.add_argument("--seeds", type=int, default=16)
    ap.add_argument("--max-s", type=float, default=600)
    ap.add_argument("--workers", type=int, default=20)
    ap.add_argument("--out", required=True)
    ap.add_argument("--static", action="store_true", help="no controller: the car's params never change (a fixed-car search)")
    a = ap.parse_args()
    terrain = json.loads(a.terrain)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    rng = random.Random(0)
    pop = [random_car(rng) for _ in range(a.pop)]
    for g in range(a.gens):
        seeds = [rng.randrange(10**9) for _ in range(a.seeds)]
        fit = evaluate(pop, terrain, seeds, a.max_s, a.workers, a.static)
        order = sorted(range(len(pop)), key=lambda i: -fit[i])
        with (out / "log.jsonl").open("a") as f:
            f.write(json.dumps({"gen": g, "best": fit[order[0]], "mean": sum(fit) / len(fit), "params": pop[order[0]]}) + "\n")
        print(f"gen {g:3d} best {fit[order[0]]:7.1f} mean {sum(fit) / len(fit):7.1f}", flush=True)
        elite = [pop[i] for i in order[: a.pop // 8]]
        parents = [pop[i] for i in order[: a.pop // 2]]
        pop = elite + [mutate(cross(rng.choice(parents), rng.choice(parents), rng), rng, 0.15) for _ in range(a.pop - len(elite))]


if __name__ == "__main__":
    main()
