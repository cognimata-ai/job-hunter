"""The purge protocol every deletion kind runs on (spec §19.5 step 3, §20.2, §3.3.3; plan M5 Task 12, D1, D5,
D16, Deviations 16, 17, 18, 53 and 56, Ruling T12-1).

**A0, the admission** (`open_purge`), in the caller's transaction at `admitted_at = now`, which holds
`active_config FOR UPDATE` (and, on an api route, its idempotency row): the `executing` manifest with its
request (`manifests.insert_manifest`; ids only), its fence rows (`fences.insert_fence`) and the `purge`
operation, payload `{manifest_id}` alone, dedup key `purge:<manifest_uuid>` (`PURGE_DEDUP_PREFIX`), its job
deferred with `defer`. The three commit together or not at all. The request lives on the manifest row, so
an operation the `purge_fences` reconciler or the retry route creates, a pending retry that replay completes
and a manifest whose operation delete-everything cancelled all compute the same content from one request
(Deviation 53). Every `purge` job runs under the procrastinate lock `purge` (`OPERATION_LOCKS`): one purge at
a time, so execution follows the UUIDv7 stream order (D5).

**The kinds** (`KindProcedure`; `register_kind` fills `KINDS`, and `privacy.kinds` imports every module that
registers one): `compute` (A2, after the quiesce, under the fence: the content of the manifest's request,
`new_content` building its common fields), `execute` (phase B, in idempotent chunks over the stored scope),
`postconditions` (the kind's own, checked in phase C after the typed ones), `reapply_admission` (replay's
re-application of A0's effects beyond the manifest, its fences and its operation: a no-op for every kind but
the source purge) and `deletes_exports` (true for every kind but `job_tombstone`: M5-R3, Deviation 18).
`compute_content` runs `compute` and completes what the kind cannot know: `fenced_at` (and a watermark's)
becomes the fence's commit cutoff the quiesce waited from (`fences.fence_committed_at`), and a kind that
deletes exports gets `deletion_graph.export_ops` last, bounded by its `max_ids["exports"]`.

**The handler** (`run_purge`, kind `purge`; Task 25's replay runs the same procedures in `migrate`):

1. the manifest not `executing` → `succeeded` (a retry that replay, or an earlier execution, completed); no
   such manifest → `failed('manifest_missing')`, a broken invariant (no code deletes a manifest) that the
   failure hook's alert makes loud, as an invalid payload is `failed('invalid_payload')`;
2. no `file_sha256` (A1-A3): when `<manifest_uuid>.json` already exists in `cfg.manifests_dir` (a crash
   after the file's rename, before A3) it is adopted, never recomputed: it must parse (`parse_manifest`) with
   the row's uuid, kind and `format` 1, in the canonical bytes whose hash `record_manifest` stores, else the
   operation ends `failed('manifest_invalid')` with the fence kept (nothing was deleted before A3 and the
   fence held, so an adoptable file's content is still the purge's). Otherwise the fence's cutoff is read
   (`fence_committed_at`) and the quiesce awaited (`wait_for_quiesce`; a timeout requeues
   `quiesce_timeout` a minute later), the content computed (`compute_content`), checked as a replay will
   read its file (`parse_manifest` of its canonical bytes: a content of any other shape than the format's
   ends the operation `failed('manifest_invalid')` before anything is written, the fence kept), and written
   (`write_manifest_file`: tmp, fsync, rename, directory fsync). Then one transaction records it
   (`record_manifest`: the file's hash, `scope`, a source fence's affected jobs). Nothing is deleted before;
3. phase B on the stored `scope`, never re-derived: the kind's `execute`, then for a kind that deletes them
   the export ops (`deletion_graph.purge_exports`). A retryable database error (a deadlock or a
   serialization failure that a chunk loses, `graphs.sanitize.is_retryable_db_error`, Task 11's lock
   contract) re-runs that phase from its start, `PHASE_TRIES` tries in all; then the operation is requeued
   `lock_conflict` a minute later (an attempt spent, so repeated losses end `failed` with the alert, never an
   endless wait; never `bug`). That count is apart from the chunk's `CHUNK_LOCK_ROUNDS`. A chunk that gives
   up on its co-members (`CoMemberChanged`) is requeued `co_member_changed` a minute later; a kind that meets
   a condition with an end of its own raises `PurgeWait`, and the operation waits without spending an
   attempt (`OperationContext.wait_in`);
4. phase C, one transaction: the kind's own writes of the completing transaction for a kind that has them
   (`CompletingKind.complete`: delete-everything's sessions, M5 Task 16, D14), the fences released, then the
   typed postconditions (`check_postconditions`; the `fence` op's "no active fence" holds now) and the
   kind's. A violation rolls it back, so the fences stay
   active, and a second transaction ends the operation `failed('postcondition')`. Otherwise the manifest
   `completed`, `privacy.purge_failed` (subject `manifest:<id>`) resolved and the operation `succeeded`.

Every way the operation ends `failed` runs `purge_failed`, its failure hook, which raises
`privacy.purge_failed` (critical, subject `manifest:<id>`) at once; the `purge_fences` reconciler is the
backstop. The worker's other ends write in a transaction of their own that starts with the epoch fence.

**The chunk** (`PurgeRun.chunk`): a transaction whose locks follow §3.3.3's order (L314): (1) `fence_epoch`
(live only), (2) `active_config` `FOR SHARE` (`FOR UPDATE` for a step that re-points it), (4) the
`job_ids` rows and those of every duplicate-group co-member of `with_groups_of`, `FOR UPDATE` in one
ascending-id statement (`dedup.merge.lock_jobs`), then the co-members read again (`merge.co_members`): a
member that joined between the statement's snapshot and its lock rolls the round back, and the chunk starts
again, at most `CHUNK_LOCK_ROUNDS` rounds, then `CoMemberChanged`; `held` is the locked set; (5)
`lock_operation_owner` (live only). The kind's statements follow, in the schedule's later steps (the bulk
rows, the month row, the ledger rows; `scoring/targets.py`), with `ranking.bump` the transaction's last. A
replay's chunk takes neither the epoch nor the owner. No kind takes a lock before `chunk` returns. A kind
deletes in idempotent chunks: the job rows before the rows to delete are selected, every statement bounded
by `max_ids` (D16), and a `file_remove` only after the transaction that changed the rows naming its files
committed.

**A live purge's external effects** (`LiveEffects`, Deviation 56): the process's Message Batches client and
its dead-man pinger, from the worker's `OperationDeps`; a replay has none (`PurgeRun.effects` is `None`).

Fault points (test env only): `privacy.purge.before_file`, `after_file`, `after_record`, `mid_delete` (after
the kind's chunks, before the export ops) and `before_complete`. Ids, counts, kinds and codes only reach a log
line or an alert.
"""

