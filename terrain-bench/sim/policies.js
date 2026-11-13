// Scripted closed-loop drivers for calibration baselines. Each factory returns (obs, params) -> params update | null.
"use strict";
const MAX = { motor_speed: [30, 30], torque_scale: [2, 2], wheel_radius: [0.6, 0.6] };
const wrap = (a) => Math.atan2(Math.sin(a), Math.cos(a));
const POLICIES = {
  // Opus 5.5's controller from pilot3 (runs/pilot3/opus/work/scripts/ctl3.py), ported: pitch control with a dead-band,
  // plus a stuck state machine (back up, then charge). Acts every 3 ticks, as Opus stepped it.
  opus_p3: (g) => {
    let best = -1e9, tbest = 0, mode = "run", tm = 0, k = 0, last = null;
    return (o) => {
      if (k++ % 3) return null;
      const t = o.t, x = o.pos[0];
      if (x > best + 0.05) { best = x; tbest = t; }
      if (mode === "run" && t - tbest > g.st) { mode = "back"; tm = t; }
      else if (mode === "back" && t - tm > g.bt) { mode = "charge"; tm = t; }
      else if (mode === "charge" && (t - tm > 6 || x > best + 0.5)) { mode = "run"; tbest = t; }
      const v = { run: g.vmax, back: g.bv, charge: g.cv }[mode];
      const rot = o.rot, w = o.angvel;
      const e = rot > g.thr ? rot - g.thr : rot < -g.thr ? rot + g.thr : 0;
      const sp = Math.max(-30, Math.min(30, v - Math.abs(v) * (g.kp * e + g.kd * w)));
      if (sp === last) return null;
      last = sp;
      return { motor_speed: [sp, sp] };
    };
  },
  // tunable feedback driver: speed = base - kp*pitch - kd*angvel - kdrop*drop_ahead + krise*rise_ahead, clamped
  tuned: (g) => (o) => {
    const e = wrap(o.rot);
    const ahead = o.terrain_ahead === null ? 0 : o.terrain_ahead - o.pos[1];
    const s = g.base - g.kp * e - g.kd * o.angvel + g.kh * ahead - g.kv * Math.max(0, o.vel[0] - g.vmax);
    const c = Math.max(-30, Math.min(30, s));
    return { motor_speed: [c * g.rear, c * g.front] };
  },
  // pitch control: brake when the nose rises, drive when it drops; P on angle plus D on angular velocity
  pitch: () => {
    let first = true;
    return (o) => {
      if (first) { first = false; return { wheel_radius: [0.6, 0.6], torque_scale: [2, 2] }; }
      const e = wrap(o.rot) + 0.4 * o.angvel;      // + is nose up
      const s = Math.max(-10, Math.min(30, 18 - 30 * e));
      return { motor_speed: [s, s] };
    };
  },
  // pitch control plus slowing for drops seen 2 m ahead
  pitch_ahead: () => {
    let first = true;
    return (o) => {
      if (first) { first = false; return { wheel_radius: [0.6, 0.6], torque_scale: [2, 2] }; }
      const e = wrap(o.rot) + 0.4 * o.angvel;
      const drop = o.terrain_ahead === null ? 0 : Math.max(0, o.pos[1] - 0.6 - o.terrain_ahead);
      const s = Math.max(-10, Math.min(30, 20 - 30 * e - 6 * drop - 0.5 * Math.max(0, o.vel[0] - 8)));
      return { motor_speed: [s, s] };
    };
  },
  // default car, but motors off whenever the ground 2 m ahead is lower than the chassis (coast downhill)
  coast: () => {
    let last = null;
    return (o) => {
      const down = o.terrain_ahead !== null && o.terrain_ahead < o.pos[1] - 0.6;
      const t = down ? [0, 0] : [1, 1];
      if (t.join() === last) return null;
      last = t.join();
      return { torque_scale: t };
    };
  },
  // slow down when the ground ahead drops away, full power when it rises
  grade: () => {
    let first = true, last = null;
    return (o) => {
      if (first) { first = false; return MAX; }
      if (o.terrain_ahead === null) return null;
      const g = (o.terrain_ahead - o.pos[1]);
      const s = g < -1.5 ? 8 : g < -0.5 ? 15 : 30;
      if (s === last) return null;
      last = s;
      return { motor_speed: [s, s] };
    };
  },
  // keep the chassis level: cut the rear wheel's torque when the nose pitches up, the front's when it pitches down
  level: () => {
    let first = true, last = null;
    return (o) => {
      if (first) { first = false; return MAX; }
      const r = o.rot % (2 * Math.PI);
      const t = r > 0.6 ? [0.3, 2] : r < -0.6 ? [2, 0.3] : [2, 2];
      const key = t.join();
      if (key === last) return null;
      last = key;
      return { torque_scale: t };
    };
  },
};
module.exports = { POLICIES };
