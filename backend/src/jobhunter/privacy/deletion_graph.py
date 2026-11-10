"""The purges' deletion graph (spec §20.2, §20.4, §10.10, §7.16.6; plan M5 Task 11, P1, G04, M5-R3, Deviation
13): what a purge deletes beyond the rows that cascade, and what it settles first. Tasks 12 to 16 (the purge
kinds) call it from their chunks.

**Target-set deletion** (Task 11). `purge_target_set(conn, target_ids, *, manifest_id, now) -> PurgedTargets`
deletes a set T of at most `scoring.targets.TARGET_SET_CHUNK` (200) score targets with every row they own
(`targets.delete_target_set`), for the purge of the deletion manifest `manifest_id`, in the caller's
transaction. The caller has committed its fence over T's jobs (A0) and holds `active_config FOR SHARE` and the
job rows `FOR UPDATE` in id order (steps 2 and 4), so no batch-item processing, publication or cancellation
of T's targets runs meanwhile (each takes a job row first), and no batch preparation batches them
(`batches.ELIGIBLE_SQL` leaves fenced jobs out). M3's `delete_target_set` alone would delete a batched
target's item and call while its batch can still return results: its late line then names no item of ours
(`batch.custom_id_anomaly`) and the submission stays `ended` for good, holding `batch_one_in_flight` (P1); and
an online try's entry would lose its call unsettled (G04). So, in the lock schedule's order:
1. the batch submissions that hold an item of T and are still live or released (`batches.IN_FLIGHT`,
   `released`) `FOR UPDATE` in id order (step 5, their owner rows), then T's items in them, read under those
   locks;
2. the accounts of every entry settled below and of no other (`spend_ledger.lock_for_purge`: their month
   rows, then their funded bulk rows: the entries of each `preparing` submission, the `dispatched` entries of
   T's other items, and T's online entries read now) and the bulk rows of T's unreleased follow-up items
   (`bulk.lock_for_targets`, which `delete_target_set` takes again), steps 6 and 7, before any row of step
   10;
3. each `preparing` submission abandoned (`batches.abandon_preparing`: every entry `not_billed(
   'never_dispatched')`, its targets `pending`, their attempts `cancelled('batch_abandoned')`; no follow-on),
   whichever of its targets T holds: the execution that prepared it is fenced at its next step
   (`submit_start`), so none of its requests is sent;
4. the cancel recorded (`batches.record_cancel`, sent by the poll after the commit) on each
   `submit_started`, `submit_ambiguous`, `submitted` or `ended` submission that holds an `in_batch` target of
   T; `delete_target_set`'s step 1 cancels T's targets individually (`batch_submission_id := NULL`), and the
   batch's other targets stay `in_batch`: their items come back `canceled` and retry online
   (`batch_canceled`, M5-R3);
5. every item of T in such a submission (any but a `preparing` one), whatever its target's state, with its
   entry `dispatched` → `ambiguous` (`spend_ledger.settle`, counted at its reservation until the month
   closes; its call, deleted below, `ambiguous` with `PURGED_CODE`): a target cancelled individually out of
   a running batch (§10.10) leaves its item in flight too. Then every such item still `awaiting` gives its
   follow-up hold back (`bulk.release_item_hold`): no decision of a deleted item ever will;
6. one `purged_batch_items (submission_id, custom_id, manifest_id)` row, ids only, for every item of T whose
   submission can still return results (`submit_started`, `submit_ambiguous`, `submitted`, `ended`,
   `released`: a release moves its targets out of `in_batch` and settles their entries, yet its batch is
   still polled or adopted). It is the positive evidence on which the poll skips the item's late line as
   `purged` (any other line of no item stays foreign) and the adoption keeps the submission's original
   membership (`scoring.batch_results`, `scoring.batch_adoption`); it goes with its submission;
7. `delete_target_set(T, on_cancelled=...)`. Its hook runs once its step 1 has cancelled T, so no try of T's
   can be reserved or dispatched any more (`mark_dispatched` re-checks its owner under the owner lock): T's
   online entries, those read before step 2 and any its attempts' calls hold now (`reserved` or `dispatched`,
   not a batch item's), are taken again (`lock_for_purge`, its rows current after the writes above) and
   settled (`spend_ledger.settle_for_purge`: a `reserved` one released, the counters `-r` and a funded
   allocation credited; a `dispatched` one `ambiguous`). Every later reader of an online entry joins its
   call, so one left unsettled when its call goes could never be settled or recovered; the coroutine's late
   `settle` or `release_never_dispatched` finds it alone, a no-op. The hook's read exists for one race: a try
   of a target of T reserved after the first read, which committed before step 1 took its target. Its locks
   are held already, but for that try's month row and funded bulk row when no entry of step 2 shared them:
   those it takes first here, after rows of step 10 (T's targets and attempts), the one place the chunk
   leaves the schedule. Their partners: that try's own late `settle` (its month row, then its attempt, which
   the cancel ended under this chunk's lock), and a funded writer that holds that bulk row while it waits for
   a month or target row this chunk holds.
`delete_target_set` then deletes T with its items and calls; the ledger rows stay. `ranking.bump` is the
caller's, last.

**The chunk can lose a deadlock** (40P01), which its order (the month and bulk rows before the target rows,
which a non-owner takes at step 10) keeps possible, so **the caller retries the whole chunk transaction on a
retryable database error** (`graphs.sanitize.is_retryable_db_error`: a deadlock, a serialization failure, a
lost connection; Task 12's `privacy.purge.run_purge`, which re-runs the phase from its start, three
tries in all, then requeues `lock_conflict`: Ruling T12-1). The partners:
- a score attempt reserving a try for a target of T (`spend_ledger.reserve`, which takes no job row): it holds
  its target `FOR SHARE` (the owner row, step 5) and waits for a month row this chunk holds, while the chunk
  waits for that target (step 10, as a non-owner), as with every non-owner mover of a target
  (`targets._cancel`); `llm_ledger_unsettled`'s recovery is the same pair (the owner `FOR UPDATE`, then the
  month);
- a funded writer on a bulk row: a funded reservation, settlement or follow-up hold release that holds a
  target or month row and waits for a bulk row this chunk holds (the pair M3's `delete_target_set` has with
  its `bulk.lock_for_targets`);
- the late locks of step 7 above.
PostgreSQL aborts one of the pair. Either outcome is whole: an aborted try retries and meets the cancel
(`ClaimLost`); an aborted chunk wrote nothing, and its retry finds what is left (over targets deleted already
it is a no-op).

**The exports** (M5 Task 12; §20.3 L6507, M5-R3, Deviation 18). Every kind but `job_tombstone` deletes every
export (`KindProcedure.deletes_exports`), since one may hold the purged content. At A2, `export_ops(conn,
max_export_id=...)` names the `running` and `ready` exports within the bound: a `field_redact exports
storage_path` with the marker `status = 'purged'` (any content-free state satisfies it: `manifests`), and a
`file_remove exports` with the key `<id>-*` per export (`export_files_key`: a ready export's published
archive and an unpublished attempt's, Task 17). Phase B runs them, `purge_exports(run, ops)`, after the kind's
own chunks, with Task 17's table of export states: in one chunk (`PurgeRun.chunk`, no job rows) a named row
still `running` or `ready` → `purged` with `storage_path` NULL, a row already content-free (`failed`,
`expired`, `purged`) left as it is; the files are unlinked only after that commit, so no committed `ready`
row ever names a missing file, and a crash in between leaves only files no row names, which the idempotent
re-execution removes. A running export's own publication then finds its row `purged` and unlinks what it
wrote (Task 17).

**Job-graph deletion** (M5 Task 13; §20.2's tombstoning steps 4-7, Deviation 50). `delete_job_graph(conn,
job_id, *, write_tombstone, held, now) -> JobGraphDeleted` is the one complete cleanup before a job goes,
which job tombstoning (`write_tombstone=True`), the Adzuna purge and delete-everything (`False`) share. It
deletes J and, first and deepest first, every job whose `merged_into_job_id` leads to J
(`jobs.merged_into_job_id` restricts deleting J): the graph G, read under the caller's lock. The caller's
transaction holds the rows of G and of every co-member of their duplicate groups `FOR UPDATE` in one
ascending statement (`held`,
`PurgeRun.chunk(job_ids=G, with_groups_of=G)`), and the caller has deleted G's targets in chunks of their
own (`purge_target_set`): a job of G it does not hold, or a target of G left (by job or by revision), is an
`InvariantViolation` before anything is written. A J that no longer exists is no error: nothing is deleted
(the chunk of a re-execution after the commit). In order:
- (4) the golden cases on G's revisions, their rows `FOR UPDATE`: an active one is an `InvariantViolation`
  (tombstoning left such a job out, the Adzuna purge and delete-everything delete every case that draws on
  the purged data first); the inactive ones are deleted before the revisions, since
  `golden_cases.job_revision_id` restricts, with their `eval_case_results` and eval calls (cascade). The
  entries of those calls still `reserved` or `dispatched` (a run that included the case before it was made
  inactive) are settled first, as the golden-case deletion settles them (`spend_ledger.lock_for_purge`, then
  the cases' rows, the entries read again under them, `settle_for_purge`): a call's cascade must leave no
  entry unsettled (G04);
- (5) the `merge_events` naming a job of G as `from_job_id` or `into_job_id` (both restrict), every
  `reversed_by_event_id` that points at one of them cleared first; the `identity_conflicts` whose
  `posting_job_id` is in G deleted, G removed from the other conflicts' `candidate_job_ids`, and a conflict
  left without a candidate deleted, the `dedup.identity_conflict` alert of each deleted conflict resolved;
  each job of G leaves its duplicate group, deepest first, through `groups.leave_group(reason=MEMBER_LEFT,
  held=held)`: it checks that every member is held, dissolves a group left with fewer than two members or
  re-elects one whose representative left (so `duplicate_groups.representative_job_id`, a restricting key,
  names no job of G) and settles the group's deferred targets (a survivor's target no longer deferrable moves
  to `pending`, D01); `representative.reelect` alone would neither check the locks nor settle. Then each
  job's `dedup.ambiguous_grouping` alert is resolved (M1 Carry-forward 11), also when it was in no group,
  and its `dedup.field_conflict` alerts (subject `job:<id>:<field>`), as a merge resolves its from-job's
  (carry 7): no later event of a deleted job would resolve either;
- (6) `current_revision_id := NULL` (a deferred key), G's revisions deleted (lineage, occurrences cascade),
  then its postings (observations, `ad_key_sources`, snapshot absences and the conflicts on them cascade);
  each key G owns that another job's posting also supplies is re-assigned to the job of its earliest
  remaining provenance row (`first_seen_at`, then the posting id, as the unmerge orders them), and G's
  other `ad_keys` are deleted. The foreign key of a re-assigned key takes `FOR KEY SHARE` on its heir's row:
  the one lock of this transaction outside the schedule, a lost deadlock its caller retries;
- (7) with `write_tombstone`, J's `job_tombstones` row (`former_job_id = J`, `tombstoned_at = now`), whose
  `user_actions` holds J's four user-action timestamps `seen_at`, `saved_at`, `applied_at` and `hidden_at`
  as ISO 8601 with their offset (`null` when unset; never the note, M3 ledger T22-b: what the identity
  resolution's restore reads, §8.6 step 3), and a `tombstone_keys` row per deleted key
  (`identity.tombstone_key_hash`), upserted `ON CONFLICT (key_hash) DO UPDATE SET tombstone_id =
  EXCLUDED.tombstone_id` (P15): a key a later tombstone holds names the later one. A re-assigned key is
  hashed by no tombstone: it still has an owner. Then G's `user_job_actions` and the jobs, deepest first.
`det_results`, `llm_results` and the targets went with the targets; `score_feedback` keeps its row with the
job and revision NULL (`ON DELETE SET NULL`). `ranking.bump` is the caller's, last.

Ids, counts and states only reach a log line.
"""

