#!/usr/bin/env bash
# Fails when the committed frontend/src/api/schema.d.ts is not what the api's OpenAPI document generates (§14.5).
#
#   scripts/check-openapi-drift.sh      from any directory; needs `npm ci` done in frontend/ and the uv environment
#
# It exports the api's OpenAPI document to frontend/openapi.json (git-ignored), regenerates schema.d.ts from it
# with `npm run gen:api`, and exits 1 on any difference from the committed file (or when the file is not tracked).
# The regenerated file stays in the working tree, so a failing run leaves the update ready to commit.
set -euo pipefail

if [ "$#" -ne 0 ]; then
    echo "usage: $0" >&2
    exit 2
fi
cd "$(dirname "${BASH_SOURCE[0]}")/.."
schema=frontend/src/api/schema.d.ts

if [ ! -d frontend/node_modules ]; then
    echo "frontend/node_modules is missing: run 'npm ci' in frontend/ first" >&2
    exit 2
fi

uv run python -m jobhunter.api.openapi_export frontend/openapi.json
npm --prefix frontend run gen:api --silent

if ! git ls-files --error-unmatch -- "$schema" >/dev/null 2>&1; then
    echo "$schema is not tracked by git: commit the generated file" >&2
    exit 1
fi
if ! git diff --exit-code -- "$schema"; then
    echo "$schema differs from the api's OpenAPI document: commit the regenerated file" >&2
    exit 1
fi
echo "OpenAPI types are up to date"
