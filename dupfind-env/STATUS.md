# dupfind-env — status

Agent benchmark: write a program that finds duplicate files in a tree, as fast as possible. It is
graded on hidden trap cases. The task as given to models is `task/PROMPT.md`, and the request it was
built from is `BRIEF.md`. The working copy is `~/dupfind-env` (copied here, not symlinked). Its results
page runs on tailnet port 8921.

## Layout

- `task/`: what the agent sees. `PROMPT.md`, public case generators (`tools/cases_public.py`, built by
  `build_task.py` by stripping the hidden-only cases out of `grader/cases.py`), `tools/check.py`, and
  the expected answers for the public cases.
- `grader/`: `cases.py` (23 case generators, run at hidden scale), `grade.py`, `reffind.py` (the
  reference finder that writes the expected answers), `timer.py`, and `baseline.json` (the naive
  times).
- `ref/`: three reference solutions, `naive`, `smart` and `sloppy`. `calibrate.sh` grades them.
- `run_agents.py` runs the CLI agents in docker. `make_page.py` builds the results page, which
  `serve_data.py` serves.
- `docker/`: the Dockerfiles for the `dupfind-sandbox:1` and `dupfind-gemini:1` images, copied from
  `~/emptyfs-db-env/docker`. Locally these images are the efsdb v1 images under a second tag.
  `docker/sandbox/scan.py` is the copy from 2026-11-14, which was edited after those images were
  built.

## Reward

Geometric mean over cases of naive time ÷ agent time. A wrong answer or a timeout scores 0.01 for
that case.

## Results so far

- 2026-11-13 sweep (15 min, tag `sweep15`, 21 cases): every model got 21/21 right. Scores: Gemini
  5.00, Opus 4.70, Sol 3.59, Sonnet 3.21, Terra 3.10, Haiku 0.99. The traps did not separate the
  models.
- 2026-11-14: two filesystem traps were added, `cross_device` (tmpfs mounts whose inode numbers
  collide) and `bind_alias` (one directory bind-mounted in several places), making 23 cases. The
  workstation went down at 2:41pm PT during the second sweep (`sweep15v2`, 6 models × 3 repeats,
  15 min); the six unfinished third repeats were re-run after it came back (unit
  `dupfind-sweep15v2b`) and all 18 were graded together.
- `sweep15v2` result: 17 of 18 runs delivered a finder and all 17 were correct on all 23 cases.
  Mean reward: Opus 4.52, Sonnet 4.03, Sol 3.03, Terra 2.80, Gemini 2.46 (one run never wrote
  `run.sh` and scored 0.01; the other two 2.82 and 4.55), Haiku 2.17.
- On the 23-case set (`runs/_grade23`): naive 1.02, smart Python reference 1.14, sloppy 0.17
  (13/23). The 11/13 solutions: Gemini fails `bind_alias` (5.00 → 3.65); the other five pass.
- Write-up: `drafts/find-duplicates/index.html` on this branch.

## Not done

- `task/PROMPT.md.v1` is copied into every agent's workspace by `make_workspace`; remove the `.v1`
  files from `task/` before the next sweep.
- Several solutions trust a home-made 128-bit hash with no byte compare; no fixed trap can catch that.
- Not checked in (see `.gitignore`): `runs/` (3 GB of agent transcripts and work dirs), the generated
  page in `site/`, `grader/expected/` and `ref/grades/`. `calibrate.sh` regenerates the last two.
