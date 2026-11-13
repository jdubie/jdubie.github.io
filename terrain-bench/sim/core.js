// Physics core shared by the simulator server and the batch evaluator: a headless port of the "HTML5 Genetic Algorithm 2D
// Car Thingy" (cawro.js) by Rafael Matsunaga (http://rednuht.org/genetic_cars_2/; mirror read for this port:
// https://santex.github.io/singularity/seed/idx.html), itself loosely based on BoxCar2D (http://boxcar2d.com/). Terrain,
// car construction, physics constants and the stall rule are cawro's, on the same box2d.js and seedrandom.js the page
// loads (vendor/). New: one car, no genetic algorithm, every parameter changeable mid-run, and the terrain's difficulty
// ramp is configurable (TERRAINS; "cawro" is the original).
"use strict";
const fs = require("fs");
const path = require("path");
const vm = require("vm");
const crypto = require("crypto");

const VENDOR = path.join(__dirname, "..", "vendor");
vm.runInThisContext(fs.readFileSync(path.join(VENDOR, "box2d.js"), "utf8"));
vm.runInThisContext(fs.readFileSync(path.join(VENDOR, "seedrandom.js"), "utf8"));

// ---- cawro constants (cawro.js lines 4-77) ----
const FPS = 60;                    // box2dfps; world.Step(1/60, 20, 20)
const GRAVITY = new b2Vec2(0.0, -9.81);
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

// ---- terrain (cw_createFloor / cw_createFloorTile, verbatim geometry) ----
function rotate(coords, angle) {
  return coords.map((c) => ({ x: Math.cos(angle) * c.x - Math.sin(angle) * c.y, y: Math.sin(angle) * c.x + Math.cos(angle) * c.y }));
}

// Course shapes. Tile k's slope is uniform in +-amp(k), amp(k) = min_angle + (max_angle - min_angle) * min(1, k / ramp_tiles) ^ power, one
// Math.random() per tile as cawro draws it, so the "cawro" preset reproduces the original page's courses seed for seed.
const TERRAINS = {
  cawro: { tiles: 200, ramp_tiles: 200, power: 1, max_angle: 2.25 },
  // 3 km, ramping 10x slower than cawro's, and a 150 kJ battery per episode
  // 3 km; every tile at distance x about equally steep (+-30% jitter, random sign), steepness growing as sqrt(x) to
  // 0.9 rad at the end; the car crashes if it tips past 90 degrees
  v3: { tiles: 2000, ramp_tiles: 2000, power: 0.5, max_angle: 0.9, jitter: 0.3, crash_angle: 1.57 },
  // v3, but already 0.45 rad steep at the start and rising linearly to 1.0 rad at 3 km, so that only a well-designed,
  // well-driven car gets far, and course luck cannot carry a weak one
  v4: { tiles: 2000, ramp_tiles: 2000, power: 1, min_angle: 0.45, max_angle: 1.0, jitter: 0.3, crash_angle: 1.57 },
  // v4's courses with the "free" design space (FreeCar): any chassis, 1-4 wheels with suspension, sliding ballast
  // sum-of-sines ground (sineTiles) with the free design space
  sines: { shape: "sines", length: 3000, tile_w: 0.75, waves: 6, wavelength: [4, 120], slope: [0.05, 0.25], gain: [1.8, 4.5],
           crash_angle: 1.57, design: "free" },
  v4free: { tiles: 2000, ramp_tiles: 2000, power: 1, min_angle: 0.45, max_angle: 1.0, jitter: 0.3, crash_angle: 1.57, design: "free" },
};

