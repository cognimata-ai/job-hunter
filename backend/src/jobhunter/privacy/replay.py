"""Migrate step 7: the two-phase replay of the deletion-manifest stream (spec §19.5 "Replay rules", §19.3
step 7, §19.4 `manifests replay`, §20.4, §20.5 step 6; plan M5 Task 25, Deviations 16, 46, 53, 55 and 56,
Ruling T25-1).

**The classification** (`classify_stream`, §19.5's table) covers, in `manifest_uuid` order, the stream and
every `deletion_manifests` row. The stream is read by `identities.read_stream`, the one strict reader step 1
uses: a file that does not parse or is named for another uuid raises `ManifestInvalid`, a directory that
does not exist `FileNotFoundError`, and either stops `migrate`. A file of a row with another kind, or whose
bytes do not hash to the row's `file_sha256`, raises `ManifestConflict`; a row's `scope` is read as step 1
reads it (`parse_manifest` of its canonical bytes, then that hash).
- no row with the uuid (the manifest is newer than the dump): `replayed`; the row is inserted from the file as
  `executing`, `created_at` its `admitted_at` and its request rebuilt from the content (ids only, Deviation
  53: the source, the version, the case, the jobs or the idempotency row the content names);
- an `executing` row and its file in the stream: `replayed`. A row without `file_sha256` adopts the file (a
  crash after the rename, before A3): the file's content is recorded, never recomputed;
- an `executing` row without its file: `pending_retry`. With `file_sha256` its content is `scope` (step 1
  rewrites its file first; a replay without step 1 writes it from `scope`); without, nothing was deleted
  under it, and its content is computed in phase A under the restored fence (§19.5 step 3);
- a `completed` or `shipped` row (its content the file's, else `scope`): `satisfied` while every
  postcondition holds (`check_postconditions` on the volumes `files`, and the kind's) and no fence of it
  stands (none does once a manifest completed: one standing is a replay that stopped before its phase C);
  otherwise `checked`, and replayed again. Without `files` no `file_remove` can be verified: a manifest with
  one is `checked`.

**The bounds** (final review M3, M8): before any postcondition is read or anything written, the content of
every manifest of the stream and of every recorded row is checked for the `max_ids` keys its replay reads
(`required_bounds`): those its kind's procedure reads (`KindProcedure.max_ids_keys`, which each kind declares
beside the code that reads them) and those its typed operations' selectors read (`manifests.bound_keys`). A
manifest that lacks one (a release whose procedure reads a table an older release's manifest never bounded)
raises `ManifestBoundMissing`, a `ManifestInvalid` naming the manifest's uuid, its kind and the first missing
key, logged `replay_bound_missing` with the three, and `migrate` stops as for any refused file
(`migrate_failed`, the step and the error's class), every manifest unreplayed. A pending retry without a hash
has no content yet: it is computed under the restored schema, every key present.

**The replay** (`replay_stream`) ends at once when every manifest is `satisfied`. Otherwise:
- **phase A** (Ruling T25-3: each manifest's effects at its place in the stream). One transaction under
  `active_config FOR UPDATE` (§3.3.3 step 2): the rows the database lacks inserted; every replayed manifest's
  fences made active (an active fence of the manifest kept, its job ids widened to the stored ones; one
  reconstructed from the content's `fence` operations, delete-everything's `global`, or a pending retry's
  request, where none stands); an adopted file recorded (`record_manifest`, which adds the stored affected
  jobs to the source fence); and the replay recorded in each replayed manifest's `replay_log` as `{restore_id,
  at, phase: "started", high_water}`, `high_water` each table's greatest id when the replay began
  (`max_identity_ids`, before any row of the replay: every row above it is the replay's own). A `migrate`
  after a crash resumes the replay a `started` entry names that no completion entry closes: its restore id
  and its high water, never new ones. Then, for each manifest in stream order: its admission, in a
  transaction of its own under `active_config FOR UPDATE`: each table's greatest id (its turn), its kind's
  `reapply_admission` (the source purge's disabling settings version, `purge_fence_at` and run cancels; a
  no-op for the other kinds) and `{restore_id, at, phase: "admitted", turn}` in its `replay_log`, which a
  resumed replay reads instead of re-applying the admission over the effects of the manifests after it; for a
  pending retry, its completion from its row: without a hash its content computed (`purge.compute_content`,
  the cutoff of the fences read once after the first transaction; `api` and `worker` are stopped, so there
  is no quiesce), checked as the file will be read (`purge.unparsable`), written (`write_manifest_file`: tmp,
  fsync, rename) and recorded; with one, its file written from `scope`; then its procedure (`PurgeRun.replay`:
  no effects, no owner, the volumes `migrate` was given, and the replay's own rows before its turn,
  `replayed_rows`: per table `(high_water, turn]`, which delete-everything's bounded selectors also cover,
  so a settings version an earlier replayed source purge re-created, or a tombstone an earlier tombstoning
  re-created, goes as its live counterpart went), and for a kind that deletes exports its export ops right
  after it (`deletion_graph.purge_exports`), as the live handler's step 3 runs them. A source purge leaves
  its rebuild to phase B (its replay mode deletes its tainted revisions instead): no rebuild runs between
  manifests; after each manifest's execution the fault point `privacy.replay.after_execute`
  (`AFTER_EXECUTE`), between two manifests of the stream. Then the fault point `privacy.replay.after_a`
  (`AFTER_A`);
- **phase B.** The stored affected jobs of every replayed source purge that phase A left, each followed up
  `merged_into_job_id` to its root, are rebuilt once per root by `source_purge.rebuild_jobs`, with the run
  of the latest replayed source purge whose stored set holds a job of that root (T15-4 (1)); each rebuild
  recomputes the job's current `det_results` instances for the active and the pending scope. Then the
  fault point `privacy.replay.after_b` (`AFTER_B`);
- **phase C.** One transaction: each kind's writes of the completing transaction (`CompletingKind.complete`,
  delete-everything's sessions), every fence of the replayed manifests released, then every postcondition
  of every replayed manifest checked (the `fence` operations' "no active fence" holds now; Task 12's order).
  A violation rolls it back, so every fence stays active; it is logged (`replay_postcondition_failed`: ids,
  ops, tables and counts) and raises `ReplayFailed`, which stops `migrate`. Otherwise every `executing`
  manifest becomes `completed`, and every replayed one's `replay_log` gains its completion entry
  `{restore_id, at, counts}` (the replay's id, its `now`, and the counts of the manifest's replayed
  operations by op, of the exports it purged and of the jobs its run rebuilt).

A crash anywhere restarts step 7 from the stored sets: the writes of phase A are idempotent (an inserted or
recorded manifest is then an `executing` row with its file, a reconstructed fence an active one, an admission
recorded once), the procedures re-run on what is left within the same replay's marks, and phase C is all or
nothing. The replay writes a manifest file only for a pending retry, and never over a valid one
(`write_manifest_file` adopts equal bytes and refuses others). It changes the database and the volumes only
(Deviation 56): it needs no Anthropic or healthchecks secret, and delete-everything's recorded batch cancels
are dropped (`batch_cancel_skipped_replay`).

**Work in flight in the dump** (Ruling T25-2 (8)) never makes a replayed kind wait: nothing can end it in a
restore. The kinds' waits and give-ups, by site:
- `golden_case` (a running eval run includes the case, `PurgeWait('eval_run_running')`): the dump's run is
  ended as the `eval_run` failure hook ends an interrupted run (`fail_eval_run`), logged with its id, and the
  case deleted;
- `source_purge`'s frozen embeddings (`PurgeWait('embedding_reused')`, a revision taking a frozen embedding
  between rounds) and `PurgeRun.chunk` (`CoMemberChanged`, co-members joining a group under its lock, three
  rounds running): each needs a writer running beside the purge, which no consistent dump holds and no
  replay has; they keep stopping `migrate`, the fences active.
Ids, counts and codes only reach a log line.
"""

