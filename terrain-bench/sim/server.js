// Headless, real-time port of the "HTML5 Genetic Algorithm 2D Car Thingy" (cawro.js) by Rafael Matsunaga
// (http://rednuht.org/genetic_cars_2/; mirror read for this port: https://santex.github.io/singularity/seed/idx.html),
// itself loosely based on BoxCar2D (http://boxcar2d.com/). Terrain, car construction, physics constants, the death rule
// and the score are cawro's, run on the same box2d.js and seedrandom.js the page loads (vendor/). What is new here:
// one car instead of a population of 20, no genetic algorithm, and an agent that drives over a unix socket.
// The car keeps its params from one course to the next; cawro's score is replaced by distance alone.
//
//   node sim/server.js --socket /path/sim.sock [--record DIR] [--speed 1] [--max-s 180]
//
// --record writes DIR/episodes.jsonl (every episode), DIR/progress.jsonl (best distance so far, 1 Hz) and DIR/best.json
// (the furthest episode, every tick of every body, for replay).
//
// Protocol (newline-delimited JSON, both directions) is documented in PROTOCOL.md.
"use strict";
const fs = require("fs");
const net = require("net");
const path = require("path");
const vm = require("vm");
const crypto = require("crypto");

const VENDOR = path.join(__dirname, "..", "vendor");
vm.runInThisContext(fs.readFileSync(path.join(VENDOR, "box2d.js"), "utf8"));
vm.runInThisContext(fs.readFileSync(path.join(VENDOR, "seedrandom.js"), "utf8"));

// ---- cawro constants (cawro.js lines 4-77) ----
const FPS = 60;                    // box2dfps; world.Step(1/60, 20, 20)
const GRAVITY = new b2Vec2(0.0, -9.81);
const MAX_FLOOR_TILES = 200;
const GROUND_W = 1.5, GROUND_H = 0.15;
const CHASSIS_MIN = 0.1, CHASSIS_MAX = 1.1;   // vertex |coord| in [0.1, 1.2]
const WHEEL_R = [0.2, 0.7], WHEEL_D = [40, 140];
const MAX_HEALTH = FPS * 10;       // 10 s without +0.02 m of progress kills the car
const CAWRO_MOTOR_SPEED = 20;      // rad/s, fixed in cawro
// ---- this benchmark ----
const LOOKAHEAD_M = 2.0;           // terrain_ahead is sampled this far ahead of the chassis origin, in world +x
const MOTOR_SPEED_MAX = 30;        // rad/s
const TORQUE_SCALE_MAX = 2.0;      // x cawro's stall torque (car mass * g / wheel radius)
// Sign of each chassis vertex (x, y), as cawro_createRandomCar generates them; 0 means that coordinate is fixed at 0.
const VERTEX_SIGNS = [[1, 0], [1, 1], [0, 1], [-1, 1], [-1, 0], [-1, -1], [0, -1], [1, -1]];

const DEFAULT_PARAMS = {
  chassis: [[1.0, 0], [0.6, 0.4], [0, 0.5], [-0.6, 0.4], [-1.0, 0], [-0.6, -0.3], [0, -0.3], [0.6, -0.3]],
  wheel_vertex: [4, 0],
  wheel_radius: [0.4, 0.4],
  wheel_density: [70, 70],
  motor_speed: [CAWRO_MOTOR_SPEED, CAWRO_MOTOR_SPEED],
  torque_scale: [1.0, 1.0],
};

function parseArgs(argv) {
  const a = { socket: null, record: null, speed: 1, maxS: 180 };
  for (let i = 2; i < argv.length; i++) {
    const k = argv[i], v = argv[++i];
    if (k === "--socket") a.socket = v;
    else if (k === "--record") a.record = v;
    else if (k === "--speed") a.speed = parseFloat(v);
    else if (k === "--max-s") a.maxS = parseFloat(v);
    else throw new Error("unknown arg " + k);
  }
  if (!a.socket) throw new Error("--socket is required");
  return a;
}

