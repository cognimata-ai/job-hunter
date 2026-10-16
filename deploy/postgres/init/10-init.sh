#!/usr/bin/env bash
# Runs once, on the first start with an empty data directory, as the bootstrap superuser (spec §19.3).
# Role passwords and the database name reach SQL only as psql variables (:'x_pw', :"db"): the passwords are
# read from the secret files by psql itself, so they appear on no command line and in no log.
set -euo pipefail

for secret in db_app db_migrator db_backup db_restore; do
    [ -s "/run/secrets/${secret}" ] || { echo "10-init.sh: secret file ${secret} is missing or empty" >&2; exit 1; }
done

psql -v ON_ERROR_STOP=1 -v db="$POSTGRES_DB" --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<'EOSQL'
\set app_pw `cat /run/secrets/db_app`
\set mig_pw `cat /run/secrets/db_migrator`
\set bak_pw `cat /run/secrets/db_backup`
\set rst_pw `cat /run/secrets/db_restore`

CREATE ROLE jobhunter_migrator LOGIN PASSWORD :'mig_pw';
CREATE ROLE jobhunter_app LOGIN PASSWORD :'app_pw';
CREATE ROLE jobhunter_backup LOGIN PASSWORD :'bak_pw';
CREATE ROLE jobhunter_restore LOGIN CREATEDB PASSWORD :'rst_pw';

ALTER DATABASE :"db" OWNER TO jobhunter_migrator;

CREATE EXTENSION vector;
CREATE EXTENSION pg_trgm;
CREATE EXTENSION unaccent;

CREATE DATABASE jobhunter_template;
\connect jobhunter_template
CREATE EXTENSION vector;
CREATE EXTENSION pg_trgm;
CREATE EXTENSION unaccent;
ALTER DATABASE jobhunter_template IS_TEMPLATE true;
\connect :"db"

ALTER DEFAULT PRIVILEGES FOR ROLE jobhunter_migrator GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO jobhunter_app;
ALTER DEFAULT PRIVILEGES FOR ROLE jobhunter_migrator GRANT USAGE, SELECT ON SEQUENCES TO jobhunter_app;

GRANT pg_read_all_data TO jobhunter_backup;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
EOSQL