import asyncio
import dataclasses
import hashlib
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Final, Literal, Protocol, runtime_checkable
from uuid import UUID

from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from jobhunter.db.sql import pg_error, q
from jobhunter.dedup import merge
from jobhunter.errors import CoMemberChanged, FencedOut
from jobhunter.faults import afault_point
from jobhunter.observability.alerts import raise_alert, resolve_alert
from jobhunter.observability.logging import get_logger
from jobhunter.privacy.deletion_graph import export_ops, is_export_op, purge_exports
from jobhunter.privacy.fences import (
    FenceSpec,
    fence_committed_at,
    insert_fence,
    release_fences,
    wait_for_quiesce,
)
from jobhunter.privacy.manifests import (
    MANIFEST_FORMAT,
    FileRoots,
    ManifestContent,
    ManifestInvalid,
    ManifestKind,
    ManifestRow,
    Operation,
    Violation,
    canonical_bytes,
    check_postconditions,
    complete_manifest,
    insert_manifest,
    manifest_file_name,
    parse_manifest,
    read_manifest,
    read_manifest_file,
    record_manifest,
    write_manifest_file,
)
from jobhunter.privacy.reconcilers import PURGE_FAILED_ALERT
from jobhunter.types import JSONValue
from jobhunter.worker.epoch import fence_epoch
from jobhunter.worker.operations import PURGE_DEDUP_PREFIX as PURGE_DEDUP_PREFIX
from jobhunter.worker.operations import (
    PURGE_KIND,
    OperationClaim,
    OperationContext,
    create_operation,
    lock_operation_owner,
    register_operation_handler,
)
from jobhunter.worker.tasks import Deferrer