from collections.abc import Collection, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Final

from sqlalchemy import TextClause
from sqlalchemy.engine import Row
from sqlalchemy.ext.asyncio import AsyncConnection

from jobhunter.db.sql import q
from jobhunter.db.transitions import transition_where
from jobhunter.dedup import groups
from jobhunter.dedup.identity import tombstone_key_hash
from jobhunter.dedup.merge import IDENTITY_CONFLICT_ALERT
from jobhunter.dedup.representative import AMBIGUOUS_GROUPING
from jobhunter.dedup.revisions import FIELD_CONFLICT_ALERT
from jobhunter.dedup.user_actions import ACTION_TIMESTAMPS, to_iso
from jobhunter.errors import InvariantViolation
from jobhunter.llm import bulk, spend_ledger
from jobhunter.llm.spend_ledger import CallOutcome, PurgeSettled, Reservation
from jobhunter.normalize.fields import CONFLICT_FIELDS
from jobhunter.normalize.keys import AdKey
from jobhunter.observability.alerts import resolve_alert
from jobhunter.observability.logging import get_logger
from jobhunter.privacy.manifests import KEY_WILDCARD, Operation, remove_files
from jobhunter.scoring import batches, targets
from jobhunter.types import JSONValue

if TYPE_CHECKING:
    from jobhunter.privacy.purge import PurgeRun

