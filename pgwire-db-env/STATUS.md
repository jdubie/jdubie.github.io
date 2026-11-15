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

- SQL early abort: after 3 consecutive unit timeouts a file's remaining units score 0 (`MAX_CONSEC_TIMEOUTS` in
  grader/regress.py). PostgreSQL rechecked after the change: 26127/26127 (`ref/grade-postgres-sql`). Protocol was
  left unbounded (worst case ~68 x 8 s; an abort there would couple independent tests).
- Public kit checked end to end against real PostgreSQL through a TCP forwarder in pgw-sandbox: protocol 1.000,
  clients 1.000 (all five groups), sql 1.000.

In progress / not done:
- Baseline grades: cheat (must be 0; acid alone already logged 22 content violations), mini, v1-haiku
  (units pgw-cal-*). mini and v1-haiku were started before the early abort and grade SQL without it; cheat
  was restarted with it.
- The one 20-minute Haiku 5.5 smoke run (unit pgw-smoke, started 11:45pm PT in parallel with the baselines,
  per Jack). Its workspace was copied before the abort sentence was added to PROMPT.md. No wider sweep.
- Results page on :8941, memory file.

Not checked in (see `.gitignore`): Postgres source in `pgsrc/`, generated reference outputs, logs,
runs, and the copy of the regress files in `task/public/regress/` (rebuild with `python3 build_task.py`).