if TYPE_CHECKING:
    from jobhunter.config import AppConfig
    from jobhunter.llm.batches import BatchesClient
    from jobhunter.observability.deadman import DeadmanPinger
    from jobhunter.profile.storage import CvStore

# Read-then-lock rounds of a chunk whose co-members keep changing (as `dedup.groups._LOCK_ROUNDS`).
CHUNK_LOCK_ROUNDS: Final = 3
# Tries of a phase that loses a lock to a retryable database error, in all (Ruling T12-1).
PHASE_TRIES: Final = 3
# How long a requeued purge waits: a quiesce timeout, a lost lock, a co-member that kept changing.
RETRY_AFTER: Final = timedelta(minutes=1)

# The error codes a purge operation ends or is given back with.
QUIESCE_TIMEOUT_CODE: Final = "quiesce_timeout"
LOCK_CONFLICT: Final = "lock_conflict"
CO_MEMBER_CHANGED: Final = "co_member_changed"
MANIFEST_INVALID: Final = "manifest_invalid"
POSTCONDITION: Final = "postcondition"
UNKNOWN_KIND: Final = "unknown_kind"
INVALID_PAYLOAD: Final = "invalid_payload"
MANIFEST_MISSING: Final = "manifest_missing"

# The fault points (module docstring).
BEFORE_FILE: Final = "privacy.purge.before_file"
AFTER_FILE: Final = "privacy.purge.after_file"
AFTER_RECORD: Final = "privacy.purge.after_record"
MID_DELETE: Final = "privacy.purge.mid_delete"
BEFORE_COMPLETE: Final = "privacy.purge.before_complete"

_BIGINT_END: Final = 2**63
LOGGED_IDS: Final = 20
_CONFIG_FOR_SHARE: Final = q("SELECT 1 FROM active_config WHERE id = 1 FOR SHARE")
_CONFIG_FOR_UPDATE: Final = q("SELECT 1 FROM active_config WHERE id = 1 FOR UPDATE")
# The claim's worker epoch (no lock: the claim is the row's one writer), as the replay handler reads it.
_OWN_EPOCH: Final = q(
    "SELECT worker_epoch FROM operations WHERE id = :i AND claim_id = :c AND status = 'running'"
)

log = get_logger("jobhunter.privacy.purge")


# ---- A0 ----------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class OpenedPurge:
    """What A0 committed: the manifest and its `purge` operation."""

    manifest_id: int
    manifest_uuid: UUID
    operation_id: int


async def open_purge(
    conn: AsyncConnection,
    *,
    kind: ManifestKind,
    request: Mapping[str, JSONValue],
    fences: Sequence[FenceSpec],
    now: datetime,
    defer: Deferrer | None,
) -> OpenedPurge:
    """A0 in the caller's transaction, which holds `active_config FOR UPDATE` (module docstring): the
    manifest of `kind` with its `request` (ids only) at `admitted_at = now`, its `fences` and the `purge`
    operation, payload `{manifest_id}`, its job deferred with `defer` (`None` defers nothing)."""
    manifest = await insert_manifest(conn, kind=kind, request=request, now=now)
    for spec in fences:
        await insert_fence(conn, manifest_id=manifest.id, spec=spec)
    operation_id, _ = await create_operation(
        conn,
        kind=PURGE_KIND,
        payload={"manifest_id": manifest.id},
        dedup_key=f"{PURGE_DEDUP_PREFIX}{manifest.manifest_uuid}",
        defer=defer,
    )
    log.info(
        "purge_opened", manifest_id=manifest.id, kind=kind, operation_id=operation_id, fences=len(fences)
    )
    return OpenedPurge(manifest.id, manifest.manifest_uuid, operation_id)


