# Build PostgreSQL, wire protocol and all, storing everything in empty files

Write a PostgreSQL 16-compatible database server, from scratch, in /work. Real PostgreSQL clients (psql,
pgbench, libpq, psycopg 3, node-postgres, pgJDBC) must not be able to tell it apart from PostgreSQL 16,
and it must be ACID. Its one unusual rule: **every byte it persists must live in filesystem metadata.
Every file it creates must be empty.** File names, the directory tree, timestamps, permission bits and
hard-link counts are yours to use. File contents are not.

You have about {minutes} minutes. Work autonomously; nobody will answer questions. Keep improving it until
time runs out. The score is the share of hidden checks you pass, so partial credit is real and breadth pays.

## What to deliver

- `/work/start.sh`: started as `sh /work/start.sh` from `/work`. It must run the server in the foreground,
  listening on **0.0.0.0:5432**, storing its data under the directory in `$PGDATA_DIR`. That directory is
  empty on first start and exactly as you left it on later starts (including after a SIGKILL).
- `/work/build.sh` (optional): run once, offline, before any server starts, with `/work` writable. Compile
  here. During grading `/work` is **read-only**.
- Languages: anything in the image: Python 3.12 (no `sqlite3`, no pip packages), C/C++ (gcc), Rust (rustc and
  cargo, no crates), Go (standard library only). There is no network. Don't embed, call or port an existing
  database engine: write the parser, planner/executor, storage, transaction manager and protocol yourself.
  The PostgreSQL clients installed in the image are for testing only.

## The server clients expect

- Roles: `postgres` (superuser, no password: trust), and three password roles, all superusers:
  `alice` / `wonderland` must authenticate with **SCRAM-SHA-256**, `bob` / `builder` with **MD5**,
  `carol` / `cleartext` with a **cleartext password**. A wrong password or an unknown role is a FATAL
  `ErrorResponse` (SQLSTATE class 28) and the connection closes.
- Database: clients connect to `regression` (also accept `postgres`). It was created with `initdb --locale=C
  --encoding=UTF8`: server_version 16.x, UTF8, C collation (text compares by byte), DateStyle `ISO, MDY`,
  IntervalStyle `postgres`, TimeZone `Etc/UTC`, standard_conforming_strings on, integer_datetimes on.
  Startup-packet parameters (libpq sends `DateStyle`, `TimeZone`, `application_name`, ... from PGDATESTYLE,
  PGTZ, PGAPPNAME) set those GUCs for the session.
- Answers are compared with **real PostgreSQL 16.16** answering the same thing.

## How it is graded

The hidden grader runs your server in a sandbox (below) and compares it with PostgreSQL 16:

**reward = 0.20 protocol + 0.15 clients + 0.30 sql + 0.25 acid + 0.10 scale**, and **0** if the storage
rule is ever broken.

1. **protocol (0.20)**: ~70 byte-level conformance tests, each a scripted message sequence whose
   responses must match PostgreSQL's message for message after normalization. Covered: startup,
   SSLRequest/GSSENCRequest refusal (answer `N`), SCRAM-SHA-256 (the client checks your server signature),
   MD5 and cleartext auth, ParameterStatus at startup (application_name, client_encoding, DateStyle,
   integer_datetimes, IntervalStyle, is_superuser, server_encoding, server_version, session_authorization,
   standard_conforming_strings, TimeZone) and after `SET` of those, BackendKeyData, NegotiateProtocolVersion for
   3.x minor versions and `_pq_` options, simple query (multi-statement implicit transactions, EmptyQuery,
   error stops the rest), ReadyForQuery status `I`/`T`/`E`, every command tag, NoticeResponse; the
   **extended protocol**: Parse/Bind/Describe/Execute/Sync/Flush/Close, named and unnamed statements and
   portals, ParameterDescription and parameter type inference, NoData, portal row limits with
   PortalSuspended, named portals across Sync inside a transaction, errors discarding everything up to Sync,
   pipelines (one implicit transaction per Sync); **binary format** for parameters and results of int2/4/8,
   float4/8, bool, text, varchar, bpchar, name, "char", bytea, numeric, date, time, timestamp, timestamptz,
   interval, uuid, json, jsonb, oid and arrays; **COPY** FROM STDIN and TO STDOUT in text, csv and binary,
   CopyFail, bad rows, COPY inside the extended protocol; **CancelRequest** with BackendKeyData (a running
   `pg_sleep` is cancelled with 57014); **LISTEN/NOTIFY** with NotificationResponse (transactional,
   duplicates folded, delivered to idle listeners, pid of the notifier); FunctionCall (`F`, one test);
   large values (MB-sized), 60 concurrent connections, Terminate, unknown message types.
   RowDescription is compared by column name, type OID, type size, typmod and format. Errors and notices
   are compared by severity and **SQLSTATE class** (first two characters); one test checks the full SQLSTATE
   of the dozen codes drivers branch on (22012, 42P01, 42703, 23505, 23502, 23514, 23503, 22P02, 42601,
   22003, 42883, 42P07). Error message text is never compared.
