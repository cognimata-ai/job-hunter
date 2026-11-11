#!/usr/bin/env bash
# The production host's one-time setup (spec §19.8 "Host setup"; plan M5 Task 22, Ruling T22-3). Run it as
# root from the checkout at /opt/jobhunter, before the first `up`:
#
#   sudo /opt/jobhunter/scripts/host-setup.sh --secrets-from <dir> [--delete-sources] [--dry-run]
#
# First a preflight, which only reads and runs in a dry run too; any failure stops the script before it changes
# anything:
# - a real run only: it runs as root, from the checkout at /opt/jobhunter, and deploy/release.lock and
#   /etc/jobhunter/jobhunter.env exist (step 5 needs them);
# - every secret file of deploy/secrets.example is in <dir>, except the two optional ones (step 3);
# - gid 10010 is absent, or is the group jobhunter-secrets with no members, and no host user has gid 10010 as
#   its primary group: no host user may read the secrets (§19.2);
# - the swap check: no swap, or encrypted swap only, so the parse_jobs tmpfs never pages a decrypted CV to a
#   plain disk (§19.8 step 5). A swap area counts as encrypted when a dm-crypt device is in its block device
#   stack; a swap file is judged by the device of its filesystem. Any other swap stops it, zram included.
#
# Then the steps, in order:
# 1. the group jobhunter-secrets (gid 10010), when the host has no group 10010; no user is added to it.
# 2. the directory /etc/jobhunter/secrets, root:10010 mode 0750.
# 3. each secret file of deploy/secrets.example, from <dir>, installed root:10010 mode 0440. Never 0400: the app
#    services run as uid 10001 and read their secrets through group 10010. Two names are optional,
#    data_key_previous and ntfy_token: for those, an empty file means none (deploy/secrets.example). One that
#    <dir> lacks is installed as an empty file, unless /etc/jobhunter/secrets has it already (the previous key
#    during a rotation).
# 4. the egress rule (§16.9): deploy/firewall/jobhunter-egress.service copied into /etc/systemd/system,
#    enabled and started; it loads deploy/firewall/docker-user.nft.
# 5. each role's startup assertions (§16.8) in its own container, with no service started: api, worker,
#    migrate, backup and restore. The command line is the production one of deploy/compose.yaml's header, run
#    from /opt/jobhunter (a bare `docker compose` reads neither lock file).
# 6. with --delete-sources, the source files installed in step 3, once every step has passed.
#
# Each command is printed before it runs. --dry-run runs none of them: it prints them, having read the names
# in <dir> but no file's content. No value is ever printed: a secret reaches its file through `install`, never
# through a command line or this output.
set -euo pipefail

readonly APP_DIR=/opt/jobhunter
readonly SECRETS_DIR=/etc/jobhunter/secrets
readonly ENV_FILE=/etc/jobhunter/jobhunter.env
readonly GID=10010
readonly GROUP=jobhunter-secrets
readonly UNIT=jobhunter-egress.service
readonly OPTIONAL=" data_key_previous ntfy_token "
readonly ROLES="api worker migrate backup restore"

usage() {
    echo "usage: $0 --secrets-from <dir> [--delete-sources] [--dry-run]" >&2
    exit 2
}

die() {
    echo "host-setup: $*" >&2
    exit 1
}

note() {
    echo "host-setup: $*"
}

# step COMMAND...: print it (shell-quoted), then run it unless this is a dry run. No command here carries a
# value, only names.
step() {
    local shown
    shown=$(printf '%q ' "$@")
    echo "${shown% }"
    if [ "$dry_run" -eq 0 ]; then
        "$@"
    fi
}

# Sets group_exists: 0 when the host has no group with gid 10010, 1 when it is jobhunter-secrets with no
# members; any other group with that gid stops the script.
check_group() {
    local entry name members status=0
    entry=$(getent group "$GID") || status=$?
    case $status in
    0) ;;
    2)
        group_exists=0
        note "the host has no group $GID: step 1 adds $GROUP"
        return 0
        ;;
    *) die "getent group $GID failed ($status); nothing was changed" ;;
    esac
    IFS=: read -r name _ _ members <<<"$entry"
    [ "$name" = "$GROUP" ] || die "gid $GID is the group $name, not $GROUP; nothing was changed"
    [ -z "$members" ] ||
        die "the group $GROUP has members ($members): no host user may be one (§19.2); nothing was changed"
    group_exists=1
    note "gid $GID is the group $GROUP, with no members"
}

