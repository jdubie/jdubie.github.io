// Batch evaluator for calibrating terrains and baseline cars: runs one episode per job, many jobs in parallel threads.
//
//   node sim/batch.js [--workers N] < jobs.jsonl > results.jsonl
//
// job:    {"id": any, "seed": "s", "terrain": {...} | "cawro", "params": {...} (merged over the defaults), "max_s": 600,
//          "policy": optional name in POLICIES}
// result: {"id": ..., "distance": m, "reason": "stalled" | "finished" | "timeout" | "error", "sim_s": s}
"use strict";
const { Worker, isMainThread, parentPort } = require("worker_threads");
const os = require("os");
const readline = require("readline");

if (isMainThread) {
  const i = process.argv.indexOf("--workers");
  const n = i > 0 ? +process.argv[i + 1] : os.cpus().length;
  const jobs = [];
  const rl = readline.createInterface({ input: process.stdin });
  rl.on("line", (l) => l.trim() && jobs.push(JSON.parse(l)));
  rl.on("close", () => {
    let next = 0, live = 0;
    const spawn = () => {
      const w = new Worker(__filename);
      live++;
      const feed = () => (next < jobs.length ? w.postMessage(jobs[next++]) : (w.terminate(), live--));
      w.on("message", (r) => { process.stdout.write(JSON.stringify(r) + "\n"); feed(); });
      w.on("error", (e) => { process.stderr.write(String(e) + "\n"); live--; });
      feed();
    };
    for (let k = 0; k < Math.min(n, jobs.length); k++) spawn();
  });
} else {
  const core = require("./core.js");
  const { POLICIES } = require("./policies.js");
  parentPort.on("message", (job) => {
    const terrain = typeof job.terrain === "string" ? core.TERRAINS[job.terrain] : job.terrain;
    const params = { ...core.defaultParams(terrain), ...(job.params || {}) };
    const out = { id: job.id };
    try {
      const ep = new core.Episode(0, String(job.seed), params, terrain, false);
      const policy = job.policy ? POLICIES[job.policy](job.policy_args || {}) : null;
      let reason = null;
      while (!(reason = ep.over(job.max_s || 600))) {
        if (policy) {
          const upd = policy(ep.obs(), ep.car.params);
          if (upd) ep.car.set(upd);
        }
        ep.step();
      }
      Object.assign(out, { distance: ep.maxX, reason, sim_s: ep.frames / core.FPS, energy_used: ep.used });
    } catch (e) {
      Object.assign(out, { distance: 0, reason: "error", error: e.message });
    }
    parentPort.postMessage(out);
  });
}
