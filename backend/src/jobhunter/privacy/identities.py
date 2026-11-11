"""The identity advancement of `migrate` (spec §19.3 steps 1, 3 and 5, §19.5 "Identity advancement"; plan M5
Task 23, R25, Deviation 38): no id a deletion manifest names, or one of its selectors' bounds covers, is ever
allocated again, so a later replay never deletes a row created after the manifest.

**The stream** is every `*.json` file of the manifests directory (a `*.json.tmp` being written ends otherwise
and is never one), each parsed by `parse_manifest`: a file that does not parse, or is named for another uuid,
stops `migrate` (`ManifestInvalid`), as §19.5 says. **A recorded manifest** is a `deletion_manifests` row with
its content: one with `file_sha256`, which the §7.14 CHECK gives every `completed` and `shipped` row, and A3
gives an `executing` row together with its `scope`. An `executing` row without it has no content, and nothing
was deleted under it yet: it has no bounds. Before 0010 the table does not exist, and no manifest is recorded.

- `rewrite_missing_files` (step 1's first half) writes the file of each recorded manifest the stream lacks,
  from its `scope`, through `write_manifest_file`: the file it shipped, or would have. A scope whose canonical
  bytes do not hash to `file_sha256`, or that names another uuid, stops `migrate` with `ManifestConflict`
  before anything is written or advanced. A file the stream holds is left as it is.
- `manifest_bounds`: per table, the largest of every `max_ids` and watermark value and every id that an
  operation lists (`ids` and `keep`), over the stream and every recorded scope. A `row_delete` under a
  `parent` bound lists ids of its parent table (`bound.table`); any other list names ids of the operation's
  table.
- `advance_identities`: for each identity or serial column of `public` (`identity_columns`, procrastinate's
  `bigserial`s included), the target is the larger of the column's maximum and its table's bound. The
  sequence's next allocatable value is read from the sequence itself (`last_value + 1` once called, else
  `last_value`: a fresh sequence's next value is its `last_value`, 1); when it is at or below the target,
  `setval(pg_get_serial_sequence(table, column), target, true)` makes the next value `target + 1`. Otherwise
  nothing is done: a sequence is never lowered and a table above every bound is never touched. `setval` takes
  no table lock, so a deploy's `migrate` blocks no reader or writer of the tables. It returns the tables it
  advanced with their targets.

Table and column names come from the catalogue, the sequence's from `pg_get_serial_sequence`; every value is a
bind.
"""

import hashlib
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any, Final, cast
from uuid import UUID

from sqlalchemy.engine import Row
from sqlalchemy.ext.asyncio import AsyncConnection

from jobhunter.db.sql import q
from jobhunter.privacy.manifests import (
    ManifestConflict,
    ManifestContent,
    canonical_bytes,
    identity_columns,
    manifest_file_name,
    max_identity_ids,
    parse_manifest,
    read_manifest_file,
    write_manifest_file,
)

_STREAM_SUFFIX: Final = ".json"
_RECORDED_TABLE: Final = q("SELECT to_regclass('public.deletion_manifests') IS NOT NULL")
# Every manifest the database holds with its content (module docstring), in stream order.
_RECORDED: Final = q(
    "SELECT manifest_uuid, file_sha256, scope FROM deletion_manifests "
    "WHERE file_sha256 IS NOT NULL ORDER BY manifest_uuid"
)
_SEQUENCE: Final = q("SELECT pg_get_serial_sequence(:table, :column)")
_SETVAL: Final = q("SELECT setval(pg_get_serial_sequence(:table, :column), :target, true)")


async def _recorded(conn: AsyncConnection) -> list[Row[Any]]:
    if not (await conn.execute(_RECORDED_TABLE)).scalar_one():
        return []
    return list((await conn.execute(_RECORDED)).all())


async def rewrite_missing_files(conn: AsyncConnection, manifest_dir: Path) -> list[UUID]:
    """Write the file of each recorded manifest that `manifest_dir` lacks from its `scope` (module docstring);
    the uuids written, in stream order. `ManifestConflict` for a scope that is not its file's content."""
    written: list[UUID] = []
    for row in await _recorded(conn):
        if read_manifest_file(manifest_dir, row.manifest_uuid) is not None:
            continue
        content = cast(ManifestContent, row.scope)
        if hashlib.sha256(canonical_bytes(content)).digest() != bytes(row.file_sha256):
            raise ManifestConflict(
                f"manifest {row.manifest_uuid}: its scope does not hash to its file_sha256"
            )
        if content["manifest_uuid"] != str(row.manifest_uuid):
            raise ManifestConflict(f"manifest {row.manifest_uuid}: its scope is another manifest's")
        write_manifest_file(manifest_dir, content)
        written.append(row.manifest_uuid)
    return written


def _stream(manifest_dir: Path) -> Iterator[ManifestContent]:
    names = sorted(path.name for path in manifest_dir.iterdir() if path.name.endswith(_STREAM_SUFFIX))
    for name in names:
        data = read_manifest_file(manifest_dir, name.removesuffix(_STREAM_SUFFIX))
        if data is not None:  # gone since the listing: nothing to bound
            yield parse_manifest(data, file_name=name)


def _named_ids(content: ManifestContent) -> Iterator[tuple[str, int]]:
    """Every `(table, id)` bound of `content` (module docstring)."""
    yield from content["max_ids"].items()
    if (watermark := content.get("watermark")) is not None:
        yield from watermark["max_ids"].items()
    for op in content["operations"]:
        if op["op"] == "row_delete":
            bound = op["bound"]
            table = bound.get("table", op["table"]) if bound["by"] == "parent" else op["table"]
            for listed in (op.get("ids", []), op.get("keep", [])):
                yield from ((table, value) for value in listed)
        elif op["op"] == "field_redact":
            yield from ((op["table"], value) for value in op["ids"])


async def manifest_bounds(conn: AsyncConnection, manifest_dir: Path) -> dict[str, int]:
    """Per table, the largest id that a manifest of `manifest_dir` or a recorded manifest bounds or names
    (module docstring). `ManifestInvalid` for a file or a scope that does not parse."""
    contents = list(_stream(manifest_dir))
    for row in await _recorded(conn):
        scope = canonical_bytes(cast(ManifestContent, row.scope))
        contents.append(parse_manifest(scope, file_name=manifest_file_name(row.manifest_uuid)))
    bounds: dict[str, int] = {}
    for content in contents:
        for table, value in _named_ids(content):
            bounds[table] = max(bounds.get(table, value), value)
    return bounds


async def advance_identities(conn: AsyncConnection, bounds: Mapping[str, int]) -> dict[str, int]:
    """Move every identity of `public` whose next value is at or below its target to restart above it (module
    docstring); the tables advanced, with their targets."""
    quote = conn.dialect.identifier_preparer.quote_identifier
    maxima = await max_identity_ids(conn)
    advanced: dict[str, int] = {}
    for table, column in await identity_columns(conn):
        target = max(maxima[table], bounds.get(table, 0))
        names = {"table": f"public.{quote(table)}", "column": column}
        sequence = (await conn.execute(_SEQUENCE, names)).scalar_one()
        # The catalogue's name of the sequence, quoted where it needs it.
        state = (await conn.execute(q(f"SELECT last_value, is_called FROM {sequence}"))).one()  # noqa: S608
        allocatable = state.last_value + 1 if state.is_called else state.last_value
        if allocatable <= target:
            await conn.execute(_SETVAL, {**names, "target": target})
            advanced[table] = target
    return advanced
