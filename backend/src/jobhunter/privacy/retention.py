"""Daily retention (spec §20.1), of the M0 data, of the M1 collection data and of the M2 profile data: the
body of the `retention_daily` task.

Each rule removes what has passed its window. Every window is measured from the caller's `now`, never from the
database's `now()` (ruling P14), with one exception: procrastinate's finished jobs are deleted with
procrastinate's own predicate (`tasks.delete_finished_jobs`), which compares their latest event with the
database clock.

Each rule runs in a transaction of its own (ruling T13-e), or in bounded batches of their own ("Batches"
below), and in an error boundary of its own (ruling T13-g):
a rule that fails is reported as `-1`, logged `retention_rule_failed` and raised as the warn alert
`retention.failed` (subject: the rule's name; context: the rule and the error class, never the message), and
the rules after it still run. A rule that succeeds resolves its alert. Every rule is idempotent, so the next
day's run repeats whatever failed.

The rules run in dependency order (§20.1: a row goes only after what references it): the checkpoint threads
of a run's attempts go before the run, and a run's `run_attempts` go with it (`ON DELETE CASCADE`). A live
run (`queued` or `running`) is never deleted, however old.

**The M1 rules** (Task 24), after `operations`; none deletes what the live pipeline still uses:
- `raw_items`: 90 days by `created_at`; an observation keeps its provenance copy (`raw_item_id := NULL`). A
  `deferred('job_fenced')` item stays past the 90 days, as long as its request: a later run re-resolves it
  once its fence is released (§7.16.1).
- `raw_bodies`: `body_zstd := NULL, purged_at := now` once `expires_at` has passed. The window is 0, so the
  cutoff is `now`: `expires_at` already holds `fetched_at + 30 d` and the deduplication extension (§7.7: a row
  whose body a newer row references expires no earlier than that row), so a 30-day window compared with it
  would keep a body about 60 days.
- `ats_snapshots`: 30 days by `taken_at`, except each instance's last complete snapshot, its open mass
  event's first snapshot (`mass_event_snapshot_id`) and every unacknowledged `mass_disappearance` snapshot
  of an event that is still open (Task 19: the confirmation and the acknowledgement read them).
- `embeddings`: every row that no `job_revisions.embedding_id` and no `profile_vectors.embedding_id`
  references (§20.1; M2 Task 19). A row that an embedding writer holds `FOR KEY SHARE`
  (`embeddings.upsert_embedding`, about to reference it: a revision's embed batch, or `embed_profile`'s write)
  is skipped (`SKIP LOCKED`) and goes in a later pass if it is still unreferenced then. A profile purge
  deletes the version's `profile_vectors` itself (§20.2 step 3; the schema's `ON DELETE CASCADE` of
  `profile_vectors.profile_version_id` fires only when the version row is deleted, which only
  delete-everything does), so their embeddings are unreferenced from then on and go with the next pass; until
  the purge has deleted the vectors they are kept, as the foreign key (which has no cascade) requires.
- `coverage_intervals`: 13 months by `coalesce(closed_at, created_at)`.
- `fetch_requests`: terminal ones 13 months by `created_at`, their responses and raw items with them
  (cascade); it depends on `raw_items`.

**Shared bodies.** A response whose body a newer, unpurged response still references (`body_ref_id`; an
unchanged board answers the same bytes every day) is kept with its request, and its run, by both
`fetch_requests` and `runs`: its deletion would set the newer row's `body_ref_id` to NULL while that row has
no body of its own and no `purged_at`, which its CHECK refuses. Once that body expires, `raw_bodies` purges
the newer row too, and both rules can delete in that same pass. So while identical bytes recur (each new
response references the stored body and extends its `expires_at`), the oldest request of that body and its
run stay past the 13 months, for as long as the bytes keep recurring: metadata only, plus the one stored
body that the newest response still needs. The controller records this as a plan deviation (Task 24).

**The M2 rules** (Task 23), after `fetch_requests`, and `profile_checkpoints` after `checkpoints`:
- `draft_content`: a draft `confirmed`, `discarded` or `failed` for 30 days (`status_changed_at`) whose
  `content_purged_at` is NULL loses `draft_json` and `reviewed_text` (`content_purged_at := now`), and its
  `extract_profile` calls their `output_json`; its ids, states, hashes, usage and stored confirm response
  stay. Each batch locks its drafts `FOR UPDATE` and checks their status and age again under the lock, so a
  draft that a retry reopened meanwhile (`failed → extracting_profile`, Task 14, which clears
  `content_purged_at`) is skipped; a reopened draft that ends again is purged again 30 days after its new end.
- `cv_files`: a CV file and its texts (`storage_path`, `raw_text_enc`, `stripped_text`). Kept: the file of the
  active profile version and of the previous one, and that of every open draft. "Previous" follows the
  activation order, not `version_no`: the version of the newest confirmed draft before the newest one that
  names another version, so a reactivated older version keeps the version it replaced as its previous. Only
  the user's confirmed drafts count: the adversarial seed's name synthetic versions (Ruling T19-a). The
  draft ids are the confirm order: one draft is open at a time and only an open (`ready`) draft is confirmed,
  and a draft older than the open one cannot come back to confirm after it, since every new upload discards
  every `failed` draft (`profile.upload.open_new_draft`, pinned by `test_profile_upload_api.py`'s
  `test_a_new_upload_discards_the_reviewable_draft_and_every_failed_one`), so a retry reopens the newest draft
  only. Purged at once: every other file of a confirmed draft (an older version's, and an upload whose confirm
  reused another version with the same `profile_hash`: that version keeps its own file). Purged after 30 days:
  the file of a draft `discarded` or `failed` that long (`status_changed_at`), and an upload that no draft
  references (`uploaded_at`). The file is deleted from the CV store first (one already gone is fine), then the
  row loses the three columns and gets `purged_at` (Deviation 21). A file whose deletion fails otherwise (an
  `OSError`) keeps its row as it is and is logged `cv_file_unlink_failed` (its row's id, `file_id`, and the
  error class); the batch's other files go and commit, the later batches run, and then the rule fails
  (`CvFilesNotDeleted`, so `retention.failed` is raised) until a pass deletes it. A batch is one transaction
  in the §9.1 order: the fence, `active_config` `FOR SHARE` on its own (`settings.active.lock_active`,
  Deviation 43), the protection set read under it, the candidates' drafts `FOR UPDATE` with their status and
  age checked again, the candidate rows `FOR UPDATE` (the fault point `retention.cv_files.after_select`
  follows), the deletions and the row updates. A confirm takes `active_config` `FOR UPDATE` and a retry the
  draft `FOR UPDATE`, so either waits for the batch, or the batch waits for it and then sees what it
  committed: no file is deleted under a protection set that a committed reactivation or reopening changed.
- `cv_store_orphans`: a file of the CV store that no `cv_files.storage_path` names and that was written more
  than a day ago (its modification time): the file of an upload whose transaction rolled back (Task 9).
- `eval_runs`: a run that ended 13 months ago (`finished_at`, else `started_at`), except the latest
  completed run of each kind and a run still `running`; its calls go with it (`ON DELETE CASCADE`).
- `profile_checkpoints`: a `profile-…` or `queries-…` thread whose operation, the one its name carries
  (`graphs.profile.graph.thread_operation_id`, Deviation 27), finished 14 days ago or no longer exists. A
  thread belongs to one execution of one operation, so the thread of a live or a newer operation, of the same
  draft or profile version included, is never touched, and a name the profile graphs did not write is never
  deleted.

**Batches** (M1 Carry-forward 17, and every M2 rule). A rule that may meet a large backlog works in bounded
batches, each a fenced transaction of its own: `checkpoints` and `profile_checkpoints` 500 threads,
`procrastinate_jobs` 1,000 jobs, `draft_content` 100 drafts, `cv_files` and `cv_store_orphans` 100 files,
`eval_runs` 20 runs (their calls cascade). The next batch starts after the last key of the one before
(keyset), so a pass always ends, also over rows a batch skipped. The fence is held for one batch only, so it
never holds a takeover back past `TAKEOVER_LOCK_TIMEOUT_MS` on a large backlog; a takeover that commits
between two batches stops the rule after the batch in flight (`FencedOut`), and the next pass resumes.

Isolation has one exception, the dependent rules (ruling T21-f): a rule names in `depends_on` the rules whose
work its own would make impossible to finish later. `runs` depends on `checkpoints`, because its cascade
removes the `run_attempts` through which the checkpoint threads are found; `fetch_requests` depends on
`raw_items`, because its cascade deletes the raw items of the requests' responses, which the `raw_items` rule
(with its exception for deferred items) must have handled first. A rule whose dependency failed (or
was skipped itself) earlier in the same pass is skipped: it is reported as `-1` and logged
`retention_rule_skipped` (with `blocked_by`), and it neither raises nor resolves an alert, so the failed
dependency's alert stays open until a pass in which both succeed. Independent rules still run.

**The epoch fence** (final review C2). Retention is worker-owned maintenance, so every write of a pass is
fenced by the worker epoch (`epoch.fence_epoch`, the first statement of its transaction; no rule locks a job
row, so the guard may come first, Ruling T19-a): each row rule's transaction, each batch and each alert write.
The rules that delete through another pool or on disk, the saver's (`checkpoints` and `profile_checkpoints`,
per thread), procrastinate's (`procrastinate_jobs`) and the CV store's (`cv_files`, `cv_store_orphans`), hold
the fence in their batch's open transaction while they delete, so no takeover can commit in between. A
superseded worker's pass (a newer epoch is current) is therefore `FencedOut` before its first write, or before
the first one after the takeover: it is logged `retention_fenced` (the rule it stopped at, the counts so far),
writes nothing more (no other rule, no alert) and raises the `FencedOut` to its caller.

This module is a task boundary (ruff `BLE001` is allowed here). The catch around a rule persists its outcome
as the alert, written in a transaction of its own. When that write fails too, `retention_alert_write_failed`
is logged: the database that would hold the outcome is the one that failed, so the log line is what is left
(as in `worker.reconcilers`). Neither log line carries an exception message. A `FencedOut` is no failure of a
rule: it is never caught as one.
"""

