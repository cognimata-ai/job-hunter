"""Deletion manifests (spec §7.14, §19.5; plan M5 Task 9, D1-D3, Deviations 16, 17 and 53): the typed content
of a purge, its canonical file, the rows the purge writes for it, and each operation's postcondition.

**The content** (`ManifestContent`, `MANIFEST_FORMAT` 1) holds ids, table and column names, selectors and
storage keys only, never content (Global Constraints, Manifests and exports): the uuid, the kind,
`admitted_at` (A0's own clock, the manifest row's `created_at`), `fenced_at` (the fence's commit cutoff,
`fences.fence_committed_at`), `max_ids` (`max_identity_ids` when the content was computed), the ordered typed
operations, and for a source or profile purge the affected set and the watermark; `retained` names the rows a
kind keeps by id. A check of A0's own writes compares with `admitted_at`; a bound on rows other transactions
wrote compares with `fenced_at`.

**The file** is `canonical_bytes(content)` (D2: sorted keys, compact separators, UTF-8), so the file `migrate`
rewrites from the row's `scope` (a `jsonb` column, which keeps no key order) hashes to the `file_sha256` its
shipment matched. `write_manifest_file` writes `<uuid>.json.tmp`, fsyncs it, renames it to `<uuid>.json` and
fsyncs the directory (`profile.storage.fsync_directory`); it returns the sha256. A file already there with
equal bytes is adopted (its hash is returned: a retry after a crash past the rename, Deviation 17); one with
other bytes raises `ManifestConflict`, and a manifest's file is never overwritten. One purge runs at a time
and `migrate` runs alone, so no second writer of a uuid races the check before the rename. `parse_manifest`
reads a file of the stream and raises `ManifestInvalid(reason)` for one a replay must not trust:
`not_json`, `unknown_format`, `unknown_op`, `invalid` (any other shape than the format's, an extra field
included) and `name_mismatch` (a file name other than `<manifest_uuid>.json`). The replay adds
`bound_missing` (`ManifestBoundMissing`, with the manifest's uuid, its kind and the key): a `max_ids` without
a key its replay reads, its typed operations' (`bound_keys`) or its kind's.

**The rows.** `insert_manifest` inserts the `executing` row with a UUIDv7, the request (ids only) and an empty
`scope`, `created_at` the caller's `now` (A0, with its fences: `fences.insert_fence`). `record_manifest` (A3)
stores the content in `scope` with the file's hash, once: a retry may record the same content again, other
content raises `ManifestConflict`; it adds the affected jobs to the manifest's active `source` fence. Nothing
is deleted before it commits. `complete_manifest` moves the row `executing → completed` (`transition()`); the
§7.14 CHECK refuses it without the file's hash. `read_manifest` reads a row, `read_manifest_file` a file
(the purge's adoption of a file a crash left before A3, M5 Task 12).

**`max_identity_ids`**: per table of the `public` schema, the greatest value of its identity column, or 0: a
column whose sequence it owns, an identity (`login_lockouts.generation`, the surrogate `api_idempotency.id`,
Task 1) or a `serial` (procrastinate's tables: Deviation 16, every id a manifest lists is one identity
advancement moves past). A table has one at most.

**Postconditions** (`check_postconditions`, §19.5's column). Every one must stay true under later normal
activity, since §19.5 replays a completed manifest whose postcondition fails: a selector names a property a
row is born with and never loses (`all`; `source:<id>` on `postings` and `fetch_requests`, through their
source instance, and on `ad_keys`, its `key_ns`), and is evaluated only within the operation's bound:

- `bound.by = "id"`: the table's identity column (`bound.column`, default `id`) `≤ max_ids[table]`;
  `"parent"`: a table without one, its column `bound.column` holding the parent's id `≤ max_ids[bound.table]`;
  `"created_at"`: `created_at ≤ fenced_at`; `"thread"` (the checkpoint tables): the owner a row's `thread_id`
  names (`THREAD_FORMS`: a run, a score cycle, a profile execution's operation) within its table's bound. A
  thread of any other form is a violation, never a silent pass. `ids` and `keep` name values of the bound's
  column, so they need an `id` or `parent` bound.
- `row_delete`: no named row; no row within the bound that matches the selector, apart from the kept ids.
- `field_redact`: every named row that still exists has the columns NULL and its marker set: a marker value of
  `MARKER_SET` means the column is not NULL (a time the redaction set: checked relative, never as an exact
  value a later save may change), any other value its text; for `exports.status` any content-free state of the
  table (`EXPORT_CONTENT_FREE`). For `jobs.current_revision_id`, which the rebuild sets again, the column
  instead names none of the revisions the manifest deletes (by id, or within the bound of an `all` selector
  and not among its kept ids).
- `file_remove`: no named file on the volume (`FileRoots`); a key that is no plain file name is a violation.
  A prefix key `<stem>-*` (a non-empty stem, the separator, `KEY_WILDCARD`) names every file of the volume
  whose name begins with `<stem>-` (an export's `<id>-*`: its published archive and an unpublished attempt's,
  M5 Task 12); a `*` anywhere else makes the key no file name. A directory is never a named file.
  `named_files` is the one reading of a key, which `remove_files` (a purge's or a replay's unlink) shares.
- `fence`: no fence of the manifest is active (a final-state condition: the completing transaction releases
  the fences first, then checks, and rolls back on a violation); for a `source` fence, for the stored affected
  jobs that still exist, no `field_provenance` entry or current-revision lineage row names a posting or
  observation of the source at or below the watermark, and no current `det_results` instance or golden case at
  or below it draws on the source. Data above the watermark is preserved and never counted. A source fence
  without its watermark cannot be verified, so it is a violation.
- A manifest that deletes `llm_calls` rows also has no `llm_spend_ledger` request entry within its bound
  `reserved` or `dispatched` without its call (G04): no reader could settle it any more.

A violation names the operation's index, its op, its table (`None` for a file) and a detail of counts only.
Table and column names come from the content; each is quoted as an identifier.
"""

