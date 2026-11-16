Build a NEW agent benchmark at ~/blackbox-db-env: **black-box reverse engineering of a database**. The model gets no spec, only a running server it can probe over TCP. It must work out the server's wire protocol and SQL behaviour by experiment, then build a compatible server from scratch. Jack (the user) chose this to make the task hard and to defeat memorization.

## Background — read first
- ~/pgwire-db-env (symlink to /home/robot/jdubie-blog-drafts/pgwire-db-env; read BRIEF.md, STATUS.md, PROMPT.md, run_agents.py, grader/, docker/). It's the sibling benchmark: "write Postgres 16 where every file is empty". Reuse its harness, sandbox, snapshotting and page approach. **Do not edit anything in ~/pgwire-db-env, and don't touch pgw-* units, images or volumes.** Another agent is running a model sweep there right now. Copy what you need, and use the prefix `bbx-` for every unit, image, volume and network.
- What we learned there: Opus 5.5 scored 0.429 in 20 minutes by writing a 2,600-line Postgres from memory. Its storage was a 140-line layer, and everything else was recalled. That's the problem this benchmark fixes.

## Design (Jack's direction; decide details yourself and justify them on the page)
1. **The hidden server ("oracle") is real Postgres 16 behind a seeded translating proxy**, so reference answers cost nothing. Each seed produces a different variant. Perturb enough that a memorized Postgres scores low:
   - **Wire format:** remap message type bytes, change the length-field endianness or width, reorder fields in RowDescription/ErrorResponse, alter the startup/auth handshake.
   - **SQL surface:** rename keywords and some function names, and change some type names, rewriting both ways in the proxy.
   - **A few semantic changes, only where the proxy can apply them exactly** (for example renamed SQLSTATE codes, or output formatting of numbers and booleans). Avoid any change that needs a new engine.
   - Keep each change deterministic and fully specified by the seed, and write the seed's full "spec" to a hidden file for the page.
2. **The model's sandbox:** network access to one oracle instance only (host and port given), nothing else. There is no Postgres documentation, no psql, no Postgres client libraries (they'd speak the wrong protocol anyway), and no internet. The prompt says only: "a SQL database server is at host:port; build a server that behaves identically; languages as in pgwire-db-env; persistence and crash safety are graded". Decide whether to say that it is "derived from a well-known open-source database". Lean towards saying nothing, and record the choice on the page.
3. **Grading is differential:** the candidate has no oracle access at grade time. The grader generates fresh workloads from a hidden seed, never shown to the model, so a record-and-replay cache of what it saw while probing fails: DDL, DML, queries, errors, transactions, plus a share of protocol-level edge cases (extended-protocol-equivalent flows if the variant has them, malformed messages, cancel). It sends each workload to a fresh oracle and to the candidate and compares responses byte for byte after the proxy's encoding, with per-message partial credit. Add a durability and crash component (kill -9 mid-workload, restart, compare state against the oracle given the acknowledged commits), reusing pgwire-db-env's crash scenarios where they fit. Drop the empty-files rule: one difficulty at a time.
4. **Calibration before any model run, all required:**
   - The oracle itself as the candidate scores 1.0.
   - **Unmodified real Postgres 16 as the candidate scores low.** This is the key check that memorization doesn't pay; report the number.
   - A record-and-replay cheat (one that caches every response seen during a probing session) scores low on the hidden workloads.
   - Different seeds give different variants. Check that a server built for seed A scores low on seed B. The oracle for seed A works as that server.
5. **Then exactly ONE model run: Opus 5.5, 20 minutes,** with snapshots every 2 minutes (SNAPSHOT_EVERY_S=120). Grade the final and each snapshot, and plot reward over time. No other model runs; a sweep is Jack's call.

## Rules on this box (must follow)
- Heavy or long jobs (docker builds, grading, agent runs) go in `systemd-run --user --unit=bbx-<name> -p MemoryMax=40G -p MemorySwapMax=0 -E HOME=$HOME -E PATH="$PATH" ...`, never in the foreground. A pgwire sweep is using the CPU at the same time, so run at most 2 grades at once.
- Never `rm` through a variable or a glob built from one. Keep one job per shell command.
- Don't use the GPU hosts, and touch nothing in prod.
- **Results page** on tailnet port 8951: a conclusion sentence at the top, the calibration table, the per-seed variant summary, scoring, Opus's reward-over-time chart, caveats and next steps. Serve it with pgwire-db-env's serve_data.py as `systemd-run --user --unit=bbx-site` on 0.0.0.0:8951. Check: 200 from the URL, bound to 0.0.0.0, and a headless Playwright screenshot looks right.
- Write a memory file ~/.claude/projects/-home-robot-Developer-teleop/memory/blackbox-db-env-site.md (frontmatter name/description/metadata.type=project, like pgwire-db-env-site.md) and add one line to MEMORY.md in that directory.

## Git
The project lives in /home/robot/jdubie-blog-drafts (a worktree of jdubie/jdubie.github.io on branch `drafts`). Create ~/blackbox-db-env as a symlink to /home/robot/jdubie-blog-drafts/blackbox-db-env, the way pgwire-db-env is set up. Commit and push to `drafts` at each milestone (design + proxy, grader, calibration, Opus run, page) without asking. Add paths deliberately (`git add blackbox-db-env/<path>`, never -A) and gitignore large generated files. Each commit message says what's unverified and ends with:
Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Other sessions push to `drafts` too. Before every push, run `git pull --rebase origin drafts`, then `git push origin drafts`. Never touch master. Put a BRIEF.md (this brief) and a STATUS.md in the directory.

## Report back
Concisely: the variant design and what each seed changes, the grading components and weights, the calibration numbers (especially unmodified Postgres and the replay cheat), Opus's reward and its curve, how Opus went about probing (did it recognise Postgres underneath?), anything unverified, the page URLs (http://jacks-workstation:8951/, http://jacks-workstation.tailef7cf1.ts.net:8951/, http://100.90.216.116:8951/), and the shas pushed.