from collections.abc import Awaitable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final

import procrastinate
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine
from sqlalchemy.sql.elements import TextClause

from jobhunter.db.sql import q
from jobhunter.errors import FencedOut
from jobhunter.faults import afault_point
from jobhunter.graphs.profile.graph import THREAD_PREFIXES, thread_operation_id
from jobhunter.observability.alerts import raise_alert, resolve_alert
from jobhunter.observability.logging import get_logger
from jobhunter.profile.storage import CvStore
from jobhunter.settings.active import lock_active
from jobhunter.worker.epoch import fence_epoch
from jobhunter.worker.tasks import delete_finished_jobs


@dataclass(frozen=True)
class RetentionRule:
    """What `name` keeps: rows younger than `window` (measured from the caller's `now`).

    `depends_on`: the earlier rules that must have succeeded in the same pass for this one to run.
    """

    name: str
    window: timedelta
    depends_on: tuple[str, ...] = ()


# 13 calendar months last 393 to 397 days; the longest bound keeps every row for at least 13 months.
THIRTEEN_MONTHS: Final = timedelta(days=397)

# In the order they run, which is also the order of the report.
RULES: Final[tuple[RetentionRule, ...]] = (
    RetentionRule("api_idempotency", timedelta(0)),  # once expired (created + 7 days)
    RetentionRule("sessions", timedelta(days=1)),  # expiry + 1 day
    RetentionRule("login_attempts", timedelta(days=30)),
    RetentionRule("alerts", timedelta(days=90)),  # after resolution
    RetentionRule("operations", timedelta(days=30)),  # after they finished
    RetentionRule("raw_items", timedelta(days=90)),
    RetentionRule("raw_bodies", timedelta(0)),  # once `expires_at` (fetched + 30 days, extended) has passed
    RetentionRule("ats_snapshots", timedelta(days=30)),
    RetentionRule("embeddings", timedelta(0)),  # once nothing references them
    RetentionRule("coverage_intervals", THIRTEEN_MONTHS),
    # Its cascade deletes the raw items of the requests' responses, so never after a failed `raw_items`.
    RetentionRule("fetch_requests", THIRTEEN_MONTHS, depends_on=("raw_items",)),
    # The M2 rules (Task 23).
    RetentionRule("draft_content", timedelta(days=30)),  # after the draft was confirmed, discarded or failed
    RetentionRule("cv_files", timedelta(days=30)),  # a discarded or failed draft's, an abandoned upload's
    RetentionRule("cv_store_orphans", timedelta(days=1)),  # after the file was written
    RetentionRule("eval_runs", THIRTEEN_MONTHS),
    RetentionRule("checkpoints", timedelta(days=14)),  # threads of the runs that ended that long ago
    RetentionRule("profile_checkpoints", timedelta(days=14)),  # threads of operations ended that long ago
    # Its cascade deletes the run_attempts that name the checkpoint threads, so never after a failed purge.
    RetentionRule("runs", THIRTEEN_MONTHS, depends_on=("checkpoints",)),
    RetentionRule("procrastinate_jobs", timedelta(days=7)),  # finished jobs
)

