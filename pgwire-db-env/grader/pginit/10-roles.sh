#!/bin/sh
# Give the reference PostgreSQL the roles and authentication methods the task specifies.
set -e
psql -v ON_ERROR_STOP=1 -U postgres -d postgres <<'SQL'
SET password_encryption = 'scram-sha-256';
CREATE ROLE alice SUPERUSER LOGIN PASSWORD 'wonderland';
SET password_encryption = 'md5';
CREATE ROLE bob SUPERUSER LOGIN PASSWORD 'builder';
SET password_encryption = 'scram-sha-256';
CREATE ROLE carol SUPERUSER LOGIN PASSWORD 'cleartext';
SQL
cat > "$PGDATA/pg_hba.conf" <<'HBA'
local all all trust
host all alice all scram-sha-256
host all bob all md5
host all carol all password
host all all all trust
HBA