import dataclasses
import hashlib
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Final, Literal, cast
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from jobhunter.db.sql import q
from jobhunter.faults import afault_point
from jobhunter.ids import uuid7
from jobhunter.observability.logging import get_logger
from jobhunter.privacy.deletion_graph import is_export_op, purge_exports
from jobhunter.privacy.fences import FenceSpec, fence_committed_at, insert_fence, release_fences
from jobhunter.privacy.identities import read_stream
from jobhunter.privacy.kinds import delete_everything, golden_case, job_tombstone, profile_purge, source_purge
from jobhunter.privacy.manifests import (
    FileRoots,
    ManifestBoundMissing,
    ManifestConflict,
    ManifestContent,
    ManifestRow,
    Operation,
    bound_keys,
    canonical_bytes,
    check_postconditions,
    complete_manifest,
    manifest_file_name,
    max_identity_ids,
    parse_manifest,
    read_manifest,
    record_manifest,
    write_manifest_file,
)
from jobhunter.privacy.purge import (
    KINDS,
    CompletingKind,
    KindProcedure,
    PurgeRun,
    compute_content,
    unparsable,
)
from jobhunter.types import JSONValue

if TYPE_CHECKING:
    from jobhunter.config import AppConfig

# The fault points (module docstring): after each manifest's execution in phase A, after phase A, after
# phase B.
AFTER_EXECUTE: Final = "privacy.replay.after_execute"
AFTER_A: Final = "privacy.replay.after_a"
AFTER_B: Final = "privacy.replay.after_b"
LOGGED_IDS: Final = 20
# The phases of a replay's `replay_log` entries before its completion entry (module docstring).
STARTED: Final = "started"
ADMITTED: Final = "admitted"

