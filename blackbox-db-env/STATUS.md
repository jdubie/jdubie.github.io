# blackbox-db-env: status

Agent benchmark: reverse-engineer a database server you can only probe over TCP (PostgreSQL 16 behind a
seeded translating proxy), then build one that answers byte for byte the same. Brief: `BRIEF.md`; task as
the model sees it: `task/PROMPT.md`. Lives at `~/blackbox-db-env` (symlink here). Page on :8951.

## 2026-11-15 11:20pm PT
Done: variant spec + codec + SQL rewriter (`oracle/variant.py`), proxy (`oracle/proxy.py`), images
bbx-oracle:1 and bbx-sandbox:1, workload generator, grader (workloads + durability), replay cheat builder,
agent harness with a private network (oracle + Anthropic-only egress). Oracle-as-candidate: 1.000.
Not yet: remaining calibrations, Opus run, page.
