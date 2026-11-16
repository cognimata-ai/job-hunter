#!/usr/bin/env bash
# Builds the app image of the release before migration 0012 and tags it `jobhunter-app:prev` (plan M5 Task 27),
# for the upgrade-then-rollback drill (tests/stack/test_prod_drills.py), which crosses the newest expand
# migration: 0012 (the M5 final review's fix E2) since it was added, 0011 before.
#
#   scripts/build-prev-image.sh      from any directory, in a clone with the full history
#
# The preceding release is the parent of the commit that added 0012, found with `git log --diff-filter=A` on
# 0012's path; a shallow clone cannot answer that, so it is refused (CI checks out with `fetch-depth: 0`).
# That tree is checked out in a `git worktree` under a new `mktemp -d` directory, which a trap removes on every
# exit (`git worktree remove --force`, the directory, then `git worktree prune`). The build passes what
# compose's `x-app-build` passes: `target: runtime`, the five base images of that tree's deploy/images.lock as
# build arguments, and the optional `build_ca` secret (the calling shell's SSL_CERT_FILE, /dev/null when unset,
# as compose's `${SSL_CERT_FILE:-/dev/null}`); the image is labelled with the commit it was built from. The
# script never removes an image: `jobhunter-app:prev` stays tagged until an operator removes it.
set -euo pipefail

if [ "$#" -ne 0 ]; then
    echo "usage: $0" >&2
    exit 2
fi
cd "$(dirname "${BASH_SOURCE[0]}")/.."

readonly MIGRATION=backend/src/jobhunter/db/migrations/versions/0012_m5_unship_manifest.py
readonly TAG=jobhunter-app:prev

if [ "$(git rev-parse --is-shallow-repository)" = true ]; then
    echo "build-prev-image: this clone is shallow, so the commit that added $MIGRATION cannot be found;" \
        "fetch the full history first (git fetch --unshallow; in CI, actions/checkout with fetch-depth: 0)" >&2
    exit 1
fi
added=$(git log --diff-filter=A --format=%H -- "$MIGRATION" | tail -n 1)
if [ -z "$added" ]; then
    echo "build-prev-image: no commit of this history adds $MIGRATION" >&2
    exit 1
fi
if ! prev=$(git rev-parse --verify --quiet "$added^"); then
    echo "build-prev-image: $added, which adds $MIGRATION, has no parent in this clone" >&2
    exit 1
fi

workdir=$(mktemp -d)
tree="$workdir/prev"
cleanup() {
    git worktree remove --force "$tree" >/dev/null 2>&1 || true
    rm -rf -- "$workdir"
    git worktree prune
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

git worktree add --detach --quiet "$tree" "$prev"
lock="$tree/deploy/images.lock"

# The image `name` of the preceding release's lock file (a line `name=<reference>`).
image_of() {
    local reference
    reference=$(sed -n "s/^$1=//p" "$lock")
    if [ -z "$reference" ]; then
        echo "build-prev-image: $lock names no '$1' image" >&2
        exit 1
    fi
    printf '%s' "$reference"
}
python_image=$(image_of python)
uv_image=$(image_of uv)
node_image=$(image_of node)
restic_image=$(image_of restic)
pgvector_image=$(image_of pgvector)

echo "build-prev-image: building $TAG from $prev, the parent of $added (which adds 0012)" >&2
docker build \
    --file "$tree/Dockerfile" \
    --target runtime \
    --build-arg "PYTHON_IMAGE=$python_image" \
    --build-arg "UV_IMAGE=$uv_image" \
    --build-arg "NODE_IMAGE=$node_image" \
    --build-arg "RESTIC_IMAGE=$restic_image" \
    --build-arg "PGVECTOR_IMAGE=$pgvector_image" \
    --secret "id=build_ca,src=${SSL_CERT_FILE:-/dev/null}" \
    --label "org.opencontainers.image.revision=$prev" \
    --tag "$TAG" \
    "$tree"

docker image inspect --format "build-prev-image: $TAG {{.Id}} {{.Size}} bytes" "$TAG"