// ---- terrain (cw_createFloor / cw_createFloorTile, verbatim geometry) ----
function rotate(coords, angle) {
  return coords.map((c) => ({ x: Math.cos(angle) * c.x - Math.sin(angle) * c.y, y: Math.sin(angle) * c.x + Math.cos(angle) * c.y }));
}

function createFloor(world, seed) {
  Math.seedrandom(seed);
  const tiles = [], surface = [];     // surface: top edge of every tile, [[x0,y0],[x1,y1]]
  let pos = { x: -5, y: 0 };
  for (let k = 0; k < MAX_FLOOR_TILES; k++) {
    const angle = (Math.random() * 3 - 1.5) * 1.5 * k / MAX_FLOOR_TILES;
    const bd = new b2BodyDef();
    bd.position.Set(pos.x, pos.y);
    const body = world.CreateBody(bd);
    const fd = new b2FixtureDef();
    fd.shape = new b2PolygonShape();
    fd.friction = 0.5;
    const local = rotate([{ x: 0, y: 0 }, { x: 0, y: -GROUND_H }, { x: GROUND_W, y: -GROUND_H }, { x: GROUND_W, y: 0 }], angle);
    fd.shape.SetAsArray(local);
    body.CreateFixture(fd);
    const end = { x: pos.x + local[3].x, y: pos.y + local[3].y };
    surface.push([[pos.x, pos.y], [end.x, end.y]]);
    tiles.push(body);
    pos = end;
  }
  Math.seedrandom(crypto.randomBytes(16).toString("hex"));   // cawro re-randomises after the floor (unseeded form needs a browser)
  const endX = Math.max(...surface.map((s) => Math.max(s[0][0], s[1][0])));
  return { tiles, surface, endX };
}

// Topmost terrain surface on the vertical line x (late tiles can tilt past vertical, so take the max). null past the course.
function terrainHeight(surface, x) {
  let best = null;
  for (const [[x0, y0], [x1, y1]] of surface) {
    const lo = Math.min(x0, x1), hi = Math.max(x0, x1);
    if (x < lo || x > hi) continue;
    const y = hi - lo < 1e-9 ? Math.max(y0, y1) : y0 + (y1 - y0) * (x - x0) / (x1 - x0);
    if (best === null || y > best) best = y;
  }
  return best;
}

// ---- parameters ----
function validate(p) {
  const err = (m) => { throw new Error(m); };
  const pair = (name, lo, hi) => {
    const v = p[name];
    if (!Array.isArray(v) || v.length !== 2 || v.some((x) => typeof x !== "number" || !isFinite(x))) err(`${name}: want [rear, front] numbers`);
    if (v.some((x) => x < lo || x > hi)) err(`${name}: each in [${lo}, ${hi}], got ${JSON.stringify(v)}`);
  };
  pair("wheel_radius", ...WHEEL_R);
  pair("wheel_density", ...WHEEL_D);
  pair("motor_speed", -MOTOR_SPEED_MAX, MOTOR_SPEED_MAX);
  pair("torque_scale", 0, TORQUE_SCALE_MAX);
  const wv = p.wheel_vertex;
  if (!Array.isArray(wv) || wv.length !== 2 || wv.some((i) => !Number.isInteger(i) || i < 0 || i > 7) || wv[0] === wv[1])
    err("wheel_vertex: two different integers in 0..7");
  const c = p.chassis;
  if (!Array.isArray(c) || c.length !== 8) err("chassis: want 8 [x, y] vertices");
  c.forEach((v, i) => {
    if (!Array.isArray(v) || v.length !== 2 || v.some((x) => typeof x !== "number" || !isFinite(x))) err(`chassis[${i}]: want [x, y]`);
    VERTEX_SIGNS[i].forEach((s, j) => {
      const x = v[j], m = Math.abs(x);
      if (s === 0 ? x !== 0 : Math.sign(x) !== s || m < CHASSIS_MIN - 1e-9 || m > CHASSIS_MIN + CHASSIS_MAX + 1e-9)
        err(`chassis[${i}][${j}] = ${x}: ${s === 0 ? "must be 0" : `sign ${s > 0 ? "+" : "-"}, magnitude in [${CHASSIS_MIN}, ${CHASSIS_MIN + CHASSIS_MAX}]`}`);
    });
  });
}