# ---- the run and its chunks --------------------------------------------------------------------------------


@dataclass(frozen=True)
class LiveEffects:
    """A live purge's external effects (Deviation 56): the process's Message Batches client and its dead-man
    pinger (`None` without one)."""

    batches_client: "BatchesClient"
    deadman: "DeadmanPinger | None"


@dataclass(frozen=True)
class PurgeOwner:
    """A live purge's owner: the worker epoch its claim was made under, and the claim."""

    epoch: int
    claim: OperationClaim


@dataclass(frozen=True)
class Chunk:
    """A chunk's transaction and the job rows it holds `FOR UPDATE`."""

    conn: AsyncConnection
    held: frozenset[int]


class PurgeWait(Exception):
    """A kind's phase B met a condition with an end of its own (a running eval run): the operation waits
    `retry_after` with `error_code`, no attempt spent (module docstring). Its transaction rolls back."""

    def __init__(self, error_code: str, retry_after: timedelta) -> None:
        super().__init__(error_code)
        self.error_code = error_code
        self.retry_after = retry_after


class _SetChanged(Exception):
    """A round of a chunk found a co-member it does not hold: it rolls back and the chunk starts again."""


@dataclass(frozen=True)
class PurgeRun:
    """One execution of a manifest's procedure: live (the worker's, `effects` and `owner` set) or a replay
    (`migrate`'s, neither). `now` is the execution's clock; `manifest` the row it runs for."""

    engine: AsyncEngine
    cfg: "AppConfig"
    now: datetime
    cv_store: "CvStore"
    manifest: ManifestRow
    mode: Literal["live", "replay"]
    effects: LiveEffects | None
    owner: PurgeOwner | None

    def __post_init__(self) -> None:
        live = self.mode == "live"
        if live != (self.effects is not None) or live != (self.owner is not None):
            raise ValueError("a live purge has its effects and its owner, a replay neither")

    @property
    def files(self) -> FileRoots:
        """The volumes a `file_remove` names."""
        return FileRoots(cv_store=self.cfg.cv_store_dir, exports=self.cfg.exports_dir)

    @asynccontextmanager
    async def chunk(
        self,
        *,
        config: Literal["share", "update"] = "share",
        job_ids: Sequence[int] = (),
        with_groups_of: Sequence[int] = (),
    ) -> AsyncIterator[Chunk]:
        """A transaction with the chunk's locks taken in §3.3.3's order (module docstring); it commits when
        the block ends, and rolls back on an exception. `CoMemberChanged` after `CHUNK_LOCK_ROUNDS` rounds
        whose co-members changed under their lock."""
        ids, grouped = sorted(set(job_ids)), sorted(set(with_groups_of))
        for _ in range(CHUNK_LOCK_ROUNDS):
            try:
                async with self.engine.begin() as conn:
                    held = await self._lock(conn, config, ids, grouped)
                    yield Chunk(conn, held)
                return
            except _SetChanged:
                log.info(
                    "purge_chunk_set_changed",
                    manifest_id=self.manifest.id,
                    job_ids=ids[:LOGGED_IDS],
                    with_groups_of=grouped[:LOGGED_IDS],
                )
        raise CoMemberChanged(
            f"deletion manifest {self.manifest.id}: a chunk's duplicate groups kept changing under its lock"
        )

    async def _lock(
        self, conn: AsyncConnection, config: Literal["share", "update"], ids: list[int], grouped: list[int]
    ) -> frozenset[int]:
        if self.owner is not None:
            await fence_epoch(conn, self.owner.epoch)  # (1)
        await conn.execute(_CONFIG_FOR_UPDATE if config == "update" else _CONFIG_FOR_SHARE)  # (2)
        held = frozenset(await merge.lock_jobs(conn, ids, with_groups_of=grouped))  # (4)
        if grouped and not (await merge.co_members(conn, grouped)) <= held:
            raise _SetChanged
        if self.owner is not None:
            await lock_operation_owner(conn, self.owner.claim)  # (5)
        return held


