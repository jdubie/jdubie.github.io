# Agent and grading sandbox for the pgwire empty-files database task.
# Compilers plus PostgreSQL CLIENTS only (psql, pgbench, libpq, psycopg 3, node-postgres, pgJDBC):
# no database engine. sqlite is removed from Python so it can't be used as the query engine.
# The grader runs these same clients from this image; never COPY grader/, expected answers or reference data in here.
FROM node:24-bookworm-slim AS node
RUN mkdir -p /opt/clients/node && cd /opt/clients/node && npm init -y >/dev/null && npm i --no-audit --no-fund pg@8.13.1

FROM python:3.12-slim
RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential rustc cargo golang-go git ca-certificates curl gnupg \
      procps attr file less openjdk-21-jdk-headless \
 && curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc | gpg --dearmor -o /usr/share/keyrings/pgdg.gpg \
 && echo "deb [signed-by=/usr/share/keyrings/pgdg.gpg] http://apt.postgresql.org/pub/repos/apt trixie-pgdg main" > /etc/apt/sources.list.d/pgdg.list \
 && apt-get update && apt-get install -y --no-install-recommends postgresql-client-16 \
 && rm -rf /var/lib/apt/lists/*
# pgbench ships in the server package; extract only that binary from the .deb (it links libpq5, already installed).
RUN cd /tmp && apt-get update && apt-get download postgresql-16 && dpkg-deb -x postgresql-16_*.deb /tmp/pg16 \
 && cp /tmp/pg16/usr/lib/postgresql/16/bin/pgbench /usr/lib/postgresql/16/bin/pgbench && rm -rf /tmp/pg16 /tmp/*.deb /var/lib/apt/lists/* \
 && ln -sf /usr/lib/postgresql/16/bin/pgbench /usr/bin/pgbench && pgbench --version && psql --version
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /opt/clients/node /opt/clients/node
RUN python3 -m venv /opt/clients/venv && /opt/clients/venv/bin/pip install --no-cache-dir "psycopg[binary]==3.2.3" \
 && curl -fsSL -o /opt/clients/postgresql-42.7.4.jar https://repo1.maven.org/maven2/org/postgresql/postgresql/42.7.4/postgresql-42.7.4.jar
RUN rm -f /usr/local/lib/python3.12/lib-dynload/_sqlite3*.so && rm -rf /usr/local/lib/python3.12/sqlite3 \
 && python3 -c "import importlib.util as u; assert u.find_spec('sqlite3') is None" \
 && /opt/clients/venv/bin/python -c "import psycopg; print('psycopg', psycopg.__version__, 'libpq', psycopg.pq.version())" \
 && (cd /opt/clients/node && node -e "console.log('pg', require('pg/package.json').version)") \
 && (dpkg -l | grep -E 'postgresql-client-16|libpq5|openjdk-21' | awk '{print $2, $3}') > /opt/clients/VERSIONS \
 && echo "psycopg[binary] 3.2.3, pg (node) 8.13.1, pgjdbc 42.7.4, node $(node --version)" >> /opt/clients/VERSIONS
COPY scan.py /opt/efs/scan.py
ENV HOME=/tmp CLIENTS=/opt/clients
WORKDIR /work
