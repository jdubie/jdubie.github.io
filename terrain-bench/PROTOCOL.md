# terrain-bench simulator protocol

A 2D car (box2d) on rough terrain. Its wheel motors push it to the right. You cannot steer. What you control is the car
itself: its chassis shape, where its two wheels attach, their size and weight, and each wheel motor's target speed and
torque. You can change any of these at any tick.

**Time is yours.** Nothing moves unless you step it. You can run up to **8 environments** at once, each on its own
course, and step each one as many ticks at a time as you like. Physics runs at 60 ticks per simulated second.

## Connecting

Unix stream socket `./sim.sock`, newline-delimited JSON in both directions. Each request gets exactly one reply. Add
an `"id"` to a request and it is echoed in the reply, so you can send several requests (for example, to different
environments) before reading the replies. Replies to different environments can arrive in any order.

```python
import json, socket
s = socket.socket(socket.AF_UNIX); s.connect("sim.sock")
f = s.makefile("rw")
def rpc(**m):
    f.write(json.dumps(m) + "\n"); f.flush()
    return json.loads(f.readline())

env = rpc(cmd="make")["env"]
rpc(cmd="set", env=env, params={"motor_speed": [15, 15]})
r = rpc(cmd="step", env=env, n=60)          # one simulated second
print(r["obs"]["pos"], r["events"])
```

## Commands

| command | reply |
|---|---|
| `{"cmd": "make"}` | `{env, events}`: a new environment on a new course, with the default params |
| `{"cmd": "step", "env": E, "n": N, "obs": "last"}` | advance N ticks (1–6000). `obs` is `"last"` (the final tick's observation), `"all"` (a list, one per tick) or `"none"`. Reply: `{ticks, obs, events, best_distance}`. Stepping stops early at the end of an episode, so `ticks` can be less than N |
| `{"cmd": "set", "env": E, "params": {...}}` | change any subset of the params below; applies from the next tick. Reply carries the full new set |
| `{"cmd": "get_params", "env": E}` | the current params |
| `{"cmd": "reset", "env": E}` | end this environment's episode now and start a new random course from the beginning, keeping the params |
| `{"cmd": "close", "env": E}` | remove the environment |
| `{"cmd": "status"}` | `{envs, best_distance, wall_s, ticks_total}` |

A rejected command replies `{"type": "error", "msg": ...}` and changes nothing.

`events` lists `episode_end` (`{episode, seed, reason, distance, sim_s, params}`) and `episode_start`
(`{episode, params}`) in the order they happened. When an episode ends, the next one starts immediately on a new random
course, keeping the car's params. `reason` is `stalled`, `crashed`, `finished`, `timeout` (600 simulated seconds) or
`reset`. If the physics engine ever hangs, the step replies with an error, the episode ends as `physics_error` (its
distance still counts), and the environment restarts on a new course.

## Observations

| field | meaning |
|---|---|
| `episode`, `tick`, `t` | episode number, tick within it, simulated seconds (`tick / 60`) |
| `pos` | `[x, y]` of the chassis origin, metres. The car starts at `[0, 4]`; +x is the direction of travel, +y is up |
| `rot` | chassis angle, radians, counter-clockwise positive (nose up), not wrapped |
| `vel` | `[vx, vy]` of the chassis centre of mass, m/s |
| `angvel` | chassis angular velocity, rad/s, counter-clockwise positive |
| `terrain_ahead` | height `y` (world frame, metres) of the top of the ground 2.0 m ahead of the chassis origin (`x = pos[0] + 2`); `null` past the end of the course |

## Params

Every pair is `[rear, front]`, i.e. `[wheel 0, wheel 1]`.

| param | default | allowed | meaning |
|---|---|---|---|
| `chassis` | `[[1,0],[0.6,0.4],[0,0.5],[-0.6,0.4],[-1,0],[-0.6,-0.3],[0,-0.3],[0.6,-0.3]]` | see below | 8 vertices, metres, chassis frame |
| `wheel_vertex` | `[4, 0]` | two different ints 0..7 | which chassis vertex each wheel's axle sits on |
| `wheel_radius` | `[0.4, 0.4]` | 0.2 .. 0.7 m | |
| `wheel_density` | `[70, 70]` | 40 .. 140 kg/m² | |
| `motor_speed` | `[20, 20]` | -30 .. 30 rad/s | the speed each motor drives its wheel towards; positive drives forward (+x). A motor brakes a wheel spinning faster than this |
| `torque_scale` | `[1, 1]` | 0 .. 2 | motor torque limit as a multiple of (car mass × 9.81 / wheel radius) |

Chassis vertices go counter-clockwise from the front, one per 45° sector, and each coordinate keeps its sign:
vertex 0 is `[+, 0]`, 1 `[+, +]`, 2 `[0, +]`, 3 `[-, +]`, 4 `[-, 0]`, 5 `[-, -]`, 6 `[0, -]`, 7 `[+, -]`. A non-zero
coordinate has magnitude 0.1 .. 1.2 m; a `0` one must be exactly 0. The chassis is 8 triangles fanned from the origin,
density 80, friction 10. Wheels have friction 1, ground 0.5.

Changing `chassis` or `wheel_vertex` mid-episode rebuilds the chassis and moves both wheels onto their new axles,
keeping their velocity. Changing a wheel's radius or density rebuilds that wheel. The motor torque limit is recomputed
from the new mass whenever anything changes. Motor torque acts on the chassis too: a wheel accelerating forward pitches
the nose up, a wheel braking pitches it down.

## Courses, episodes and reward

A course is 2000 ground tiles, each 1.5 m long, laid end to end from `x = -5`. Each tile's slope is random, and the
range grows steadily with distance: flat at the start, ±1.6 rad by the end.

An episode ends when the car:

- **crashes**: the chassis tips more than 90° from upright, either way;
- **stalls**: 10 simulated seconds pass without it getting 0.02 m further right than its best so far (each tick it is
  also nearly stopped, `|vx| < 0.001`, costs 5 extra ticks);
- reaches the end of the course, or 600 simulated seconds pass.

The reward for a run is the furthest x any episode in any environment reaches at any point during the run.