# ---- the kinds ---------------------------------------------------------------------------------------------


class KindProcedure(Protocol):
    """A manifest kind's procedure (module docstring)."""

    kind: ManifestKind
    deletes_exports: bool

    async def compute(
        self, conn: AsyncConnection, manifest: ManifestRow, *, now: datetime
    ) -> ManifestContent:
        """A2: the content of `manifest.request` (`new_content`), under the fence after the quiesce."""
        ...

    async def execute(self, run: PurgeRun, content: ManifestContent) -> None:
        """Phase B on the stored content, in idempotent chunks (`run.chunk`); the export ops are not its."""
        ...

    async def postconditions(self, conn: AsyncConnection, content: ManifestContent) -> list[Violation]:
        """The kind's own postconditions, beyond the typed operations'."""
        ...

    async def reapply_admission(self, conn: AsyncConnection, manifest: ManifestRow, *, now: datetime) -> None:
        """Replay's re-application of A0's effects beyond the manifest, its fences and its operation."""
        ...


@runtime_checkable
class CompletingKind(Protocol):
    """A kind with writes of its own in the completing transaction (phase C), before its fences are released
    and its postconditions checked: delete-everything's sessions (M5 Task 16, D14). Every other kind has
    none."""

    async def complete(self, conn: AsyncConnection, content: ManifestContent) -> None:
        """The kind's writes in the completing transaction, which holds the epoch fence and the owner."""
        ...


KINDS: dict[ManifestKind, KindProcedure] = {}


def register_kind(proc: KindProcedure) -> KindProcedure:
    """Register `proc` for its kind; `ValueError` when another procedure has the kind already."""
    registered = KINDS.get(proc.kind)
    if registered is not None and registered is not proc:
        raise ValueError(f"another procedure is already registered for manifest kind {proc.kind!r}")
    KINDS[proc.kind] = proc
    return proc


def new_content(
    manifest: ManifestRow, *, max_ids: Mapping[str, int], operations: Sequence[Operation]
) -> ManifestContent:
    """The content a kind's `compute` returns: format 1, the manifest's uuid and kind, `admitted_at` (its
    row's `created_at`, A0's clock), `max_ids` and the ordered operations. `fenced_at` is `admitted_at` here
    until `compute_content` stamps the fence's cutoff over it; a kind adds `affected`, `watermark` or
    `retained` itself."""
    admitted_at = manifest.created_at.isoformat()
    return {
        "format": MANIFEST_FORMAT,
        "manifest_uuid": str(manifest.manifest_uuid),
        "kind": manifest.kind,
        "admitted_at": admitted_at,
        "fenced_at": admitted_at,
        "max_ids": dict(max_ids),
        "operations": list(operations),
    }


async def compute_content(
    conn: AsyncConnection, proc: KindProcedure, manifest: ManifestRow, *, fenced_at: datetime, now: datetime
) -> ManifestContent:
    """A2 on `conn` (module docstring): `proc`'s content of `manifest` with `fenced_at` (and a watermark's)
    the fence's commit cutoff, and the export ops last for a kind that deletes exports. `ValueError` for a
    content of another manifest, or one that deletes exports without an `exports` bound."""
    content = await proc.compute(conn, manifest, now=now)
    if content["manifest_uuid"] != str(manifest.manifest_uuid) or content["kind"] != manifest.kind:
        raise ValueError("the kind computed another manifest's content")
    stamped = content.copy()
    stamped["fenced_at"] = fenced_at.isoformat()
    if (watermark := content.get("watermark")) is not None:
        stamped["watermark"] = {"fenced_at": fenced_at.isoformat(), "max_ids": watermark["max_ids"]}
    if proc.deletes_exports:
        if "exports" not in content["max_ids"]:
            raise ValueError("a content that deletes exports bounds them: max_ids has no exports")
        exports = await export_ops(conn, max_export_id=content["max_ids"]["exports"])
        stamped["operations"] = [*content["operations"], *exports]
    return stamped


