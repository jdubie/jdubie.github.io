# blackbox-db-env: status

Agent benchmark: reverse-engineer a database server you can only probe over TCP (PostgreSQL 16 behind a
seeded translating proxy), then build one that answers byte for byte the same and survives SIGKILL.
Brief: `BRIEF.md`; task as the model sees it: `task/PROMPT.md`. Lives at `~/blackbox-db-env` (symlink
here). Page on :8951 (unit bbx-site), regenerate with `python3 make_page.py` (prose in `site/notes.json`).

## Where it stands, 2026-11-17 11:30pm PT

Done:
- Oracle (`oracle/variant.py`, `oracle/proxy.py`, images bbx-oracle:1 and :2), sandbox bbx-sandbox:1,
  grader (60 SQL sessions, 50 protocol workloads, 14 durability runs; reward 0.45/0.25/0.30),
  replay cheat (`cheat/`), harness with a private network (oracle + Anthropic-only egress).
- Calibration on oracle v2 (v1 in calib/v1, within 0.001): oracle 1.000; stock PostgreSQL 0.033; other-seed
  oracle 0.033 / 0.034; replay cheat 0.058 (0.173 before logins got random application names);
  ablations wire 0.264, wire+semantics 0.320, wire+dialect 0.744.
- The one model run: Opus 5.5, 20 min, oracle v1 (`runs/opus20`, summary in `results/opus20`): 0.066,
  never logged in. It decoded the framing, recognised PostgreSQL, then brute-forced the renamed startup
  key, which v1 gave no way to discover. Benchmark bug; v2 adds HINTs (startup keys; dialect spellings
  of PostgreSQL words the client used). Grading defaults to v2; BBX_ORACLE_VERSION=1 for v1.

- Run 2 (approved by Jack): Opus 5.5, 20 min, oracle v2, fresh private seeds (`secret/`, gitignored: seeds,
  spec, run scripts, calibration). Calibration on those seeds: oracle 1.000, stock PostgreSQL 0.033.
  Final 0.336 (sql 0.346, protocol 0.723, durability 0): logged in at ~3 min via the v2 hints, 500-line
  server. Durability 0 because verify queries use casts it never implemented (grader coupling).
  Seed-free summaries in `results/opus20v2`; its server and transcript are not committed (they spell out the
  secret variant).

- 2026-11-16 durability fix (Jack): verify with plain `SELECT *`, rows as a multiset, integers canonicalised;
  scenario writes use only INSERT ... VALUES / UPDATE / DELETE with literals (+ DDL in the DDL scenario).
  Oracle 14/14 on both seeds, stock PostgreSQL 0.033; all calibration rows' durability re-run (unchanged).
  Regrade: run 1 still 0.066; run 2 0.336 -> 0.636 (durability 14/14 from minute 8).
- Sweep on the run-2 private seeds (20 min each, finals + snapshots graded): Sol 0.941, Terra 0.875, Opus run 2 0.636 /
  repeat 0.634, Sonnet 0.589, Haiku 0.064 (no login), Gemini 0.000 (no server). Terra t004/t006 graded with a 15 s
  per-workload cap (BBX_WORKLOAD_CAP_S). Seed-free timelines in results/sweep-*; raw runs local only.

- v3 built and calibrated (no model runs): heavier semantic layer computed by PostgreSQL itself (oracle/semantics3.py,
  image bbx-oracle:3; overrides via search_path, NULL ordering by query rewrite, ICU collation) and exact-session
  scoring (workload = 1 if byte-exact else 0.3 x partial). Private v3 seeds in secret/v3. Calibration: oracle 1.000,
  stock PG 0.005, wire+dialect 0.432 (target was <~0.35; 0.257 of it is durability), wire 0.079, wire+semantics 0.184,
  cheat 0.020, cross 0.005/0.010, perfect v2 server 0.672. v2 sweep rescored: Sol 0.941->0.611, Terra 0.875->0.532,
  Opus 0.636->0.436 / 0.634->0.412, Sonnet 0.589->0.364. Summaries in results/v3/.

Not done:
- Decide v3 scoring knobs if 0.432 for wire+dialect is too high (partial factor 0.15 -> 0.345; or durability weight).
- A v3 sweep (Jack's call).
- Further runs: Jack's call.