// ---- car (cw_Car, cw_createChassis, cw_createWheel) ----
class Car {
  constructor(world, params) {
    this.world = world;
    this.params = JSON.parse(JSON.stringify(params));
    const bd = new b2BodyDef();
    bd.type = b2Body.b2_dynamicBody;
    bd.position.Set(0.0, 4.0);
    this.chassis = world.CreateBody(bd);
    this.wheels = [0, 1].map(() => {
      const wd = new b2BodyDef();
      wd.type = b2Body.b2_dynamicBody;
      return world.CreateBody(wd);
    });
    this.joints = [null, null];
    this.buildChassis();
    [0, 1].forEach((i) => this.buildWheel(i));
    this.buildJoints(true);
  }

  buildChassis() {
    for (let f = this.chassis.GetFixtureList(); f;) { const n = f.GetNext(); this.chassis.DestroyFixture(f); f = n; }
    const v = this.params.chassis.map(([x, y]) => new b2Vec2(x, y));
    for (let i = 0; i < 8; i++) {
      const fd = new b2FixtureDef();
      fd.shape = new b2PolygonShape();
      fd.density = 80; fd.friction = 10; fd.restitution = 0.2; fd.filter.groupIndex = -1;
      fd.shape.SetAsArray([v[i], v[(i + 1) % 8], b2Vec2.Make(0, 0)], 3);
      this.chassis.CreateFixture(fd);
    }
  }

  buildWheel(i) {
    const w = this.wheels[i];
    for (let f = w.GetFixtureList(); f;) { const n = f.GetNext(); w.DestroyFixture(f); f = n; }
    const fd = new b2FixtureDef();
    fd.shape = new b2CircleShape(this.params.wheel_radius[i]);
    fd.density = this.params.wheel_density[i]; fd.friction = 1; fd.restitution = 0.2; fd.filter.groupIndex = -1;
    w.CreateFixture(fd);
  }

  // Revolute joints at the chosen chassis vertices. On a rebuild the wheels are moved onto their new anchors, keeping velocity.
  buildJoints(place) {
    this.joints.forEach((j) => j && this.world.DestroyJoint(j));
    [0, 1].forEach((i) => {
      const [ax, ay] = this.params.chassis[this.params.wheel_vertex[i]];
      if (place) this.wheels[i].SetPosition(this.chassis.GetWorldPoint(new b2Vec2(ax, ay)));
      const jd = new b2RevoluteJointDef();
      jd.localAnchorA.Set(ax, ay);
      jd.localAnchorB.Set(0, 0);
      jd.enableMotor = true;
      jd.bodyA = this.chassis;
      jd.bodyB = this.wheels[i];
      this.joints[i] = this.world.CreateJoint(jd);
    });
    this.applyMotors();
  }

  // cawro: maxMotorTorque = car mass * g / wheel radius; motorSpeed = -20 (negative turns the wheel clockwise, i.e. drives +x).
  applyMotors() {
    const mass = this.chassis.GetMass() + this.wheels[0].GetMass() + this.wheels[1].GetMass();
    [0, 1].forEach((i) => {
      this.joints[i].SetMaxMotorTorque(this.params.torque_scale[i] * mass * -GRAVITY.y / this.params.wheel_radius[i]);
      this.joints[i].SetMotorSpeed(-this.params.motor_speed[i]);
    });
    [this.chassis, ...this.wheels].forEach((b) => b.SetAwake(true));
  }

