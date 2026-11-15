# pgwire-db-env — status

Agent benchmark: write a PostgreSQL 16-compatible server (full v3 wire protocol + SQL) where every
file it persists is empty. The task as given to models is `task/PROMPT.md`; the brief it was built
from is `BRIEF.md`. Lives at `~/pgwire-db-env` (a symlink to this directory); results page on :8941.

## Where it stands, 2026-11-14 11:30pm PT (resumed after the reboot)

Done:
- Grader: protocol (68 tests), clients (psql 8, pgbench 6, psycopg 29, node 12, JDBC 14 items), sql (upstream
  parallel_schedule minus 10 excluded files: 45,506 units, 26,127 scored), acid (21 scenarios, 42 seeded runs,
  plus 50 commit-window crash points), scale (pgbench -s 10).
- Regress reference re-run after the COPY splitter fix (`make_reference.py regress`): 0 nondeterministic units.
- Full Postgres 16 calibration, storage check off (`ref/grade-postgres-full`): 1.000 on every component;
  68/68, 26127/26127, 42/42 + 50/50, all client groups, all scale items.
- Public kit: `build_task.py` assembles `task/tools/` and `task/public/` from the grader's own code
  (`task/kit/` holds the local shim and runner). Public subset: 25 protocol tests, all client scripts,
  test_setup + 34 regress files (7,315 scored statements), 11 ACID scenarios on seed 101, the scale run.
  Plumbing checked by running it on v1's toy server inside pgw-sandbox (protocol, acid; clients slow but running).
- Grader modules now append task/tools to sys.path, so the kit's dbbox shim cannot shadow grader/dbbox.py.

In progress / not done:
- Baseline grades: cheat (must be 0), mini, v1-haiku (unit pgw-cal).
- One 20-minute Haiku 5.5 smoke run. No wider sweep without Jack's say-so.
- Results page on :8941, memory file.

Not checked in (see `.gitignore`): Postgres source in `pgsrc/`, generated reference outputs, logs,
runs, and the copy of the regress files in `task/public/regress/` (rebuild with `python3 build_task.py`).
