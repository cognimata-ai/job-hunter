#!/usr/bin/env bash
# Creates the secret files of the dev stack (deploy/compose.dev.yaml; spec §19.2).
#
#   scripts/dev-secrets.sh [dir]        dir defaults to .secrets/dev in the repository root (git-ignored)
#
# Each missing secret is a new random value: `openssl rand -base64 32` for session_secret and data_key,
# `openssl rand -hex 24` for the database passwords. The worker's anthropic_key is the value of
# JOBHUNTER_ANTHROPIC_API_KEY when it is set, else an empty file: compose needs the file to exist, and the
# worker reads an empty one as no key (its LLM calls then fail llm_not_configured; M2 Task 23). The value is
# written by a shell builtin, so it never appears in a command line. A file gets group 10010
# (jobhunter-secrets) and mode 0440 before it appears under its name, so it is never readable by anyone else,
# and the directory gets group 10010 and mode 0750. An existing secret is never overwritten: a key given once
# anthropic_key exists is not written, and the script prints `kept anthropic_key (remove it to set a key)`.
# Otherwise it prints `created <name>`, or `kept <name>` when another run created the file meanwhile; it never
# prints a value.
#
# Compose bind-mounts a secret with the host file's owner and mode, and the app runs as uid 10001 with the
# supplementary group 10010, so setting the group needs root: run it with sudo.
set -euo pipefail

if [ "$#" -gt 1 ]; then
    echo "usage: $0 [dir]" >&2
    exit 2
fi
dir="${1:-$(cd "$(dirname "$0")/.." && pwd)/.secrets/dev}"

umask 077
tmp=""
trap 'if [ -n "$tmp" ]; then rm -f -- "$tmp"; fi' EXIT

cannot_set_group() {
    echo "dev-secrets: cannot set group 10010 on $1; run as root (sudo) — the app runs as uid 10001 with group 10010 and cannot read the file otherwise" >&2
    exit 1
}

mkdir -p -- "$dir"
{ chgrp 10010 -- "$dir" && chmod 0750 -- "$dir"; } 2>/dev/null || cannot_set_group "$dir"

# create NAME COMMAND...: write the output of COMMAND to a private temporary file, give it group 10010 and mode
# 0440, then link it in under NAME. `ln` never replaces a file, so a secret that exists by then is kept.
create() {
    local name=$1 file="$dir/$1"
    shift
    if [ -e "$file" ] || [ -L "$file" ]; then
        return 0
    fi
    tmp=$(mktemp "$dir/.$name.XXXXXX")
    "$@" >"$tmp"
    { chgrp 10010 -- "$tmp" && chmod 0440 -- "$tmp"; } 2>/dev/null || cannot_set_group "$file"
    local outcome
    if ln -- "$tmp" "$file" 2>/dev/null; then
        outcome=created
    elif [ -e "$file" ]; then
        outcome=kept # another run created it meanwhile; its value stays
    else
        echo "dev-secrets: cannot create $file" >&2
        exit 1
    fi
    rm -f -- "$tmp"
    tmp=""
    echo "dev-secrets: $outcome $name"
}

for name in session_secret data_key; do
    create "$name" openssl rand -base64 32
done
for name in pg_bootstrap db_app db_migrator db_backup db_restore; do
    create "$name" openssl rand -hex 24
done
# The key, or nothing: `printf` is a builtin, so the value is never an argument of another process.
anthropic_key_value() {
    printf '%s' "${JOBHUNTER_ANTHROPIC_API_KEY:-}"
}
key_file="$dir/anthropic_key"
if [ -n "${JOBHUNTER_ANTHROPIC_API_KEY:-}" ] && { [ -e "$key_file" ] || [ -L "$key_file" ]; }; then
    echo "dev-secrets: kept anthropic_key (remove it to set a key)"
else
    create anthropic_key anthropic_key_value
fi