# One statement per row rule, each bound to its rule's `cutoff` (`now - window`): a DELETE, or for
# `raw_bodies` the UPDATE that nulls the bodies.
_DELETES: Final[dict[str, TextClause]] = {
    "api_idempotency": q("DELETE FROM api_idempotency WHERE expires_at < :cutoff"),
    "sessions": q("DELETE FROM sessions WHERE absolute_expires_at < :cutoff OR idle_expires_at < :cutoff"),
    "login_attempts": q("DELETE FROM login_attempts WHERE at < :cutoff"),
    "alerts": q("DELETE FROM alerts WHERE resolved_at < :cutoff"),
    "operations": q(
        "DELETE FROM operations "
        "WHERE status IN ('succeeded', 'failed', 'cancelled') AND finished_at < :cutoff"
    ),
    "raw_items": q("DELETE FROM raw_items WHERE created_at < :cutoff AND disposition <> 'deferred'"),
    "raw_bodies": q(
        "UPDATE raw_responses SET body_zstd = NULL, purged_at = :cutoff "
        "WHERE expires_at < :cutoff AND purged_at IS NULL"
    ),
    "ats_snapshots": q(
        "DELETE FROM ats_snapshots s WHERE s.taken_at < :cutoff "
        "AND NOT EXISTS (SELECT 1 FROM source_instances i "
        "WHERE i.last_complete_snapshot_id = s.id OR i.mass_event_snapshot_id = s.id) "
        "AND NOT (s.status = 'mass_disappearance' AND s.acknowledged_at IS NULL "
        "AND EXISTS (SELECT 1 FROM source_instances i "
        "WHERE i.mass_event_snapshot_id = coalesce(s.event_snapshot_id, s.id)))"
    ),
    "embeddings": q(
        "DELETE FROM embeddings WHERE id IN (SELECT e.id FROM embeddings e WHERE e.created_at < :cutoff "
        "AND NOT EXISTS (SELECT 1 FROM job_revisions r WHERE r.embedding_id = e.id) "
        "AND NOT EXISTS (SELECT 1 FROM profile_vectors v WHERE v.embedding_id = e.id) "
        "ORDER BY e.id FOR UPDATE SKIP LOCKED)"
    ),
    "coverage_intervals": q("DELETE FROM coverage_intervals WHERE coalesce(closed_at, created_at) < :cutoff"),
    # Terminal and created before the cutoff, unless a newer, unpurged response references the body of its
    # response or of a detail request's (with a list request goes its detail requests: cascade). A shared
    # body therefore keeps its oldest request (metadata) past 13 months while identical bytes recur
    # ("Shared bodies" above; a deviation the controller records).
    "fetch_requests": q(
        "DELETE FROM fetch_requests p "
        "WHERE p.status IN ('succeeded', 'failed', 'ambiguous', 'skipped') AND p.created_at < :cutoff "
        "AND NOT EXISTS (SELECT 1 FROM fetch_requests f "
        "JOIN raw_responses b ON b.fetch_request_id = f.id JOIN raw_responses d ON d.body_ref_id = b.id "
        "WHERE (f.id = p.id OR f.parent_fetch_request_id = p.id) AND d.purged_at IS NULL)"
    ),
    # Terminal and finished, or never started (`missed`, `skipped_overlap`) and queued, before the cutoff;
    # not while a newer, unpurged response references a body of its requests (their cascade would orphan it),
    # so the run of a shared body's oldest request stays past 13 months too while its bytes recur.
    "runs": q(
        "DELETE FROM runs r "
        "WHERE ((r.status IN ('succeeded', 'partial', 'failed', 'failed_invariant', 'cancelled') "
        "AND r.finished_at < :cutoff) "
        "OR (r.status IN ('missed', 'skipped_overlap') AND r.queued_at < :cutoff)) "
        "AND NOT EXISTS (SELECT 1 FROM fetch_requests f "
        "JOIN raw_responses b ON b.fetch_request_id = f.id JOIN raw_responses d ON d.body_ref_id = b.id "
        "WHERE f.run_id = r.id AND d.purged_at IS NULL)"
    ),
}