# The call's code of a batch entry a purge made `ambiguous` (the call then goes with its item).
PURGED_CODE: Final = "purged"
LOGGED_IDS: Final = 20  # the target ids a log line names, at most
# The submissions a purge locks: every one that can still return results, and a `preparing` one.
_LOCKED_STATES: Final = (*batches.IN_FLIGHT, "released")
# Those that can still return results: an item of theirs a purge deletes leaves its evidence (step 6).
_RETURNING: Final = tuple(state for state in _LOCKED_STATES if state != "preparing")
_SET: Final = "CAST(:ids AS bigint[])"
_SUBMISSIONS: Final = "CAST(:subs AS bigint[])"
_SUBMISSIONS_OF_SET: Final = q(
    f"SELECT DISTINCT submission_id FROM batch_items WHERE target_id = ANY({_SET})"  # noqa: S608
)
_LOCK_SUBMISSIONS: Final = q(
    f"SELECT id, state FROM batch_submissions WHERE id = ANY({_SUBMISSIONS}) "  # noqa: S608
    "AND state = ANY(CAST(:states AS text[])) ORDER BY id FOR UPDATE"
)
# T's items in the locked submissions, with their targets' pins and their entries, in id order.
_ITEMS_OF_SET: Final = q(
    "SELECT i.id, i.submission_id, i.decision, i.hold_released_at, t.state AS target_state, "  # noqa: S608
    "t.batch_submission_id, c.id AS call_id, l.id AS ledger_id, l.state AS entry_state, l.budget_month, "
    "l.reserved_usd, l.purpose_class, l.model_requested FROM batch_items i "
    "JOIN score_targets t ON t.id = i.target_id JOIN llm_calls c ON c.id = i.llm_call_id "
    "JOIN llm_spend_ledger l ON l.id = c.spend_ledger_id "
    f"WHERE i.target_id = ANY({_SET}) AND i.submission_id = ANY({_SUBMISSIONS}) ORDER BY i.id"
)
# Every entry of the given submissions' items (a `preparing` one's, which its abandon releases).
_ENTRIES_OF_SUBMISSIONS: Final = q(
    "SELECT c.spend_ledger_id FROM batch_items i JOIN llm_calls c ON c.id = i.llm_call_id "  # noqa: S608
    f"WHERE i.submission_id = ANY({_SUBMISSIONS}) ORDER BY c.spend_ledger_id"
)
# T's online entries still unsettled: those of its attempts' calls, never a batch item's.
_ONLINE_ENTRIES: Final = q(
    "SELECT l.id FROM llm_calls c JOIN llm_spend_ledger l ON l.id = c.spend_ledger_id "  # noqa: S608
    f"WHERE c.attempt_id IN (SELECT id FROM score_attempts WHERE target_id = ANY({_SET})) "
    "AND NOT l.batch AND l.state IN ('reserved', 'dispatched') ORDER BY l.id"
)
_RECORD_PURGED: Final = q(
    "INSERT INTO purged_batch_items (submission_id, custom_id, manifest_id) "  # noqa: S608
    "SELECT i.submission_id, i.custom_id, :manifest FROM batch_items i "
    f"JOIN batch_submissions s ON s.id = i.submission_id WHERE i.target_id = ANY({_SET}) "
    "AND s.state = ANY(CAST(:states AS text[])) ORDER BY i.submission_id, i.custom_id"
)

