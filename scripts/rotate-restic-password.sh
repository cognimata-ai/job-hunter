#!/usr/bin/env bash
# The restic repository password's rotation (spec §19.8 "Secret rotation"; M5 follow-ups Task F4 (c)). Run it as
# root, with the new password in a file only root can read, once the offline recovery kit holds it:
#
#   sudo scripts/rotate-restic-password.sh [--compose '<command line>'] [--secrets-dir DIR] [--dry-run] <new-file>
#
# --compose defaults to the production command line of deploy/compose.yaml's header (run from the checkout this
# script is in) and --secrets-dir to /etc/jobhunter/secrets, as for scripts/rotate-db-role.sh, so the RUNBOOK
# calls it without either; the rotation stack case passes the compose test stack's command line and
# .secrets/test. The command line is split on whitespace (no quoting); a relative --secrets-dir or <new-file>
# is taken from the directory it is called from.
#
# restic_password is not the key of the data: restic encrypts the repository with a master key that each of
# the repository's keys wraps, one key per password. So the rotation adds a key for the new password and
# removes the old password's key; every snapshot stays as it is and opens with the new password. The steps, in
# order:
# 1. in a one-off backup container, `<compose> run --rm --no-deps -T backup sh -c '…' < <new-file>`: its fresh
#    mount of restic_password is the installed (old) password, and its environment the repository's
#    (RESTIC_REPOSITORY, AWS_DEFAULT_REGION, and the S3 keys, which it reads from their secret files into
#    restic's environment as the backup jobs do). It reads the new password from its stdin into a file of its
#    own on /dev/shm (a RAM filesystem, mode 0600, removed when it ends), then:
#    a. the key the old password opens (`restic key list --json`, the one marked `current`);
#    b. `restic key add --new-password-file <it>`, unless the new password opens a key already (an earlier run
#       added it);
#    c. the check: the new password opens the repository, through a key other than the old one, else it stops
#       with the old key in place;
#    d. `restic key remove <the old key>`, as the new key (restic refuses to remove the key in use);
#    e. the check that the old password opens nothing any more.
#    Each restic call names its password file by a flag (`--password-file`, `--new-password-file`), so no
#    password is on a command line or in an environment; each waits for another's repository lock as the
#    backup jobs' calls do (`--retry-lock 30m`: `key remove` takes the exclusive lock, so it waits for a running
#    backup, prune or ship) and keeps no cache (`--no-cache`). Key ids are shortened to 8 hex digits in what it
#    prints; restic's own output is not printed. An installed password that opens no key any more (an earlier
#    run removed it before its install) while the new one opens one ends the one-off here, and the script goes
#    on with steps 2 and 3.
# 2. the new file installed as the secret, as production installs one: root:10010, mode 0440.
# 3. a restart of backup, whose container then mounts the new file (the restore container is one-off). Until
#    then, a restic call of the running backup container still uses the old password and fails with restic's
#    wrong password: the RUNBOOK runs this outside the backup jobs' times.
#
# Each command is printed before it runs (with its `< <file>` when it reads one). --dry-run runs none of them: it
# checks the arguments and that the new file holds a password (`grep -q`, which prints nothing), then prints the
# commands. Only service names, paths, key ids and the commands are printed, never a value. After a failure,
# run it again: steps b and the early end of step 1 make a run after any stop finish the rotation.
set -euo pipefail

readonly DEFAULT_COMPOSE="docker compose -p jobhunter --env-file deploy/images.lock --env-file deploy/release.lock \
--env-file /etc/jobhunter/jobhunter.env -f deploy/compose.yaml"
readonly DEFAULT_SECRETS_DIR=/etc/jobhunter/secrets
readonly GID=10010
readonly NAME=restic_password

