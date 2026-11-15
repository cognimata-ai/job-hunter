#!/usr/bin/env bash
# The restic repository password's rotation (spec §19.8 "Secret rotation"; M5 follow-ups Task F4 (c), Ruling
# FU-10). Run it as root, with the new password in a file only root can read, once the offline recovery kit
# holds it:
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
# order, so that no restic call of the running backup service ever meets a removed password (Ruling FU-10):
# 1. in a one-off backup container, `<compose> run --rm --no-deps -T backup sh -c '…' < <new-file>`: its fresh
#    mount of restic_password is the installed (old) password, and its environment the repository's
#    (RESTIC_REPOSITORY, AWS_DEFAULT_REGION, and the S3 keys, which it reads from their secret files into
#    restic's environment as the backup jobs do). It reads the new password from its stdin into a file of its
#    own on /dev/shm (a RAM filesystem, mode 0600, removed when it ends), reads the key the old password opens
#    (`restic key list --json`, the one marked `current`), and runs `restic key add --new-password-file <it>`,
#    unless the new password opens a key already (an earlier run added it).
# 2. the check, in the same one-off: the new password opens the repository through a key other than the old
#    one, else it stops with nothing changed but the added key. The one-off's last line on stdout names the old
#    key's id (`old-key <id>`), which the script keeps for step 5.
# 3. the new file installed as the secret, as production installs one: root:10010, mode 0440.
# 4. a restart of backup, whose container then mounts the new file (the restore container is one-off). Until
#    then, the running backup service's restic calls still use the old password, whose key is still there.
# 5. in a second one-off backup container, `<compose> run --rm --no-deps -T backup sh -c '…' <old key id>`,
#    whose fresh mount is the new password: `restic key remove <old key id>` as the new key (restic refuses to
#    remove the key in use), then the check that the key is gone. A failure here leaves the old key in the
#    repository: the script exits non-zero naming its id, and the RUNBOOK (section 7) says how to remove it by
#    hand.
# Each restic call names its password file by a flag (`--password-file`, `--new-password-file`), so no password
# is on a command line or in an environment; each waits for another's repository lock as the backup jobs' calls
# do (`--retry-lock 30m`: `key remove` takes the exclusive lock, so it waits for a running backup, prune or
# ship) and keeps no cache (`--no-cache`). The one-offs print their notes on stderr, key ids shortened to 8 hex
# digits; restic's own output is not printed.
#
# Each command is printed before it runs (with its `< <file>` when it reads one). --dry-run runs none of them: it
# checks the arguments and that the new file holds a password (`grep -q`, which prints nothing), then prints the
# commands, the old key's id as `<old-key-id>`. Only service names, paths, key ids and the commands are printed,
# never a value. After a failure before the install, run it again: a new password that opens a key already is
# not added twice. After the install, it says what is left to do, and so does a run again that finds the new
# password installed already (an earlier run stopped after its install): restart backup first, then remove the
# old key by hand (fix round 1, review I1), since its restart may not have happened.
set -euo pipefail

readonly DEFAULT_COMPOSE="docker compose -p jobhunter --env-file deploy/images.lock --env-file deploy/release.lock \
--env-file /etc/jobhunter/jobhunter.env -f deploy/compose.yaml"
readonly DEFAULT_SECRETS_DIR=/etc/jobhunter/secrets
readonly GID=10010
readonly NAME=restic_password
readonly NO_KEY_YET="<old-key-id>" # the dry run's stand-in for the id step 2 names

# What both one-off scripts start with: notes and failures on stderr, a scratch directory on /dev/shm, the S3
# keys in the environment, and `key_of FILE`, the id of the key the password in FILE opens (nothing when it opens
# none, restic's exit 12; the listing stays in "$work/keys").
readonly PRELUDE=$(
    cat <<'SCRIPT'
set -eu
umask 077
note() { echo "rotate-restic-password: $*" >&2; }
fail() { echo "rotate-restic-password: $*" >&2; exit 1; }
short() { printf '%.8s' "$1"; }
work=$(mktemp -d -p /dev/shm)
trap 'rm -rf "$work"' EXIT
installed=/run/secrets/restic_password
AWS_ACCESS_KEY_ID=$(cat /run/secrets/s3_access_key_id)
AWS_SECRET_ACCESS_KEY=$(cat /run/secrets/s3_secret_access_key)
export AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY
key_of() {
    status=0
    restic key list --json --no-cache --retry-lock 30m --password-file "$1" >"$work/keys" 2>/dev/null || status=$?
    case $status in
    0) python -I -c 'import json, sys; print(next(k["id"] for k in json.load(sys.stdin) if k["current"]))' <"$work/keys" ;;
    12) ;;
    *) fail "restic key list exited $status" ;;
    esac
}
SCRIPT
)