2. **clients (0.15)**: mean over five real clients, each the share of its items matching PostgreSQL:
   psql (logins, `\d`-family output, `\l`, `\du`, `\dn`, `\copy`, `\gset`, `\gexec`, `\if`, output formats),
   pgbench (`-i` client-side and server-side generation, TPC-B with `-M simple|extended|prepared`, `-S`; no
   failed transactions, a minimum throughput, and the balance invariant), psycopg 3.2.3 (auth, server-side
   binding, binary dumpers/loaders, prepared statements, executemany, pipeline mode, COPY text/binary,
   savepoints, error diagnostics, server-side and scrollable cursors, notifies, cancel, statement_timeout,
   TypeInfo/EnumInfo catalog lookups), node-postgres 8.13.1 and pgJDBC 42.7.4 (server-prepared binary
   statements, batches, metadata queries against pg_catalog, fetch-size portals, CopyManager, query
   timeout cancel). These exact scripts are in `tools/clients/`, with PostgreSQL's answers.
3. **sql (0.30)**: PostgreSQL's own regression suite, `src/test/regress` at REL 16.16, the **whole
   parallel_schedule run serially through real psql** against one database, ~45,000 statements. Every
   statement's output is compared with PostgreSQL's: result tables exactly as psql prints them (rows in any
   order when the statement has no top-level ORDER BY), command tags, NOTICE/WARNING text, `\d` output,
   and the SQLSTATE class of errors (psql runs with VERBOSITY=terse; error text is ignored). Score = mean
   over test files of (statements passed / statements scored). Not scored: EXPLAIN output, statements needing
   C functions from regress.so or server-side file paths, and the tests tablespace, create_function_c,
   collate.*, euc_kr, opr_sanity, type_sanity, misc_sanity, oidjoins. Only observable statements are scored
   (a bare `CREATE TABLE` tag earns nothing by itself; its effect is seen by later statements); an expected
   error earns credit only if the latest statement expected to succeed also passed. Server-side
   `COPY t FROM 'file'` is sent as psql `\copy` instead. Each statement has 20 s; a server that dies is
   restarted on the same data and the file continues in a new session. psql runs with
   `PGTZ=PST8PDT PGDATESTYLE='Postgres, MDY' PGAPPNAME=pg_regress/<test>`, as pg_regress does.