// jitter (optional, 0..1): instead of uniform in +-amp, the slope is amp * (1 - jitter * v) with a random sign, so every
// tile at a given distance is about equally steep and difficulty depends on distance, not on luck.
// max_run (optional): at most this many tiles in a row tilt the same way, so no course has a long climb that others lack.
let runSign = 0, runLen = 0, wallAt = 0, wallLeft = 0;
function tileAngle(t, k) {
  if (k === 0) { runSign = 0; runLen = 0; wallLeft = 0; if (t.walls) wallAt = t.walls.every[0] + Math.floor(Math.random() * (t.walls.every[1] - t.walls.every[0])); }
  // walls (optional): every `every` tiles (uniform in the range), `len` tiles in a row climb at an angle growing linearly
  // from angle0 to angle1 over the ramp: obstacles that need a run-up, between stretches of rough ground
  if (t.walls && (wallLeft > 0 || k >= wallAt)) {
    if (wallLeft === 0) wallLeft = t.walls.len;
    if (--wallLeft === 0) wallAt = k + t.walls.every[0] + Math.floor(Math.random() * (t.walls.every[1] - t.walls.every[0]));
    const f = Math.min(1, k / t.ramp_tiles);
    return t.walls.angle0 + (t.walls.angle1 - t.walls.angle0) * f;
  }
  const a0 = t.min_angle || 0;   // steepness at the start of the course
  const amp = a0 + (t.max_angle - a0) * Math.pow(Math.min(1, k / t.ramp_tiles), t.power);
  const u = Math.random();
  if (t.jitter === undefined) return (u * 2 - 1) * amp;
  let sign = u < 0.5 ? -1 : 1;
  // persist (optional, 0..1): chance a tile keeps the previous tile's tilt, which turns zigzags into long hills
  if (t.persist && runSign && Math.random() < t.persist) sign = runSign;
  if (t.max_run && sign === runSign && runLen >= t.max_run) sign = -sign;
  runLen = sign === runSign ? runLen + 1 : 1;
  runSign = sign;
  return sign * amp * (1 - t.jitter * ((u * 2) % 1));
}

// shape "sines": the ground follows a sum of `waves` sine waves, each with a wavelength log-uniform in `wavelength` (m), a
// steepest slope uniform in `slope` and a random phase, all drawn from the course seed. Its height is scaled by a gain
// growing linearly from gain[0] at the start to gain[1] at `length` m, and eased in over the first 10 m. Tiles are
// `tile_w` m wide, straight segments between points on the curve.
function sineTiles(t) {
  const U = (a, b) => a + (b - a) * Math.random();
  const waves = [];
  for (let i = 0; i < t.waves; i++) {
    const lam = Math.exp(U(Math.log(t.wavelength[0]), Math.log(t.wavelength[1])));
    waves.push({ k: 2 * Math.PI / lam, a: U(t.slope[0], t.slope[1]) * lam / (2 * Math.PI), ph: U(0, 2 * Math.PI) });
  }
  const raw = (x) => waves.reduce((sum, w) => sum + w.a * Math.sin(w.k * x + w.ph), 0);
  const y = (x) => {
    const gain = t.gain[0] + (t.gain[1] - t.gain[0]) * Math.min(1, Math.max(0, x) / t.length);
    return gain * Math.min(1, Math.max(0, x + 5) / 10) * (raw(x) - raw(-5));
  };
  const pts = [];
  for (let x = -5; x <= t.length; x += t.tile_w) pts.push([x, y(x)]);
  return pts;
}

