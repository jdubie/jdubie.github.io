# Agent and grading sandbox for the black-box database task.
# Compilers and generic network tools only. No database engine, no PostgreSQL client or library,
# no database documentation. sqlite is removed from Python so it can't serve as the query engine.
# Never COPY grader/, oracle/, specs or reference data in here.
FROM python:3.12-slim
RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential rustc cargo golang-go git ca-certificates curl \
      procps file less netcat-openbsd bsdextrautils strace \
 && rm -rf /var/lib/apt/lists/* /usr/share/doc/*
RUN rm -f /usr/local/lib/python3.12/lib-dynload/_sqlite3*.so && rm -rf /usr/local/lib/python3.12/sqlite3 \
 && python3 -c "import importlib.util as u; assert u.find_spec('sqlite3') is None" \
 && ! ls /usr/lib/x86_64-linux-gnu | grep -i libpq \
 && ! command -v psql
ENV HOME=/tmp
WORKDIR /work
