"""A deletion manifest's file written in a stack container as a purge writes it (M5 Task 33): the stack tests
that need a manifest in the `manifests` volume (`tests/stack/test_backup_image.py` on the dev stack,
`tests/stack/test_prod_backup.py` on the compose test stack) run `WRITE_MANIFEST` with `python -c` in a
container that mounts the volume writable, with the configuration role as its one argument (`migrate` in the
dev stack's one-off `migrate`, `worker` in the test stack's worker).

The file is a `job_tombstone` of job 1. Its content is computed by the kind's own A2 (`purge.compute_content`
over `JobTombstone.compute`, with `max_identity_ids` read on the stack's database as that role), and written
by `write_manifest_file`, so a replay of the manifest stream (the `migrate` service's, a restore's) takes
it: its `max_ids` hold every identity table, `score_targets` among them, which the kind's `execute` reads.
On a fresh database there is no job 1, so the content tombstones nothing (its `fence` op names no job) and a
replay of it deletes nothing; on a kept one, `JobTombstone.compute`'s due rule leaves out a job 1 that is not
due, so it tombstones only what retention would. The writer records no row, but a `migrate` that replays the
stream afterwards (the shipper's dependency, a restore) records the manifest from its file (§19.5:
`replayed`, then completed), and that row stays: a test that writes here removes its file at its end and
says what its database keeps (`tests/stack/test_backup_image.py`).

The script prints one JSON line, `{"written": [<uuid>, <sha256 hex>], "stream": {<uuid>: <sha256 hex>}}`: the
file it wrote, and every file of the stream; `written_manifest` reads it.
"""

import json
import uuid
from typing import Final

WRITE_MANIFEST: Final = """
import asyncio, hashlib, json, sys
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool
from jobhunter.clock import utcnow
from jobhunter.config import load_config
from jobhunter.ids import uuid7
from jobhunter.privacy.kinds.job_tombstone import KIND, REQUEST_KEY, JobTombstone
from jobhunter.privacy.manifests import ManifestRow, write_manifest_file
from jobhunter.privacy.purge import compute_content

async def main(role):
    cfg = load_config(role)
    now = utcnow()
    row = ManifestRow(
        id=1, manifest_uuid=uuid7(), kind=KIND, status="executing", request={REQUEST_KEY: [1]},
        file_sha256=None, scope=None, created_at=now, completed_at=None, shipped_at=None,
    )
    engine = create_async_engine(cfg.dsn("sqlalchemy"), poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            content = await compute_content(conn, JobTombstone(), row, fenced_at=now, now=now)
    finally:
        await engine.dispose()
    sha = write_manifest_file(cfg.manifests_dir, content)
    files = cfg.manifests_dir.glob("*.json")
    stream = {p.stem: hashlib.sha256(p.read_bytes()).hexdigest() for p in files}
    print(json.dumps({"written": [content["manifest_uuid"], sha.hex()], "stream": stream}))

asyncio.run(main(sys.argv[1]))
"""


def written_manifest(stdout: str) -> tuple[str, str, dict[str, str]]:
    """What `WRITE_MANIFEST` printed last on `stdout`: the file's uuid (in canonical form, so it is safe in a
    query's literal), its sha256 hex, and the stream's files as {uuid: sha256 hex}."""
    printed = json.loads([line for line in stdout.splitlines() if line.strip()][-1])
    manifest_uuid, sha = printed["written"]
    stream = {str(name): str(digest) for name, digest in printed["stream"].items()}
    return str(uuid.UUID(manifest_uuid)), str(sha), stream
