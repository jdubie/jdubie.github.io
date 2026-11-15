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

- Crash-point grader bug fixed (found by the v1-haiku baseline): a control trial must see the window's commit
  acknowledged, or a server that cannot run the window (no COPY / no extended protocol) passed it vacuously.
  `grade.py --only acid --keep` regraded the affected runs.
- Baselines: cheat 0 (82 violations; raw 0.014), v1 toy 0.033, v1 Haiku server 0.215 (0.256 before the fix;
  acid 0.65 is legit: its statement-log-in-file-names design survives crashes). mini and v1-haiku SQL were graded
  before the 3-timeout abort.
- Smoke run (the only model run): Haiku 5.5, 20 min, `runs/smoke/haiku__r0`: 0.143 (0.163 before the crash-point
  fix), no violations. 1,450-line Python server, one empty file per row. Stopped doing work at minute 14
  (134 one-turn "holding" resumes).
- Kit: `run_public.py clients <group>` now runs just that group (Haiku assumed it did).
- Results page `site/index.html` (`make_page.py` + `site/notes.json`), served by unit pgw-site on 0.0.0.0:8941.
  Memory file `pgwire-db-env-site.md` + MEMORY.md line written.

Not done:
- Any sweep: Jack's call. Ideas on the page's next steps (stronger keep-going nudge, audit ACID for vacuous passes,
  check scale limits against a metadata-only design).

Not checked in (see `.gitignore`): Postgres source in `pgsrc/`, generated reference outputs, logs,
runs, and the copy of the regress files in `task/public/regress/` (rebuild with `python3 build_task.py`).
