#!/bin/sh
set -eu
# Separate the application role from the database administrator. psql's quoted
# variable syntax escapes the password, including quotes, without shell SQL.
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
  -v runtime_password="$BROUTE_DB_PASSWORD" <<'SQL'
CREATE ROLE broute LOGIN PASSWORD :'runtime_password';
REVOKE ALL ON DATABASE broute FROM PUBLIC;
GRANT CONNECT, TEMPORARY ON DATABASE broute TO broute;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
GRANT USAGE, CREATE ON SCHEMA public TO broute;
SQL