import hashlib
import json
import os
import stat
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any, Final, Literal, NotRequired, TypedDict, assert_never, cast
from uuid import UUID

from pydantic import ConfigDict, TypeAdapter, ValidationError, with_config
from sqlalchemy.engine import Row
from sqlalchemy.ext.asyncio import AsyncConnection
from sqlalchemy.sql.elements import TextClause

from jobhunter.db.sql import q
from jobhunter.db.transitions import transition
from jobhunter.graphs.profile.threads import NUMBER
from jobhunter.graphs.profile.threads import THREAD_PATTERN as PROFILE_THREAD_PATTERN
from jobhunter.ids import uuid7
from jobhunter.profile.storage import fsync_directory
from jobhunter.types import JSONValue

MANIFEST_FORMAT: Final = 1
SELECTOR_ALL: Final = "all"
SOURCE_SELECTOR: Final = "source:"
# A `field_redact` marker value that means "set" (not NULL), not a value of its own (module docstring).
MARKER_SET: Final = "set"
# The wildcard of a `file_remove` prefix key `<stem>-*`, which names every file `<stem>-` begins (module
# docstring); a key may hold it nowhere else.
KEY_WILDCARD: Final = "*"
PREFIX_KEY_SUFFIX: Final = "-" + KEY_WILDCARD
# The states of an export that hold no content (Task 17's table): each satisfies an `exports.status` marker.
EXPORT_CONTENT_FREE: Final = ("purged", "expired", "failed")

ManifestKind = Literal[
    "delete_everything",
    "source_purge",
    "profile_purge",
    "retention_purge",
    "job_tombstone",
    "golden_case_delete",
]
ManifestStatus = Literal["executing", "completed", "shipped"]
FenceKind = Literal["global", "source", "profile", "jobs"]
InvalidReason = Literal[
    "not_json", "unknown_format", "unknown_op", "invalid", "name_mismatch", "bound_missing"
]

# Each TypedDict of the format refuses an extra field and a value of another type when a file is parsed.
_FORMAT: Final = ConfigDict(extra="forbid", strict=True)


@with_config(_FORMAT)
class Bound(TypedDict):
    by: Literal["id", "parent", "created_at", "thread"]
    table: NotRequired[str]
    column: NotRequired[str]


@with_config(_FORMAT)
class FenceOp(TypedDict):
    op: Literal["fence"]
    kind: FenceKind
    key: str
    job_ids: list[int]


@with_config(_FORMAT)
class RowDeleteOp(TypedDict):
    op: Literal["row_delete"]
    table: str
    ids: NotRequired[list[int]]
    selector: NotRequired[str]
    keep: NotRequired[list[int]]
    bound: Bound


@with_config(_FORMAT)
class FieldRedactOp(TypedDict):
    op: Literal["field_redact"]
    table: str
    ids: list[int]
    columns: list[str]
    marker: NotRequired[dict[str, str]]


@with_config(_FORMAT)
class FileRemoveOp(TypedDict):
    op: Literal["file_remove"]
    volume: Literal["cv_store", "exports"]
    keys: list[str]


Operation = FenceOp | RowDeleteOp | FieldRedactOp | FileRemoveOp
OPS: Final = frozenset({"fence", "row_delete", "field_redact", "file_remove"})


@with_config(_FORMAT)
class Affected(TypedDict):
    job_ids: list[int]
    profile_version_ids: list[int]
    tuple_ids: list[int]


@with_config(_FORMAT)
class Watermark(TypedDict):
    fenced_at: str
    max_ids: dict[str, int]


@with_config(_FORMAT)
class ManifestContent(TypedDict):
    format: Literal[1]
    manifest_uuid: str
    kind: ManifestKind
    admitted_at: str
    fenced_at: str
    max_ids: dict[str, int]
    operations: list[Operation]
    affected: NotRequired[Affected]
    watermark: NotRequired[Watermark]
    retained: NotRequired[dict[str, list[int]]]


_CONTENT: Final = TypeAdapter(ManifestContent)


class ManifestInvalid(Exception):
    """A manifest file a replay must not trust; `reason` says why (module docstring)."""

    def __init__(self, reason: InvalidReason) -> None:
        super().__init__(reason)
        self.reason: InvalidReason = reason


class ManifestBoundMissing(ManifestInvalid):
    """A manifest whose `max_ids` lacks a key its kind reads (`replay`, final review M3): its uuid, its kind
    and the key, which its message names too (ids and names only)."""

    def __init__(self, manifest_uuid: str, kind: str, key: str) -> None:
        super().__init__("bound_missing")
        self.args = (f"deletion manifest {manifest_uuid} ({kind}): max_ids has no bound for {key}",)
        self.manifest_uuid = manifest_uuid
        self.kind = kind
        self.key = key


