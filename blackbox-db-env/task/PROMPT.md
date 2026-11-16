# Build a database server that behaves exactly like this one

A SQL database server is running at **`oracle:7000`** (TCP). Build, from scratch in `/work`, a server that
behaves identically to it: for the same bytes sent to it, it must send back the same bytes, hold the same
data, and keep that data across crashes.

There is no specification and no documentation. Everything you learn about it, you learn by connecting
to `oracle:7000` and experimenting. Credentials that work on it:

- user `admin`, password `opensesame`
- user `guest`, no password
- database `main`

You have about {minutes} minutes. Work autonomously; nobody will answer questions. Keep improving your
server until time runs out. Partial credit is real and fine-grained, so breadth pays.

## What to deliver

- `/work/start.sh`: started as `sh /work/start.sh` from `/work`. It must run your server in the foreground,
  listening on **0.0.0.0:7000**, storing its data under the directory in `$DATA_DIR`. That directory is
  empty on first start and exactly as you left it on later starts, including after a SIGKILL.
- `/work/build.sh` (optional): run once, offline, before any server starts, with `/work` writable. Compile
  here. During grading `/work` is **read-only** and `/tmp` is a small scratch space wiped on every restart.
- Languages: anything in this image: Python 3.12 (standard library only, no `sqlite3`, no pip packages),
  C/C++ (gcc), Rust (rustc and cargo, no crates), Go (standard library only). Do not embed, call or port an
  existing database engine: write the protocol, parser, executor and storage yourself. There is no network
  apart from `oracle:7000`.

## How it is graded

Your server is graded with **no access to the server above**. A hidden generator writes fresh workloads
(different tables, values and statement mixes from anything you will have seen), plays them against a fresh
copy of `oracle:7000` and against a fresh copy of your server, and compares the replies.

**reward = 0.45 sql + 0.25 protocol + 0.30 durability**

1. **sql (0.45)**: 60 sessions, each logging in as `guest` to `main` and sending a few dozen statements one
   request at a time: creating and altering tables, inserting, updating and deleting rows, queries
   (filters, ordering, grouping, aggregates, joins, subqueries, expressions, built-in functions, casts),
   statements that fail, and transactions (commit, rollback, savepoints, failures inside a transaction).
   Every reply message after login is compared **byte for byte** with the original's. A message that is
   identical earns 1; a message of the right kind with some of its fields right earns up to 0.5; extra or
   missing messages earn 0. A session whose login fails scores 0. Sessions run one after another on the
   same server; each uses its own tables.
2. **protocol (0.25)**: 50 short workloads, scored the same way from the first byte the server sends:
   logins (both users, wrong password, unknown user or database, malformed or unexpected login requests),
   every kind of request/response flow the original supports, several requests sent at once, unknown and
   malformed messages, cancelling a long-running request from a second connection, and closing.
3. **durability (0.30)**: 14 runs. Your server is killed with SIGKILL while one or more clients are writing
   (or right after they finish), restarted on the same `$DATA_DIR`, and queried. A run passes if what it
   holds equals what the original would hold given the writes that were acknowledged, plus any subset of
   the ones in flight at the kill. Lost, partial or invented writes fail it.

Values that are random per connection are not compared byte for byte, only their shape. Each reply has a
few seconds; a server that stops answering scores nothing for the rest of that workload. A server that
dies is restarted on the same data. The server container has 4 CPUs and 6 GB of memory.

Keep `start.sh` working at all times. A server that does not start scores 0.