  set(update) {
    const next = { ...this.params, ...JSON.parse(JSON.stringify(update)) };
    for (const k of Object.keys(update)) if (!(k in DEFAULT_PARAMS)) throw new Error(`unknown param ${k}`);
    validate(next);
    const changed = (k) => k in update && JSON.stringify(update[k]) !== JSON.stringify(this.params[k]);
    const geometry = changed("chassis") || changed("wheel_vertex");
    const wheels = [0, 1].filter((i) => (changed("wheel_radius") && update.wheel_radius[i] !== this.params.wheel_radius[i]) ||
                                        (changed("wheel_density") && update.wheel_density[i] !== this.params.wheel_density[i]));
    this.params = next;
    if (changed("chassis")) this.buildChassis();
    wheels.forEach((i) => this.buildWheel(i));
    if (geometry) this.buildJoints(true);
    else this.applyMotors();
  }

  destroy() {
    [this.chassis, ...this.wheels].forEach((b) => this.world.DestroyBody(b));
  }
}

// ---- episode (one car on one course; cw_Car.checkDeath) ----
const r4 = (x) => Math.round(x * 1e4) / 1e4;

class Episode {
  constructor(index, seed, params) {
    this.index = index; this.seed = seed; this.wallStart = Date.now() / 1000;
    this.world = new b2World(GRAVITY, true);
    this.floor = createFloor(this.world, seed);
    this.car = new Car(this.world, params);
    this.frames = 0; this.health = MAX_HEALTH; this.maxX = 0;
    this.track = [];                                   // per tick: chassis x,y,angle, wheel0 x,y,angle, wheel1 x,y,angle
    this.changes = [[0, this.car.params]];             // [tick, full params] whenever params change
  }

  step() {
    this.world.Step(1 / FPS, 20, 20);
    this.frames++;
    const p = this.car.chassis.GetPosition();
    if (p.x > this.maxX + 0.02) { this.health = MAX_HEALTH; this.maxX = p.x; }
    else {
      if (p.x > this.maxX) this.maxX = p.x;
      if (Math.abs(this.car.chassis.GetLinearVelocity().x) < 0.001) this.health -= 5;
      this.health--;
    }
    this.track.push([this.car.chassis, ...this.car.wheels].flatMap((b) => [r4(b.GetPosition().x), r4(b.GetPosition().y), r4(b.GetAngle())]));
  }

  obs() {
    const c = this.car.chassis, p = c.GetPosition(), v = c.GetLinearVelocity();
    const h = terrainHeight(this.floor.surface, p.x + LOOKAHEAD_M);
    return { type: "obs", episode: this.index, tick: this.frames, t: +(this.frames / FPS).toFixed(4),
             pos: [p.x, p.y], rot: c.GetAngle(), vel: [v.x, v.y], angvel: c.GetAngularVelocity(), terrain_ahead: h };
  }

  result(reason) {
    return { type: "episode_end", episode: this.index, seed: this.seed, reason, distance: this.maxX, sim_s: this.frames / FPS,
             wall_start: this.wallStart, wall_end: Date.now() / 1000, params: this.car.params };
  }

  recording(reason) {
    return { ...this.result(reason), fps: FPS, surface: this.floor.surface, changes: this.changes, track: this.track };
  }
}