class ManifestConflict(Exception):
    """A manifest's file or recorded content would change: never overwritten (module docstring)."""


# --- the file -----------------------------------------------------------------------------------------------


def canonical_bytes(content: ManifestContent) -> bytes:
    """The manifest's file bytes: canonical JSON, sorted keys, compact separators, UTF-8 (D2)."""
    return json.dumps(content, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def manifest_file_name(manifest_uuid: str | UUID) -> str:
    """The name of a manifest's file in the stream: `<manifest_uuid>.json`."""
    return f"{manifest_uuid}.json"


def _canonical_uuid(text: str) -> bool:
    try:
        return str(UUID(text)) == text
    except ValueError:
        return False


def _refuse_constant(name: str) -> None:
    raise ValueError(f"{name} is no JSON number")


def parse_manifest(data: bytes, *, file_name: str) -> ManifestContent:
    """The content of the manifest file `file_name` holding `data`; `ManifestInvalid` for a file that is not
    JSON (`NaN` and `Infinity` included), of another format, with an unknown op, of any other shape than the
    format's, or not named `<manifest_uuid>.json` with its uuid in canonical form."""
    try:
        raw: Any = json.loads(data.decode("utf-8"), parse_constant=_refuse_constant)
    except ValueError:  # `UnicodeDecodeError` and `JSONDecodeError` are both
        raise ManifestInvalid("not_json") from None
    if not isinstance(raw, dict):
        raise ManifestInvalid("invalid")
    declared = raw.get("format")
    if type(declared) is not int or declared != MANIFEST_FORMAT:
        raise ManifestInvalid("unknown_format")
    operations = raw.get("operations")
    if isinstance(operations, list) and any(
        isinstance(op, dict) and not (isinstance(op.get("op"), str) and op["op"] in OPS) for op in operations
    ):
        raise ManifestInvalid("unknown_op")
    try:
        content = _CONTENT.validate_python(raw)
    except ValidationError:
        raise ManifestInvalid("invalid") from None
    if not _canonical_uuid(content["manifest_uuid"]):
        raise ManifestInvalid("invalid")
    if file_name != manifest_file_name(content["manifest_uuid"]):
        raise ManifestInvalid("name_mismatch")
    return content


_FILE_MODE: Final = 0o600
_WRITE_FLAGS: Final = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW | os.O_CLOEXEC
_READ_FLAGS: Final = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC


def _existing(path: Path) -> bytes | None:
    try:
        fd = os.open(path, _READ_FLAGS)
    except FileNotFoundError:
        return None
    with os.fdopen(fd, "rb") as existing:
        return existing.read()


def read_manifest_file(directory: Path, manifest_uuid: str | UUID) -> bytes | None:
    """The bytes of the manifest's file in `directory`, `None` when there is none (a symbolic link is never
    followed: `OSError`)."""
    return _existing(directory / manifest_file_name(manifest_uuid))


def write_manifest_file(directory: Path, content: ManifestContent) -> bytes:
    """Write `content`'s file in `directory` and return its sha256 (module docstring): `<uuid>.json.tmp`
    (mode 0600; a leftover of a crashed write is truncated), fsync, rename to `<uuid>.json`, fsync of the
    directory. An existing file with the same bytes is adopted; one with other bytes raises
    `ManifestConflict`."""
    manifest_uuid = content["manifest_uuid"]
    if not _canonical_uuid(manifest_uuid):
        raise ValueError("a manifest's uuid names its file: it must be in canonical form")
    data = canonical_bytes(content)
    digest = hashlib.sha256(data).digest()
    name = manifest_file_name(manifest_uuid)
    final = directory / name
    existing = _existing(final)
    if existing is not None:
        if existing != data:
            raise ManifestConflict(f"manifest {manifest_uuid}: its file holds other bytes")
        fsync_directory(directory)  # its rename may not have reached the disk before a crash
        return digest
    tmp = directory / f"{name}.tmp"
    fd = os.open(tmp, _WRITE_FLAGS, _FILE_MODE)
    try:
        with os.fdopen(fd, "wb") as out:
            os.fchmod(out.fileno(), _FILE_MODE)  # whatever the umask
            out.write(data)
            out.flush()
            os.fsync(out.fileno())
        os.rename(tmp, final)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    fsync_directory(directory)
    return digest


# --- the rows -----------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class ManifestRow:
    """A `deletion_manifests` row; `scope` is `None` until A3 records the content."""

    id: int
    manifest_uuid: UUID
    kind: ManifestKind
    status: ManifestStatus
    request: Mapping[str, JSONValue]
    file_sha256: bytes | None
    scope: ManifestContent | None
    created_at: datetime
    completed_at: datetime | None
    shipped_at: datetime | None


_ROW: Final = (
    "id, manifest_uuid, kind, status, request, file_sha256, scope, created_at, completed_at, shipped_at"
)
_INSERT: Final = q(
    "INSERT INTO deletion_manifests (manifest_uuid, kind, status, request, created_at) "  # noqa: S608 (constant)
    f"VALUES (:uuid, :kind, 'executing', :request, :now) RETURNING {_ROW}",
    jsonb=["request"],
)
_READ: Final = q(f"SELECT {_ROW} FROM deletion_manifests WHERE id = :id")  # noqa: S608 (constant)
# A3's record, once: a retry may store the same hash (and so the same content) again.
_RECORD: Final = q(
    "UPDATE deletion_manifests SET file_sha256 = :sha, scope = :scope "
    "WHERE id = :id AND status = 'executing' AND (file_sha256 IS NULL OR file_sha256 = :sha) RETURNING id",
    jsonb=["scope"],
)
# The stored affected set added to the manifest's active source fence, distinct and in order.
_ADD_AFFECTED: Final = q(
    "UPDATE purge_fences SET job_ids = ARRAY(SELECT DISTINCT u.j "
    "FROM unnest(job_ids || CAST(:jobs AS bigint[])) AS u(j) ORDER BY u.j) "
    "WHERE manifest_id = :id AND kind = 'source' AND released_at IS NULL"
)
# Every column of `public` that owns its sequence: an identity (`i`) or a serial (`a`), as
# `pg_get_serial_sequence` finds them.
_IDENTITY_COLUMNS: Final = q(
    "SELECT c.relname AS table_name, a.attname AS column_name FROM pg_depend d "
    "JOIN pg_class s ON s.oid = d.objid AND s.relkind = 'S' "
    "JOIN pg_class c ON c.oid = d.refobjid AND c.relkind IN ('r', 'p') "
    "JOIN pg_namespace n ON n.oid = c.relnamespace AND n.nspname = 'public' "
    "JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum = d.refobjsubid "
    "WHERE d.classid = CAST('pg_class' AS regclass) AND d.refclassid = CAST('pg_class' AS regclass) "
    "AND d.deptype IN ('a', 'i') ORDER BY c.relname, a.attname"
)


def _manifest_row(row: Row[Any]) -> ManifestRow:
    return ManifestRow(
        id=int(row.id),
        manifest_uuid=row.manifest_uuid,
        kind=row.kind,
        status=row.status,
        request=row.request,
        file_sha256=None if row.file_sha256 is None else bytes(row.file_sha256),
        scope=cast(ManifestContent, row.scope) if row.scope else None,
        created_at=row.created_at,
        completed_at=row.completed_at,
        shipped_at=row.shipped_at,
    )


async def insert_manifest(
    conn: AsyncConnection, *, kind: ManifestKind, request: Mapping[str, JSONValue], now: datetime
) -> ManifestRow:
    """Insert an `executing` manifest of `kind` (a new UUIDv7, the request, an empty scope, `created_at =
    now`: the manifest's `admitted_at`), in the caller's transaction; the row."""
    params = {"uuid": uuid7(), "kind": kind, "request": dict(request), "now": now}
    return _manifest_row((await conn.execute(_INSERT, params)).one())


async def read_manifest(conn: AsyncConnection, manifest_id: int) -> ManifestRow | None:
    """The manifest `manifest_id` (a plain read), `None` when there is none."""
    row = (await conn.execute(_READ, {"id": manifest_id})).first()
    return None if row is None else _manifest_row(row)


async def identity_columns(conn: AsyncConnection) -> list[tuple[str, str]]:
    """Every `(table, column)` of `public` whose column owns its sequence, an identity or a serial (module
    docstring), by table; `RuntimeError` for a table with more than one."""
    columns = [(str(r.table_name), str(r.column_name)) for r in await conn.execute(_IDENTITY_COLUMNS)]
    tables = [table for table, _ in columns]
    if twice := sorted({table for table in tables if tables.count(table) > 1}):
        raise RuntimeError(f"tables with more than one identity column: {twice}")
    return columns


async def max_identity_ids(conn: AsyncConnection) -> dict[str, int]:
    """Per table of `public` with an identity column (a serial included), its greatest value, or 0 (module
    docstring), in one statement after the catalogue's."""
    columns = await identity_columns(conn)
    if not columns:
        return {}
    quote = conn.dialect.identifier_preparer.quote_identifier
    # Quoted identifiers of the catalogue only.
    maxima = ", ".join(
        f"(SELECT coalesce(max({quote(column)}), 0) FROM public.{quote(table)})"  # noqa: S608
        for table, column in columns
    )
    values = (await conn.execute(q(f"SELECT {maxima}"))).one()
    return {table: int(value) for (table, _), value in zip(columns, values, strict=True)}


async def record_manifest(
    conn: AsyncConnection, manifest: ManifestRow, content: ManifestContent, sha256: bytes
) -> None:
    """A3 (module docstring): `file_sha256 := sha256` and `scope := content` on the `executing` manifest,
    then `content`'s affected jobs added to its active source fence. `ValueError` for a content of another
    manifest or a hash that is not `canonical_bytes(content)`'s; `ManifestConflict` when the manifest has left
    `executing` or holds another file's hash."""
    if content["manifest_uuid"] != str(manifest.manifest_uuid) or content["kind"] != manifest.kind:
        raise ValueError("the content is another manifest's")
    if hashlib.sha256(canonical_bytes(content)).digest() != sha256:
        raise ValueError("the hash is not the content's file hash")
    params = {"id": manifest.id, "sha": sha256, "scope": dict(content)}
    if (await conn.execute(_RECORD, params)).first() is None:
        raise ManifestConflict(f"manifest {manifest.id}: not executing, or recorded with another file")
    affected = content.get("affected")
    if affected is not None and affected["job_ids"]:
        await conn.execute(_ADD_AFFECTED, {"id": manifest.id, "jobs": sorted(set(affected["job_ids"]))})


async def complete_manifest(conn: AsyncConnection, manifest_id: int) -> None:
    """`executing → completed` (`completed_at := now()`); `TransitionConflict` when it is not `executing`, and
    the §7.14 CHECK refuses a manifest without its file's hash."""
    await transition(conn, "deletion_manifests", manifest_id, ["executing"], "completed")


# --- the postconditions -------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Violation:
    """A failed postcondition: the operation's index and op, its table (`None` for a file), counts only."""

    op_index: int
    op: str
    table: str | None
    detail: str


@dataclass(frozen=True)
class FileRoots:
    """Where the volumes a `file_remove` names are mounted (`cfg.cv_store_dir`, `cfg.exports_dir`)."""

    cv_store: Path
    exports: Path

    def root(self, volume: Literal["cv_store", "exports"]) -> Path:
        """The directory `volume` is mounted at."""
        return self.cv_store if volume == "cv_store" else self.exports


@dataclass(frozen=True)
class ThreadForm:
    """A checkpoint thread name one writer gives (`runs.thread_id`, the score graph's and the profile graph's
    `thread_id`): its owner's table and a pattern, the same in Python's `re` and PostgreSQL's regular
    expressions, whose first group is the owner's id. A profile execution's form is its writer's own grammar
    (`graphs.profile.threads.THREAD_PATTERN`, built from the writer's prefix map), so a new thread kind there
    is a known form here; the run's and the cycle's have one shape each, pinned against their writers."""

    owner: str
    pattern: str


THREAD_FORMS: Final = (
    ThreadForm("runs", f"run-({NUMBER})-a{NUMBER}"),
    ThreadForm("score_cycles", f"cycle-({NUMBER})"),
    ThreadForm("operations", PROFILE_THREAD_PATTERN),
)
# The `source:<id>` selector of each table it may name: an immutable property of the row (module docstring).
_SOURCE_SELECTORS: Final[Mapping[str, str]] = MappingProxyType(
    {
        "postings": "EXISTS (SELECT 1 FROM source_instances si "
        "WHERE si.id = t.source_instance_id AND si.source_id = :selector_source)",
        "fetch_requests": "EXISTS (SELECT 1 FROM source_instances si "
        "WHERE si.id = t.source_instance_id AND si.source_id = :selector_source)",
        "ad_keys": "t.key_ns = :selector_source",
    }
)
_ACTIVE_FENCES: Final = q(
    "SELECT count(*) FROM purge_fences f JOIN deletion_manifests m ON m.id = f.manifest_id "
    "WHERE m.manifest_uuid = CAST(:uuid AS uuid) AND f.released_at IS NULL"
)
# A source fence's bounded checks over the stored affected jobs that still exist (module docstring).
_SOURCE_DRAWS: Final = q(
    "WITH a AS (SELECT j.id, j.current_revision_id, j.field_provenance FROM jobs j "
    "WHERE j.id = ANY(CAST(:jobs AS bigint[]))) "
    "SELECT (SELECT count(*) FROM a CROSS JOIN LATERAL jsonb_each(a.field_provenance) AS e(field, entry) "
    "JOIN source_instances si ON si.id = CAST(e.entry ->> 'source_instance_id' AS bigint) "
    "WHERE si.source_id = :source "
    "AND (coalesce(CAST(e.entry ->> 'posting_id' AS bigint) <= :postings, false) "
    "OR coalesce(CAST(e.entry ->> 'observation_id' AS bigint) <= :observations, false))) AS provenance, "
    "(SELECT count(*) FROM a JOIN job_revision_sources s ON s.job_revision_id = a.current_revision_id "
    "WHERE s.source_id = :source AND (coalesce(s.posting_id <= :postings, false) "
    "OR coalesce(s.observation_id <= :observations, false))) AS lineage, "
    "(SELECT count(*) FROM det_results d WHERE d.job_id IN (SELECT id FROM a) AND d.is_current "
    "AND d.id <= :det_results AND jsonb_path_exists(d.det_inputs, '$.job.*.sources[*] ? (@ == $source)', "
    "jsonb_build_object('source', CAST(:source AS text)))) AS instances, "
    "(SELECT count(*) FROM golden_cases g JOIN job_revisions r ON r.id = g.job_revision_id "
    "WHERE r.job_id IN (SELECT id FROM a) AND g.id <= :golden_cases "
    "AND CAST(:source AS text) = ANY(g.det_input_sources)) AS golden"
)
# The watermark's bound of each table the checks read, by the bind that carries it.
_WATERMARK_BINDS: Final[Mapping[str, str]] = MappingProxyType(
    {
        "postings": "postings",
        "observations": "posting_observations",
        "det_results": "det_results",
        "golden_cases": "golden_cases",
    }
)
# G04: request entries within the bound that no reader can settle, their call deleted.
_UNSETTLED_WITHOUT_CALL: Final = q(
    "SELECT count(*) FROM llm_spend_ledger l WHERE l.kind = 'request' "
    "AND l.state IN ('reserved', 'dispatched') AND l.id <= :bound "
    "AND NOT EXISTS (SELECT 1 FROM llm_calls c WHERE c.spend_ledger_id = l.id)"
)


Quote = Callable[[str], str]


def _rows(n: int, noun: str = "row", plural: str | None = None) -> str:
    return f"{n} {noun}" if n == 1 else f"{n} {plural or noun + 's'}"


def _remain(n: int) -> str:
    return "remains" if n == 1 else "remain"


def _instant(text: str) -> datetime | None:
    """`text` as a time with its zone, `None` when it is none."""
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    return None if moment.tzinfo is None else moment


async def _count(conn: AsyncConnection, statement: str | TextClause, params: Mapping[str, object]) -> int:
    # A built statement's identifiers are quoted names of the content; every value is a bind.
    clause = q(statement) if isinstance(statement, str) else statement
    return int((await conn.execute(clause, dict(params))).scalar_one())


def _key_column(bound: Bound) -> str | None:
    """The column `ids` and `keep` name: the identity of an `id` bound, the parent's id of a `parent` one."""
    if bound["by"] == "id":
        return bound.get("column", "id")
    if bound["by"] == "parent":
        return bound.get("column")
    return None


def _thread_forms() -> dict[str, object]:
    """The binds `form_<i>` of `THREAD_FORMS`, each anchored to the whole name."""
    return {f"form_{i}": f"^(?:{form.pattern})$" for i, form in enumerate(THREAD_FORMS)}


def thread_bound_sql(
    max_ids: Mapping[str, int], *, column: str = "thread_id"
) -> tuple[str, dict[str, object]]:
    """The predicate "the checkpoint thread `column` (a SQL expression of the caller's code, never content)
    lies within the bound": its name has a form of `THREAD_FORMS` whose owner id is at or below
    `max_ids[form.owner]`; any other name is false. With its binds (`form_<i>`, `owner_<i>`). The `thread`
    bound's postcondition and delete-everything's deletion of the threads share it (M5 Task 16).
    `ValueError` when `max_ids` has no bound for an owner table."""
    if missing := [form.owner for form in THREAD_FORMS if form.owner not in max_ids]:
        raise ValueError(f"no max_ids bound for {missing[0]}")
    params = _thread_forms()
    cases: list[str] = []
    for i, form in enumerate(THREAD_FORMS):
        params[f"owner_{i}"] = max_ids[form.owner]
        cases.append(
            f"WHEN {column} ~ :form_{i} "
            f"THEN CAST((regexp_match({column}, :form_{i}))[1] AS numeric) <= :owner_{i}"
        )
    return f"CASE {' '.join(cases)} ELSE false END", params


def _within(
    bound: Bound, table: str, content: ManifestContent, quote: Quote
) -> tuple[str, dict[str, object]] | str:
    """The predicate over `t` of `bound` with its binds, or why it cannot be evaluated."""
    max_ids = content["max_ids"]
    by = bound["by"]
    if by == "id" or by == "parent":
        owner = table if by == "id" else bound.get("table")
        column = _key_column(bound)
        if owner is None or column is None:
            return "a parent bound names its table and its column"
        if owner not in max_ids:
            return f"no max_ids bound for {owner}"
        return f"t.{quote(column)} <= :bound_max", {"bound_max": max_ids[owner]}
    if by == "created_at":
        fenced_at = _instant(content["fenced_at"])
        if fenced_at is None:
            return "fenced_at is no time with its zone"
        return "t.created_at <= :bound_fenced_at", {"bound_fenced_at": fenced_at}
    if by == "thread":
        try:
            return thread_bound_sql(max_ids, column="t.thread_id")
        except ValueError as unbounded:
            return str(unbounded)
    assert_never(by)


def _selected(selector: str, table: str) -> tuple[str, dict[str, object]] | None:
    """The predicate over `t` of an immutable `selector` with its binds; `None` for one `table` has not."""
    if selector == SELECTOR_ALL:
        return "true", {}
    if selector.startswith(SOURCE_SELECTOR) and table in _SOURCE_SELECTORS:
        return _SOURCE_SELECTORS[table], {"selector_source": selector.removeprefix(SOURCE_SELECTOR)}
    return None


async def _row_delete(
    conn: AsyncConnection, index: int, op: RowDeleteOp, content: ManifestContent
) -> list[Violation]:
    table, bound = op["table"], op["bound"]
    quote = conn.dialect.identifier_preparer.quote_identifier
    rows = f"public.{quote(table)} AS t"
    key = _key_column(bound)
    ids, keep = op.get("ids", []), op.get("keep", [])
    found: list[str] = []
    if "ids" not in op and "selector" not in op:
        found.append("it names neither ids nor a selector")
    if (ids or keep) and key is None:
        found.append(f"ids name no row under a {bound['by']} bound")
    if ids and key is not None:
        sql = f"SELECT count(*) FROM {rows} WHERE t.{quote(key)} = ANY(CAST(:ids AS bigint[]))"  # noqa: S608
        if n := await _count(conn, sql, {"ids": ids}):
            found.append(f"{_rows(n)} named by id {_remain(n)}")
    if "selector" in op:
        within = _within(bound, table, content, quote)
        selected = _selected(op["selector"], table)
        if isinstance(within, str):
            found.append(within)
        elif selected is None:
            found.append(f"the selector {op['selector']} does not apply to {table}")
        elif key is not None or not keep:  # a keep without a key column is reported above
            (bounded, params), (matching, selector_params) = within, selected
            kept = ""
            if keep and key is not None:
                kept = f" AND NOT coalesce(t.{quote(key)} = ANY(CAST(:keep AS bigint[])), false)"
                params = {**params, "keep": keep}
            sql = f"SELECT count(*) FROM {rows} WHERE ({bounded}) AND ({matching}){kept}"  # noqa: S608
            if n := await _count(conn, sql, {**params, **selector_params}):
                found.append(f"{_rows(n)} within the bound {_remain(n)}")
        if bound["by"] == "thread":
            known = " OR ".join(f"t.thread_id ~ :form_{i}" for i in range(len(THREAD_FORMS)))
            sql = f"SELECT count(*) FROM {rows} WHERE NOT ({known})"  # noqa: S608
            if n := await _count(conn, sql, _thread_forms()):
                found.append(f"{_rows(n)} of an unknown thread form")
    return [Violation(index, "row_delete", table, detail) for detail in found]


@dataclass(frozen=True)
class _DeletedRevisions:
    """The revisions a manifest deletes: `ids` by id, and with an `all` selector every one up to `bound`
    except the `kept` ones."""

    ids: list[int]
    bound: int | None
    kept: list[int]


def _deleted_revisions(content: ManifestContent) -> _DeletedRevisions | str:
    """The revisions the manifest deletes, or why they are unknown."""
    ids: set[int] = set()
    kept: set[int] = set()
    bound: int | None = None
    for op in content["operations"]:
        if op["op"] != "row_delete" or op["table"] != "job_revisions":
            continue
        ids.update(op.get("ids", []))
        if "selector" in op:
            if op["selector"] != SELECTOR_ALL or op["bound"]["by"] != "id":
                return "job_revisions are deleted by a selector the pointer cannot be checked against"
            if "job_revisions" not in content["max_ids"]:
                return "no max_ids bound for job_revisions"
            bound = content["max_ids"]["job_revisions"]
            kept.update(op.get("keep", []))
    return _DeletedRevisions(sorted(ids), bound, sorted(kept))


async def _field_redact(
    conn: AsyncConnection, index: int, op: FieldRedactOp, content: ManifestContent
) -> list[Violation]:
    table = op["table"]
    if not op["ids"]:
        return []
    quote = conn.dialect.identifier_preparer.quote_identifier
    conditions: list[str] = []
    params: dict[str, object] = {"ids": op["ids"]}
    for column in op["columns"]:
        if (table, column) != ("jobs", "current_revision_id"):
            conditions.append(f"t.{quote(column)} IS NULL")
            continue
        deleted = _deleted_revisions(content)
        if isinstance(deleted, str):
            return [Violation(index, "field_redact", table, deleted)]
        params["deleted_ids"] = deleted.ids
        names = ["coalesce(t.current_revision_id = ANY(CAST(:deleted_ids AS bigint[])), false)"]
        if deleted.bound is not None:
            params["deleted_max"], params["kept_revisions"] = deleted.bound, deleted.kept
            names.append(
                "(coalesce(t.current_revision_id <= :deleted_max, false) "
                "AND NOT coalesce(t.current_revision_id = ANY(CAST(:kept_revisions AS bigint[])), false))"
            )
        conditions.append(f"NOT ({' OR '.join(names)})")
    for i, (column, value) in enumerate(sorted(op.get("marker", {}).items())):
        if (table, column) == ("exports", "status"):
            params["content_free"] = list(EXPORT_CONTENT_FREE)
            conditions.append("t.status = ANY(CAST(:content_free AS text[]))")
        elif value == MARKER_SET:
            conditions.append(f"t.{quote(column)} IS NOT NULL")
        else:
            params[f"marker_{i}"] = value
            conditions.append(f"CAST(t.{quote(column)} AS text) = :marker_{i}")
    redacted = " AND ".join(conditions) or "true"
    sql = (
        f"SELECT count(*) FROM public.{quote(table)} AS t "  # noqa: S608 (quoted names of the content)
        f"WHERE t.id = ANY(CAST(:ids AS bigint[])) AND NOT coalesce(({redacted}), false)"
    )
    if n := await _count(conn, sql, params):
        return [Violation(index, "field_redact", table, f"{_rows(n)} not redacted")]
    return []


def _plain_name(key: str) -> bool:
    return key not in ("", ".", "..") and "/" not in key and "\x00" not in key


def _valid_key(key: str) -> bool:
    """A plain file name without a wildcard, or a prefix key `<stem>-*`: a non-empty plain stem, then the
    separator and `KEY_WILDCARD`, so `5-*` never names a file of `50-`."""
    if not key.endswith(PREFIX_KEY_SUFFIX):
        return _plain_name(key) and KEY_WILDCARD not in key
    stem = key.removesuffix(PREFIX_KEY_SUFFIX)
    return stem != "" and _plain_name(stem) and KEY_WILDCARD not in stem


def _no_directory(path: Path) -> bool:
    """Whether `path` exists and is no directory (a symbolic link is never followed)."""
    try:
        return not stat.S_ISDIR(os.lstat(path).st_mode)
    except FileNotFoundError:
        return False


def named_files(root: Path, key: str) -> list[Path]:
    """The entries of `root` the `file_remove` key `key` names (module docstring), in name order: the file of
    a plain name when it exists, or every entry whose name begins with a prefix key's `<stem>-`; a directory
    is never one. `ValueError` for a key that is neither."""
    if not _valid_key(key):
        raise ValueError("a file_remove key is a file name or a prefix key <stem>-*")
    if not key.endswith(PREFIX_KEY_SUFFIX):
        return [root / key] if _no_directory(root / key) else []
    prefix = key.removesuffix(KEY_WILDCARD)
    try:
        names = os.listdir(root)
    except FileNotFoundError:
        return []
    return [root / name for name in sorted(names) if name.startswith(prefix) and _no_directory(root / name)]


def remove_files(root: Path, keys: Sequence[str]) -> int:
    """Unlink every file of `root` the keys name (`named_files`; one gone meanwhile is fine), then fsync the
    directory when one went; how many were unlinked."""
    removed = 0
    for key in keys:
        for path in named_files(root, key):
            try:
                path.unlink()
            except FileNotFoundError:
                continue
            removed += 1
    if removed:
        fsync_directory(root)
    return removed


def _file_remove(index: int, op: FileRemoveOp, files: FileRoots) -> list[Violation]:
    root = files.root(op["volume"])
    found: list[str] = []
    if odd := sum(not _valid_key(key) for key in op["keys"]):
        found.append(f"{_rows(odd, 'key')} on {op['volume']} {'is' if odd == 1 else 'are'} no file name")
    if present := sum(len(named_files(root, key)) for key in op["keys"] if _valid_key(key)):
        found.append(f"{_rows(present, 'file')} on {op['volume']} {_remain(present)}")
    return [Violation(index, "file_remove", None, detail) for detail in found]


async def _fence(conn: AsyncConnection, index: int, op: FenceOp, content: ManifestContent) -> list[Violation]:
    found: list[Violation] = []
    if n := await _count(conn, _ACTIVE_FENCES, {"uuid": content["manifest_uuid"]}):
        found.append(Violation(index, "fence", "purge_fences", f"{_rows(n, 'fence')} of the manifest active"))
    if op["kind"] != "source":
        return found
    watermark = content.get("watermark")
    if watermark is None:
        return [*found, Violation(index, "fence", "jobs", "a source fence without its watermark")]
    if missing := [table for table in _WATERMARK_BINDS.values() if table not in watermark["max_ids"]]:
        return [*found, Violation(index, "fence", "jobs", f"the watermark has no bound for {missing}")]
    affected = content.get("affected")
    jobs = sorted(set(op["job_ids"]) | set(affected["job_ids"] if affected is not None else []))
    if not jobs:
        return found
    params = {
        "jobs": jobs,
        "source": op["key"],
        **{bind: watermark["max_ids"][table] for bind, table in _WATERMARK_BINDS.items()},
    }
    draws = (await conn.execute(_SOURCE_DRAWS, params)).one()
    for table, n, what, plural in (
        ("jobs", draws.provenance, "field provenance entry", "field provenance entries"),
        ("job_revision_sources", draws.lineage, "current-revision lineage row", None),
        ("det_results", draws.instances, "current det_results instance", None),
        ("golden_cases", draws.golden, "golden case", None),
    ):
        if n:
            detail = f"{_rows(int(n), what, plural)} at or below the watermark drawing on the purged source"
            found.append(Violation(index, "fence", table, detail))
    return found


async def _ledger(conn: AsyncConnection, content: ManifestContent) -> list[Violation]:
    deleting = [
        index
        for index, op in enumerate(content["operations"])
        if op["op"] == "row_delete" and op["table"] == "llm_calls"
    ]
    if not deleting:
        return []
    bound = content["max_ids"].get("llm_spend_ledger")
    if bound is None:
        return [
            Violation(deleting[0], "row_delete", "llm_spend_ledger", "no max_ids bound for llm_spend_ledger")
        ]
    if n := await _count(conn, _UNSETTLED_WITHOUT_CALL, {"bound": bound}):
        detail = f"{_rows(n, 'request entry', 'request entries')} reserved or dispatched without its call"
        return [Violation(deleting[0], "row_delete", "llm_spend_ledger", detail)]
    return []


def bound_keys(content: ManifestContent) -> frozenset[str]:
    """The `max_ids` keys `content`'s typed operations read when their postconditions are checked: each
    selector's bound (its table's own for an `id` bound, its parent's for a `parent` one, every `THREAD_FORMS`
    owner's for a `thread` one; a `created_at` bound reads none, nor do ids) and, for a manifest that deletes
    `llm_calls`, `llm_spend_ledger` (G04). `replay` refuses a manifest that lacks one before it starts."""
    keys: set[str] = set()
    for op in content["operations"]:
        if op["op"] != "row_delete":
            continue
        if op["table"] == "llm_calls":
            keys.add("llm_spend_ledger")
        if "selector" not in op:
            continue
        bound = op["bound"]
        if bound["by"] == "id":
            keys.add(op["table"])
        elif bound["by"] == "parent" and "table" in bound:
            keys.add(bound["table"])
        elif bound["by"] == "thread":
            keys.update(form.owner for form in THREAD_FORMS)
    return frozenset(keys)


async def check_postconditions(
    conn: AsyncConnection, content: ManifestContent, *, files: FileRoots
) -> list[Violation]:
    """Every failed postcondition of `content`'s operations, in operation order, on the caller's connection
    (module docstring); an empty list when every one holds."""
    found: list[Violation] = []
    for index, op in enumerate(content["operations"]):
        if op["op"] == "row_delete":
            found += await _row_delete(conn, index, op, content)
        elif op["op"] == "field_redact":
            found += await _field_redact(conn, index, op, content)
        elif op["op"] == "file_remove":
            found += _file_remove(index, op, files)
        else:
            found += await _fence(conn, index, op, content)
    return found + await _ledger(conn, content)