Outcome = Literal["replayed", "pending_retry", "checked", "satisfied"]

_ROWS: Final = q("SELECT id FROM deletion_manifests ORDER BY manifest_uuid")
_ACTIVE_FENCES_OF: Final = q(
    "SELECT count(*) FROM purge_fences WHERE manifest_id = :m AND released_at IS NULL"
)
_CONFIG_FOR_UPDATE: Final = q("SELECT 1 FROM active_config WHERE id = 1 FOR UPDATE")
_INSERT: Final = q(
    "INSERT INTO deletion_manifests (manifest_uuid, kind, status, scope, file_sha256, request, created_at) "
    "VALUES (:uuid, :kind, 'executing', :scope, :sha, :request, :admitted_at) RETURNING id",
    jsonb=["scope", "request"],
)
_ACTIVE_FENCE: Final = q(
    "SELECT id FROM purge_fences WHERE manifest_id = :m AND kind = :kind AND released_at IS NULL "
    "ORDER BY id LIMIT 1"
)
_WIDEN_FENCE: Final = q(
    "UPDATE purge_fences SET job_ids = ARRAY(SELECT DISTINCT u.j "
    "FROM unnest(job_ids || CAST(:jobs AS bigint[])) AS u(j) ORDER BY u.j) WHERE id = :id"
)
# Each stored job that still exists with its root (followed up `merged_into_job_id`).
_ROOTS: Final = q(
    "WITH RECURSIVE up(start, id, parent) AS (SELECT j.id, j.id, j.merged_into_job_id FROM jobs j "
    "WHERE j.id = ANY(CAST(:jobs AS bigint[])) "
    "UNION SELECT up.start, p.id, p.merged_into_job_id FROM jobs p JOIN up ON p.id = up.parent) "
    "SELECT start AS job_id, id AS root_id FROM up WHERE parent IS NULL ORDER BY start"
)
_REPLAY_LOGS: Final = q(
    "SELECT id, replay_log FROM deletion_manifests WHERE id = ANY(CAST(:ids AS bigint[])) ORDER BY id"
)
_LOG_REPLAY: Final = q(
    "UPDATE deletion_manifests SET replay_log = replay_log || jsonb_build_array(:entry) WHERE id = :id",
    jsonb=["entry"],
)
# The volumes of a classification without `files`: never read, since a content with a `file_remove` is then
# `checked` before its postconditions are (module docstring).
_UNREAD: Final = FileRoots(cv_store=Path("/nonexistent/cv_store"), exports=Path("/nonexistent/exports"))

log = get_logger("jobhunter.privacy.replay")


@dataclass(frozen=True)
class Classified:
    """A manifest of the stream or the database with its row of §19.5's table (module docstring)."""

    manifest_uuid: UUID
    outcome: Outcome


@dataclass(frozen=True)
class ReplayReport:
    """What a replay did: the manifests replayed (pending retries included), the pending retries completed
    from their rows, and the roots its phase B rebuilt."""

    replayed: int
    pending_completed: int
    rebuilt_jobs: int


class ReplayFailed(Exception):
    """A replay that cannot complete: a postcondition failed in phase C (rolled back, every fence active), or
    a manifest of no known kind; `migrate` stops."""


@dataclass(frozen=True)
class _Entry:
    """A classified manifest: its content (`None` for a pending retry without one) and its row (`None` for a
    manifest newer than the dump)."""

    manifest_uuid: UUID
    outcome: Outcome
    content: ManifestContent | None
    row: ManifestRow | None


@dataclass(frozen=True)
class _Restore:
    """One replay of the stream, new or resumed after a crash: its id and each table's greatest id when it
    began (`high_water`: a row above it is the replay's own)."""

    restore_id: str
    high_water: Mapping[str, int]


@dataclass
class _Replayed:
    """A manifest in the replay: its row (read again as each phase writes it), its content (set once a pending
    retry's is), its procedure, its turn (each table's greatest id when its admission began, once recorded)
    and the counts its completion entry carries."""

    row: ManifestRow
    content: ManifestContent | None
    proc: KindProcedure
    pending: bool
    turn: dict[str, int] | None = None
    counts: dict[str, int] = field(default_factory=dict)

    @property
    def stored(self) -> ManifestContent:
        if self.content is None:
            raise RuntimeError(f"deletion manifest {self.row.id}: replayed without its content")
        return self.content


# ---- the classification ------------------------------------------------------------------------------------


def _sha256(content: ManifestContent) -> bytes:
    return hashlib.sha256(canonical_bytes(content)).digest()


