#!/bin/sh
# Downloads the embedding model files pinned in model.lock into <model-dir> and checks each one with
# `sha256sum --check` against the lock (spec §4.1: the model is pinned by HF revision and sha256).
#
#   embedder/download_model.sh <model-dir>
#
# The embedder image runs this at build (embedder/Dockerfile); locally it fills the gitignored .model-cache/
# for spike V7 and the `embedder_model` tests. A file that is already present with the locked digest is not
# downloaded again. A download is checked before it takes its name, so a file of another digest never gets
# the locked name; any mismatch exits non-zero: the lock is the authority, never the download.
# MODEL_LOCK overrides the lock path (default: model.lock next to this script).
set -eu

if [ "$#" -ne 1 ] || [ -z "$1" ]; then
    echo "usage: download_model.sh <model-dir>" >&2
    exit 64
fi
dest=$1
lock=${MODEL_LOCK:-$(dirname "$0")/model.lock}

repo=$(sed -n 's/^repo=//p' "$lock")
revision=$(sed -n 's/^revision=//p' "$lock")
case "$repo" in
    "" | *[!A-Za-z0-9._/-]* | */*/* ) echo "model.lock: bad repo" >&2; exit 65 ;;
esac
if ! printf '%s' "$revision" | grep -Eq '^[0-9a-f]{40}$'; then
    echo "model.lock: revision must be a 40-hex commit" >&2
    exit 65
fi

mkdir -p "$dest"
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
sums="$work/model.lock.sha256"
: > "$sums"

# File lines are `<path>=sha256:<64 hex>`; repo, revision and model_id carry no digest and are skipped.
grep '=sha256:' "$lock" > "$work/files"
while IFS='=' read -r file digest; do
    hex=${digest#sha256:}
    case "$file" in
        "" | /* | *..* | *[!A-Za-z0-9._/-]* ) echo "model.lock: bad file path: $file" >&2; exit 65 ;;
    esac
    if ! printf '%s' "$hex" | grep -Eq '^[0-9a-f]{64}$'; then
        echo "model.lock: bad sha256 for $file" >&2
        exit 65
    fi
    printf '%s  %s\n' "$hex" "$file" >> "$sums"
    if [ -f "$dest/$file" ] && (cd "$dest" && printf '%s  %s\n' "$hex" "$file" | sha256sum --check --status); then
        echo "present: $file"
        continue
    fi
    mkdir -p "$dest/$(dirname "$file")"
    echo "downloading: $file"
    curl --fail --silent --show-error --location --proto '=https' --proto-redir '=https' --tlsv1.2 --retry 3 \
        --output "$dest/$file.part" "https://huggingface.co/$repo/resolve/$revision/$file"
    # A download takes the locked name only once it has the locked digest.
    got=$(sha256sum < "$dest/$file.part" | cut -d ' ' -f 1)
    if [ "$got" != "$hex" ]; then
        rm -f "$dest/$file.part"
        echo "$file: sha256 mismatch: model.lock sha256:$hex, download sha256:$got" >&2
        exit 1
    fi
    mv "$dest/$file.part" "$dest/$file"
done < "$work/files"

# The final check: every locked file, the present ones included, against its locked digest.
(cd "$dest" && sha256sum --check --strict "$sums")