# The batch sizes ("Batches" in the module docstring).
THREAD_BATCH: Final = 500
JOB_BATCH: Final = 1000
DRAFT_BATCH: Final = 100
CV_FILE_BATCH: Final = 100
ORPHAN_BATCH: Final = 100
EVAL_RUN_BATCH: Final = 20
# Reached once a `cv_files` batch has selected and locked its candidates, before it deletes anything.
CV_FILES_AFTER_SELECT: Final = "retention.cv_files.after_select"
_BIGINT_END: Final = 2**63

# The checkpoint threads of the attempts of runs that ended before the cutoff and still have rows in any of
# the saver's tables (so a thread is purged, and counted, once; a partly deleted one is finished next time),
# one batch after `:after`.
_ENDED_THREADS: Final = q(
    "SELECT a.thread_id FROM run_attempts a JOIN runs r ON r.id = a.run_id "
    "WHERE r.status IN ('succeeded', 'partial', 'failed', 'failed_invariant', 'cancelled') "
    "AND r.finished_at < :cutoff AND a.thread_id > :after "
    "AND (EXISTS (SELECT 1 FROM checkpoints c WHERE c.thread_id = a.thread_id) "
    "OR EXISTS (SELECT 1 FROM checkpoint_blobs b WHERE b.thread_id = a.thread_id) "
    "OR EXISTS (SELECT 1 FROM checkpoint_writes w WHERE w.thread_id = a.thread_id)) "
    "ORDER BY a.thread_id LIMIT :limit"
)
# The threads with rows in any of the saver's tables whose names start like the profile graphs' (one batch
# after `:after`); which of them go is decided by the name's operation (`_profile_threads`).
_PROFILE_THREADS: Final = q(
    "SELECT t.thread_id FROM (SELECT thread_id FROM checkpoints UNION SELECT thread_id FROM checkpoint_blobs "
    "UNION SELECT thread_id FROM checkpoint_writes) AS t "
    "WHERE t.thread_id LIKE ANY(CAST(:prefixes AS text[])) AND t.thread_id > :after "
    "ORDER BY t.thread_id LIMIT :limit"
)
# Of the operations `:ids`, those whose threads stay: live, or ended at or after the cutoff.
_KEPT_OPERATIONS: Final = q(
    "SELECT id FROM operations WHERE id = ANY(CAST(:ids AS bigint[])) "
    "AND NOT (status IN ('succeeded', 'failed', 'cancelled') AND finished_at IS NOT NULL "
    "AND finished_at < :cutoff)"
)