# ---- the handler -------------------------------------------------------------------------------------------


def manifest_id_of(payload: Mapping[str, JSONValue]) -> int | None:
    """The manifest a purge operation's payload names, `None` for a payload that names none."""
    value = payload.get("manifest_id")
    if isinstance(value, bool) or not isinstance(value, int) or not 0 < value < _BIGINT_END:
        return None
    return value


def _subject(manifest_id: int) -> str:
    return f"manifest:{manifest_id}"


async def purge_failed(
    conn: AsyncConnection, operation_id: int, payload: Mapping[str, JSONValue], error_code: str | None
) -> None:
    """The `purge` operation's failure hook (module docstring): `privacy.purge_failed` raised at once for its
    manifest, in the transaction that ends the operation `failed`; the fence is kept."""
    manifest_id = manifest_id_of(payload)
    if manifest_id is None:
        log.error("purge_hook_payload_invalid", operation_id=operation_id)
        return  # no manifest to alert on: the operation still ends `failed`, with `operation.failed`
    await raise_alert(
        conn,
        code=PURGE_FAILED_ALERT,
        severity="critical",
        subject=_subject(manifest_id),
        message=f"the purge of deletion manifest {manifest_id} failed; its fence is kept",
        context={"manifest_id": manifest_id, "operation_id": operation_id, "reason": "failed"},
    )
    log.error("purge_failed", manifest_id=manifest_id, operation_id=operation_id, error_code=error_code)


@dataclass(frozen=True)
class _Ending:
    """The handler's ends that commit nothing else: each in a transaction of its own that starts with the
    epoch fence."""

    ctx: OperationContext
    epoch: int
    clock: Callable[[], datetime]

    async def _write(self, end: Callable[[AsyncConnection], Awaitable[None]]) -> None:
        async with self.ctx.engine.begin() as conn:
            await fence_epoch(conn, self.epoch)
            await end(conn)

    async def succeed(self) -> None:
        await self._write(lambda conn: self.ctx.finish_in(conn, status="succeeded"))

    async def fail(self, error_code: str) -> None:
        await self._write(lambda conn: self.ctx.finish_in(conn, status="failed", error_code=error_code))

    async def requeue(self, error_code: str) -> None:
        not_before = self.clock() + RETRY_AFTER
        await self._write(
            lambda conn: self.ctx.requeue_in(conn, error_code=error_code, not_before=not_before)
        )

    async def wait(self, error_code: str, retry_after: timedelta) -> None:
        not_before = self.clock() + retry_after
        await self._write(lambda conn: self.ctx.wait_in(conn, error_code=error_code, not_before=not_before))


