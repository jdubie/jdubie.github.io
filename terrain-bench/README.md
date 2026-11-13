# terrain-bench

An agent benchmark: tune and drive a 2D car over rough terrain, in real time, through a unix socket.

The agent sees the chassis pose, velocity and angular velocity, plus one number (the ground height 2 m ahead), at 60 Hz.
It can change the car while it drives: chassis shape, wheel placement, wheel size and density, and each motor's
speed and torque. It can also reset to a new course. It cannot steer or brake. The protocol the agent reads is
[PROTOCOL.md](PROTOCOL.md).

## Credit

The car, the courses, the physics and the score are not ours. They come from the **HTML5 Genetic Algorithm 2D Car
Thingy** by Rafael Matsunaga ([rednuht.org/genetic_cars_2](http://rednuht.org/genetic_cars_2/)). This port was read
from the copy at [santex.github.io/singularity/seed/idx.html](https://santex.github.io/singularity/seed/idx.html).
That program is in turn loosely based on [BoxCar2D](http://boxcar2d.com/) and uses the
[Box2D](https://box2d.org/) physics engine.

[`sim/server.js`](sim/server.js) is a headless port of its `cawro.js`. It runs on the same `box2d.js` and David Bau's
`seedrandom.js` that the page loads, copied unchanged into [`vendor/`](vendor/). Kept from the original:

- terrain generation (200 tiles of 1.5 m, slope range growing with distance, seeded with `seedrandom`, so a seed
  gives the same course as in the browser)
- car construction: 8-vertex chassis, two motorised wheels, densities, frictions, and the genome's bounds
- `world.Step(1/60, 20, 20)`, the stall ("health") rule, and the score: furthest x plus average speed

Changed for the benchmark:

- one car instead of a population of 20, and no genetic algorithm; the agent is the designer
- wheels start on their axles instead of at the origin
- motor speed and torque limit are adjustable; cawro fixes them at 20 rad/s and car mass × g / radius
- every parameter can be changed mid-run
- a 180 s episode cap

## Running

Needs `node` and the `claude` CLI.

```
python harness.py run --models none,haiku --budget 20     # 20 practice minutes per agent, 5 hidden courses
python harness.py summary
python report.py runs/<tag>                               # static page in runs/<tag>/site
node sim/server.js --socket /tmp/sim.sock                 # a practice simulator to drive by hand
```

`none` is the baseline: no agent, so the default car drives the evaluation courses on its own.

Each run gets `runs/<tag>/<model>-<rep>/`: the prompt, the agent's transcript, every practice and evaluation
episode (`practice.jsonl`, `eval.jsonl`), the `./drive` log, and a copy of the agent's working directory.

Caveats:

- The agent runs with Bash in a temp dir on the host, not in a container. Nothing stops it from reading this
  directory, including `runs/<tag>/seeds.json`. Knowing a course's seed lets it rebuild the course offline.
- Evaluation is real time, so a 5-course run takes up to 15 minutes and scores carry timing noise. The same
  commands sent at the same ticks on the same seed reproduce exactly.
