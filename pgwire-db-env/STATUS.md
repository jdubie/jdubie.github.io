# pgwire-db-env — status

Agent benchmark: write a PostgreSQL 16-compatible server (full v3 wire protocol + SQL) where every
file it persists is empty. The task as given to models is `task/PROMPT.md`; the brief it was built
from is `BRIEF.md`. Lives at `~/pgwire-db-env` (a symlink to this directory); results page on :8941.

## Where it stood, 2026-11-14

The build agent stopped mid-step at about 2:41pm PT and the workstation was down until 10:51pm.

Done:
- Grader components: `grader/conformance.py` (byte-level protocol), `grader/clients.py` (psql,
  pgbench, psycopg), `grader/regress.py` (upstream regress schedule, per-statement credit),
  `grader/acid.py` + `grader/crashpoint*` (crash and isolation), `grader/scale.py`.
- Expected answers from real Postgres: protocol 68/68 and clients 63 items kept as stable across two
  runs (`make_reference.py`).
- Test servers to calibrate against: `ref/cheat`, `ref/mini` (v1's toy), `ref/v1-haiku`.
- `task/PROMPT.md` written; the public kit (`task/kit/`) was being written.

Real Postgres calibration (storage rule off): protocol 1.0, clients 1.0, scale 1.0. ACID never
finished; the regress reference run was cut off partway.

Not done:
- Regress reference (rerun `python3 make_reference.py regress`), Postgres ACID calibration.
- Scores for the cheat (should be 0), mini and v1-haiku (should be low).
- Public kit, then one 20-minute Haiku 5.5 smoke run. No wider sweep without Jack's say-so.
- Results page on :8941, memory file.

Not checked in (see `.gitignore`): Postgres source in `pgsrc/`, generated reference outputs, logs,
runs.