def _recorded_content(row: ManifestRow) -> ManifestContent | None:
    """The content a row records (its `scope` with its hash), read as step 1 reads it; `None` without one."""
    if row.file_sha256 is None or row.scope is None:
        return None
    content = parse_manifest(canonical_bytes(row.scope), file_name=manifest_file_name(row.manifest_uuid))
    if _sha256(content) != row.file_sha256:
        raise ManifestConflict(f"manifest {row.manifest_uuid}: its scope does not hash to its file_sha256")
    return content


def _same_manifest(row: ManifestRow, content: ManifestContent) -> None:
    """`ManifestConflict` when the stream's file is not the row's manifest (module docstring)."""
    if content["kind"] != row.kind:
        raise ManifestConflict(f"manifest {row.manifest_uuid}: its file is of another kind")
    if row.file_sha256 is not None and _sha256(content) != row.file_sha256:
        raise ManifestConflict(f"manifest {row.manifest_uuid}: its file does not hash to its file_sha256")


async def _rows(conn: AsyncConnection) -> list[ManifestRow]:
    rows: list[ManifestRow] = []
    for manifest_id in (await conn.execute(_ROWS)).scalars():
        row = await read_manifest(conn, int(manifest_id))
        if row is not None:
            rows.append(row)
    return rows


async def _holds(
    conn: AsyncConnection, row: ManifestRow, content: ManifestContent, files: FileRoots | None
) -> bool:
    """Whether a completed or shipped manifest is satisfied (module docstring)."""
    if (await conn.execute(_ACTIVE_FENCES_OF, {"m": row.id})).scalar_one():
        return False
    if files is None and any(op["op"] == "file_remove" for op in content["operations"]):
        return False
    proc = KINDS.get(row.kind)
    if proc is None:
        return False
    found = await check_postconditions(conn, content, files=_UNREAD if files is None else files)
    return not found and not await proc.postconditions(conn, content)


def required_bounds(content: ManifestContent) -> frozenset[str]:
    """The `max_ids` keys a replay of `content` reads (module docstring, "The bounds"): its kind's
    (`KindProcedure.max_ids_keys`) and its typed operations' (`manifests.bound_keys`); none of a kind no
    procedure replays, which is refused by its own name."""
    proc = KINDS.get(content["kind"])
    return frozenset() if proc is None else proc.max_ids_keys | bound_keys(content)


def _check_bounds(manifest_uuid: UUID, content: ManifestContent) -> None:
    """`ManifestBoundMissing` (logged `replay_bound_missing`) when `content`'s `max_ids` lacks a key its
    replay reads (module docstring, "The bounds"); the first missing key in name order is named."""
    if missing := sorted(required_bounds(content) - content["max_ids"].keys()):
        kind = content["kind"]
        log.error("replay_bound_missing", manifest_uuid=str(manifest_uuid), kind=kind, key=missing[0])
        raise ManifestBoundMissing(str(manifest_uuid), kind, missing[0])


async def _classify(conn: AsyncConnection, manifest_dir: Path, files: FileRoots | None) -> list[_Entry]:
    stream = {UUID(content["manifest_uuid"]): content for content in read_stream(manifest_dir)}
    rows = {row.manifest_uuid: row for row in await _rows(conn)}
    found: list[tuple[UUID, ManifestContent | None, ManifestRow | None, ManifestContent | None]] = []
    for manifest_uuid in sorted(stream.keys() | rows.keys()):
        content, row = stream.get(manifest_uuid), rows.get(manifest_uuid)
        if row is not None and content is not None:
            _same_manifest(row, content)
        recorded = content if content is not None or row is None else _recorded_content(row)
        found.append((manifest_uuid, content, row, recorded))
    for manifest_uuid, _, _, recorded in found:  # before any postcondition reads a bound (final review M3)
        if recorded is not None:
            _check_bounds(manifest_uuid, recorded)
    entries: list[_Entry] = []
    for manifest_uuid, content, row, recorded in found:
        if row is None:
            entries.append(_Entry(manifest_uuid, "replayed", content, None))
            continue
        if row.status == "executing":
            outcome: Outcome = "replayed" if content is not None else "pending_retry"
        elif recorded is None:
            raise ManifestConflict(f"manifest {manifest_uuid}: {row.status} without its content")
        else:
            outcome = "satisfied" if await _holds(conn, row, recorded, files) else "checked"
        entries.append(_Entry(manifest_uuid, outcome, recorded, row))
    return entries


async def classify_stream(
    conn: AsyncConnection, manifest_dir: Path, *, files: FileRoots | None = None
) -> list[Classified]:
    """Each manifest of the stream in `manifest_dir` and of the database with its row of §19.5's table, in
    `manifest_uuid` order (module docstring); `files` are the volumes a `file_remove` is checked on.
    `ManifestInvalid` for a file that does not parse or is named for another uuid, `ManifestConflict` for one
    that is not its row's."""
    return [Classified(e.manifest_uuid, e.outcome) for e in await _classify(conn, manifest_dir, files)]