log = get_logger("jobhunter.privacy.deletion_graph")


@dataclass(frozen=True)
class PurgedTargets:
    """What `purge_target_set` did: the targets it deleted, the submissions whose cancel it recorded and those
    it abandoned, the batch entries it made `ambiguous`, and the online entries it released and made
    `ambiguous`."""

    targets: int
    submissions_cancel_requested: tuple[int, ...]
    submissions_abandoned: tuple[int, ...]
    entries_ambiguous: int
    online_entries_released: int
    online_entries_ambiguous: int


def _reservation(item: Row[tuple[object, ...]]) -> Reservation:
    return Reservation(
        ledger_id=int(item.ledger_id),
        llm_call_id=int(item.call_id),
        month=item.budget_month,
        reserved_usd=item.reserved_usd,
        purpose_class=item.purpose_class,
        model=item.model_requested,
        batch=True,
    )


async def _ids(conn: AsyncConnection, statement: TextClause, params: dict[str, object]) -> list[int]:
    return [int(i) for i in (await conn.execute(statement, params)).scalars()]


async def purge_target_set(
    conn: AsyncConnection, target_ids: Sequence[int], *, manifest_id: int, now: datetime
) -> PurgedTargets:
    """Delete the targets `target_ids` (at most `targets.TARGET_SET_CHUNK`) for the purge of manifest
    `manifest_id`, settling and recording first what their batches and online tries still hold (module
    docstring), in the caller's transaction; `ValueError` for a larger set.

    The chunk can lose a deadlock (40P01) to a concurrent score reservation, which takes its target before
    its month row, or to a funded writer on a bulk row (module docstring): the caller retries the whole chunk
    transaction on a retryable database error (`graphs.sanitize.is_retryable_db_error`)."""
    ids = sorted(set(target_ids))
    if len(ids) > targets.TARGET_SET_CHUNK:
        raise ValueError(f"a purge chunk takes at most {targets.TARGET_SET_CHUNK} targets, not {len(ids)}")
    if not ids:
        return PurgedTargets(0, (), (), 0, 0, 0)
    candidates = await _ids(conn, _SUBMISSIONS_OF_SET, {"ids": ids})
    locked: dict[int, str] = {}
    if candidates:  # 1. the owner rows (step 5), then T's items under them
        params = {"subs": candidates, "states": list(_LOCKED_STATES)}
        locked = {int(r.id): str(r.state) for r in await conn.execute(_LOCK_SUBMISSIONS, params)}
    items = (await conn.execute(_ITEMS_OF_SET, {"ids": ids, "subs": list(locked)})).all() if locked else []
    preparing = sorted(s for s, state in locked.items() if state == "preparing")
    in_flight = [item for item in items if locked[int(item.submission_id)] != "preparing"]
    batched = sorted(
        {
            int(item.submission_id)
            for item in in_flight
            if item.target_state == "in_batch" and item.batch_submission_id == item.submission_id
        }
    )
    online = await _ids(conn, _ONLINE_ENTRIES, {"ids": ids})
    abandoned_entries = await _ids(conn, _ENTRIES_OF_SUBMISSIONS, {"subs": preparing}) if preparing else []
    # 2. steps 6 and 7, before any row of step 10: the accounts of the entries settled below, and no other
    dispatched = [int(item.ledger_id) for item in in_flight if item.entry_state == "dispatched"]
    entries = [*abandoned_entries, *dispatched, *online]
    await spend_ledger.lock_for_purge(conn, entries, now=now)
    await bulk.lock_for_targets(conn, ids)
    for submission_id in preparing:  # 3.
        await batches.abandon_preparing(conn, submission_id, now=now)
    requested = tuple(await batches.record_cancel(conn, submission_ids=batched, now=now))  # 4.
    ambiguous = released_holds = 0
    call = CallOutcome("ambiguous", error_code=PURGED_CODE)
    for item in in_flight:  # 5. the month row, the funded bulk row, the ledger rows, the attempt
        if item.entry_state == "dispatched":
            await spend_ledger.settle(
                conn, _reservation(item), outcome="ambiguous", evidence=None, usage=None, call=call, now=now
            )
            ambiguous += 1
    for item in in_flight:  # the bulk row, then the item
        if item.decision is None and item.hold_released_at is None:
            await bulk.release_item_hold(conn, item_id=int(item.id))
            released_holds += 1
    params = {"ids": ids, "manifest": manifest_id, "states": list(_RETURNING)}
    recorded = (await conn.execute(_RECORD_PURGED, params)).rowcount  # 6.
    settled = PurgeSettled(0, 0)

    async def settle_online(cancelled: AsyncConnection) -> None:  # 7., T cancelled (module docstring)
        nonlocal settled
        current = sorted({*online, *await _ids(cancelled, _ONLINE_ENTRIES, {"ids": ids})})
        rows = await spend_ledger.lock_for_purge(cancelled, current, now=now)
        settled = await spend_ledger.settle_for_purge(cancelled, current, rows, now=now)

    deleted = await targets.delete_target_set(conn, ids, now=now, on_cancelled=settle_online)
    purged = PurgedTargets(
        targets=deleted,
        submissions_cancel_requested=requested,
        submissions_abandoned=tuple(preparing),
        entries_ambiguous=ambiguous,
        online_entries_released=settled.released,
        online_entries_ambiguous=settled.ambiguous,
    )
    log.info(
        "purge_target_set_deleted",
        manifest_id=manifest_id,
        targets=purged.targets,
        submissions_cancel_requested=list(requested),
        submissions_abandoned=preparing,
        entries_ambiguous=ambiguous,
        holds_released=released_holds,
        purged_batch_items=recorded,
        online_entries_released=settled.released,
        online_entries_ambiguous=settled.ambiguous,
        target_ids=ids[:LOGGED_IDS],
    )
    return purged