# `draft_content`: the candidates (one batch after `:after`), then the same rows locked and checked again.
_ENDED_DRAFT_STATES: Final = ("confirmed", "discarded", "failed")
_DRAFT_CANDIDATES: Final = q(
    "SELECT id FROM profile_drafts WHERE status IN ('confirmed', 'discarded', 'failed') "
    "AND status_changed_at < :cutoff AND content_purged_at IS NULL AND id > :after ORDER BY id LIMIT :limit"
)
_LOCK_DRAFTS: Final = q(
    "SELECT id, status, status_changed_at, content_purged_at FROM profile_drafts "
    "WHERE id = ANY(CAST(:ids AS bigint[])) ORDER BY id FOR UPDATE"
)
_PURGE_DRAFT_CONTENT: Final = q(
    "UPDATE profile_drafts SET draft_json = NULL, reviewed_text = NULL, content_purged_at = :now "
    "WHERE id = ANY(CAST(:ids AS bigint[]))"
)
_PURGE_EXTRACT_OUTPUTS: Final = q(
    "UPDATE llm_calls SET output_json = NULL WHERE profile_draft_id = ANY(CAST(:ids AS bigint[])) "
    "AND purpose = 'extract_profile' AND output_json IS NOT NULL"
)

# `cv_files`: the protection set under `active_config` (the active version's file, the previous version's, an
# open draft's), the candidates (one batch after `:after`), their drafts and rows locked, the purge. The
# newest and the previous confirmed drafts are the user's own: the adversarial seed's confirmed drafts name
# synthetic versions (Ruling T19-a), newer than the user's but never activated.
_PROTECTED_FILES: Final = q(
    "WITH newest AS (SELECT d.id, d.confirmed_profile_version_id AS version FROM profile_drafts d "
    "JOIN profile_versions v ON v.id = d.confirmed_profile_version_id AND NOT v.synthetic_eval "
    "WHERE d.status = 'confirmed' ORDER BY d.id DESC LIMIT 1), "
    "previous AS (SELECT d.confirmed_profile_version_id AS version FROM profile_drafts d JOIN newest n "
    "ON d.id < n.id AND d.confirmed_profile_version_id <> n.version "
    "JOIN profile_versions v ON v.id = d.confirmed_profile_version_id AND NOT v.synthetic_eval "
    "WHERE d.status = 'confirmed' ORDER BY d.id DESC LIMIT 1) "
    "SELECT v.cv_file_id FROM profile_versions v WHERE v.cv_file_id IS NOT NULL "
    "AND (v.id = CAST(:active AS bigint) OR v.id IN (SELECT version FROM newest) "
    "OR v.id IN (SELECT version FROM previous)) "
    "UNION SELECT cv_file_id FROM profile_drafts "
    "WHERE status IN ('uploaded', 'extracting_text', 'text_review', 'extracting_profile', 'ready')"
)
# Unprotected and unpurged, with a draft or uploaded before the cutoff, and every draft of it lets it go: one
# confirmed (the file is not protected), or discarded or failed before the cutoff (`_lets_its_file_go`).
_FILE_CANDIDATES: Final = q(
    "SELECT c.id FROM cv_files c WHERE c.purged_at IS NULL AND c.id > :after "
    "AND c.id <> ALL(CAST(:protected AS bigint[])) "
    "AND (c.uploaded_at < :cutoff OR EXISTS (SELECT 1 FROM profile_drafts d WHERE d.cv_file_id = c.id)) "
    "AND NOT EXISTS (SELECT 1 FROM profile_drafts d WHERE d.cv_file_id = c.id "
    "AND NOT (d.status = 'confirmed' "
    "OR (d.status IN ('discarded', 'failed') AND d.status_changed_at < :cutoff))) "
    "ORDER BY c.id LIMIT :limit"
)
_LOCK_FILE_DRAFTS: Final = q(
    "SELECT id, cv_file_id, status, status_changed_at FROM profile_drafts "
    "WHERE cv_file_id = ANY(CAST(:files AS bigint[])) ORDER BY id FOR UPDATE"
)
_LOCK_FILES: Final = q(
    "SELECT id, storage_path FROM cv_files WHERE id = ANY(CAST(:files AS bigint[])) AND purged_at IS NULL "
    "ORDER BY id FOR UPDATE"
)
_PURGE_FILES: Final = q(
    "UPDATE cv_files SET storage_path = NULL, raw_text_enc = NULL, stripped_text = NULL, purged_at = :now "
    "WHERE id = ANY(CAST(:files AS bigint[]))"
)
# `cv_store_orphans`: which of the store's names a row still names.
_NAMED_FILES: Final = q("SELECT storage_path FROM cv_files WHERE storage_path = ANY(CAST(:names AS text[]))")
# `eval_runs`: one batch of the ended runs before the cutoff, never the latest completed run of a kind.
_DELETE_EVAL_RUNS: Final = q(
    "DELETE FROM eval_runs WHERE id IN (SELECT r.id FROM eval_runs r "
    "WHERE r.status IN ('completed', 'failed') AND coalesce(r.finished_at, r.started_at) < :cutoff "
    "AND r.id NOT IN (SELECT DISTINCT ON (kind) id FROM eval_runs WHERE status = 'completed' "
    "ORDER BY kind, coalesce(finished_at, started_at) DESC, id DESC) "
    "ORDER BY r.id LIMIT :limit)"
)

