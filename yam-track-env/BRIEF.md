# yam-track: what was asked

An agent benchmark. A simulated YAM 6-DOF arm has hidden dynamics: link masses, CoMs, inertias, rotor inertia,
friction and torque constants all differ from the nominal MJCF the agent gets. The agent reaches the arm only through
a lockstep Unix socket (30 Hz frames, 1500 Hz drive loop) and has to make it track joint references.
The first version (`prompts/v0.md`) scored mrad on synthetic references.

Asks, 2026-11-14:

1. Design an eval set hidden from the agent. It should target every kind of parameter (inertia, Coriolis, static
   friction, gravity comp), plus a few realistic trajectories taken from the right arm in prod bag rollouts.
2. Two versions:
   - **v1**: the agent gets the test trajectories themselves and 10-30 min to minimise tracking error on each. It
     steps the robot (many connections in parallel, Unix socket), which is how its progress is monitored.
   - **v2**: the agent gets representative practice trajectories. The eval set is held out until the end and
     scores the `controller.py` it leaves behind.
3. Report error in mm, not mrad ("not intuitive").
4. Sweep: Opus, Sonnet, Haiku, Sol (`gpt-6.1-sol`), Terra (`gpt-6.1-terra`) and Grok, 2 robots each, v1 at 8 min,
   v2 at 16 min.