# ---- phase A: the rows, the fences, the admissions ---------------------------------------------------------


def _named(content: ManifestContent, table: str, key: Literal["ids", "keep"] = "ids") -> list[int]:
    named: list[int] = []
    for op in content["operations"]:
        if op["op"] == "row_delete" and op["table"] == table:
            named += op.get(key, [])
    return named


def _fence_keys(content: ManifestContent, kind: str) -> list[str]:
    return [op["key"] for op in content["operations"] if op["op"] == "fence" and op["kind"] == kind]


def _request_of(content: ManifestContent) -> dict[str, JSONValue]:
    """The request (ids only) of a manifest inserted from its file, as its A0 wrote it (module docstring)."""
    kind = content["kind"]
    if kind == source_purge.KIND and (sources := _fence_keys(content, "source")):
        return {source_purge.REQUEST_KEY: sources[0]}
    if (
        kind == profile_purge.KIND
        and (affected := content.get("affected"))
        and affected["profile_version_ids"]
    ):
        return {profile_purge.REQUEST_KEY: affected["profile_version_ids"][0]}
    if kind == golden_case.KIND and (cases := _named(content, "golden_cases")):
        return {golden_case.REQUEST_KEY: cases[0]}
    if kind == job_tombstone.KIND:
        jobs: list[JSONValue] = [*sorted(set(_named(content, "jobs")))]
        return {job_tombstone.REQUEST_KEY: jobs}
    if kind == delete_everything.KIND and (rows := _named(content, "api_idempotency", "keep")):
        return {delete_everything.REQUEST_KEY: rows[0]}
    return {}


def _content_fences(content: ManifestContent) -> list[FenceSpec]:
    affected = content.get("affected")
    stored = set(affected["job_ids"]) if affected is not None else set()
    specs: list[FenceSpec] = []
    for op in content["operations"]:
        if op["op"] != "fence":
            continue
        if op["kind"] == "source":
            jobs = tuple(sorted(set(op["job_ids"]) | stored))
            specs.append(FenceSpec(kind="source", source_id=op["key"], job_ids=jobs))
        elif op["kind"] == "profile":
            specs.append(FenceSpec(kind="profile", profile_version_id=int(op["key"])))
        elif op["kind"] == "jobs" and op["job_ids"]:
            specs.append(FenceSpec(kind="jobs", job_ids=tuple(sorted(set(op["job_ids"])))))
        elif op["kind"] == "global":
            specs.append(FenceSpec(kind="global"))
    return specs


def _request_fences(row: ManifestRow) -> list[FenceSpec]:
    """A pending retry's fences, from its request (its A0 committed them with it: reconstructed only where
    none stands)."""
    request = row.request
    if row.kind == source_purge.KIND and isinstance(source := request.get(source_purge.REQUEST_KEY), str):
        return [FenceSpec(kind="source", source_id=source)]
    version = request.get(profile_purge.REQUEST_KEY)
    if row.kind == profile_purge.KIND and isinstance(version, int) and not isinstance(version, bool):
        return [FenceSpec(kind="profile", profile_version_id=version)]
    jobs = request.get(job_tombstone.REQUEST_KEY)
    if row.kind == job_tombstone.KIND and isinstance(jobs, list) and jobs:
        return [FenceSpec(kind="jobs", job_ids=tuple(sorted({int(cast(int, j)) for j in jobs})))]
    return []


def _fence_specs(row: ManifestRow, content: ManifestContent | None) -> list[FenceSpec]:
    """The fences a replayed manifest stands under (module docstring), one per kind."""
    specs = _content_fences(content) if content is not None else _request_fences(row)
    if row.kind == delete_everything.KIND and not any(spec.kind == "global" for spec in specs):
        specs.append(FenceSpec(kind="global"))
    return specs


async def _make_fences_active(
    conn: AsyncConnection, row: ManifestRow, content: ManifestContent | None
) -> int:
    """Every fence of the manifest active (module docstring); how many were reconstructed."""
    reconstructed = 0
    for spec in _fence_specs(row, content):
        active = (await conn.execute(_ACTIVE_FENCE, {"m": row.id, "kind": spec.kind})).scalar_one_or_none()
        if active is None:
            await insert_fence(conn, manifest_id=row.id, spec=spec)
            reconstructed += 1
        elif spec.job_ids:
            await conn.execute(_WIDEN_FENCE, {"id": int(active), "jobs": sorted(set(spec.job_ids))})
    return reconstructed


async def _reread(conn: AsyncConnection, manifest_id: int) -> ManifestRow:
    row = await read_manifest(conn, manifest_id)
    if row is None:  # a broken invariant: no code deletes a manifest
        raise RuntimeError(f"deletion manifest {manifest_id} is gone")
    return row


