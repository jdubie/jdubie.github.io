// terrain-bench simulator: stepped ("fake time") parallel environments over a unix socket. Physics is sim/core.js, a
// headless port of Rafael Matsunaga's HTML5 Genetic Algorithm 2D Car Thingy (cawro.js), after BoxCar2D, on Box2D.
//
//   node sim/server.js --socket /path/sim.sock [--record DIR] [--terrain NAME|JSON] [--envs 8] [--max-s 600] [--episodes N]
//
// Nothing moves unless a client steps it. Each environment runs in its own worker thread; a step that hangs inside the
// physics engine is killed by a watchdog and the environment restarts on a new course. --record writes
// DIR/episodes.jsonl (every finished episode), DIR/progress.jsonl (best distance so far, 1 Hz wall clock) and
// DIR/best.json (the furthest episode, every tick of every body, for replay). Protocol: PROTOCOL.md.
"use strict";
const { Worker, isMainThread, parentPort, workerData } = require("worker_threads");
const crypto = require("crypto");
const fs = require("fs");
const net = require("net");
const path = require("path");

const MAX_STEP = 6000;   // ticks per step command

if (!isMainThread) {
  // ---- one environment ----
  const core = require("./core.js");
  const { terrain, maxS, recordDir, envId } = workerData;
  const zlib = require("zlib");
  const GEOMETRY = ["chassis", "wheel_vertex", "wheel_radius", "wheel_density"];
  // Every episode, for replay: chassis and wheel poses every 4th tick (15 Hz), the body's geometry whenever it changed,
  // and the terrain up to a little past the furthest point reached. ~35 kB gzipped for a long episode.
  const saveEpisode = (r) => {
    if (!recordDir) return;
    const every = 4, track = ep.track.filter((_, i) => i % every === 0 || i === ep.track.length - 1);
    const geo = [];
    let prev = null;
    for (const [tick, p] of ep.changes) {
      const g = JSON.stringify(GEOMETRY.map((k) => p[k]));
      if (g !== prev) geo.push([tick, p]);
      prev = g;
    }
    const surface = ep.floor.surface.filter(([[x0]]) => x0 < ep.maxX + 30);
    const body = { env: envId, ...r, stride: every, n_ticks: ep.track.length, track, changes: geo, surface };
    fs.writeFileSync(path.join(recordDir, "episodes", `${r.episode}.json.gz`), zlib.gzipSync(JSON.stringify(body)));
  };
  let params = workerData.params, index = workerData.index, ep = null;
  const start = () => { ep = new core.Episode(index++, crypto.randomBytes(4).toString("hex"), params, terrain, true); return startMsg(); };
  const startMsg = () => ({ type: "episode_start", episode: ep.index, params: ep.car.params });
  let done = false;   // the run's episode budget ran out: no episode in this environment any more
  const end = (reason, best) => {
    const r = ep.result(reason);
    saveEpisode(r);
    params = ep.car.params;
    const rec = ep.maxX > best ? ep.recording(reason) : null;
    return { r, rec };
  };
  parentPort.on("message", (m) => {
    const out = { rid: m.rid, events: [], recs: [], ends: [] };
    try {
      if (m.cmd === "init") { out.events.push(start()); out.started = 1; }
      else if (m.cmd === "step") {
        if (done) throw new Error("the run's episode budget is used up; this environment has no episode");
        const all = [];
        let n = 0;
        for (; n < m.n; n++) {
          ep.step();
          if (m.obs === "all") all.push(ep.obs());
          const reason = ep.over(maxS);
          if (reason) {
            const { r, rec } = end(reason, m.best);
            out.ends.push(r); out.events.push(r);
            if (rec) out.recs.push(rec);
            out.last = ep.obs();
            if (m.allow_new) { out.events.push(start()); out.started = 1; } else done = true;
            n++;
            break;
          }
        }
        out.ticks = n;
        out.obs = m.obs === "all" ? all : m.obs === "none" ? undefined : out.last || ep.obs();
        delete out.last;
      } else if (done && ["set", "get_params", "reset"].includes(m.cmd)) {
        throw new Error("the run's episode budget is used up; this environment has no episode");
      } else if (m.cmd === "set") {
        ep.car.set(m.params || {});
        ep.changes.push([ep.frames, ep.car.params]);
        out.params = ep.car.params;
      } else if (m.cmd === "get_params") out.params = ep.car.params;
      else if (m.cmd === "reset") {
        const { r, rec } = end("reset", m.best);
        out.ends.push(r); out.events.push(r);
        if (rec) out.recs.push(rec);
        if (m.allow_new) { out.events.push(start()); out.started = 1; } else done = true;
      } else if (m.cmd === "finish") {          // run over: report the episode in progress
        if (done) throw new Error("no episode");
        const { r, rec } = end("run_end", m.best);
        done = true;
        out.ends.push(r);
        if (rec) out.recs.push(rec);
      }
    } catch (e) {
      out.error = e.message;
    }
    out.maxX = ep ? ep.maxX : 0;
    out.episode = ep ? ep.index : null;
    out.params_now = ep ? ep.car.params : params;
    parentPort.postMessage(out);
  });
  return;
}