FAILED_ALERT_CODE: Final = "retention.failed"


class CvFilesNotDeleted(Exception):
    """The `cv_files` rule ran to its end, but `count` files could not be deleted (`cv_file_unlink_failed`
    names their rows); their rows still name them, and the next pass tries again."""

    def __init__(self, count: int) -> None:
        super().__init__(f"{count} CV files could not be deleted")
        self.count = count


log = get_logger("jobhunter.privacy.retention")


async def _delete_rows(engine: AsyncEngine, statement: TextClause, cutoff: datetime, epoch: int) -> int:
    async with engine.begin() as conn:
        await fence_epoch(conn, epoch)
        return (await conn.execute(statement, {"cutoff": cutoff})).rowcount


async def _delete_threads(saver: AsyncPostgresSaver, threads: Sequence[str]) -> None:
    """The saver's own deletion of each thread (checkpoints, blobs and writes, on its autocommit pool), in a
    fenced transaction of the caller's: no takeover commits meanwhile."""
    for thread in threads:
        await saver.adelete_thread(thread)


async def _purge_threads(engine: AsyncEngine, saver: AsyncPostgresSaver, cutoff: datetime, epoch: int) -> int:
    """Delete the checkpoint threads of the runs that ended before `cutoff`, `THREAD_BATCH` per fenced
    transaction; the number of threads."""
    deleted, after = 0, ""
    while True:
        async with engine.begin() as fenced:
            await fence_epoch(fenced, epoch)  # held while the saver deletes the batch
            params = {"cutoff": cutoff, "after": after, "limit": THREAD_BATCH}
            batch = [str(thread) for thread in (await fenced.execute(_ENDED_THREADS, params)).scalars()]
            await _delete_threads(saver, batch)
        deleted += len(batch)
        if len(batch) < THREAD_BATCH:
            return deleted
        after = batch[-1]


async def _profile_threads(
    engine: AsyncEngine, saver: AsyncPostgresSaver, cutoff: datetime, epoch: int
) -> int:
    """Delete the profile graphs' threads whose operation ended before `cutoff` or is gone, one batch of
    `THREAD_BATCH` names per fenced transaction; the number of threads deleted."""
    prefixes = [f"{prefix}-%" for prefix in sorted(THREAD_PREFIXES)]
    deleted, after = 0, ""
    while True:
        async with engine.begin() as fenced:
            await fence_epoch(fenced, epoch)  # held while the saver deletes the batch
            params = {"prefixes": prefixes, "after": after, "limit": THREAD_BATCH}
            batch = [str(thread) for thread in (await fenced.execute(_PROFILE_THREADS, params)).scalars()]
            # A name the profile graphs did not write (or no `bigint` id) names no operation: it stays.
            owners = {thread: op for thread in batch if (op := thread_operation_id(thread)) is not None}
            ids = sorted({op for op in owners.values() if op < _BIGINT_END})
            kept = await fenced.execute(_KEPT_OPERATIONS, {"ids": ids, "cutoff": cutoff})
            live = {int(op) for op in kept.scalars()}
            gone = [thread for thread, op in owners.items() if op < _BIGINT_END and op not in live]
            await _delete_threads(saver, gone)
        deleted += len(gone)
        if len(batch) < THREAD_BATCH:
            return deleted
        after = batch[-1]


async def _delete_finished_jobs(
    engine: AsyncEngine, queue: procrastinate.App, window: timedelta, epoch: int
) -> int:
    """Delete procrastinate's finished jobs older than `window`, `JOB_BATCH` per fenced transaction; how
    many."""
    deleted = 0
    while True:
        async with engine.begin() as fenced:
            await fence_epoch(fenced, epoch)  # held while procrastinate deletes the batch on its own pool
            batch = await delete_finished_jobs(
                queue, older_than_hours=int(window / timedelta(hours=1)), limit=JOB_BATCH
            )
        deleted += batch
        if batch < JOB_BATCH:
            return deleted


