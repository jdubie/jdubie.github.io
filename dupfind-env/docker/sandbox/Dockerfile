# Agent and grading sandbox for the empty-files database task. Compilers and the psql client only:
# no database engines. sqlite is removed from Python so it can't be used as the query engine.
# Never COPY grader/, expected answers or reference data in here.
FROM python:3.12-slim
RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential rustc cargo golang-go git ca-certificates \
      postgresql-client procps attr file less \
 && rm -rf /var/lib/apt/lists/*
RUN rm -f /usr/local/lib/python3.12/lib-dynload/_sqlite3*.so && rm -rf /usr/local/lib/python3.12/sqlite3 \
 && python3 -c "import importlib.util as u; assert u.find_spec('sqlite3') is None"
COPY scan.py /opt/efs/scan.py
ENV HOME=/tmp
WORKDIR /work
