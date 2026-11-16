# blackbox-db-env: status

Agent benchmark: reverse-engineer a database server you can only probe over TCP (PostgreSQL 16 behind a
seeded translating proxy), then build one that answers byte for byte the same and survives SIGKILL.
Brief: `BRIEF.md`; task as the model sees it: `task/PROMPT.md`. Lives at `~/blackbox-db-env` (symlink
here). Page on :8951 (unit bbx-site), regenerate with `python3 make_page.py` (prose in `site/notes.json`).

## Where it stands, 2026-11-16 12:50am PT

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

Not done:
- Any model run on v2 (Jack's call; `run_opus.sh` with a new --tag).
- Rotating seeds 7101/90210 before any sweep (both public in this repo).