@register_operation_handler(PURGE_KIND, on_failed=purge_failed)
async def run_purge(ctx: OperationContext) -> None:
    """Run the purge of the payload's manifest (module docstring)."""
    deps = ctx.deps
    if deps is None:
        raise RuntimeError("a purge needs the worker's operation dependencies")
    claim = ctx.claim
    manifest_id = manifest_id_of(claim.payload)
    async with ctx.engine.begin() as conn:
        epoch = (await conn.execute(_OWN_EPOCH, {"i": claim.operation_id, "c": claim.claim_id})).scalar()
        if epoch is None:
            raise FencedOut("operation")
        manifest = None if manifest_id is None else await read_manifest(conn, manifest_id)
    ending = _Ending(ctx, int(epoch), deps.clock)
    if manifest_id is None:
        log.error("purge_payload_invalid", operation_id=claim.operation_id)
        await ending.fail(INVALID_PAYLOAD)
        return
    if manifest is None:  # a broken invariant: no code deletes a manifest
        log.error("purge_manifest_missing", manifest_id=manifest_id, operation_id=claim.operation_id)
        await ending.fail(MANIFEST_MISSING)
        return
    if manifest.status != "executing":  # 1.
        log.info("purge_not_executing", manifest_id=manifest_id, status=manifest.status)
        await ending.succeed()
        return
    proc = KINDS.get(manifest.kind)
    if proc is None:
        log.error("purge_kind_unknown", manifest_id=manifest.id, kind=manifest.kind)
        await ending.fail(UNKNOWN_KIND)
        return
    run = PurgeRun(
        engine=ctx.engine,
        cfg=deps.cfg,
        now=deps.clock(),
        cv_store=deps.cv_store,
        manifest=manifest,
        mode="live",
        effects=LiveEffects(batches_client=deps.batches_client, deadman=deps.deadman),
        owner=PurgeOwner(int(epoch), claim),
    )
    log.info("purge_started", manifest_id=manifest.id, kind=manifest.kind, operation_id=claim.operation_id)
    if manifest.file_sha256 is None:
        content = await _admit(run, proc, ending)  # 2.
    else:
        content = manifest.scope
        if content is None:  # a hash without its content: nothing to run on
            log.error("purge_manifest_invalid", manifest_id=manifest.id, reason="recorded_without_scope")
            await ending.fail(MANIFEST_INVALID)
            return
    if content is None or not await _phase_b(run, proc, content, ending):  # 3.
        return
    await afault_point(BEFORE_COMPLETE)
    await _phase_c(ctx, run, proc, content, ending)  # 4.


def _adoptable(data: bytes, manifest: ManifestRow) -> ManifestContent | None:
    """The content of the file a crash left before A3, when it can be adopted (module docstring)."""
    try:
        content = parse_manifest(data, file_name=manifest_file_name(manifest.manifest_uuid))
    except ManifestInvalid as invalid:
        reason: str = invalid.reason
    else:
        if content["kind"] != manifest.kind:
            reason = "kind_mismatch"
        elif canonical_bytes(content) != data:
            reason = "not_canonical"
        else:
            return content
    log.error("purge_manifest_invalid", manifest_id=manifest.id, reason=reason)
    return None


def _unparsable(content: ManifestContent, manifest: ManifestRow) -> str | None:
    """Why a replay would refuse the file of a computed content (`parse_manifest` of its canonical bytes,
    under its own name), or `None` when it would read the content back unchanged."""
    data = canonical_bytes(content)
    try:
        parsed = parse_manifest(data, file_name=manifest_file_name(manifest.manifest_uuid))
    except ManifestInvalid as invalid:
        return invalid.reason
    return None if canonical_bytes(parsed) == data else "not_canonical"


def _owner(run: PurgeRun) -> PurgeOwner:
    if run.owner is None:
        raise RuntimeError("the worker's purge is a live one")
    return run.owner


async def _admit(run: PurgeRun, proc: KindProcedure, ending: _Ending) -> ManifestContent | None:
    """A1-A3 (module docstring): the recorded content, or `None` once the operation was ended or requeued."""
    manifest, owner = run.manifest, _owner(run)
    directory = run.cfg.manifests_dir
    existing = read_manifest_file(directory, manifest.manifest_uuid)
    if existing is not None:  # a crash after the rename: adopted, never recomputed
        adopted = _adoptable(existing, manifest)
        if adopted is None:
            await ending.fail(MANIFEST_INVALID)
            return None
        content, sha = adopted, hashlib.sha256(existing).digest()
        log.info("purge_manifest_adopted", manifest_id=manifest.id)
    else:
        since = await fence_committed_at(run.engine)
        if not await wait_for_quiesce(run.engine, since=since, clock=ending.clock, sleep=asyncio.sleep):
            log.warning("purge_quiesce_timeout", manifest_id=manifest.id)
            await ending.requeue(QUIESCE_TIMEOUT_CODE)
            return None
        async with run.engine.begin() as conn:
            content = await compute_content(conn, proc, manifest, fenced_at=since, now=run.now)
        # the check a replay applies to the file, before anything is written
        if (reason := _unparsable(content, manifest)) is not None:
            log.error("purge_manifest_invalid", manifest_id=manifest.id, reason=reason, computed=True)
            await ending.fail(MANIFEST_INVALID)
            return None
        await afault_point(BEFORE_FILE)
        sha = write_manifest_file(directory, content)
        await afault_point(AFTER_FILE)
        log.info("purge_manifest_written", manifest_id=manifest.id, operations=len(content["operations"]))
    async with run.engine.begin() as conn:  # A3
        await fence_epoch(conn, owner.epoch)
        await lock_operation_owner(conn, owner.claim)
        await record_manifest(conn, manifest, content, sha)
    await afault_point(AFTER_RECORD)
    return content