# ---- the exports (M5 Task 12) ------------------------------------------------------------------------------

# The states of an export that hold content: a purge's export op names them and moves them `purged`.
EXPORT_LIVE_STATES: Final = ("running", "ready")
_LIVE_EXPORTS: Final = q(
    "SELECT id FROM exports WHERE status = ANY(CAST(:states AS text[])) AND id <= :bound ORDER BY id"
)


def export_files_key(export_id: int) -> str:
    """The `file_remove` key of an export's files: `<id>-*` (module docstring)."""
    return f"{export_id}-{KEY_WILDCARD}"


def is_export_op(op: Operation) -> bool:
    """Whether `op` is one of `export_ops`' (a purge runs those with `purge_exports`, not the kind's
    `execute`)."""
    return (op["op"] == "field_redact" and op["table"] == "exports") or (
        op["op"] == "file_remove" and op["volume"] == "exports"
    )


async def export_ops(conn: AsyncConnection, *, max_export_id: int) -> list[Operation]:
    """A2: the operations that purge the `running` and `ready` exports with an id up to `max_export_id`
    (module docstring); none when there is no such export."""
    params = {"states": list(EXPORT_LIVE_STATES), "bound": max_export_id}
    ids = [int(i) for i in (await conn.execute(_LIVE_EXPORTS, params)).scalars()]
    if not ids:
        return []
    return [
        {
            "op": "field_redact",
            "table": "exports",
            "ids": ids,
            "columns": ["storage_path"],
            "marker": {"status": "purged"},
        },
        {"op": "file_remove", "volume": "exports", "keys": [export_files_key(i) for i in ids]},
    ]