function main() {
  const args = parseArgs(process.argv);
  const clients = new Set();
  const wallT0 = Date.now() / 1000;
  let index = 0, ep = null, queue = [], best = 0, params = DEFAULT_PARAMS;
  const rec = (name) => path.join(args.record, name);
  if (args.record) fs.mkdirSync(args.record, { recursive: true });

  const send = (sock, msg) => {
    if (sock.destroyed) return;
    if (msg.type === "obs" && sock.writableLength > 1 << 20) return;   // slow reader: drop obs rather than slow down
    sock.write(JSON.stringify(msg) + "\n");
  };
  const broadcast = (msg) => clients.forEach((s) => send(s, msg));
  const startMsg = () => ({ type: "episode_start", episode: ep.index, course_end_x: ep.floor.endX, params: ep.car.params, best_distance: best });
  const start = () => { ep = new Episode(index, crypto.randomBytes(4).toString("hex"), params); broadcast(startMsg()); };
  const finish = (reason, restart = true) => {
    const r = ep.result(reason);
    if (args.record) {
      fs.appendFileSync(rec("episodes.jsonl"), JSON.stringify(r) + "\n");
      if (ep.maxX > best) fs.writeFileSync(rec("best.json"), JSON.stringify(ep.recording(reason)));
    }
    best = Math.max(best, ep.maxX);
    params = ep.car.params;            // the car keeps its params on the next course
    broadcast({ ...r, best_distance: best });
    index++;
    if (restart) start();
  };
  const progress = () => {
    if (args.record && ep) fs.appendFileSync(rec("progress.jsonl"),
      JSON.stringify({ t: r4(Date.now() / 1000 - wallT0), best: r4(Math.max(best, ep.maxX)), episode: ep.index }) + "\n");
  };

  const handle = (sock, line) => {
    let msg;
    try { msg = JSON.parse(line); } catch (e) { return send(sock, { type: "error", msg: "bad json: " + e.message }); }
    queue.push([sock, msg]);   // applied between physics steps
  };
  const apply = () => {
    const q = queue; queue = [];
    for (const [sock, msg] of q) {
      try {
        if (msg.cmd === "set") {
          ep.car.set(msg.params || {});
          ep.changes.push([ep.frames, ep.car.params]);
          send(sock, { type: "ok", cmd: "set", tick: ep.frames, params: ep.car.params });
        } else if (msg.cmd === "get_params") send(sock, { type: "ok", cmd: "get_params", params: ep.car.params });
        else if (msg.cmd === "reset") { send(sock, { type: "ok", cmd: "reset" }); finish("reset"); }
        else throw new Error(`unknown cmd ${JSON.stringify(msg.cmd)}; want set | get_params | reset`);
      } catch (e) { send(sock, { type: "error", cmd: msg.cmd, msg: e.message }); }
    }
  };

  fs.rmSync(args.socket, { force: true });
  const server = net.createServer((sock) => {
    clients.add(sock);
    let buf = "";
    sock.on("data", (d) => {
      buf += d;
      let i;
      while ((i = buf.indexOf("\n")) >= 0) { const line = buf.slice(0, i).trim(); buf = buf.slice(i + 1); if (line) handle(sock, line); }
    });
    sock.on("error", () => {});
    sock.on("close", () => clients.delete(sock));
    send(sock, startMsg());
  });
  server.listen(args.socket, () => { fs.chmodSync(args.socket, 0o777); process.stderr.write(`terrain-bench sim on ${args.socket}\n`); });

  start();
  setInterval(progress, 1000);
  const dt = 1000 / FPS / args.speed;
  const t0 = performance.now();
  let ticks = 0;
  const loop = () => {
    const owed = Math.floor((performance.now() - t0) / dt) - ticks;
    for (let n = 0; n < Math.min(owed, 10); n++, ticks++) {   // at most 10 catch-up steps per wake; never faster than real time x speed
      apply();
      ep.step();
      broadcast(ep.obs());
      if (ep.health <= 0) finish("stalled");
      else if (ep.maxX >= ep.floor.endX) finish("finished");
      else if (ep.frames >= args.maxS * FPS) finish("timeout");
    }
    if (owed > 10) ticks += owed - 10;   // fell behind: drop the backlog rather than burst
    setTimeout(loop, Math.max(0, t0 + (ticks + 1) * dt - performance.now()));
  };
  loop();
  const stop = () => { progress(); finish("run_end", false); fs.rmSync(args.socket, { force: true }); process.exit(0); };
  process.on("SIGTERM", stop);
  process.on("SIGINT", stop);
}

main();