async def _purge_draft_content(engine: AsyncEngine, cutoff: datetime, now: datetime, epoch: int) -> int:
    """`draft_content` (module docstring), `DRAFT_BATCH` drafts per fenced transaction; the drafts purged."""
    purged, after = 0, 0
    while True:
        async with engine.begin() as conn:
            await fence_epoch(conn, epoch)
            params = {"cutoff": cutoff, "after": after, "limit": DRAFT_BATCH}
            batch = [int(draft_id) for draft_id in (await conn.execute(_DRAFT_CANDIDATES, params)).scalars()]
            locked = (await conn.execute(_LOCK_DRAFTS, {"ids": batch})).all() if batch else []
            # Again under the lock: a retry may have reopened a draft since the candidates were read.
            ids = [
                int(draft.id)
                for draft in locked
                if draft.status in _ENDED_DRAFT_STATES
                and draft.status_changed_at < cutoff
                and draft.content_purged_at is None
            ]
            if ids:
                await conn.execute(_PURGE_DRAFT_CONTENT, {"ids": ids, "now": now})
                await conn.execute(_PURGE_EXTRACT_OUTPUTS, {"ids": ids})
        purged += len(ids)
        if len(batch) < DRAFT_BATCH:
            return purged
        after = batch[-1]


def _lets_its_file_go(draft: Any, cutoff: datetime) -> bool:
    """`_FILE_CANDIDATES`' test of a locked draft row: confirmed, or discarded or failed before `cutoff`."""
    return draft.status == "confirmed" or (
        draft.status in ("discarded", "failed") and draft.status_changed_at < cutoff
    )


async def _cv_files_batch(
    conn: AsyncConnection, cv_store: CvStore, cutoff: datetime, now: datetime, after: int
) -> tuple[list[int], list[int], list[int]]:
    """One `cv_files` batch after its fence, in the §9.1 order (module docstring): `(candidates, purged,
    not deleted)`."""
    active = await lock_active(conn, share=True)  # §9.1's first lock, a statement of its own (Deviation 43)
    protection = await conn.execute(_PROTECTED_FILES, {"active": active.profile_version_id})
    protected = [int(cv_file_id) for cv_file_id in protection.scalars()]
    params = {"after": after, "protected": protected, "cutoff": cutoff, "limit": CV_FILE_BATCH}
    batch = [int(cv_file_id) for cv_file_id in (await conn.execute(_FILE_CANDIDATES, params)).scalars()]
    if not batch:
        return batch, [], []
    drafts = (await conn.execute(_LOCK_FILE_DRAFTS, {"files": batch})).all()
    # Again under the drafts' lock: a retry may have reopened one since the candidates were read.
    held = {int(draft.cv_file_id) for draft in drafts if not _lets_its_file_go(draft, cutoff)}
    files = (await conn.execute(_LOCK_FILES, {"files": [f for f in batch if f not in held]})).all()
    await afault_point(CV_FILES_AFTER_SELECT)
    purged: list[int] = []
    failed: list[int] = []
    for row in files:
        if row.storage_path is not None:
            try:
                cv_store.delete(row.storage_path)  # the file first (Deviation 21); one already gone is fine
            except OSError as exc:
                # The row keeps naming its file; the batch's other files go, and the rule fails once it has
                # run (`CvFilesNotDeleted`). The log has the row's id and the class, never the path; the id is
                # `file_id`, since the §16.8 redaction masks every `cv*` key.
                log.error("cv_file_unlink_failed", file_id=int(row.id), error_class=type(exc).__name__)
                failed.append(int(row.id))
                continue
        purged.append(int(row.id))
    if purged:
        await conn.execute(_PURGE_FILES, {"files": purged, "now": now})
    return batch, purged, failed


async def _purge_cv_files(
    engine: AsyncEngine, cv_store: CvStore, cutoff: datetime, now: datetime, epoch: int
) -> int:
    """`cv_files` (module docstring), `CV_FILE_BATCH` files per fenced transaction; the files purged.
    `CvFilesNotDeleted` once every batch has run when a file could not be deleted (its row is kept)."""
    purged, not_deleted, after = 0, 0, 0
    while True:
        async with engine.begin() as conn:
            await fence_epoch(conn, epoch)  # held while the batch deletes its files
            batch, files, failed = await _cv_files_batch(conn, cv_store, cutoff, now, after)
        purged += len(files)
        not_deleted += len(failed)
        if len(batch) < CV_FILE_BATCH:
            break
        after = batch[-1]
    if not_deleted:
        raise CvFilesNotDeleted(not_deleted)
    return purged


async def _purge_orphans(engine: AsyncEngine, cv_store: CvStore, cutoff: datetime, epoch: int) -> int:
    """`cv_store_orphans` (module docstring), `ORPHAN_BATCH` files per fenced transaction; the files
    deleted."""
    written = [name.storage_path for name in cv_store.stored() if name.modified_at < cutoff]
    deleted = 0
    for start in range(0, len(written), ORPHAN_BATCH):
        names = written[start : start + ORPHAN_BATCH]
        async with engine.begin() as conn:
            await fence_epoch(conn, epoch)  # held while the files are deleted
            named = {str(name) for name in (await conn.execute(_NAMED_FILES, {"names": names})).scalars()}
            orphans = [name for name in names if name not in named]
            for name in orphans:
                cv_store.delete(name)
        deleted += len(orphans)
    return deleted


