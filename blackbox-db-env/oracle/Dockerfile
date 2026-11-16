# The oracle: PostgreSQL 16 behind the seeded translating proxy. Runs as root so the proxy's code and seed
# are unreadable by the postgres user (and so by SQL running inside the database).
FROM postgres:16-bookworm
RUN apt-get update && apt-get install -y --no-install-recommends python3 && rm -rf /var/lib/apt/lists/*
COPY variant.py proxy.py entry.sh /oracle/
RUN chmod 700 /oracle && chmod 600 /oracle/*.py && chmod 700 /oracle/entry.sh
ENV PGDATA=/var/lib/postgresql/data
ENTRYPOINT ["/oracle/entry.sh"]