// ---- main thread: socket, envs, records ----
const core = require("./core.js");

function parseArgs(argv) {
  const a = { socket: null, record: null, terrain: "cawro", envs: 8, maxS: 600, episodes: Infinity };
  for (let i = 2; i < argv.length; i++) {
    const k = argv[i], v = argv[++i];
    if (k === "--socket") a.socket = v;
    else if (k === "--record") a.record = v;
    else if (k === "--terrain") a.terrain = v;
    else if (k === "--envs") a.envs = +v;
    else if (k === "--max-s") a.maxS = +v;
    else if (k === "--episodes") a.episodes = +v;
    else throw new Error("unknown arg " + k);
  }
  if (!a.socket) throw new Error("--socket is required");
  a.terrain = core.TERRAINS[a.terrain] || JSON.parse(a.terrain);
  return a;
}

const args = parseArgs(process.argv);
const wallT0 = Date.now() / 1000;
const rec = (name) => path.join(args.record, name);
if (args.record) fs.mkdirSync(path.join(args.record, "episodes"), { recursive: true });
let best = 0, ticksTotal = 0, rid = 0, episodeCounter = 0;
let started = 0, reserved = 0;   // episodes begun, and grants handed to requests still in flight
const left = () => args.episodes - started;
const envs = new Map();   // id -> {worker, pending: Map(rid -> resolve), maxX, params, busy}
let nextEnvId = 0;

function logEnds(env, msg) {
  for (const r of msg.ends) {
    if (args.record) fs.appendFileSync(rec("episodes.jsonl"), JSON.stringify({ env: env.id, ...r }) + "\n");
    best = Math.max(best, r.distance);
  }
  for (const r of msg.recs) {
    if (args.record && r.distance >= best) fs.writeFileSync(rec("best.json"), JSON.stringify({ env: env.id, ...r }));
  }
}

function spawn(env) {
  env.worker = new Worker(__filename, { workerData: { terrain: args.terrain, maxS: args.maxS, params: env.params, index: episodeCounter,
                                                            recordDir: args.record, envId: env.id } });
  episodeCounter += 100000;  // episode numbers stay unique across restarted workers
  env.worker.on("message", (msg) => {
    const p = env.pending.get(msg.rid);
    env.pending.delete(msg.rid);
    env.maxX = msg.maxX; env.params = msg.params_now;
    logEnds(env, msg);
    if (p) { clearTimeout(p.timer); p.resolve(msg); }
  });
  env.worker.on("error", (e) => process.stderr.write(`env ${env.id} worker error: ${e}\n`));
}

// Send one request to an env's worker, with a watchdog that restarts the env if the physics engine hangs.
// Requests that may begin an episode (make, step, reset) carry allow_new only if the budget has room, counting
// in-flight grants, so parallel environments cannot overshoot it.
function call(env, m, ticks = 0) {
  const canStart = ["init", "step", "reset"].includes(m.cmd);
  const grant = canStart && args.episodes - started - reserved > 0;
  if (grant) reserved++;
  const settle = (msg) => { if (grant) reserved--; if (msg && msg.started) started++; return msg; };
  return new Promise((resolve0) => {
    const resolve = (msg) => resolve0(settle(msg));
    const id = ++rid;
    const timer = setTimeout(() => {
      env.pending.delete(id);
      const lost = { env: env.id, type: "episode_end", reason: "physics_error", distance: env.maxX, wall_end: Date.now() / 1000 };
      if (args.record) fs.appendFileSync(rec("episodes.jsonl"), JSON.stringify(lost) + "\n");
      best = Math.max(best, env.maxX);
      env.worker.terminate();
      for (const p of env.pending.values()) { clearTimeout(p.timer); p.resolve({ error: "environment restarted" }); }
      env.pending.clear();
      spawn(env);
      call(env, { cmd: "init" }).then((init) =>
        resolve({ error: "the physics engine hung; this environment was restarted on a new course", events: [lost, ...init.events] }));
    }, 3000 + ticks * 2);
    env.pending.set(id, { resolve, timer });
    env.worker.postMessage({ ...m, rid: id, best, allow_new: grant });
  });
}