async def _delete_eval_runs(engine: AsyncEngine, cutoff: datetime, epoch: int) -> int:
    """`eval_runs` (module docstring), `EVAL_RUN_BATCH` runs per fenced transaction; the runs deleted."""
    deleted = 0
    while True:
        async with engine.begin() as conn:
            await fence_epoch(conn, epoch)
            params = {"cutoff": cutoff, "limit": EVAL_RUN_BATCH}
            batch = (await conn.execute(_DELETE_EVAL_RUNS, params)).rowcount
        deleted += batch
        if batch < EVAL_RUN_BATCH:
            return deleted


async def _apply(
    rule: RetentionRule,
    engine: AsyncEngine,
    queue: procrastinate.App,
    saver: AsyncPostgresSaver,
    cv_store: CvStore,
    now: datetime,
    epoch: int,
) -> int:
    cutoff = now - rule.window
    if rule.name == "checkpoints":
        return await _purge_threads(engine, saver, cutoff, epoch)
    if rule.name == "profile_checkpoints":
        return await _profile_threads(engine, saver, cutoff, epoch)
    if rule.name == "procrastinate_jobs":
        return await _delete_finished_jobs(engine, queue, rule.window, epoch)
    if rule.name == "draft_content":
        return await _purge_draft_content(engine, cutoff, now, epoch)
    if rule.name == "cv_files":
        return await _purge_cv_files(engine, cv_store, cutoff, now, epoch)
    if rule.name == "cv_store_orphans":
        return await _purge_orphans(engine, cv_store, cutoff, epoch)
    if rule.name == "eval_runs":
        return await _delete_eval_runs(engine, cutoff, epoch)
    return await _delete_rows(engine, _DELETES[rule.name], cutoff, epoch)


async def _raise_failure(engine: AsyncEngine, name: str, error_class: str, epoch: int) -> None:
    async with engine.begin() as conn:
        await fence_epoch(conn, epoch)
        await raise_alert(
            conn,
            code=FAILED_ALERT_CODE,
            severity="warn",
            subject=name,
            message=f"retention rule {name} failed",
            context={"rule": name, "error_class": error_class},
        )


async def _resolve_failure(engine: AsyncEngine, name: str, epoch: int) -> None:
    async with engine.begin() as conn:
        await fence_epoch(conn, epoch)
        await resolve_alert(conn, code=FAILED_ALERT_CODE, subject=name)


async def _record_alert_outcome(name: str, write: Awaitable[None]) -> None:
    """Await the alert `write` of rule `name`; a failure of it is logged and goes no further, except a
    `FencedOut` (a newer epoch is current), which ends the pass."""
    try:
        await write
    except FencedOut:
        raise
    except Exception as exc:
        log.error("retention_alert_write_failed", rule=name, error_class=type(exc).__name__)


async def run_retention_daily(
    engine: AsyncEngine,
    queue: procrastinate.App,
    saver: AsyncPostgresSaver,
    *,
    cv_store: CvStore,
    now: datetime,
    epoch: int,
) -> dict[str, int]:
    """Apply every rule of `RULES` at `now` for worker `epoch`, in order; the number deleted (or purged) by
    rule name, `-1` when it failed or was skipped (a dependency of it failed in this pass).

    `checkpoints` and `profile_checkpoints` count threads, `cv_files` and `cv_store_orphans` files. `queue`
    must be open; `cv_store` is the CV store whose files the two file rules delete. Raises `FencedOut`, logged
    `retention_fenced`, once `epoch` is no longer the current worker epoch: the pass stops before that write
    and writes nothing more (see "The epoch fence").
    """
    report: dict[str, int] = {}
    not_done: set[str] = set()  # the rules that failed or were skipped in this pass
    for rule in RULES:
        blocked_by = [name for name in rule.depends_on if name in not_done]
        if blocked_by:
            report[rule.name] = -1
            not_done.add(rule.name)
            log.warning("retention_rule_skipped", rule=rule.name, blocked_by=blocked_by)
            continue
        try:
            try:
                report[rule.name] = await _apply(rule, engine, queue, saver, cv_store, now, epoch)
            except FencedOut:
                raise
            except Exception as exc:
                report[rule.name] = -1
                not_done.add(rule.name)
                error_class = type(exc).__name__
                # Not the exception's message and not its traceback: either may carry content.
                log.error("retention_rule_failed", rule=rule.name, error_class=error_class)
                await _record_alert_outcome(rule.name, _raise_failure(engine, rule.name, error_class, epoch))
            else:
                await _record_alert_outcome(rule.name, _resolve_failure(engine, rule.name, epoch))
        except FencedOut:
            # `fence_epoch` logged both epochs; the fenced transaction wrote nothing.
            log.warning("retention_fenced", rule=rule.name, **report)
            raise
    log.info("retention_done", **report)
    return report