async def _insert(conn: AsyncConnection, content: ManifestContent) -> ManifestRow:
    params = {
        "uuid": UUID(content["manifest_uuid"]),
        "kind": content["kind"],
        "scope": dict(content),
        "sha": _sha256(content),
        "request": _request_of(content),
        "admitted_at": datetime.fromisoformat(content["admitted_at"]),
    }
    return await _reread(conn, int((await conn.execute(_INSERT, params)).scalar_one()))


def _has_entry(entries: Sequence[Mapping[str, JSONValue]], restore_id: str, phase: str) -> bool:
    return any(e.get("restore_id") == restore_id and e.get("phase") == phase for e in entries)


def _turn_of(entries: Sequence[Mapping[str, JSONValue]], restore_id: str) -> dict[str, int] | None:
    """The `turn` this replay's `admitted` entry recorded for the manifest, `None` before its admission."""
    for entry in entries:
        if entry.get("restore_id") == restore_id and entry.get("phase") == ADMITTED:
            return {str(t): int(cast(int, v)) for t, v in cast(dict[str, JSONValue], entry["turn"]).items()}
    return None


def _unfinished(logs: Sequence[Sequence[Mapping[str, JSONValue]]]) -> _Restore | None:
    """The replay an earlier `migrate` started and never completed (a `started` entry whose restore id no
    completion entry names), the latest one; `None` when there is none."""
    entries = [entry for log_ in logs for entry in log_]
    completed = {entry.get("restore_id") for entry in entries if "counts" in entry}
    started = [e for e in entries if e.get("phase") == STARTED and e.get("restore_id") not in completed]
    if not started:
        return None
    latest = max(started, key=lambda e: str(e["at"]))
    high_water = {
        str(t): int(cast(int, v)) for t, v in cast(dict[str, JSONValue], latest["high_water"]).items()
    }
    return _Restore(str(latest["restore_id"]), MappingProxyType(high_water))


def _entry(restore: _Restore, now: datetime, phase: str, marks: Mapping[str, int]) -> dict[str, JSONValue]:
    key = "high_water" if phase == STARTED else "turn"
    found: dict[str, JSONValue] = {**marks}
    return {"restore_id": restore.restore_id, "at": now.isoformat(), "phase": phase, key: found}


async def _admit(
    engine: AsyncEngine, entries: Sequence[_Entry], *, now: datetime
) -> tuple[_Restore, list[_Replayed]]:
    """Phase A's first transaction (module docstring): the replay (resumed or new) and the manifests to
    replay, in stream order, every fence of them active."""
    replayed: list[_Replayed] = []
    async with engine.begin() as conn:
        await conn.execute(_CONFIG_FOR_UPDATE)
        recorded = [entry.row.id for entry in entries if entry.row is not None]
        logs = {int(r.id): list(r.replay_log) for r in await conn.execute(_REPLAY_LOGS, {"ids": recorded})}
        restore = _unfinished(list(logs.values()))
        resumed = restore is not None
        if restore is None:  # before any row of this replay: every row above it is the replay's own
            restore = _Restore(str(uuid7()), MappingProxyType(await max_identity_ids(conn)))
        for entry in entries:
            row = entry.row
            if row is None:
                if entry.content is None:  # a file the classification read: never
                    raise RuntimeError(f"manifest {entry.manifest_uuid}: neither a row nor a file")
                row = await _insert(conn, entry.content)
            reconstructed = await _make_fences_active(conn, row, entry.content)
            if entry.content is not None and row.file_sha256 is None:  # its file adopted (A3)
                await record_manifest(conn, row, entry.content, _sha256(entry.content))
            row = await _reread(conn, row.id)
            known = logs.get(row.id, [])
            if not _has_entry(known, restore.restore_id, STARTED):
                started = _entry(restore, now, STARTED, restore.high_water)
                await conn.execute(_LOG_REPLAY, {"id": row.id, "entry": started})
            manifest = _Replayed(
                row, entry.content, KINDS[row.kind], pending=entry.outcome == "pending_retry"
            )
            manifest.turn = _turn_of(known, restore.restore_id)
            replayed.append(manifest)
            log.info(
                "replay_admitted",
                manifest_id=row.id,
                kind=row.kind,
                outcome=entry.outcome,
                fences_reconstructed=reconstructed,
            )
    log.info(
        "replay_started",
        restore_id=restore.restore_id,
        resumed=resumed,
        manifest_ids=[m.row.id for m in replayed][:LOGGED_IDS],
    )
    return restore, replayed


