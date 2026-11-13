# Changes to the vendored files

`box2d.js` and `seedrandom.js` are the files the original page loads
(https://santex.github.io/singularity/seed/), with one change:

- `box2d.js`, `b2World.prototype.SolveTOI`: the outer `for(;;)` over time-of-impact events is capped at 500 per world
  step. Without the cap, a car rebuilt mid-episode so that it overlaps the ground can make this loop find contacts
  forever, freezing the simulator (twice in sweep1; reproduced by fuzzing random designs with mid-run rebuilds, 6 of 8
  fuzz workers hung within 90 s). Later Box2D versions bound this work too. Hitting the cap leaves any remaining
  time-of-impact events in that step unresolved, so a body can overlap the ground for a tick.