# A host user whose primary group is gid 10010 is a member of the group too (§19.2), whether the group exists
# yet or not: any such user stops the script.
check_primary_group() {
    local users primary status=0
    users=$(getent passwd) || status=$?
    [ "$status" -eq 0 ] || die "getent passwd failed ($status); nothing was changed"
    primary=$(awk -F: -v gid="$GID" '$4 == gid { names = names (names ? "," : "") $1 } END { print names }' \
        <<<"$users")
    [ -z "$primary" ] ||
        die "host users with gid $GID as their primary group ($primary): no host user may be a member of" \
            "$GROUP (§19.2); nothing was changed"
    note "no host user has gid $GID as its primary group"
}

# Every active swap area must have a dm-crypt device in its block device stack.
check_swap() {
    local areas area device
    areas=$(swapon --show=NAME --noheadings --raw)
    while IFS= read -r area; do
        [ -n "$area" ] || continue
        printf -v area '%b' "$area" # --raw writes a space in a name as \x20
        device=$area
        if [ -f "$area" ]; then
            device=$(findmnt -nro SOURCE --target "$area") ||
                die "cannot find the device that holds the swap file $area; nothing was changed"
            device=${device%%\[*} # btrfs names the subvolume too: /dev/sda2[/@swap]
        fi
        if ! lsblk -nrso TYPE -- "$device" | grep -x crypt >/dev/null; then
            die "swap on $area is not encrypted: run the host without swap (swapoff, and remove it from" \
                "/etc/fstab) or with encrypted swap only (§19.8); nothing was changed"
        fi
    done <<<"$areas"
    note "the swap check passed: no swap, or encrypted swap only"
}

source_dir="" delete_sources=0 dry_run=0
while [ "$#" -gt 0 ]; do
    case $1 in
    --secrets-from)
        [ "$#" -ge 2 ] || usage
        source_dir=$2
        shift 2
        ;;
    --delete-sources)
        delete_sources=1
        shift
        ;;
    --dry-run)
        dry_run=1
        shift
        ;;
    *) usage ;;
    esac
done
[ -n "$source_dir" ] || usage
[ -d "$source_dir" ] || die "no directory $source_dir"
source_dir=$(cd "$source_dir" && pwd -P) # absolute: step 5 changes the directory before step 6
root=$(cd "$(dirname "$0")/.." && pwd -P)
names=$(sed -e 's/#.*//' "$root/deploy/secrets.example" | awk 'NF { print $1 }')
[ -n "$names" ] || die "deploy/secrets.example lists no secret"

# The preflight.
if [ "$dry_run" -eq 0 ]; then
    [ "$(id -u)" -eq 0 ] || die "run it as root"
    [ "$root" = "$(cd "$APP_DIR" 2>/dev/null && pwd -P)" ] ||
        die "run the checkout at $APP_DIR: the egress rule's unit loads the rule from there"
    for file in "$root/deploy/release.lock" "$ENV_FILE"; do
        [ -f "$file" ] || die "$file is missing; the startup checks (step 5) need it; nothing was changed"
    done
fi
for name in $names; do
    if [ ! -f "$source_dir/$name" ] && [[ $OPTIONAL != *" $name "* ]]; then
        die "$name is not in $source_dir (deploy/secrets.example lists it); nothing was changed"
    fi
done
check_group
check_primary_group
check_swap
if [ "$dry_run" -eq 1 ]; then
    note "dry run: the commands are printed, none is run"
fi

# 1. The secrets group.
if [ "$group_exists" -eq 0 ]; then
    step groupadd --gid "$GID" "$GROUP"
fi

# 2. and 3. The secrets directory and files.
step install -d -o root -g "$GID" -m 0750 "$SECRETS_DIR"
installed=""
for name in $names; do
    if [ -f "$source_dir/$name" ]; then
        step install -o root -g "$GID" -m 0440 "$source_dir/$name" "$SECRETS_DIR/$name"
        installed+=" $name"
    else
        note "$name is not in $source_dir: an empty file, unless $SECRETS_DIR/$name exists"
        if [ "$dry_run" -eq 1 ] || [ ! -e "$SECRETS_DIR/$name" ]; then
            step install -o root -g "$GID" -m 0440 /dev/null "$SECRETS_DIR/$name"
        fi
    fi
done

# 4. The egress rule.
step install -o root -g root -m 0644 "$APP_DIR/deploy/firewall/$UNIT" "/etc/systemd/system/$UNIT"
step systemctl daemon-reload
step systemctl enable --now "$UNIT"

# 5. The startup checks.
step cd "$APP_DIR"
for role in $ROLES; do
    step docker compose -p jobhunter --env-file deploy/images.lock --env-file deploy/release.lock \
        --env-file "$ENV_FILE" -f deploy/compose.yaml run --rm --no-deps "$role" \
        jobhunter startup-check --role "$role"
done

# 6. The sources.
if [ "$delete_sources" -eq 1 ]; then
    for name in $installed; do
        step rm -f -- "$source_dir/$name"
    done
fi
if [ "$dry_run" -eq 0 ]; then
    note "done"
fi