async def _admission(
    engine: AsyncEngine, manifest: _Replayed, restore: _Restore, *, now: datetime
) -> Mapping[str, int]:
    """The manifest's admission at its place in the stream (module docstring): in a transaction of its own
    under `active_config FOR UPDATE`, each table's greatest id (its turn), its kind's `reapply_admission` and
    its `admitted` entry; the turn. A restart reuses a turn its replay recorded and re-applies nothing."""
    if manifest.turn is not None:
        return manifest.turn
    async with engine.begin() as conn:
        await conn.execute(_CONFIG_FOR_UPDATE)
        turn = await max_identity_ids(conn)
        await manifest.proc.reapply_admission(conn, manifest.row, now=now, high_water=restore.high_water)
        await conn.execute(
            _LOG_REPLAY, {"id": manifest.row.id, "entry": _entry(restore, now, ADMITTED, turn)}
        )
    manifest.turn = turn
    return turn


def _own_rows(restore: _Restore, turn: Mapping[str, int]) -> dict[str, tuple[int, int]]:
    """Per table, the ids `(above, upto]` of the rows the replay created before a manifest's turn."""
    return {
        table: (restore.high_water[table], upto)
        for table, upto in turn.items()
        if table in restore.high_water and upto > restore.high_water[table]
    }


async def _complete_pending(
    engine: AsyncEngine, manifest: _Replayed, *, manifest_dir: Path, since: datetime, now: datetime
) -> None:
    """A pending retry completed from its row at its turn (module docstring): without a hash its content
    computed under the restored fence (`since`, the fences' cutoff), checked, written and recorded; with one,
    its file written from `scope`."""
    computed = manifest.content is None
    if manifest.content is None:
        async with engine.begin() as conn:
            content = await compute_content(conn, manifest.proc, manifest.row, fenced_at=since, now=now)
        if (reason := unparsable(content, manifest.row)) is not None:
            raise ReplayFailed(
                f"deletion manifest {manifest.row.id}: its computed content is no manifest: {reason}"
            )
        sha = write_manifest_file(manifest_dir, content)
        async with engine.begin() as conn:
            await record_manifest(conn, manifest.row, content, sha)
            manifest.row = await _reread(conn, manifest.row.id)
        manifest.content = content
    else:
        write_manifest_file(manifest_dir, manifest.content)  # from `scope`; an equal file is adopted
    log.info("replay_pending_completed", manifest_id=manifest.row.id, computed=computed)


async def _execute(
    engine: AsyncEngine,
    cfg: "AppConfig",
    manifest: _Replayed,
    *,
    files: FileRoots,
    now: datetime,
    own_rows: Mapping[str, tuple[int, int]],
) -> None:
    """The manifest's destructive operations at its turn, its export ops after them (module docstring)."""
    content = manifest.stored
    manifest.counts.update(Counter(op["op"] for op in content["operations"]))
    run = PurgeRun.replay(engine, cfg, manifest=manifest.row, now=now, files=files, replayed_rows=own_rows)
    await manifest.proc.execute(run, content)
    exports: list[Operation] = [op for op in content["operations"] if is_export_op(op)]
    if manifest.proc.deletes_exports and exports:
        manifest.counts["exports_purged"] = await purge_exports(run, exports)


async def _phase_a(
    engine: AsyncEngine,
    cfg: "AppConfig",
    entries: Sequence[_Entry],
    *,
    manifest_dir: Path,
    files: FileRoots,
    now: datetime,
) -> tuple[_Restore, list[_Replayed], int]:
    """Phase A (module docstring): the replay, its manifests and the pending retries completed."""
    restore, manifests = await _admit(engine, entries, now=now)
    since: datetime | None = None
    pending = 0
    for manifest in manifests:
        turn = await _admission(engine, manifest, restore, now=now)
        if manifest.pending:
            if since is None:
                since = await fence_committed_at(engine)  # the restored fences' cutoff, after their commit
            await _complete_pending(engine, manifest, manifest_dir=manifest_dir, since=since, now=now)
            pending += 1
        own = _own_rows(restore, turn)
        await _execute(engine, cfg, manifest, files=files, now=now, own_rows=own)
        await afault_point(AFTER_EXECUTE)
    log.info("replay_phase_a_done", manifest_ids=[m.row.id for m in manifests][:LOGGED_IDS])
    return restore, manifests, pending


# ---- phase B -----------------------------------------------------------------------------------------------