# Steps 1 and 2, the first one-off's script, no arguments; the new password on its stdin. Its heredoc is quoted:
# the shell of this script expands nothing in it.
readonly ADD_KEY=$(
    cat <<'SCRIPT'
new="$work/new"
cat >"$new"
grep -q '[^[:space:]]' "$new" || fail "no new password on stdin; nothing was changed"
old_id=$(key_of "$installed")
[ -n "$old_id" ] || fail "the installed password opens no key; nothing was changed"
new_id=$(key_of "$new")
if [ "$new_id" = "$old_id" ]; then
    others=$(python -I -c 'import json, sys; print(" ".join(k["id"] for k in json.load(sys.stdin) if not k["current"]))' <"$work/keys")
    note "the new password is the installed one already; nothing was changed"
    echo "installed-already $others"
    exit 0
fi
if [ -z "$new_id" ]; then
    status=0
    restic key add --no-cache --retry-lock 30m --password-file "$installed" --new-password-file "$new" >/dev/null 2>&1 || status=$?
    [ "$status" -eq 0 ] || fail "restic key add exited $status; the old key is in place"
    new_id=$(key_of "$new")
    [ -n "$new_id" ] || fail "the new key does not open the repository; the old key is in place"
    note "added key $(short "$new_id")"
fi
[ "$new_id" != "$old_id" ] || fail "the new password opens the old key; nothing is removed"
note "the new password opens the repository (key $(short "$new_id")); the old key is $(short "$old_id")"
echo "old-key $old_id"
SCRIPT
)

# Step 5, the second one-off's script: $1 the old key's id. Its mount of restic_password is the new password.
readonly REMOVE_KEY=$(
    cat <<'SCRIPT'
old_id=$1
current=$(key_of "$installed")
[ -n "$current" ] || fail "the installed password opens no key; the old key $(short "$old_id") is in place"
[ "$current" != "$old_id" ] || fail "the installed password opens the old key $(short "$old_id") itself; it stays"
status=0
restic key remove --no-cache --retry-lock 30m --password-file "$installed" "$old_id" >/dev/null 2>&1 || status=$?
[ "$status" -eq 0 ] || fail "restic key remove exited $status"
key_of "$installed" >/dev/null
python -I -c 'import json, sys; sys.exit(sys.argv[1] in [k["id"] for k in json.load(sys.stdin)])' "$old_id" <"$work/keys" ||
    fail "the old key $(short "$old_id") is still listed"
note "removed the old key $(short "$old_id"); the repository opens with the new password only"
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

# shown [< FILE] COMMAND...: the line printed for COMMAND (shell-quoted), with its input when it reads FILE.
shown() {
    local input="" line
    if [ "$1" = "<" ]; then
        input=$2
        shift 2
    fi
    line=$(printf '%q ' "$@")
    if [ -n "$input" ]; then
        echo "${line% } < $(printf '%q' "$input")"
    else
        echo "${line% }"
    fi
}

# step COMMAND...: print it, then run it unless this is a dry run. No command carries a value.
step() {
    shown "$@"
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

# 1 and 2. A key for the new password, added and checked by a one-off backup container that mounts the installed
# (old) password; its last line names the old key.
step cd "$root"
add=("${compose[@]}" run --rm --no-deps -T backup sh -c "$PRELUDE"$'\n'"$ADD_KEY" rotate-restic-password)
shown "<" "$new_file" "${add[@]}"
old_id=$NO_KEY_YET
if [ "$dry_run" -eq 0 ]; then
    if ! added=$("${add[@]}" <"$new_file"); then
        die "$secrets_dir/$NAME is unchanged and nothing was restarted; the line above says where it stopped:" \
            "run it again"
    fi
    if printf '%s\n' "$added" | grep -q '^installed-already'; then
        # An earlier run installed the new file and stopped (its restart may not have happened): never remove a
        # key before backup mounts the new password (Ruling FU-10, review I1).
        others=$(printf '%s\n' "$added" | sed -n 's/^installed-already *//p' | tail -n 1)
        if [ -z "$others" ]; then
            die "$secrets_dir/$NAME holds the new password already and it opens the repository's only key:" \
                "nothing is left to rotate; if an earlier run stopped before its restart, $(shown "${compose[@]}" restart backup)"
        fi
        die "$secrets_dir/$NAME holds the new password already (an earlier run installed it and stopped):" \
            "restart backup first ($(shown "${compose[@]}" restart backup)), then remove the old key by hand with" \
            "the command of docs/RUNBOOK.md, section 7; the repository's other keys: $others"
    fi
    old_id=$(printf '%s\n' "$added" | sed -n 's/^old-key \([0-9a-f]\{64\}\)$/\1/p' | tail -n 1)
    [ -n "$old_id" ] || die "the one-off named no old key; $secrets_dir/$NAME is unchanged: run it again"
    note "the old key is $old_id"
fi

# 3. The new file.
if ! step install -o root -g "$GID" -m 0440 "$new_file" "$secrets_dir/$NAME"; then
    die "the repository opens with both passwords, but $secrets_dir/$NAME still holds the old one: run it again"
fi

# 4. The service that holds the secret.
if ! step "${compose[@]}" restart backup; then
    die "$secrets_dir/$NAME holds the new password, but backup was not restarted: restart it, then remove the" \
        "old key $old_id by hand (docs/RUNBOOK.md, section 7)"
fi

# 5. The old key, removed by a one-off backup container that mounts the new password.
if ! step "${compose[@]}" run --rm --no-deps -T backup sh -c "$PRELUDE"$'\n'"$REMOVE_KEY" rotate-restic-password \
    "$old_id"; then
    die "the new password is installed and backup restarted, but the old key $old_id may still be in the" \
        "repository (the line above says where the removal stopped): list the keys, and if it is listed," \
        "remove it by hand (docs/RUNBOOK.md, section 7)"
fi
if [ "$dry_run" -eq 0 ]; then
    note "done: installed $NAME, restarted backup and removed the old key ${old_id:0:8}; the repository opens" \
        "with the new password only"
fi