function createFloor(world, seed, t) {
  Math.seedrandom(seed);
  const tiles = [], surface = [];     // surface: top edge of every tile, [[x0,y0],[x1,y1]]
  if (t.shape === "sines") {
    const pts = sineTiles(t);
    for (let k = 0; k + 1 < pts.length; k++) {
      const [[x0, y0], [x1, y1]] = [pts[k], pts[k + 1]];
      const angle = Math.atan2(y1 - y0, x1 - x0), len = Math.hypot(x1 - x0, y1 - y0);
      const bd = new b2BodyDef();
      bd.position.Set(x0, y0);
      const body = world.CreateBody(bd);
      const fd = new b2FixtureDef();
      fd.shape = new b2PolygonShape();
      fd.friction = 0.5;
      fd.shape.SetAsArray(rotate([{ x: 0, y: 0 }, { x: 0, y: -GROUND_H }, { x: len, y: -GROUND_H }, { x: len, y: 0 }], angle));
      body.CreateFixture(fd);
      surface.push([[x0, y0], [x1, y1]]);
      tiles.push(body);
    }
    Math.seedrandom(crypto.randomBytes(16).toString("hex"));
    return { tiles, surface, endX: pts[pts.length - 1][0], xs: surface.map((sg) => sg[0][0]) };
  }
  let pos = { x: -5, y: 0 };
  for (let k = 0; k < t.tiles; k++) {
    const angle = tileAngle(t, k);
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
function terrainHeight(surface, x, xs) {
  if (xs) {   // a course that never folds back: binary search on the tiles' start x
    let lo = 0, hi = xs.length - 1;
    if (x < xs[0] || x > surface[hi][1][0]) return null;
    while (lo < hi) { const mid = (lo + hi + 1) >> 1; if (xs[mid] <= x) lo = mid; else hi = mid - 1; }
    const [[x0, y0], [x1, y1]] = surface[lo];
    return y0 + (y1 - y0) * (x - x0) / (x1 - x0);
  }
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
      const scale = this.flat ? 0 : this.params.torque_scale[i];   // a flat battery leaves the wheels free-spinning
      this.joints[i].SetMaxMotorTorque(scale * mass * -GRAVITY.y / this.params.wheel_radius[i]);
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


// ---- the "free" design space: any star-shaped chassis, 1-4 wheels with optional suspension, a sliding ballast ----
const FREE = {
  vertices: [3, 16], radius: [0.1, 1.5], min_triangle: 0.005, chassis_density: [20, 200], wheels: [1, 4],
  wheel_radius: [0.1, 0.9], wheel_density: [20, 200], friction: [0.1, 2], motor_speed: [-40, 40], torque_scale: [0, 3],
  travel: [0.02, 0.6], hz: [0.5, 15], damping: [0, 1], ballast_mass: [0, 200], ballast_speed: 2.0,
};
const FREE_DEFAULT = {
  chassis: DEFAULT_PARAMS.chassis, chassis_density: 80,
  wheels: [
    { vertex: 4, radius: 0.4, density: 70, friction: 1, motor_speed: 20, torque_scale: 1, suspension: null },
    { vertex: 0, radius: 0.4, density: 70, friction: 1, motor_speed: 20, torque_scale: 1, suspension: null },
  ],
  ballast: { mass: 0, pos: [0, 0] },
};

function validateFree(p) {
  const err = (m) => { throw new Error(m); };
  const num = (x, what) => (typeof x === "number" && isFinite(x)) || err(`${what}: want a number, got ${JSON.stringify(x)}`);
  const range = (x, [lo, hi], what) => { num(x, what); if (x < lo || x > hi) err(`${what} = ${x}: want ${lo} .. ${hi}`); };
  const c = p.chassis;
  if (!Array.isArray(c) || c.length < FREE.vertices[0] || c.length > FREE.vertices[1]) err(`chassis: want ${FREE.vertices[0]}-${FREE.vertices[1]} [x, y] vertices`);
  c.forEach((v, i) => { if (!Array.isArray(v) || v.length !== 2) err(`chassis[${i}]: want [x, y]`); num(v[0], `chassis[${i}][0]`); num(v[1], `chassis[${i}][1]`);
    range(Math.hypot(v[0], v[1]), FREE.radius, `chassis[${i}] distance from the origin`); });
  for (let i = 0; i < c.length; i++) {   // counter-clockwise around the origin, every fan triangle a real triangle
    const a = c[i], b = c[(i + 1) % c.length];
    const cross = a[0] * b[1] - a[1] * b[0];
    if (cross / 2 < FREE.min_triangle) err(`chassis: vertices ${i} and ${(i + 1) % c.length} must go counter-clockwise around the origin, ` +
      `less than 180 degrees apart, making a triangle with the origin of at least ${FREE.min_triangle} m^2 (got ${(cross / 2).toFixed(4)})`);
  }
  let turn = 0;
  for (let i = 0; i < c.length; i++) { const a = c[i], b = c[(i + 1) % c.length]; turn += Math.atan2(a[0] * b[1] - a[1] * b[0], a[0] * b[0] + a[1] * b[1]); }
  if (Math.abs(turn - 2 * Math.PI) > 1e-6) err("chassis: the vertices must wind once around the origin");
  range(p.chassis_density, FREE.chassis_density, "chassis_density");
  const w = p.wheels;
  if (!Array.isArray(w) || w.length < FREE.wheels[0] || w.length > FREE.wheels[1]) err(`wheels: want a list of ${FREE.wheels[0]}-${FREE.wheels[1]} wheels`);
  const used = new Set();
  w.forEach((wh, i) => {
    const at = `wheels[${i}]`;
    if (!Number.isInteger(wh.vertex) || wh.vertex < 0 || wh.vertex >= c.length) err(`${at}.vertex: an index into chassis, 0..${c.length - 1}`);
    if (used.has(wh.vertex)) err(`${at}.vertex ${wh.vertex}: another wheel is already there`);
    used.add(wh.vertex);
    range(wh.radius, FREE.wheel_radius, `${at}.radius`); range(wh.density, FREE.wheel_density, `${at}.density`);
    range(wh.friction, FREE.friction, `${at}.friction`); range(wh.motor_speed, FREE.motor_speed, `${at}.motor_speed`);
    range(wh.torque_scale, FREE.torque_scale, `${at}.torque_scale`);
    const s = wh.suspension;
    if (s !== null && s !== undefined) {
      if (!Array.isArray(s.axis) || s.axis.length !== 2 || Math.hypot(s.axis[0], s.axis[1]) < 1e-3) err(`${at}.suspension.axis: a non-zero [x, y] direction`);
      range(s.travel, FREE.travel, `${at}.suspension.travel`); range(s.hz, FREE.hz, `${at}.suspension.hz`); range(s.damping, FREE.damping, `${at}.suspension.damping`);
    }
  });
  const bl = p.ballast;
  if (!bl || !Array.isArray(bl.pos) || bl.pos.length !== 2) err("ballast: want {mass, pos: [x, y]}");
  range(bl.mass, FREE.ballast_mass, "ballast.mass");
  num(bl.pos[0], "ballast.pos[0]"); num(bl.pos[1], "ballast.pos[1]");
  if (Math.hypot(...bl.pos) > FREE.radius[1]) err(`ballast.pos: at most ${FREE.radius[1]} m from the chassis origin`);
}

class FreeCar {
  constructor(world, params) {
    this.world = world;
    this.params = JSON.parse(JSON.stringify(params));
    validateFree(this.params);
    const bd = new b2BodyDef();
    bd.type = b2Body.b2_dynamicBody;
    bd.position.Set(0.0, 4.0);
    this.chassis = world.CreateBody(bd);
    this.wheels = []; this.axles = []; this.joints = []; this.extraJoints = [];
    this.ballastPos = this.params.ballast.pos.slice(); this.ballastFixture = null;
    this.build();
  }

  fixture(body, shape, density, friction, collide) {
    const fd = new b2FixtureDef();
    fd.shape = shape; fd.density = density; fd.friction = friction; fd.restitution = 0.2; fd.filter.groupIndex = -1;
    if (!collide) { fd.isSensor = true; fd.filter.maskBits = 0; }
    return body.CreateFixture(fd);
  }

  // (Re)build everything except the chassis body itself, keeping its pose and velocity; wheels start on their axles.
  build() {
    const w = this.world, ch = this.chassis, p = this.params;
    this.joints.concat(this.extraJoints).forEach((j) => w.DestroyJoint(j));
    const spins = this.wheels.map((b) => b.GetAngularVelocity());
    this.wheels.concat(this.axles).forEach((b) => w.DestroyBody(b));
    this.wheels = []; this.axles = []; this.joints = []; this.extraJoints = [];
    for (let f = ch.GetFixtureList(); f;) { const n = f.GetNext(); ch.DestroyFixture(f); f = n; }
    this.ballastFixture = null;
    const v = p.chassis.map(([x, y]) => new b2Vec2(x, y));
    for (let i = 0; i < v.length; i++) {
      const s = new b2PolygonShape();
      s.SetAsArray([v[i], v[(i + 1) % v.length], b2Vec2.Make(0, 0)], 3);
      this.fixture(ch, s, p.chassis_density, 10, true);
    }
    this.placeBallast();
    const vel = ch.GetLinearVelocity();
    p.wheels.forEach((wh, i) => {
      const [ax, ay] = p.chassis[wh.vertex];
      const at = ch.GetWorldPoint(new b2Vec2(ax, ay));
      const mk = () => { const d = new b2BodyDef(); d.type = b2Body.b2_dynamicBody; d.position.Set(at.x, at.y); const b = w.CreateBody(d); b.SetLinearVelocity(vel.Copy()); return b; };
      const wheel = mk();
      this.fixture(wheel, new b2CircleShape(wh.radius), wh.density, wh.friction, true);
      if (spins[i] !== undefined) wheel.SetAngularVelocity(spins[i]);
      let hub = ch, hubAnchor = new b2Vec2(ax, ay);
      if (wh.suspension) {   // the wheel turns on an axle that slides along `axis`, held by a spring
        const s = wh.suspension, n = Math.hypot(...s.axis), dir = new b2Vec2(s.axis[0] / n, s.axis[1] / n);
        const axle = mk();
        this.fixture(axle, new b2CircleShape(0.05), 20, 0, false);
        this.axles.push(axle);
        const pj = new b2PrismaticJointDef();
        pj.bodyA = ch; pj.bodyB = axle; pj.localAnchorA.Set(ax, ay); pj.localAnchorB.Set(0, 0);
        pj.localAxisA.Set(dir.x, dir.y); pj.enableLimit = true; pj.lowerTranslation = -s.travel; pj.upperTranslation = s.travel;
        this.extraJoints.push(w.CreateJoint(pj));
        const dj = new b2DistanceJointDef();
        dj.bodyA = ch; dj.bodyB = axle; dj.localAnchorA.Set(ax + dir.x, ay + dir.y); dj.localAnchorB.Set(0, 0);
        dj.length = 1; dj.frequencyHz = s.hz; dj.dampingRatio = s.damping;
        this.extraJoints.push(w.CreateJoint(dj));
        hub = axle; hubAnchor = new b2Vec2(0, 0);
      }
      const rj = new b2RevoluteJointDef();
      rj.bodyA = hub; rj.bodyB = wheel; rj.localAnchorA.Set(hubAnchor.x, hubAnchor.y); rj.localAnchorB.Set(0, 0); rj.enableMotor = true;
      this.joints.push(w.CreateJoint(rj));
      this.wheels.push(wheel);
    });
    this.applyMotors();
  }

  placeBallast() {
    if (this.ballastFixture) this.chassis.DestroyFixture(this.ballastFixture);
    this.ballastFixture = null;
    const m = this.params.ballast.mass;
    if (m > 0) {
      const s = new b2CircleShape(0.1);
      s.SetLocalPosition(new b2Vec2(this.ballastPos[0], this.ballastPos[1]));
      this.ballastFixture = this.fixture(this.chassis, s, m / (Math.PI * 0.01), 0, false);
    }
    this.chassis.ResetMassData();
  }

  // Per tick: the ballast slides toward its target at most ballast_speed m/s.
  tick() {
    const t = this.params.ballast.pos, b = this.ballastPos, dx = t[0] - b[0], dy = t[1] - b[1], d = Math.hypot(dx, dy);
    if (d < 1e-9 || this.params.ballast.mass === 0) { this.ballastPos = t.slice(); return; }
    const k = Math.min(1, FREE.ballast_speed / FPS / d);
    this.ballastPos = [b[0] + dx * k, b[1] + dy * k];
    this.placeBallast();
  }

  mass() { return this.chassis.GetMass() + this.wheels.concat(this.axles).reduce((s, b) => s + b.GetMass(), 0); }

  applyMotors() {
    const mass = this.mass();
    this.params.wheels.forEach((wh, i) => {
      this.joints[i].SetMaxMotorTorque(this.flat ? 0 : wh.torque_scale * mass * -GRAVITY.y / wh.radius);
      this.joints[i].SetMotorSpeed(-wh.motor_speed);
    });
    [this.chassis, ...this.wheels, ...this.axles].forEach((b) => b.SetAwake(true));
  }

  // update: any subset of the params; a `wheels` list replaces the whole list (send every wheel).
  set(update) {
    for (const k of Object.keys(update)) if (!(k in FREE_DEFAULT)) throw new Error(`unknown param ${k}; want ${Object.keys(FREE_DEFAULT).join(", ")}`);
    const next = { ...this.params, ...JSON.parse(JSON.stringify(update)) };
    if (update.ballast) next.ballast = { ...this.params.ballast, ...update.ballast };
    if (next.wheels) next.wheels = next.wheels.map((w) => ({ suspension: null, ...w }));
    validateFree(next);
    const structural = (a, b) => JSON.stringify([a.chassis, a.chassis_density, a.wheels.map((w) => [w.vertex, w.radius, w.density, w.friction, w.suspension])]) !==
                                 JSON.stringify([b.chassis, b.chassis_density, b.wheels.map((w) => [w.vertex, w.radius, w.density, w.friction, w.suspension])]);
    const rebuild = structural(next, this.params);
    const massChange = next.ballast.mass !== this.params.ballast.mass;
    this.params = next;
    if (rebuild) this.build();
    else { if (massChange) this.placeBallast(); this.applyMotors(); }
  }
}

// ---- episode (one car on one course; cw_Car.checkDeath) ----
const r4 = (x) => Math.round(x * 1e4) / 1e4;

class Episode {
  // terrain.energy: battery per episode in joules (absent = unlimited). Motor work is |motor impulse x relative wheel
  // speed| per wheel per tick, so braking against a spinning wheel costs as much as driving it.
  constructor(index, seed, params, terrain, record = true) {
    this.index = index; this.seed = seed; this.wallStart = Date.now() / 1000; this.record = record;
    this.energy = terrain.energy || null; this.used = 0;
    this.crashAngle = terrain.crash_angle || null;
    this.world = new b2World(GRAVITY, true);
    this.floor = createFloor(this.world, seed, terrain);
    this.design = terrain.design || "cawro";
    this.car = this.design === "free" ? new FreeCar(this.world, params) : new Car(this.world, params);
    this.frames = 0; this.health = MAX_HEALTH; this.maxX = 0;
    this.track = [];                                   // per tick: chassis x,y,angle, wheel0 x,y,angle, wheel1 x,y,angle
    this.changes = [[0, this.car.params]];             // [tick, full params] whenever params change
  }

  step() {
    this.world.Step(1 / FPS, 20, 20);
    this.frames++;
    const car = this.car, wc = car.chassis.GetAngularVelocity();
    if (car.tick) car.tick();
    for (let i = 0; i < car.joints.length; i++) this.used += Math.abs(car.joints[i].m_motorImpulse * (car.wheels[i].GetAngularVelocity() - wc));
    if (this.energy !== null && this.used >= this.energy && !car.flat) { car.flat = true; car.applyMotors(); }
    const p = this.car.chassis.GetPosition();
    if (p.x > this.maxX + 0.02) { this.health = MAX_HEALTH; this.maxX = p.x; }
    else {
      if (p.x > this.maxX) this.maxX = p.x;
      if (Math.abs(this.car.chassis.GetLinearVelocity().x) < 0.001) this.health -= 5;
      this.health--;
    }
    if (this.record) {   // chassis x, y, angle; each wheel x, y, angle; for a free car, then the ballast's chassis-frame x, y
      const row = [this.car.chassis, ...this.car.wheels].flatMap((b) => [r4(b.GetPosition().x), r4(b.GetPosition().y), r4(b.GetAngle())]);
      if (this.car.ballastPos) row.push(r4(this.car.ballastPos[0]), r4(this.car.ballastPos[1]));
      this.track.push(row);
    }
  }

  obs() {
    const c = this.car.chassis, p = c.GetPosition(), v = c.GetLinearVelocity();
    const h = terrainHeight(this.floor.surface, p.x + LOOKAHEAD_M, this.floor.xs);
    return { type: "obs", episode: this.index, tick: this.frames, t: +(this.frames / FPS).toFixed(4),
             pos: [p.x, p.y], rot: c.GetAngle(), vel: [v.x, v.y], angvel: c.GetAngularVelocity(), terrain_ahead: h,
             ...(this.energy !== null ? { energy_left: Math.max(0, this.energy - this.used) } : {}) };
  }

  result(reason) {
    return { type: "episode_end", design: this.design, episode: this.index, seed: this.seed, reason, distance: this.maxX, sim_s: this.frames / FPS, energy_used: this.used,
             wall_start: this.wallStart, wall_end: Date.now() / 1000, params: this.car.params };
  }

  // Why this episode is over, or null. cawro's stall rule, plus the end of the course and an episode cap.
  over(maxS) {
    if (this.health <= 0) return "stalled";
    if (this.crashAngle !== null) {   // terrain.crash_angle: the chassis tipping past this from upright ends the episode
      const a = Math.atan2(Math.sin(this.car.chassis.GetAngle()), Math.cos(this.car.chassis.GetAngle()));
      if (Math.abs(a) > this.crashAngle) return "crashed";
    }
    if (this.maxX >= this.floor.endX) return "finished";
    if (this.frames >= maxS * FPS) return "timeout";
    return null;
  }

  recording(reason) {
    return { ...this.result(reason), fps: FPS, surface: this.floor.surface, changes: this.changes, track: this.track };
  }
}


const defaultParams = (terrain) => (terrain.design === "free" ? FREE_DEFAULT : DEFAULT_PARAMS);

module.exports = { FPS, DEFAULT_PARAMS, FREE_DEFAULT, FREE, TERRAINS, Episode, terrainHeight, validate, validateFree, defaultParams, r4 };