4. **acid (0.25)**: half crash and isolation scenarios, half commit-window crash points.
   *Scenarios* (several random seeds each): atomicity (rollback, errors abort the transaction, a failing
   statement leaves nothing), crashes (SIGKILL of the whole server mid-transaction, during COMMIT, under 4
   concurrent writers on the simple and on the extended protocol: every acknowledged commit survives, no
   row is torn, constraints and sequences still hold after recovery), **pgbench TPC-B with 8 clients killed
   mid-run** (balance invariant after recovery, every transaction pgbench logged as complete is in
   pgbench_history), READ COMMITTED (no dirty reads, no lost updates, statement snapshots under concurrent
   transfers, `SELECT ... FOR UPDATE`), REPEATABLE READ snapshots and first-updater-wins (40001),
   SERIALIZABLE write skew, `FOR UPDATE NOWAIT` (55P03) and `SKIP LOCKED`, deadlock detection (40P01),
   concurrent unique inserts, and an idle open transaction must block neither readers nor writers of other
   rows. *Crash points*: your server runs under a supervisor that counts your mutating filesystem syscalls
   (create, mkdir, link, rename, unlink, rmdir, chmod, utime*, truncate, write, fsync, ...) and SIGKILLs
   everything at the Nth one, for N enumerated across a commit window (a multi-table transaction, a
   300-row INSERT, a sequence of 40 autocommit statements, DDL in a transaction, a COPY, and a pipelined
   extended-protocol batch). After each kill the state must be exactly before or after the window (for a
   sequence: exactly a prefix) and every acknowledged commit must be present.
5. **scale (0.10)**: `pgbench -i -s 10` (1,000,000 accounts, via COPY) within 10 minutes; then SIGKILL and
   restart: from container start to a correct `SELECT count(*), sum(abalance) FROM pgbench_accounts` earns
   full credit within 10 s, none after 30 s; `pgbench -S` throughput (full credit at 500 tps); TPC-B
   throughput with 8 clients (full credit at 100 tps, no failures, invariant holds); restart again after the
   writes. On the same 4 KB tmpfs, creating a million empty files from Python takes ~2.5 s and listing and
   decoding them all ~0.8 s, so replaying a log of everything ever executed will not fit; recovery has to be
   proportional to state, not to history.

### The empty-files rule (enforced; breaking it makes the whole reward 0)

During grading your data directory is a tmpfs that can hold only **4 KB of file content in total** (and at
most 4 million inodes), and the server runs under a seccomp profile that blocks `setxattr`, `symlink`,
`mknod`/`mkfifo`, `fallocate`, `io_uring` and `truncate`/`ftruncate` to a non-zero length. The root
filesystem is read-only. `/tmp` is a 64 MB tmpfs wiped on every restart. The server container has 4 CPUs
and 6 GB of memory. The data directory is scanned every few seconds. Any non-empty file, allocated block,
xattr, symlink or special file found in it, at any moment, sets the reward to 0. Allowed: directories,
empty regular files, names up to 255 bytes, any depth, `rename`, `link`, `utime`/`utimensat` timestamps,
`chmod` bits. You may keep anything you like in memory, but after a crash only what is in the data
directory exists.

## Tools (in /work/tools; the clients are in the image)

- `python3 tools/run_public.py [protocol|clients|sql|acid|scale] [filter] [-v]` starts your server from
  `start.sh` on a fresh data directory and runs the public checks with PostgreSQL's expected answers:
  ~25 of the protocol tests, all client scripts, 30 regression files run as a schedule (with
  test_setup first), a subset of the ACID scenarios, and the scale run. It scans the data directory with
  the grader's scanner (`/opt/efs/scan.py`). It does not apply the tmpfs limit, seccomp or crash points.
  Respect them anyway.
- `tools/pgproto.py`: the byte-level protocol client the conformance tests are written with.
  `tools/conformance_public.py`: the public conformance tests. `tools/pgwire.py`: a small simple-query client.
- `tools/regress.py`: the exact harness that splits, runs and scores the regression files through psql.
  `public/regress/sql/` holds **all** upstream regression files and `public/regress/data/` their data;
  `public/expected/regress.json` holds PostgreSQL's per-statement output for the public subset.
- psql and pgbench (16.16) are on PATH; `/opt/clients/venv/bin/python` has psycopg 3.2.3;
  `node` with `/opt/clients/node/node_modules/pg`; `java -cp /opt/clients/postgresql-42.7.4.jar File.java`.

Keep `start.sh` working at all times. A server that doesn't start scores 0.
