# terrain-bench

An agent benchmark: coding agents drive a 2D car over rough terrain, in real time, through a unix socket.

A run is one simulator and one agent for a fixed wall-clock time (30 minutes by default). The car drives from the first
second, on default settings, whether or not the agent is connected. The agent sees the chassis pose, velocity and
angular velocity, plus one number (the ground height 2 m ahead), at 60 Hz. It can change the car while it drives
(chassis shape, wheel placement, wheel size and density, each motor's speed and torque) and reset to a new random
course whenever it likes. It cannot steer or brake. **Reward is the furthest x any episode reaches during the run.**
There is no separate practice or evaluation phase. The agent-facing protocol is [PROTOCOL.md](PROTOCOL.md).

## Credit

The car, the courses and the physics are not ours. They come from the **HTML5 Genetic Algorithm 2D Car
Thingy** by Rafael Matsunaga ([rednuht.org/genetic_cars_2](http://rednuht.org/genetic_cars_2/)). This port was read
from the copy at [santex.github.io/singularity/seed/idx.html](https://santex.github.io/singularity/seed/idx.html).
That program is in turn loosely based on [BoxCar2D](http://boxcar2d.com/) and uses the
[Box2D](https://box2d.org/) physics engine.

[`sim/server.js`](sim/server.js) is a headless port of its `cawro.js`. It runs on the same `box2d.js` and David Bau's
`seedrandom.js` that the page loads, copied unchanged into [`vendor/`](vendor/). Kept from the original:

- terrain generation (200 tiles of 1.5 m, slope range growing with distance, seeded with `seedrandom`, so a seed
  gives the same course as in the browser)
- car construction: 8-vertex chassis, two motorised wheels, densities, frictions, and the genome's bounds
- `world.Step(1/60, 20, 20)` and the stall ("health") rule

Changed for the benchmark:

- one car instead of a population of 20, and no genetic algorithm; the agent is the designer
- wheels start on their axles instead of at the origin
- motor speed and torque limit are adjustable; cawro fixes them at 20 rad/s and car mass × g / radius
- every parameter can be changed mid-run, and the car keeps its parameters from one course to the next
- a 180 s episode cap; reward is distance alone (cawro adds average speed)

## Running

Needs `node`, `ffmpeg`, pycairo, Docker, and the sandbox images and proxy network from `~/libbet-eval` (the Dockerfiles
are copied into [`docker/`](docker/)).

```
python harness.py run --models none,opus,sonnet,haiku,codex,gemini --minutes 30 --tag sweep1   # all in parallel
python harness.py summary runs/sweep1
python render.py runs/sweep1                  # runs/sweep1/site/terrain_bench.mp4
node sim/server.js --socket /tmp/sim.sock     # a simulator to drive by hand
```

`none` is the baseline: no agent, the default car alone for the same time, resetting only when it stalls.

The video ([`render.py`](render.py)) is the race, then the reward over time. The race replays every model's furthest
episode side by side from the same start, one lane each, drawn as the original page draws its cars, with the company
logo on the chassis (logos: [`assets/logos/`](assets/logos/)). Each lane is its own course, because every reset draws a
new one. The reward plot shows furthest distance so far against wall-clock time.

Each run gets `runs/<tag>/<model>/`: the agent transcript, its working directory, and `sim/` with every episode
(`episodes.jsonl`), the best distance at 1 Hz (`progress.jsonl`) and the furthest episode tick by tick (`best.json`).

Caveats:

- Courses are random per reset, so a longer run or a reset-heavy strategy samples more courses; the `none` baseline
  shows how far course luck alone gets the default car.
- Programs an agent starts die when its CLI session ends. It is then resumed with a nudge, and meanwhile the car
  drives on its last settings.