async def _tries(run: PurgeRun, phase: str, step: Callable[[], Awaitable[object]]) -> bool:
    """Run `step`, again from its start after a retryable database error, `PHASE_TRIES` tries in all (Ruling
    T12-1); `False` when every try lost."""
    # Imported here: the api imports the kinds' requests (and so this module), never langgraph.
    from jobhunter.graphs.sanitize import is_retryable_db_error

    for tries in range(1, PHASE_TRIES + 1):
        try:
            await step()
        except DBAPIError as exc:
            if not is_retryable_db_error(exc):
                raise
            log.warning(
                "purge_phase_lost_a_lock",
                manifest_id=run.manifest.id,
                phase=phase,
                tries=tries,
                sqlstate=pg_error(exc).sqlstate,
            )
            continue
        return True
    return False


async def _phase_b(run: PurgeRun, proc: KindProcedure, content: ManifestContent, ending: _Ending) -> bool:
    """Phase B on the stored content (module docstring); `False` once the operation was given back."""
    exports = [op for op in content["operations"] if is_export_op(op)]
    try:
        if not await _tries(run, "execute", lambda: proc.execute(run, content)):
            await ending.requeue(LOCK_CONFLICT)
            return False
        await afault_point(MID_DELETE)
        if proc.deletes_exports and exports:
            if not await _tries(run, "exports", lambda: purge_exports(run, exports)):
                await ending.requeue(LOCK_CONFLICT)
                return False
    except PurgeWait as wait:
        log.info("purge_waiting", manifest_id=run.manifest.id, error_code=wait.error_code)
        await ending.wait(wait.error_code, wait.retry_after)
        return False
    except CoMemberChanged:
        log.warning("purge_chunk_gave_up", manifest_id=run.manifest.id)
        await ending.requeue(CO_MEMBER_CHANGED)
        return False
    return True


class _Violated(Exception):
    """Phase C found violations: its transaction rolls back."""

    def __init__(self, violations: list[Violation]) -> None:
        super().__init__(len(violations))
        self.violations = violations


async def _phase_c(
    ctx: OperationContext, run: PurgeRun, proc: KindProcedure, content: ManifestContent, ending: _Ending
) -> None:
    """Phase C (module docstring): one transaction that releases the fences, checks every postcondition and
    completes, or a rollback and the operation `failed('postcondition')`."""
    manifest_id, owner = run.manifest.id, _owner(run)
    try:
        async with run.engine.begin() as conn:
            await fence_epoch(conn, owner.epoch)
            await lock_operation_owner(conn, owner.claim)
            if isinstance(proc, CompletingKind):
                await proc.complete(conn, content)
            await release_fences(conn, manifest_id)
            found = await check_postconditions(conn, content, files=run.files)
            found += await proc.postconditions(conn, content)
            if found:
                raise _Violated(found)
            await complete_manifest(conn, manifest_id)
            await resolve_alert(conn, code=PURGE_FAILED_ALERT, subject=_subject(manifest_id))
            await ctx.finish_in(conn, status="succeeded")
    except _Violated as violated:
        log.error(
            "purge_postcondition_failed",
            manifest_id=manifest_id,
            violations=[dataclasses.asdict(v) for v in violated.violations],
        )
        await ending.fail(POSTCONDITION)
        return
    log.info("purge_completed", manifest_id=manifest_id, kind=run.manifest.kind)
