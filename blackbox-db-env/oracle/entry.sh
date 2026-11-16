#!/bin/bash
# Initialise once (roles admin/guest, database main, both non-superusers), then run PostgreSQL on its unix
# socket only and the proxy in front. A SIGKILL of the container is a crash of both; restart recovers.
set -e
mkdir -p "$PGDATA" /var/run/postgresql
chown -R postgres:postgres "$PGDATA" /var/run/postgresql
chmod 700 "$PGDATA"
if [ ! -s "$PGDATA/PG_VERSION" ]; then
  gosu postgres initdb --locale=C --encoding=UTF8 -A trust -U postgres -D "$PGDATA" >/dev/null
  gosu postgres pg_ctl -D "$PGDATA" -o "-c listen_addresses=''" -w start >/dev/null
  gosu postgres psql -q -v ON_ERROR_STOP=1 -U postgres -d postgres <<'SQL'
CREATE ROLE admin LOGIN;
CREATE ROLE guest LOGIN;
CREATE DATABASE main OWNER admin;
SQL
  gosu postgres psql -q -v ON_ERROR_STOP=1 -U postgres -d main -c "GRANT ALL ON SCHEMA public TO admin, guest; GRANT ALL ON DATABASE main TO guest;"
  gosu postgres pg_ctl -D "$PGDATA" -m fast -w stop >/dev/null
fi
gosu postgres postgres -D "$PGDATA" -c listen_addresses='' -c max_connections=300 -c fsync=on \
  -c synchronous_commit=on -c deadlock_timeout=1s &
for i in $(seq 1 300); do
  [ -S /var/run/postgresql/.s.PGSQL.5432 ] && gosu postgres psql -q -U postgres -d main -c 'select 1' >/dev/null 2>&1 && break
  sleep 0.1
done
cd /oracle
exec python3 -u proxy.py
