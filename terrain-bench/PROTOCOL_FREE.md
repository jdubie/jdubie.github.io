# terrain-bench simulator protocol

A 2D car (box2d) on rough terrain. Its wheel motors push it to the right. You cannot steer. What you control is the car
itself: its chassis shape and weight, how many wheels it has (1–4) and where they attach, their size, weight and grip,
any suspension, each wheel motor's target speed and torque, and a movable ballast weight. You can change any of these
at any tick.

**Time is yours.** Nothing moves unless you step it. You can run up to **8 environments** at once, each on its own
course, and step each one as many ticks at a time as you like. Physics runs at 60 ticks per simulated second.

**Episodes are not.** A run allows a fixed number of episodes in total, across all environments (the prompt says how
many). Every course driven counts: the first one in each environment you make, and each one after a crash, stall,
finish or reset. When none are left, commands that would need an episode reply with an error.

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
| `{"cmd": "step", "env": E, "n": N, "obs": "last"}` | advance N ticks (1–6000). `obs` is `"last"` (the final tick's observation), `"all"` (a list, one per tick) or `"none"`. Reply: `{ticks, obs, events, best_distance, episodes_left}`. Stepping stops early at the end of an episode, so `ticks` can be less than N |
| `{"cmd": "set", "env": E, "params": {...}}` | change any subset of the params below; applies from the next tick. Reply carries the full new set |
| `{"cmd": "get_params", "env": E}` | the current params |
| `{"cmd": "reset", "env": E}` | end this environment's episode now and start a new random course from the beginning, keeping the params |
| `{"cmd": "close", "env": E}` | remove the environment |
| `{"cmd": "status"}` | `{envs, best_distance, wall_s, ticks_total, episodes_used, episodes_left}` |

A rejected command replies `{"type": "error", "msg": ...}` and changes nothing.

`events` lists `episode_end` (`{episode, seed, reason, distance, sim_s, params}`) and `episode_start`
(`{episode, params}`) in the order they happened. When an episode ends, the next one starts immediately on a new random
course, keeping the car's params, if the budget has an episode left. `reason` is `stalled`, `crashed`, `finished`, `timeout` (600 simulated seconds) or
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

You design the whole car. Defaults are a two-wheeled car (the one `make` gives you):

```json
{"chassis": [[1,0],[0.6,0.4],[0,0.5],[-0.6,0.4],[-1,0],[-0.6,-0.3],[0,-0.3],[0.6,-0.3]],
 "chassis_density": 80,
 "wheels": [{"vertex": 4, "radius": 0.4, "density": 70, "friction": 1, "motor_speed": 20, "torque_scale": 1, "suspension": null},
            {"vertex": 0, "radius": 0.4, "density": 70, "friction": 1, "motor_speed": 20, "torque_scale": 1, "suspension": null}],
 "ballast": {"mass": 0, "pos": [0, 0]}}
```

| param | allowed | meaning |
|---|---|---|
| `chassis` | 3–16 `[x, y]` vertices, metres, chassis frame | a polygon fanned from the origin: the vertices must go counter-clockwise around the origin, wind around it exactly once, each 0.1–1.5 m from it, and each consecutive pair must make a triangle with the origin of at least 0.005 m² (so less than 180° apart). Friction 10 |
| `chassis_density` | 20–200 kg/m² | |
| `wheels` | a list of 1–4 wheels; setting it replaces the whole list | |
| `wheels[i].vertex` | an index into `chassis`, one wheel per vertex | where the wheel's axle sits |
| `wheels[i].radius` | 0.1–0.9 m | |
| `wheels[i].density` | 20–200 kg/m² | |
| `wheels[i].friction` | 0.1–2 | grip against the ground (ground friction is 0.5; Box2D combines the two as their geometric mean) |
| `wheels[i].motor_speed` | -40–40 rad/s | the speed the motor drives the wheel towards; positive drives forward (+x). A motor brakes a wheel spinning faster than this |
| `wheels[i].torque_scale` | 0–3 | the motor's torque limit as a multiple of (car mass × 9.81 / wheel radius) |
| `wheels[i].suspension` | `null`, or `{axis, travel, hz, damping}` | `null` bolts the axle to the chassis. Otherwise the axle slides along `axis` (an `[x, y]` direction, chassis frame) up to `travel` (0.02–0.6 m) either way from the vertex, held there by a spring of stiffness `hz` (0.5–15, natural frequency in Hz) and `damping` (0–1, damping ratio) |
| `ballast` | `{mass, pos}` | a weight carried inside the car, touching nothing: `mass` 0–200 kg, `pos` `[x, y]` in the chassis frame, at most 1.5 m from the origin. Set `pos` at any tick and the weight slides there at 2 m/s |

`set` accepts any subset of these; for `ballast` you may send just `mass` or just `pos`. Changing the chassis, the
wheels list, or any wheel's vertex, radius, density, friction or suspension rebuilds the car in place mid-episode,
keeping the chassis pose and velocity and putting the wheels back on their axles. Changing only motor speeds, torque
scales or the ballast does not rebuild anything. The motor torque limits are recomputed from the car's mass whenever
anything changes. Motor torque acts on the chassis too: a wheel accelerating forward pitches the nose up, a wheel
braking pitches it down.

## Courses, episodes and reward

A course is 2000 ground tiles, each 1.5 m long, laid end to end from `x = -5`. Every tile at a given distance is about
equally steep, tilted up or down at random: 0.45 rad (±30%) at the start, rising linearly to 1.0 rad (±30%) at the end.

An episode ends when the car:

- **crashes**: the chassis tips more than 90° from upright, either way;
- **stalls**: 10 simulated seconds pass without it getting 0.02 m further right than its best so far (each tick it is
  also nearly stopped, `|vx| < 0.001`, costs 5 extra ticks);
- reaches the end of the course, or 600 simulated seconds pass.

The reward for a run is the furthest x any episode in any environment reaches at any point during the run.
