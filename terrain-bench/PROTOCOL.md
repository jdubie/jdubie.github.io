# terrain-bench simulator protocol

A 2D car (box2d) drives itself to the right across rough terrain. You cannot steer or brake directly. What you control
is the car itself: its chassis shape, where its two wheels attach, their size and weight, and each wheel motor's speed and
torque. You can change any of these while it drives.

The simulator runs in **real time at 60 Hz** and never waits for you.

## Connecting

Unix stream socket, newline-delimited JSON in both directions. Python:

```python
import json, socket
s = socket.socket(socket.AF_UNIX); s.connect("sim.sock")
f = s.makefile("rw")
for line in f:
    msg = json.loads(line)
    if msg["type"] == "obs" and msg["tick"] == 60:
        f.write(json.dumps({"cmd": "set", "params": {"motor_speed": [25, 25]}}) + "\n"); f.flush()
```

Several clients may connect at once. Every client receives every message; any client may send commands.

## Messages from the simulator

`obs` — every physics tick (60 per second):

| field | meaning |
|---|---|
| `episode`, `tick`, `t` | episode number, tick within it, sim seconds (`tick / 60`) |
| `pos` | `[x, y]` of the chassis origin, metres. The car starts at `[0, 4]`; +x is the direction of travel, +y is up |
| `rot` | chassis angle, radians, counter-clockwise positive, not wrapped |
| `vel` | `[vx, vy]` of the chassis centre of mass, m/s |
| `angvel` | chassis angular velocity, rad/s, counter-clockwise positive |
| `terrain_ahead` | height `y` (world frame, metres) of the top of the ground 2.0 m ahead of the chassis origin (`x = pos[0] + 2`); `null` past the end of the course |

If your client reads too slowly the simulator drops `obs` messages for it rather than slowing down.

`episode_start` — `{episode, course_end_x, params, mode, episodes_left}`. The car is rebuilt with the default params at
the start of every episode.

`episode_end` — `{episode, seed, reason, distance, avg_speed, score, sim_s, params}`. The next episode starts
immediately on a new course. `reason` is `stalled`, `finished`, `timeout` (180 s) or `reset`.

`ok` / `error` — the reply to each command. A rejected `set` changes nothing and says why.

`done` — evaluation only: every course has been driven; the socket closes.

## Commands

| command | effect |
|---|---|
| `{"cmd": "set", "params": {...}}` | change any subset of the params below, applied before the next tick; the reply carries the full new set |
| `{"cmd": "get_params"}` | reply with the current params |
| `{"cmd": "reset"}` | end this episode now (scored as it stands) and start a new course from the beginning |

## Params

Every pair is `[rear, front]`, i.e. `[wheel 0, wheel 1]`.

| param | default | allowed | meaning |
|---|---|---|---|
| `chassis` | `[[1,0],[0.6,0.4],[0,0.5],[-0.6,0.4],[-1,0],[-0.6,-0.3],[0,-0.3],[0.6,-0.3]]` | see below | 8 vertices, metres, chassis frame |
| `wheel_vertex` | `[4, 0]` | two different ints 0..7 | which chassis vertex each wheel's axle sits on |
| `wheel_radius` | `[0.4, 0.4]` | 0.2 .. 0.7 m | |
| `wheel_density` | `[70, 70]` | 40 .. 140 kg/m² | |
| `motor_speed` | `[20, 20]` | -30 .. 30 rad/s | positive drives the car forward (+x) |
| `torque_scale` | `[1, 1]` | 0 .. 2 | motor torque limit as a multiple of (car mass × 9.81 / wheel radius) |

Chassis vertices go counter-clockwise from the front, one per 45° sector, and each coordinate keeps its sign:
vertex 0 is `[+, 0]`, 1 `[+, +]`, 2 `[0, +]`, 3 `[-, +]`, 4 `[-, 0]`, 5 `[-, -]`, 6 `[0, -]`, 7 `[+, -]`. A non-zero
coordinate has magnitude 0.1 .. 1.2 m; a `0` one must be exactly 0. The chassis is 8 triangles fanned from the origin,
density 80, friction 10. Wheels have friction 1, ground 0.5.

Changing `chassis` or `wheel_vertex` mid-run rebuilds the chassis and moves both wheels onto their new axles, keeping
their velocity. Changing a wheel's radius or density rebuilds that wheel. The motor torque limit is recomputed from the
new mass whenever anything changes.

## Courses and scoring

A course is 200 ground tiles, each 1.5 m long, laid end to end from `x = -5`. Each tile's slope is random and the
range grows with distance: flat at the start, up to ±2.25 rad by the end, so late tiles can be steeper than vertical.
No car is expected to finish.

The car **stalls** when it goes 10 s (600 ticks) without getting 0.02 m further right than its best so far; each tick
it is also nearly stopped (`|vx| < 0.001`) costs 5 extra ticks.

Score per course = furthest x reached + average speed (that distance divided by sim seconds driven). A reset scores
the episode as it stands.
