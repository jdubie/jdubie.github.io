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
  # oracle v3 with the semantic layer on: ICU default collation and the variant's overrides (semantics3.py)
  V3=$(python3 -c "import os; print(int(int(os.environ.get('BBX_VERSION','2')) >= 3 and 'semantics' in os.environ.get('BBX_LAYERS','wire,dialect,semantics')))")
  gosu postgres psql -q -v ON_ERROR_STOP=1 -U postgres -d postgres -c "CREATE ROLE admin LOGIN; CREATE ROLE guest LOGIN;"
  if [ "$V3" = 1 ]; then
    COLL=$(cd /oracle && python3 -c "import os; from semantics3 import make_sem3; print(make_sem3(int(os.environ['BBX_SEED']))['collation'])")
    gosu postgres psql -q -v ON_ERROR_STOP=1 -U postgres -d postgres -c "CREATE DATABASE main OWNER admin TEMPLATE template0 LOCALE_PROVIDER icu ICU_LOCALE '$COLL' LOCALE 'C'"
  else
    gosu postgres psql -q -v ON_ERROR_STOP=1 -U postgres -d postgres -c "CREATE DATABASE main OWNER admin"
  fi
  gosu postgres psql -q -v ON_ERROR_STOP=1 -U postgres -d main -c "ALTER SCHEMA public OWNER TO postgres; GRANT ALL ON SCHEMA public TO admin, guest; GRANT ALL ON DATABASE main TO guest;"
  if [ "$V3" = 1 ]; then
    (cd /oracle && python3 -c "import os; from semantics3 import make_sem3, setup_sql; print(setup_sql(make_sem3(int(os.environ['BBX_SEED']))))") \
      | gosu postgres psql -q -v ON_ERROR_STOP=1 -U postgres -d main >/dev/null
  fi
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
