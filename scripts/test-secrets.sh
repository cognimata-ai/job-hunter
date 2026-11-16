#!/usr/bin/env bash
# Creates the dummy secret files of the compose test stack (deploy/compose.test.yaml; plan M5 Task 21):
#
#   sudo scripts/test-secrets.sh [dir]     dir defaults to .secrets/test in the repository root (git-ignored)
#
# Every one of the 20 names of deploy/secrets.example (spec §19.2), as production lays them out: the directory
# root:10010 mode 0750, each file root:10010 mode 0440 (group 10010 is jobhunter-secrets; the services read
# the files through it). The values are dummies, never a real key:
# - random: session_secret, data_key and restic_password (`openssl rand -base64 32`), the database passwords
#   (`openssl rand -hex 24`);
# - empty, so the role runs without it: data_key_previous (no rotation), anthropic_key (the stack's worker runs
#   on the recorded fake), the provider keys, langsmith_key (no tracing);
# - fixed: a dummy ntfy token and healthchecks API key (stubsvc records whether one was sent, never its value),
#   the dead-man ping URL on stubsvc, and the S3 pair of the S3 stand-in (deploy/compose.s3stub.yaml), public
#   test constants.
# An existing file is never overwritten, and no value is printed: the script prints `created <name>`, or
# `kept <name>` when another run created it meanwhile. A value reaches its file through a shell builtin or
# openssl's stdout, never a command line.
set -euo pipefail

if [ "$#" -gt 1 ]; then
    echo "usage: $0 [dir]" >&2
    exit 2
fi
dir="${1:-$(cd "$(dirname "$0")/.." && pwd)/.secrets/test}"

umask 077
tmp=""
trap 'if [ -n "$tmp" ]; then rm -f -- "$tmp"; fi' EXIT

need_root() {
    echo "test-secrets: cannot set owner root and group 10010 on $1; run it as root (sudo)" >&2
    exit 1
}

mkdir -p -- "$dir"
{ chown 0:10010 -- "$dir" && chmod 0750 -- "$dir"; } 2>/dev/null || need_root "$dir"

# create NAME COMMAND...: write the output of COMMAND to a private temporary file, make it root:10010 0440, then
# link it in under NAME. `ln` never replaces a file, so a secret that exists by then is kept.
create() {
    local name=$1 file="$dir/$1"
    shift
    if [ -e "$file" ] || [ -L "$file" ]; then
        return 0
    fi
    tmp=$(mktemp "$dir/.$name.XXXXXX")
    "$@" >"$tmp"
    { chown 0:10010 -- "$tmp" && chmod 0440 -- "$tmp"; } 2>/dev/null || need_root "$file"
    local outcome
    if ln -- "$tmp" "$file" 2>/dev/null; then
        outcome=created
    elif [ -e "$file" ]; then
        outcome=kept # another run created it meanwhile; its value stays
    else
        echo "test-secrets: cannot create $file" >&2
        exit 1
    fi
    rm -f -- "$tmp"
    tmp=""
    echo "test-secrets: $outcome $name"
}

# fixed VALUE: the value, by the builtin printf, so it is never an argument of another process.
fixed() {
    printf '%s' "$1"
}

for name in session_secret data_key restic_password; do
    create "$name" openssl rand -base64 32
done
for name in pg_bootstrap db_app db_migrator db_backup db_restore; do
    create "$name" openssl rand -hex 24
done
for name in data_key_previous anthropic_key rapidapi_key serpapi_key adzuna_app_id adzuna_app_key langsmith_key; do
    create "$name" true
done
create ntfy_token fixed dummy-ntfy-token-for-the-test-stack
create healthchecks_api_key fixed dummy-healthchecks-key-for-the-test-stack
create healthchecks_ping_url fixed http://stubsvc:8099/hc/ping/2f4e1c9a-6b1d-4c35-9a0e-5d7b8e3f1a20
create s3_access_key_id fixed jobhunter-test
create s3_secret_access_key fixed jobhunter-test-secret-0000000000
