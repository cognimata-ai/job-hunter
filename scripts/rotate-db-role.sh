#!/usr/bin/env bash
# A database role's password rotation (spec §19.8 "Secret rotation"; plan M5 Task 28, Deviation 39, R7). Run it
# as root, with the new password in a file only root can read:
#
#   sudo scripts/rotate-db-role.sh [--compose '<command line>'] [--secrets-dir DIR] [--dry-run] \
#     <secret-name> <new-file>
#
# <secret-name> is one of db_app, db_migrator, db_backup and db_restore. --compose defaults to the production
# command line of deploy/compose.yaml's header (run from the checkout this script is in) and --secrets-dir to
# /etc/jobhunter/secrets, so the RUNBOOK calls it without either; the rotation drill passes the compose test
# stack's command line and .secrets/test, its secrets directory. The command line is split on whitespace (no
# quoting); a relative --secrets-dir or <new-file> is taken from the directory it is called from.
#
# The steps, in order:
# 1. the new file installed as the secret, as production installs one: root:10010, mode 0440. Compose
#    bind-mounts a secret file when a container starts, so every running container keeps the old one.
# 2. the role's password set to it in PostgreSQL, by a one-off postgres container (its fresh mount sees the new
#    file): `<compose> run --rm --no-deps -T postgres sh -c '…'`, which connects to the running postgres as the
#    bootstrap role (`psql -h postgres -U "$POSTGRES_USER"`, its password from pg_bootstrap in the one-off's
#    environment only) and runs `ALTER ROLE <role> PASSWORD :'pw'`, psql reading the new password into the
#    variable `pw` from /run/secrets/<secret-name> itself, as deploy/postgres/init/10-init.sh does: both
#    passwords expand in the container, never on the host, and neither is on a command line.
# 3. a restart of the services that hold the secret: api and worker for db_app, backup for db_backup and
#    db_restore; db_migrator is held by none (migrate and restore are one-off).
#
# Each command is printed before it runs. --dry-run runs none of them: it checks the arguments and that the new
# file holds a password (`grep -q`, which prints nothing; an empty password would leave the role without one),
# then prints the commands. Only role and service names, paths and the commands are printed, never a value.
set -euo pipefail

readonly DEFAULT_COMPOSE="docker compose -p jobhunter --env-file deploy/images.lock --env-file deploy/release.lock \
--env-file /etc/jobhunter/jobhunter.env -f deploy/compose.yaml"
readonly DEFAULT_SECRETS_DIR=/etc/jobhunter/secrets
readonly GID=10010

# The one-off postgres container's script: $1 the secret's name, $2 the role. Its heredoc is quoted, so psql
# reads `:'secret'` and `:'pw'` as its own variables; the shell expands nothing in it.
readonly ALTER_ROLE=$(
    cat <<'SCRIPT'
set -eu
secret="/run/secrets/$1"
[ -s "$secret" ] && [ -r "$secret" ] || { echo "rotate-db-role: $1 is missing, empty or unreadable" >&2; exit 1; }
PGPASSWORD=$(cat /run/secrets/pg_bootstrap)
export PGPASSWORD
psql -X -q -v ON_ERROR_STOP=1 -h postgres -U "$POSTGRES_USER" -d postgres -v role="$2" -v secret="$secret" <<'EOSQL'
\set pw `cat :'secret'`
ALTER ROLE :"role" PASSWORD :'pw';
EOSQL
SCRIPT
)

usage() {
    echo "usage: $0 [--compose '<command line>'] [--secrets-dir DIR] [--dry-run] <secret-name> <new-file>" >&2
    exit 2
}

die() {
    echo "rotate-db-role: $*" >&2
    exit 1
}

note() {
    echo "rotate-db-role: $*"
}

# step COMMAND...: print it (shell-quoted), then run it unless this is a dry run. No command carries a value.
step() {
    local shown
    shown=$(printf '%q ' "$@")
    echo "${shown% }"
    if [ "$dry_run" -eq 0 ]; then
        "$@"
    fi
}

compose_line=$DEFAULT_COMPOSE secrets_dir=$DEFAULT_SECRETS_DIR dry_run=0
while [ "$#" -gt 0 ]; do
    case $1 in
    --compose)
        [ "$#" -ge 2 ] || usage
        compose_line=$2
        shift 2
        ;;
    --secrets-dir)
        [ "$#" -ge 2 ] || usage
        secrets_dir=$2
        shift 2
        ;;
    --dry-run)
        dry_run=1
        shift
        ;;
    -*) usage ;;
    *) break ;;
    esac
done
[ "$#" -eq 2 ] || usage
name=$1 new_file=$2

case $name in
db_app) role=jobhunter_app holders="api worker" ;;
db_migrator) role=jobhunter_migrator holders="" ;;
db_backup) role=jobhunter_backup holders="backup" ;;
db_restore) role=jobhunter_restore holders="backup" ;;
*) die "unknown secret $name: one of db_app, db_migrator, db_backup, db_restore; nothing was changed" ;;
esac
read -r -a compose <<<"$compose_line"
[ "${#compose[@]}" -gt 0 ] || usage
here=$(pwd -P)
case $secrets_dir in /*) ;; *) secrets_dir="$here/$secrets_dir" ;; esac
case $new_file in /*) ;; *) new_file="$here/$new_file" ;; esac
[ -f "$new_file" ] || die "$new_file is not a file; nothing was changed"
grep -q '[^[:space:]]' "$new_file" || die "$new_file holds no password; nothing was changed"
root=$(cd "$(dirname "$0")/.." && pwd -P) # the checkout: the prod command line's paths are relative to it
if [ "$dry_run" -eq 0 ]; then
    [ "$(id -u)" -eq 0 ] || die "run it as root"
    [ -d "$secrets_dir" ] || die "no directory $secrets_dir; nothing was changed"
else
    note "dry run: the commands are printed, none is run"
fi

# 1. The new file.
step install -o root -g "$GID" -m 0440 "$new_file" "$secrets_dir/$name"

# 2. The role's password, set by a one-off postgres container that mounts the new file.
step cd "$root"
if ! step "${compose[@]}" run --rm --no-deps -T postgres sh -c "$ALTER_ROLE" rotate-db-role "$name" "$role"; then
    die "$secrets_dir/$name holds the new password, but $role still has the old one and nothing was" \
        "restarted: run it again"
fi

# 3. The services that hold the secret.
if [ -n "$holders" ]; then
    # shellcheck disable=SC2086 # the holders are names, one word each
    step "${compose[@]}" restart $holders
else
    note "no running service holds $name: nothing to restart"
fi
if [ "$dry_run" -eq 0 ]; then
    note "done: $role has its new password; restarted ${holders:-nothing}"
fi