# The one-off backup container's script, no arguments; the new password on its stdin. Its heredoc is quoted: the
# shell of this script expands nothing in it.
readonly ROTATE_KEY=$(
    cat <<'SCRIPT'
set -eu
umask 077
note() { echo "rotate-restic-password: $*"; }
fail() { echo "rotate-restic-password: $*" >&2; exit 1; }
work=$(mktemp -d -p /dev/shm)
trap 'rm -rf "$work"' EXIT
new="$work/new"
old=/run/secrets/restic_password
cat >"$new"
grep -q '[^[:space:]]' "$new" || fail "no new password on stdin; nothing was changed"
AWS_ACCESS_KEY_ID=$(cat /run/secrets/s3_access_key_id)
AWS_SECRET_ACCESS_KEY=$(cat /run/secrets/s3_secret_access_key)
export AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY
# key_of FILE: the id of the key the password in FILE opens; nothing when it opens none (restic's exit 12).
key_of() {
    status=0
    restic key list --json --no-cache --retry-lock 30m --password-file "$1" >"$work/keys" 2>/dev/null || status=$?
    case $status in
    0) python -I -c 'import json, sys; print(next(k["id"] for k in json.load(sys.stdin) if k["current"]))' <"$work/keys" ;;
    12) ;;
    *) fail "restic key list exited $status; nothing was changed" ;;
    esac
}
short() { printf '%.8s' "$1"; }
old_id=$(key_of "$old")
new_id=$(key_of "$new")
if [ -z "$old_id" ]; then
    [ -n "$new_id" ] || fail "neither the installed password nor the new one opens the repository; nothing was changed"
    note "the installed password opens no key any more (an earlier run removed it); the new one opens key $(short "$new_id")"
    exit 0
fi
if [ -z "$new_id" ]; then
    status=0
    restic key add --no-cache --retry-lock 30m --password-file "$old" --new-password-file "$new" >/dev/null 2>&1 || status=$?
    [ "$status" -eq 0 ] || fail "restic key add exited $status; the old key is in place"
    new_id=$(key_of "$new")
    [ -n "$new_id" ] || fail "the new key does not open the repository; the old key is in place"
    note "added key $(short "$new_id")"
fi
[ "$new_id" != "$old_id" ] || fail "the new password is the installed one; nothing was changed"
note "the new password opens the repository (key $(short "$new_id"))"
status=0
restic key remove --no-cache --retry-lock 30m --password-file "$new" "$old_id" >/dev/null 2>&1 || status=$?
[ "$status" -eq 0 ] || fail "restic key remove exited $status; the old key $(short "$old_id") may still be in place"
left=$(key_of "$old")
[ -z "$left" ] || fail "the installed password still opens the repository (key $(short "$left"))"
note "removed the old key $(short "$old_id"); the installed password opens nothing any more"
SCRIPT
)

usage() {
    echo "usage: $0 [--compose '<command line>'] [--secrets-dir DIR] [--dry-run] <new-file>" >&2
    exit 2
}

die() {
    echo "rotate-restic-password: $*" >&2
    exit 1
}

note() {
    echo "rotate-restic-password: $*"
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

# step_from FILE COMMAND...: print it with its input (`< FILE`), then run it with FILE on its stdin unless this is
# a dry run. The value goes from the file to the command's stdin, never onto a command line.
step_from() {
    local input=$1 shown
    shift
    shown=$(printf '%q ' "$@")
    echo "${shown% } < $(printf '%q' "$input")"
    if [ "$dry_run" -eq 0 ]; then
        "$@" <"$input"
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
[ "$#" -eq 1 ] || usage
new_file=$1

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

# 1. The repository's keys, changed by a one-off backup container that mounts the installed password.
step cd "$root"
if ! step_from "$new_file" "${compose[@]}" run --rm --no-deps -T backup sh -c "$ROTATE_KEY" rotate-restic-password
then
    die "$secrets_dir/$NAME is unchanged and nothing was restarted; the line above says where it stopped:" \
        "run it again"
fi

# 2. The new file.
if ! step install -o root -g "$GID" -m 0440 "$new_file" "$secrets_dir/$NAME"; then
    die "the repository opens with the new password only, but $secrets_dir/$NAME still holds the old one:" \
        "install $new_file there as this script does, then restart backup"
fi

# 3. The service that holds the secret.
step "${compose[@]}" restart backup
if [ "$dry_run" -eq 0 ]; then
    note "done: the repository opens with the new password only; installed $NAME and restarted backup"
fi