async def purge_exports(run: "PurgeRun", ops: Sequence[Operation]) -> int:
    """Phase B's export ops (module docstring): the named rows still `running` or `ready` → `purged` in one
    chunk, then, after its commit, the files the keys name unlinked; how many rows moved."""
    ids = sorted(
        {i for op in ops if op["op"] == "field_redact" and op["table"] == "exports" for i in op["ids"]}
    )
    purged = 0
    if ids:
        async with run.chunk() as chunk:
            moved = await transition_where(
                chunk.conn,
                "exports",
                list(EXPORT_LIVE_STATES),
                "purged",
                where="t.id = ANY(CAST(:ids AS bigint[]))",
                params={"ids": ids},
                set_={"storage_path": None},
            )
            purged = len(moved)
    keys = [
        key for op in ops if op["op"] == "file_remove" and op["volume"] == "exports" for key in op["keys"]
    ]
    removed = remove_files(run.cfg.exports_dir, keys)
    log.info(
        "purge_exports_done",
        manifest_id=run.manifest.id,
        exports_purged=purged,
        files_removed=removed,
        export_ids=ids[:LOGGED_IDS],
    )
    return purged


# ---- the job graph (M5 Task 13) ----------------------------------------------------------------------------

_JOBS: Final = "CAST(:jobs AS bigint[])"
# J and every job whose `merged_into_job_id` leads to it, deepest first (J, depth 0, last).
_GRAPH_OF: Final = q(
    "WITH RECURSIVE g(id, depth) AS (SELECT id, 0 FROM jobs WHERE id = :job "
    "UNION ALL SELECT j.id, g.depth + 1 FROM jobs j JOIN g ON j.merged_into_job_id = g.id) "
    "SELECT id FROM g ORDER BY depth DESC, id"
)
_TARGETS_LEFT: Final = q(
    f"SELECT count(*) FROM score_targets WHERE job_id = ANY({_JOBS}) "  # noqa: S608
    f"OR job_revision_id IN (SELECT id FROM job_revisions WHERE job_id = ANY({_JOBS}))"
)
_CASES_OF_GRAPH: Final = (
    "SELECT g.id, g.active FROM golden_cases g JOIN job_revisions r ON r.id = g.job_revision_id "  # noqa: S608
    f"WHERE r.job_id = ANY({_JOBS}) ORDER BY g.id"
)
_CASES: Final = q(_CASES_OF_GRAPH)
_LOCK_CASES: Final = q(f"{_CASES_OF_GRAPH} FOR UPDATE OF g")
# The entries of the cases' eval calls that a reader could still settle: `reserved` or `dispatched`.
_CASE_ENTRIES: Final = q(
    "SELECT l.id FROM llm_calls c JOIN llm_spend_ledger l ON l.id = c.spend_ledger_id "
    "WHERE c.golden_case_id = ANY(CAST(:cases AS bigint[])) AND l.state IN ('reserved', 'dispatched') "
    "ORDER BY l.id"
)
_DELETE_CASES: Final = q("DELETE FROM golden_cases WHERE id = ANY(CAST(:cases AS bigint[]))")
_EVENTS_NAMING: Final = q(
    f"SELECT id FROM merge_events WHERE from_job_id = ANY({_JOBS}) OR into_job_id = ANY({_JOBS}) "  # noqa: S608
    "ORDER BY id FOR UPDATE"
)
_EVENTS: Final = "CAST(:events AS bigint[])"
_UNREVERSE: Final = q(
    f"UPDATE merge_events SET reversed_by_event_id = NULL WHERE reversed_by_event_id = ANY({_EVENTS})"  # noqa: S608
)
_DELETE_EVENTS: Final = q(f"DELETE FROM merge_events WHERE id = ANY({_EVENTS})")  # noqa: S608
_CONFLICTS_NAMING: Final = q(
    "SELECT id, posting_job_id, candidate_job_ids FROM identity_conflicts "  # noqa: S608
    f"WHERE posting_job_id = ANY({_JOBS}) OR candidate_job_ids && {_JOBS} ORDER BY id FOR UPDATE"
)
_DELETE_CONFLICT: Final = q("DELETE FROM identity_conflicts WHERE id = :id")
_SET_CANDIDATES: Final = q(
    "UPDATE identity_conflicts SET candidate_job_ids = CAST(:candidates AS bigint[]) WHERE id = :id"
)
_UNPOINT: Final = q(f"UPDATE jobs SET current_revision_id = NULL WHERE id = ANY({_JOBS})")  # noqa: S608
_DELETE_REVISIONS: Final = q(f"DELETE FROM job_revisions WHERE job_id = ANY({_JOBS})")  # noqa: S608
_LOCK_KEYS: Final = q(
    f"SELECT key_ns, key_value FROM ad_keys WHERE job_id = ANY({_JOBS}) "  # noqa: S608
    "ORDER BY key_ns, key_value FOR UPDATE"
)
_DELETE_POSTINGS: Final = q(f"DELETE FROM postings WHERE job_id = ANY({_JOBS})")  # noqa: S608
# Each key of G that another job's posting still supplies goes to the job of its earliest provenance row.
_REASSIGN_KEYS: Final = q(
    "UPDATE ad_keys k SET job_id = heir.job_id FROM (SELECT DISTINCT ON (s.key_ns, s.key_value) "  # noqa: S608
    "s.key_ns, s.key_value, p.job_id FROM ad_key_sources s JOIN postings p ON p.id = s.posting_id "
    f"JOIN ad_keys o ON o.key_ns = s.key_ns AND o.key_value = s.key_value WHERE o.job_id = ANY({_JOBS}) "
    "ORDER BY s.key_ns, s.key_value, s.first_seen_at, s.posting_id) heir "
    "WHERE k.key_ns = heir.key_ns AND k.key_value = heir.key_value RETURNING k.key_ns"
)
_DELETE_KEYS: Final = q(
    f"DELETE FROM ad_keys WHERE job_id = ANY({_JOBS}) RETURNING key_ns, key_value"  # noqa: S608
)
_ACTIONS_OF: Final = q(
    f"SELECT {', '.join(ACTION_TIMESTAMPS)} FROM user_job_actions WHERE job_id = :job"  # noqa: S608
)
_TOMBSTONE: Final = q(
    "INSERT INTO job_tombstones (former_job_id, user_actions, tombstoned_at) VALUES (:job, :actions, :now) "
    "RETURNING id",
    jsonb=["actions"],
)
_TOMBSTONE_KEYS: Final = q(
    "INSERT INTO tombstone_keys (key_hash, tombstone_id) "
    "SELECT h, :tombstone FROM unnest(CAST(:hashes AS bytea[])) AS h "
    "ON CONFLICT (key_hash) DO UPDATE SET tombstone_id = EXCLUDED.tombstone_id"
)
_DELETE_ACTIONS: Final = q(f"DELETE FROM user_job_actions WHERE job_id = ANY({_JOBS})")  # noqa: S608
_DELETE_JOB: Final = q("DELETE FROM jobs WHERE id = :job")