async def _rebuild(
    engine: AsyncEngine, cfg: "AppConfig", manifests: Sequence[_Replayed], *, files: FileRoots, now: datetime
) -> int:
    """Phase B (module docstring): the roots rebuilt."""
    holder: dict[int, int] = {}  # each stored job: the latest replayed source purge (its index) holding it
    for index, manifest in enumerate(manifests):
        affected = manifest.stored.get("affected")
        if manifest.row.kind == source_purge.KIND and affected is not None:
            holder.update(dict.fromkeys(affected["job_ids"], index))
    by_root: dict[int, int] = {}
    if holder:
        async with engine.connect() as conn:
            for row in (await conn.execute(_ROOTS, {"jobs": sorted(holder)})).all():
                root = int(row.root_id)
                by_root[root] = max(by_root.get(root, -1), holder[int(row.job_id)])
    groups: dict[int, list[int]] = {}
    for root, index in sorted(by_root.items()):
        groups.setdefault(index, []).append(root)
    rebuilt = 0
    for index, roots in sorted(groups.items()):
        manifest = manifests[index]
        run = PurgeRun.replay(engine, cfg, manifest=manifest.row, now=now, files=files)
        manifest.counts["jobs_rebuilt"] = await source_purge.rebuild_jobs(run, roots)
        rebuilt += manifest.counts["jobs_rebuilt"]
    log.info(
        "replay_phase_b_done",
        stored_jobs=len(holder),
        roots=sorted(by_root)[:LOGGED_IDS],
        rebuilt_jobs=rebuilt,
    )
    return rebuilt


# ---- phase C -----------------------------------------------------------------------------------------------


class _Violated(Exception):
    """Phase C found violations: its transaction rolls back."""


async def _verify_and_release(
    engine: AsyncEngine, manifests: Sequence[_Replayed], *, restore: _Restore, files: FileRoots, now: datetime
) -> None:
    """Phase C (module docstring)."""
    restore_id = restore.restore_id
    violations: list[dict[str, object]] = []
    try:
        async with engine.begin() as conn:
            for manifest in manifests:
                if isinstance(manifest.proc, CompletingKind):
                    await manifest.proc.complete(conn, manifest.row, manifest.stored)
            for manifest in manifests:
                await release_fences(conn, manifest.row.id)
            for manifest in manifests:
                found = await check_postconditions(conn, manifest.stored, files=files)
                found += await manifest.proc.postconditions(conn, manifest.stored)
                violations += [{"manifest_id": manifest.row.id, **dataclasses.asdict(v)} for v in found]
            if violations:
                raise _Violated
            for manifest in manifests:
                if manifest.row.status == "executing":
                    await complete_manifest(conn, manifest.row.id)
                entry: Mapping[str, JSONValue] = {
                    "restore_id": restore_id,
                    "at": now.isoformat(),
                    "counts": dict(sorted(manifest.counts.items())),
                }
                await conn.execute(_LOG_REPLAY, {"id": manifest.row.id, "entry": dict(entry)})
    except _Violated:
        log.error("replay_postcondition_failed", violations=violations[:LOGGED_IDS])
        manifest_ids = sorted({cast(int, v["manifest_id"]) for v in violations})
        raise ReplayFailed(
            f"the replay's postconditions failed for deletion manifests {manifest_ids}"
        ) from None


# ---- the replay --------------------------------------------------------------------------------------------


async def replay_stream(
    engine: AsyncEngine, cfg: "AppConfig", *, manifest_dir: Path, files: FileRoots, now: datetime
) -> ReplayReport:
    """Migrate step 7 on the stream in `manifest_dir` and the volumes `files` (module docstring): the
    manifests classified, then, when one is to be replayed, phases A, B and C. `ReplayFailed` when a
    postcondition fails (every fence active) or a manifest is of no known kind."""
    async with engine.connect() as conn:
        entries = await _classify(conn, manifest_dir, files)
    to_replay = [e for e in entries if e.outcome != "satisfied"]
    log.info(
        "replay_classified",
        outcomes=dict(Counter(e.outcome for e in entries)),
        replayed=[str(e.manifest_uuid) for e in to_replay][:LOGGED_IDS],
    )
    if not to_replay:
        return ReplayReport(replayed=0, pending_completed=0, rebuilt_jobs=0)
    if unknown := [str(e.manifest_uuid) for e in to_replay if _kind_of(e) not in KINDS]:
        raise ReplayFailed(f"no procedure replays the kind of manifests {unknown[:LOGGED_IDS]}")
    restore, manifests, pending = await _phase_a(
        engine, cfg, to_replay, manifest_dir=manifest_dir, files=files, now=now
    )
    await afault_point(AFTER_A)
    rebuilt = await _rebuild(engine, cfg, manifests, files=files, now=now)
    await afault_point(AFTER_B)
    await _verify_and_release(engine, manifests, restore=restore, files=files, now=now)
    log.info(
        "replay_completed",
        restore_id=restore.restore_id,
        manifest_ids=[m.row.id for m in manifests][:LOGGED_IDS],
        pending_completed=pending,
        rebuilt_jobs=rebuilt,
    )
    return ReplayReport(replayed=len(manifests), pending_completed=pending, rebuilt_jobs=rebuilt)


def _kind_of(entry: _Entry) -> str:
    if entry.row is not None:
        return entry.row.kind
    return "" if entry.content is None else entry.content["kind"]