async function makeEnv() {
  if (envs.size >= args.envs) throw new Error(`at most ${args.envs} environments; close one first`);
  if (args.episodes - started - reserved <= 0) throw new Error("the run's episode budget is used up");
  const env = { id: nextEnvId++, pending: new Map(), maxX: 0, params: core.DEFAULT_PARAMS };
  envs.set(env.id, env);
  spawn(env);
  const init = await call(env, { cmd: "init" });
  return { env: env.id, events: init.events };
}

function getEnv(m) {
  const env = envs.get(m.env);
  if (!env) throw new Error(`no environment ${JSON.stringify(m.env)}; make one with {"cmd": "make"}`);
  return env;
}

async function handle(m) {
  const reply = (x) => ({ type: "ok", cmd: m.cmd, ...(m.id !== undefined ? { id: m.id } : {}), ...x });
  switch (m.cmd) {
    case "make": return reply(await makeEnv());
    case "step": {
      const env = getEnv(m);
      const n = m.n === undefined ? 1 : m.n;
      if (!Number.isInteger(n) || n < 1 || n > MAX_STEP) throw new Error(`n: an integer 1..${MAX_STEP}`);
      const obs = m.obs || "last";
      if (!["last", "all", "none"].includes(obs)) throw new Error('obs: "last" | "all" | "none"');
      const r = await call(env, { cmd: "step", n, obs }, n);
      if (r.error) throw Object.assign(new Error(r.error), { events: r.events });
      ticksTotal += r.ticks;
      return reply({ env: env.id, ticks: r.ticks, obs: r.obs, events: r.events, best_distance: best, episodes_left: left() });
    }
    case "set": case "get_params": case "reset": {
      const env = getEnv(m);
      const r = await call(env, { cmd: m.cmd, params: m.params });
      if (r.error) throw Object.assign(new Error(r.error), { events: r.events });
      return reply({ env: env.id, params: r.params || r.params_now, events: r.events.length ? r.events : undefined });
    }
    case "close": {
      const env = getEnv(m);
      const r = await call(env, { cmd: "finish" });
      env.worker.terminate();
      envs.delete(env.id);
      return reply({ env: env.id, closed: true });
    }
    case "status":
      return reply({ envs: [...envs.keys()], best_distance: best, wall_s: Date.now() / 1000 - wallT0, ticks_total: ticksTotal,
                     episodes_used: started, episodes_left: left() });
    default:
      throw new Error(`unknown cmd ${JSON.stringify(m.cmd)}; want make | step | set | get_params | reset | close | status`);
  }
}

const progress = () => {
  if (!args.record) return;
  const live = Math.max(0, ...[...envs.values()].map((e) => e.maxX));
  fs.appendFileSync(rec("progress.jsonl"), JSON.stringify({ t: core.r4(Date.now() / 1000 - wallT0), best: core.r4(Math.max(best, live)), ticks: ticksTotal,
                                                           episodes: started }) + "\n");
};
setInterval(progress, 1000);

fs.rmSync(args.socket, { force: true });
const server = net.createServer((sock) => {
  let buf = "";
  const send = (x) => !sock.destroyed && sock.write(JSON.stringify(x) + "\n");
  sock.on("data", (d) => {
    buf += d;
    let i;
    while ((i = buf.indexOf("\n")) >= 0) {
      const line = buf.slice(0, i).trim();
      buf = buf.slice(i + 1);
      if (!line) continue;
      let m;
      try { m = JSON.parse(line); } catch (e) { send({ type: "error", msg: "bad json: " + e.message }); continue; }
      handle(m).then(send, (e) => send({ type: "error", cmd: m.cmd, ...(m.id !== undefined ? { id: m.id } : {}), ...(m.env !== undefined ? { env: m.env } : {}),
                                          msg: e.message, ...(e.events ? { events: e.events } : {}) }));
    }
  });
  sock.on("error", () => {});
});
server.listen(args.socket, () => { fs.chmodSync(args.socket, 0o777); process.stderr.write(`terrain-bench sim on ${args.socket}\n`); });

async function stop() {
  progress();
  await Promise.all([...envs.values()].map((env) => Promise.race([call(env, { cmd: "finish" }), new Promise((r) => setTimeout(r, 2000))])));
  fs.rmSync(args.socket, { force: true });
  process.exit(0);
}
process.on("SIGTERM", stop);
process.on("SIGINT", stop);