@dataclass(frozen=True)
class JobGraphDeleted:
    """What `delete_job_graph` deleted: the jobs (deepest first, J last; none when J was gone already), their
    revisions and postings, how many keys it re-assigned, and J's tombstone (`None` without one)."""

    job_ids: tuple[int, ...]
    revisions: int
    postings: int
    keys_reassigned: int
    tombstone_id: int | None


async def _delete_cases(conn: AsyncConnection, jobs: list[int], *, now: datetime) -> int:
    """Step 4 (module docstring): the inactive golden cases on G's revisions, their in-flight eval entries
    settled first; an active one is an `InvariantViolation`. How many were deleted."""
    read = [int(row.id) for row in await conn.execute(_CASES, {"jobs": jobs})]
    if not read:
        return 0
    entries = await _ids(conn, _CASE_ENTRIES, {"cases": read})
    rows = await spend_ledger.lock_for_purge(conn, entries, now=now)  # steps 6 and 7
    cases = (await conn.execute(_LOCK_CASES, {"jobs": jobs})).all()  # step 10
    if active := [int(case.id) for case in cases if case.active]:
        raise InvariantViolation(f"jobs {jobs}: active golden cases {active} draw on their revisions")
    ids = [int(case.id) for case in cases]
    current = await _ids(conn, _CASE_ENTRIES, {"cases": ids}) if ids else []
    if not set(current) <= set(entries):  # an entry reserved after the first read (module docstring)
        rows = await spend_ledger.lock_for_purge(conn, current, now=now)
    await spend_ledger.settle_for_purge(conn, current, rows, now=now)
    if ids:
        await conn.execute(_DELETE_CASES, {"cases": ids})
    return len(ids)


async def _forget_merges_and_conflicts(conn: AsyncConnection, jobs: list[int]) -> tuple[int, int]:
    """Step 5's merge events and identity conflicts (module docstring); how many of each were deleted."""
    events = await _ids(conn, _EVENTS_NAMING, {"jobs": jobs})
    if events:
        await conn.execute(_UNREVERSE, {"events": events})
        await conn.execute(_DELETE_EVENTS, {"events": events})
    deleted = 0
    graph = set(jobs)
    for conflict in (await conn.execute(_CONFLICTS_NAMING, {"jobs": jobs})).all():
        candidates = [int(job) for job in conflict.candidate_job_ids if int(job) not in graph]
        if int(conflict.posting_job_id) in graph or not candidates:
            await conn.execute(_DELETE_CONFLICT, {"id": int(conflict.id)})
            await resolve_alert(conn, code=IDENTITY_CONFLICT_ALERT, subject=f"conflict:{conflict.id}")
            deleted += 1
        else:
            await conn.execute(_SET_CANDIDATES, {"id": int(conflict.id), "candidates": candidates})
    return len(events), deleted


async def _resolve_job_alerts(conn: AsyncConnection, job_id: int) -> None:
    """The open alerts about the job whose subject names it (module docstring): no later event of a deleted
    job would resolve them."""
    await resolve_alert(conn, code=AMBIGUOUS_GROUPING, subject=f"job:{job_id}")
    for field_name in CONFLICT_FIELDS:
        await resolve_alert(conn, code=FIELD_CONFLICT_ALERT, subject=f"job:{job_id}:{field_name}")


async def _tombstone(conn: AsyncConnection, job_id: int, keys: Sequence[AdKey], *, now: datetime) -> int:
    """Step 7's tombstone of J (module docstring); its id."""
    row = (await conn.execute(_ACTIONS_OF, {"job": job_id})).first()
    actions: dict[str, JSONValue] = {
        name: None if row is None else to_iso(getattr(row, name)) for name in ACTION_TIMESTAMPS
    }
    params = {"job": job_id, "actions": actions, "now": now}
    tombstone_id = int((await conn.execute(_TOMBSTONE, params)).scalar_one())
    if keys:
        hashes = [tombstone_key_hash(key) for key in keys]
        await conn.execute(_TOMBSTONE_KEYS, {"tombstone": tombstone_id, "hashes": hashes})
    return tombstone_id


async def delete_job_graph(
    conn: AsyncConnection, job_id: int, *, write_tombstone: bool, held: Collection[int], now: datetime
) -> JobGraphDeleted:
    """Delete job `job_id` and every job merged into it, with the graph their targets do not cover, §20.2's
    tombstoning steps 4-7 (module docstring), in the caller's transaction, which holds the jobs' rows and
    their groups' co-members' (`held`) and deleted their targets; J's tombstone with `write_tombstone`.
    `InvariantViolation` for a job of the graph not held, a target left or an active golden case."""
    jobs = await _ids(conn, _GRAPH_OF, {"job": job_id})
    if not jobs:
        return JobGraphDeleted((), 0, 0, 0, None)
    if unheld := sorted(set(jobs) - set(held)):
        raise InvariantViolation(f"job {job_id}: the rows of jobs {unheld} of its graph are not locked")
    if left := int((await conn.execute(_TARGETS_LEFT, {"jobs": jobs})).scalar_one()):
        raise InvariantViolation(f"job {job_id}: {left} targets of its graph are left")
    cases = await _delete_cases(conn, jobs, now=now)  # 4.
    events, conflicts = await _forget_merges_and_conflicts(conn, jobs)  # 5.
    for job in jobs:
        await groups.leave_group(conn, job, reason=groups.MEMBER_LEFT, held=held)
        await _resolve_job_alerts(conn, job)
    await conn.execute(_UNPOINT, {"jobs": jobs})  # 6.
    revisions = (await conn.execute(_DELETE_REVISIONS, {"jobs": jobs})).rowcount
    await conn.execute(_LOCK_KEYS, {"jobs": jobs})
    postings = (await conn.execute(_DELETE_POSTINGS, {"jobs": jobs})).rowcount
    reassigned = len((await conn.execute(_REASSIGN_KEYS, {"jobs": jobs})).all())
    keys = [
        AdKey(str(row.key_ns), str(row.key_value)) for row in await conn.execute(_DELETE_KEYS, {"jobs": jobs})
    ]
    tombstone_id = await _tombstone(conn, job_id, keys, now=now) if write_tombstone else None  # 7.
    await conn.execute(_DELETE_ACTIONS, {"jobs": jobs})
    for job in jobs:  # deepest first: `jobs.merged_into_job_id` restricts
        await conn.execute(_DELETE_JOB, {"job": job})
    deleted = JobGraphDeleted(tuple(jobs), revisions, postings, reassigned, tombstone_id)
    log.info(
        "job_graph_deleted",
        job_id=job_id,
        job_ids=jobs[:LOGGED_IDS],
        revisions=revisions,
        postings=postings,
        keys_reassigned=reassigned,
        keys_deleted=len(keys),
        golden_cases=cases,
        merge_events=events,
        identity_conflicts=conflicts,
        tombstone_id=tombstone_id,
    )
    return deleted
