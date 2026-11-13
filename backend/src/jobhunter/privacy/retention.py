"""Daily retention (spec §20.1), of the M0 data, of the M1 collection data, of the M2 profile data and of the
M3 score tables: the body of the `retention_daily` task.

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

`login_lockouts` (T19-4) deletes a login lockout that ended (`locked_until`) longer ago than the login
attempts are kept, the `login_attempts` rule's window (`LOGIN_ATTEMPTS_KEPT`): a lockout row holds the client
address, and an expired one otherwise goes only at the next sign-in or re-authentication from that address
(`auth.throttle.active_lockout`). A global lockout it deletes resolves the open `auth.lockout` alert, as that
sign-in would. It takes no throttle lock: an expired lockout decides no sign-in (`peek_lockout` and
`active_lockout` read only live ones), and a new lock of the same scope waits for the row it deletes, then
inserts one of its own.

`operations` deletes an operation 30 days after it finished, except a purge operation whose deletion manifest
is still `executing` (M5 Task 9, D7): a failed purge keeps its fence until it is retried, and the
`purge_fences` reconciler re-queues a manifest only when it has no pending, running or failed operation.

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
- `exports` (M5 Task 17, Deviations 48 and 52; `privacy.export.retain_exports`, its steps each a transaction
  of their own that starts with the epoch fence): a `ready` export past `expires_at` becomes `expired` with
  `storage_path` NULL, its file removed after that commit; a `running` export whose operation is neither
  `pending` nor `running` (lost without its failure hook) becomes `failed`, its files removed; an archive of
  the exports directory written more than a day ago that no `ready` row names is removed, as
  `cv_store_orphans` does; `expired`, `failed` and `purged` rows go 30 days after `expires_at`. Its window is
  those 30 days. The directory is the worker's exports volume (`privacy.export.configure`, at its start);
  a pass in a process that never configured it changes rows only.
- `eval_runs`: a run that ended 13 months ago (`finished_at`, else `started_at`), except the latest
  completed run of each kind and a run still `running`; its calls go with it (`ON DELETE CASCADE`).
- `profile_checkpoints`: a `profile-…` or `queries-…` thread whose operation, the one its name carries
  (`graphs.profile.graph.thread_operation_id`, Deviation 27), finished 14 days ago or no longer exists. A
  thread belongs to one execution of one operation, so the thread of a live or a newer operation, of the same
  draft or profile version included, is never touched, and a name the profile graphs did not write is never
  deleted.

**The M3 rules** (Task 23; Codex r1 #37-#39, r4 B11, r5 B9; Deviation 31), after `eval_runs` and before
`checkpoints`:
- `score_history` (§20.1 L6304-6305, §20.2 L6367): the targets of an inactive tuple (any tuple but
  `active_config.llm_tuple_id`) that ended terminally (`published_*`, `refused`, `failed_*`,
  `expired_unscored`, `cancelled`) before the cutoff (`state_changed_at`), `retention.score_history_months`
  calendar months before `now` (the active settings, read once when the rule starts), each chunk deleted with
  every row its targets own by `targets.delete_target_set`. Never a target whose result an active golden case
  labels (`golden_cases.labelled_from_result_id`), nor one an unresolved batch submission still needs
  (`batches.UNRESOLVED_SUBMISSION_SQL`: its items and their calls are the item-to-ledger mapping a release or
  a reconciliation joins through, so they stay until the reconciler or the user's release resolves the
  submission, and go in the pass after that). A target of the active tuple is never deleted by age
  (`missing_targets` would score it again). Superseded `det_results` instances have no rule of their own:
  they go with their result (step 3 of the deletion), so one beneath a kept result stays, however old. A
  chunk's candidates are read without a lock; then `active_config FOR SHARE` (`lock_active`: no activation
  commits meanwhile) and the same predicate again over them, so a target whose tuple became active since is
  kept (`retention_targets_kept`, counts only); the fault point `retention.score_history.before_chunk` lies
  between the two. The deletion takes the bulk rows before the target rows (Ruling P52). No purge fence is
  needed (L6367): only terminal targets of inactive tuples go.
- `cycle_threads` (L6320): the checkpoint threads `cycle-<id>` (`graphs.score.graph.thread_id` and its
  `THREAD_PREFIX`) of the cycles that ended (`finished_at`) 14 days ago, found through the cycle row as
  `checkpoints` finds a run's threads: a `cycle-` name without a cycle is never touched, and the prefix never
  matches the profile graphs' `profile-` or `queries-` (B R17).
- `score_cycles` (L6312): 13 months after `finished_at`, once its thread has no row left in any of the
  saver's tables; its attempts keep `cycle_id` NULL (their foreign key).
- `batch_submissions` (Deviation 31, a rule §20.1 does not list): a terminal submission (`reconciled`,
  `submit_rejected`, `abandoned`, or `released` with `submit_started_at` older than
  `batches.RELEASED_LISTED_FOR`, the reconciler's listing) whose `batch_items` are all gone (with their
  targets, `score_history`) and whose end (`reconciled_at`, `released_at`, `submit_started_at` for a
  rejection, `created_at` for an abandonment) is 13 months old.
- `bulk_operations` (L6312): a `completed` or `cancelled` operation 13 months after `finished_at` that no
  submission references any more; the targets and ledger entries naming it keep theirs NULL (their foreign
  keys).
The eval rows: `eval_case_results` and their calls go with `eval_runs` (cascade), and a `running` run is
never deleted, so no call of a running run, nor the `dispatched` entry it holds, loses its row (Carry T19-d,
the reason a golden case's deletion waits for the runs that include it, `golden.in_running_run`). No M3 rule
deletes a job, a revision or a profile version, so the synthetic rows of the golden set (Ruling T19-a) and
the inputs of every golden case stay.

**The M5 rules**, after `score_history`; neither deletes anything itself:
- `job_tombstones` (Task 13, §20.1 L6397) opens the tombstoning of the closed or stale root jobs whose
  `availability_changed_at` lies 90 days back and that are not saved or applied and draw no active golden
  case, with every job merged into them (M3 plan Task 23's exemption contract): at most 10 batches of 200
  roots, each a fenced transaction of its own that commits the batch's deletion manifest, its `jobs` fence and
  its `purge` operation (`privacy.kinds.job_tombstone`), which deletes them afterwards. It counts the roots it
  fenced.
- `profile_purges` (Task 14, §20.1, §20.2's profile purge, D11) opens the purge of each eligible profile
  version: not synthetic, neither the active nor the previous version, its score history expired
  (`retention.score_history_months`, read under the admission's lock), no active golden case and no feedback
  naming it. Each is a fenced transaction of its own under `active_config FOR UPDATE` that cancels the
  version's live proposal operations and commits its manifest, its `profile` fence and its `purge` operation
  (`privacy.kinds.profile_purge`). It counts the versions it fenced.

**The backup records** (M5 Task 24, §19.5), after `runs`: `backup_records` deletes the finished
(`succeeded` or `failed`) `backup_runs`, then `backup_sets`, 13 months after `finished_at`, in one fenced
transaction. A running row stays however old (the backup container's next start ends it), and so do the newest
succeeded set (the backups' age reference, even when every set is older: backups that stopped working), the
newest succeeded restore test (the restore tests' age reference, likewise; M5 Task 26) and a set that a kept
run still names. It deletes rows only; restic's own retention is the backup container's prune.

**Batches** (M1 Carry-forward 17, and every M2 and M3 rule). A rule that may meet a large backlog works in
bounded batches, each a fenced transaction of its own: `checkpoints`, `profile_checkpoints` and
`cycle_threads` 500 threads, `procrastinate_jobs` 1,000 jobs, `draft_content` 100 drafts, `cv_files` and
`cv_store_orphans` 100 files, `eval_runs` 20 runs (their calls cascade), `score_history` 200 targets (one
target-set deletion, L6369). The next batch starts after the last key of the one before (keyset), so a pass
always ends, also over rows a batch skipped. The fence is held for one batch only, so it never holds a
takeover back past `TAKEOVER_LOCK_TIMEOUT_MS` on a large backlog; a takeover that commits between two batches
stops the rule after the batch in flight (`FencedOut`), and the next pass resumes.

Isolation has one exception, the dependent rules (ruling T21-f): a rule names in `depends_on` the rules whose
work its own would make impossible to finish later. `runs` depends on `checkpoints`, because its cascade
removes the `run_attempts` through which the checkpoint threads are found; `fetch_requests` depends on
`raw_items`, because its cascade deletes the raw items of the requests' responses, which the `raw_items` rule
(with its exception for deferred items) must have handled first; `score_cycles` depends on `cycle_threads`,
because a cycle's thread is found through its row (Codex r1 #38); `bulk_operations` depends on
`batch_submissions`, whose rows reference the operations. A rule whose dependency failed (or
was skipped itself) earlier in the same pass is skipped: it is reported as `-1` and logged
`retention_rule_skipped` (with `blocked_by`), and it neither raises nor resolves an alert, so the failed
dependency's alert stays open until a pass in which both succeed. Independent rules still run.

**The epoch fence** (final review C2). Retention is worker-owned maintenance, so every write of a pass is
fenced by the worker epoch (`epoch.fence_epoch`, the first statement of its transaction; no rule locks a job
row, so the guard may come first, Ruling T19-a): each row rule's transaction, each batch and each alert write.
The rules that delete through another pool or on disk, the saver's (`checkpoints`, `profile_checkpoints` and
`cycle_threads`, per thread), procrastinate's (`procrastinate_jobs`) and the CV store's (`cv_files`,
`cv_store_orphans`), hold the fence in their batch's open transaction while they delete, so no takeover can
commit in between. A superseded worker's pass (a newer epoch is current) is therefore `FencedOut` before its
first write, or before the first one after the takeover: it is logged `retention_fenced` (the rule it stopped
at, the counts so far), writes nothing more (no other rule, no alert) and raises the `FencedOut` to its
caller.

This module is a task boundary (ruff `BLE001` is allowed here). The catch around a rule persists its outcome
as the alert, written in a transaction of its own. When that write fails too, `retention_alert_write_failed`
is logged: the database that would hold the outcome is the one that failed, so the log line is what is left
(as in `worker.reconcilers`). Neither log line carries an exception message. A `FencedOut` is no failure of a
rule: it is never caught as one.
"""

from collections.abc import Awaitable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final

import procrastinate
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine
from sqlalchemy.sql.elements import TextClause

from jobhunter.auth import throttle
from jobhunter.db.sql import q
from jobhunter.errors import FencedOut
from jobhunter.faults import afault_point
from jobhunter.graphs.profile.graph import THREAD_PREFIXES, thread_operation_id
from jobhunter.graphs.score.graph import THREAD_PREFIX as CYCLE_THREAD_PREFIX
from jobhunter.observability.alerts import raise_alert, resolve_alert
from jobhunter.observability.logging import get_logger
from jobhunter.privacy import export
from jobhunter.privacy.kinds import job_tombstone, profile_purge
from jobhunter.privacy.retained import ENDED_TARGET_STATES, NEWEST_AND_PREVIOUS, months_before
from jobhunter.profile.storage import CvStore
from jobhunter.scoring import batches, targets
from jobhunter.settings.active import lock_active
from jobhunter.settings.dryrun import active_settings
from jobhunter.worker.epoch import fence_epoch
from jobhunter.worker.tasks import defer_in_tx, delete_finished_jobs


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
# How long a login attempt is kept, and an ended lockout of its address (T19-4).
LOGIN_ATTEMPTS_KEPT: Final = timedelta(days=30)

# In the order they run, which is also the order of the report.
RULES: Final[tuple[RetentionRule, ...]] = (
    RetentionRule("api_idempotency", timedelta(0)),  # once expired (created + 7 days)
    RetentionRule("sessions", timedelta(days=1)),  # expiry + 1 day
    RetentionRule("login_attempts", LOGIN_ATTEMPTS_KEPT),
    RetentionRule("login_lockouts", LOGIN_ATTEMPTS_KEPT),  # once the lockout ended that long ago (T19-4)
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
    # M5 Task 17: a `ready` export expires at `expires_at`; its terminal rows go this long after it
    RetentionRule("exports", export.EXPORT_ROWS_KEPT),
    RetentionRule("eval_runs", THIRTEEN_MONTHS),
    # The M3 rules (Task 23). `score_history`'s window is the active settings'
    # `retention.score_history_months` calendar months after the target ended, read when the rule starts.
    RetentionRule("score_history", timedelta(0)),
    # M5 Task 13: closed or stale jobs 90 days after `availability_changed_at`, tombstoned by a purge each
    # batch opens (the rule fences them; the `purge` operation deletes them).
    RetentionRule("job_tombstones", job_tombstone.TOMBSTONE_AFTER),
    # M5 Task 14: an old profile version's content once its score history expired (the window is the active
    # settings' `retention.score_history_months`, read under each admission's lock), by a purge per version.
    RetentionRule("profile_purges", timedelta(0)),
    RetentionRule("cycle_threads", timedelta(days=14)),  # threads of the cycles that ended that long ago
    # A cycle's thread is found through its row, so never after a failed thread purge (Codex r1 #38).
    RetentionRule("score_cycles", THIRTEEN_MONTHS, depends_on=("cycle_threads",)),
    RetentionRule("batch_submissions", THIRTEEN_MONTHS),  # after they ended, once their items are gone
    # Its submissions reference it, so never after a failed submission purge.
    RetentionRule("bulk_operations", THIRTEEN_MONTHS, depends_on=("batch_submissions",)),
    RetentionRule("checkpoints", timedelta(days=14)),  # threads of the runs that ended that long ago
    RetentionRule("profile_checkpoints", timedelta(days=14)),  # threads of operations ended that long ago
    # Its cascade deletes the run_attempts that name the checkpoint threads, so never after a failed purge.
    RetentionRule("runs", THIRTEEN_MONTHS, depends_on=("checkpoints",)),
    # M5 Task 24: finished backup runs, then backup sets, 13 months after they finished; never the newest
    # succeeded set.
    RetentionRule("backup_records", THIRTEEN_MONTHS),
    RetentionRule("procrastinate_jobs", timedelta(days=7)),  # finished jobs
)

# A cycle's checkpoint thread (`graphs.score.graph.thread_id`: the prefix and the cycle's id), whether any of
# the saver's tables still holds a row of it, and the cycles that ended before the cutoff.
_CYCLE_THREAD: Final = f"('{CYCLE_THREAD_PREFIX}' || c.id)"
_CYCLE_THREAD_ROWS: Final = (
    f"(EXISTS (SELECT 1 FROM checkpoints k WHERE k.thread_id = {_CYCLE_THREAD}) "  # noqa: S608
    f"OR EXISTS (SELECT 1 FROM checkpoint_blobs b WHERE b.thread_id = {_CYCLE_THREAD}) "
    f"OR EXISTS (SELECT 1 FROM checkpoint_writes w WHERE w.thread_id = {_CYCLE_THREAD}))"
)
_ENDED_CYCLES: Final = "c.status IN ('completed', 'failed') AND c.finished_at < :cutoff"

# One statement per row rule, each bound to its rule's `cutoff` (`now - window`): a DELETE, or for
# `raw_bodies` the UPDATE that nulls the bodies. `batch_submissions` binds `listed_since` too.
_DELETES: Final[dict[str, TextClause]] = {
    "api_idempotency": q("DELETE FROM api_idempotency WHERE expires_at < :cutoff"),
    "sessions": q("DELETE FROM sessions WHERE absolute_expires_at < :cutoff OR idle_expires_at < :cutoff"),
    "login_attempts": q("DELETE FROM login_attempts WHERE at < :cutoff"),
    "alerts": q("DELETE FROM alerts WHERE resolved_at < :cutoff"),
    # A purge operation of an `executing` manifest stays, however old (D7: its manifest is retried from it).
    "operations": q(
        "DELETE FROM operations o "
        "WHERE o.status IN ('succeeded', 'failed', 'cancelled') AND o.finished_at < :cutoff "
        "AND NOT (o.kind = 'purge' AND EXISTS (SELECT 1 FROM deletion_manifests m "
        "WHERE m.status = 'executing' AND o.payload -> 'manifest_id' = to_jsonb(m.id)))"
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
    # Ended before the cutoff and no row of its thread left (`cycle_threads` deletes them).
    "score_cycles": q(f"DELETE FROM score_cycles c WHERE {_ENDED_CYCLES} AND NOT {_CYCLE_THREAD_ROWS}"),  # noqa: S608
    # Deviation 31: terminal, ended before the cutoff, every item gone with its target (`score_history`); a
    # released one once the reconciler lists it no more (`listed_since`, `batches.RELEASED_LISTED_FOR`).
    "batch_submissions": q(
        "DELETE FROM batch_submissions s WHERE ((s.state = 'reconciled' AND s.reconciled_at < :cutoff) "
        # `submit_started_at <= :listed_since` is defensive: `released_at < :cutoff` implies it (a release
        # follows its submit-start, and the cutoff lies 13 months back, the listing's end 29 days)
        "OR (s.state = 'released' AND s.submit_started_at <= :listed_since AND s.released_at < :cutoff) "
        "OR (s.state = 'submit_rejected' AND s.submit_started_at < :cutoff) "
        "OR (s.state = 'abandoned' AND s.created_at < :cutoff)) "
        "AND NOT EXISTS (SELECT 1 FROM batch_items i WHERE i.submission_id = s.id)"
    ),
    # Finished before the cutoff, and no submission references it any more (`batch_submissions`).
    "bulk_operations": q(
        "DELETE FROM bulk_operations b WHERE b.state IN ('completed', 'cancelled') "
        "AND b.finished_at < :cutoff "
        "AND NOT EXISTS (SELECT 1 FROM batch_submissions s WHERE s.bulk_operation_id = b.id)"
    ),
}

# `login_lockouts` (T19-4): the lockouts that ended before the cutoff, by scope.
_ENDED_LOCKOUTS: Final = q("DELETE FROM login_lockouts WHERE locked_until < :cutoff RETURNING scope")

# `backup_records` (M5 Task 24): the finished runs first, except the newest succeeded restore test (Task 26),
# then the finished sets that no run left names (a restore test's `backup_set_id`), except the newest
# succeeded set: the age references of the backups' health (`observability.backup_health`), whatever their
# age.
_BACKUP_RUNS: Final = q(
    "DELETE FROM backup_runs r WHERE r.status IN ('succeeded', 'failed') AND r.finished_at < :cutoff "
    "AND r.id IS DISTINCT FROM (SELECT n.id FROM backup_runs n WHERE n.kind = 'restore_test' "
    "AND n.status = 'succeeded' ORDER BY n.finished_at DESC, n.id DESC LIMIT 1)"
)
_BACKUP_SETS: Final = q(
    "DELETE FROM backup_sets s WHERE s.status IN ('succeeded', 'failed') AND s.finished_at < :cutoff "
    "AND s.id IS DISTINCT FROM (SELECT n.id FROM backup_sets n WHERE n.status = 'succeeded' "
    "ORDER BY n.finished_at DESC, n.id DESC LIMIT 1) "
    "AND NOT EXISTS (SELECT 1 FROM backup_runs r WHERE r.backup_set_id = s.id)"
)

# The batch sizes ("Batches" in the module docstring).
THREAD_BATCH: Final = 500
JOB_BATCH: Final = 1000
DRAFT_BATCH: Final = 100
CV_FILE_BATCH: Final = 100
ORPHAN_BATCH: Final = 100
EVAL_RUN_BATCH: Final = 20
HISTORY_CHUNK: Final = targets.TARGET_SET_CHUNK  # 200: one target-set deletion (§20.2 L6369)
# Reached once a `cv_files` batch has selected and locked its candidates, before it deletes anything.
CV_FILES_AFTER_SELECT: Final = "retention.cv_files.after_select"
# Reached once a `score_history` chunk has read its candidates, before `active_config` and the check again.
SCORE_HISTORY_BEFORE_CHUNK: Final = "retention.score_history.before_chunk"
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
    f"WITH {NEWEST_AND_PREVIOUS} "  # noqa: S608 (`privacy.retained`'s constant, the profile purge's too)
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
# `cycle_threads`: the threads of the cycles that ended before the cutoff and still have rows in any of the
# saver's tables (so a thread is purged, and counted, once), one batch after cycle `:after`.
_ENDED_CYCLE_THREADS: Final = q(
    f"SELECT c.id, {_CYCLE_THREAD} AS thread_id FROM score_cycles c WHERE {_ENDED_CYCLES} "  # noqa: S608
    f"AND c.id > :after AND {_CYCLE_THREAD_ROWS} ORDER BY c.id LIMIT :limit"
)
# `score_history` (module docstring): the targets §20.1 lets go (L6304), ended in these states before the
# cutoff; one page after `:after`, and a chunk's candidates `:ids` checked again under `active_config`.
_ENDED_TARGET_STATES: Final = ENDED_TARGET_STATES
_EXPIRED_HISTORY: Final = (
    "t.state = ANY(CAST(:states AS text[])) AND t.state_changed_at < :cutoff "  # noqa: S608
    "AND t.llm_tuple_id IS DISTINCT FROM (SELECT a.llm_tuple_id FROM active_config a WHERE a.id = 1) "
    "AND NOT EXISTS (SELECT 1 FROM llm_results r JOIN golden_cases g ON g.labelled_from_result_id = r.id "
    "WHERE r.target_id = t.id AND g.active) "
    f"AND NOT {batches.UNRESOLVED_SUBMISSION_SQL}"
)
_HISTORY_PAGE: Final = q(
    f"SELECT t.id FROM score_targets t WHERE {_EXPIRED_HISTORY} AND t.id > :after "  # noqa: S608
    "ORDER BY t.id LIMIT :limit"
)
_HISTORY_AGAIN: Final = q(
    f"SELECT t.id FROM score_targets t WHERE {_EXPIRED_HISTORY} "  # noqa: S608
    "AND t.id = ANY(CAST(:ids AS bigint[])) ORDER BY t.id"
)

FAILED_ALERT_CODE: Final = "retention.failed"


class CvFilesNotDeleted(Exception):
    """The `cv_files` rule ran to its end, but `count` files could not be deleted (`cv_file_unlink_failed`
    names their rows); their rows still name them, and the next pass tries again."""

    def __init__(self, count: int) -> None:
        super().__init__(f"{count} CV files could not be deleted")
        self.count = count


log = get_logger("jobhunter.privacy.retention")


async def _delete_rows(
    engine: AsyncEngine, statement: TextClause, params: Mapping[str, object], epoch: int
) -> int:
    async with engine.begin() as conn:
        await fence_epoch(conn, epoch)
        return (await conn.execute(statement, dict(params))).rowcount


async def _delete_lockouts(engine: AsyncEngine, cutoff: datetime, epoch: int) -> int:
    """`login_lockouts` (module docstring), in one fenced transaction: the lockouts that ended before `cutoff`
    deleted, and the `auth.lockout` alert resolved when the global one is among them; how many."""
    async with engine.begin() as conn:
        await fence_epoch(conn, epoch)
        scopes = list((await conn.execute(_ENDED_LOCKOUTS, {"cutoff": cutoff})).scalars())
        if throttle.GLOBAL_SCOPE in scopes:
            await resolve_alert(conn, code=throttle.LOCKOUT_ALERT_CODE, subject=throttle.GLOBAL_SCOPE)
    return len(scopes)


async def _delete_backup_records(engine: AsyncEngine, cutoff: datetime, epoch: int) -> int:
    """`backup_records` (module docstring), in one fenced transaction: the runs, then the sets; the rows
    deleted."""
    async with engine.begin() as conn:
        await fence_epoch(conn, epoch)
        runs = (await conn.execute(_BACKUP_RUNS, {"cutoff": cutoff})).rowcount
        sets = (await conn.execute(_BACKUP_SETS, {"cutoff": cutoff})).rowcount
    return runs + sets


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


async def _purge_cycle_threads(
    engine: AsyncEngine, saver: AsyncPostgresSaver, cutoff: datetime, epoch: int
) -> int:
    """Delete the checkpoint threads of the cycles that ended before `cutoff`, `THREAD_BATCH` per fenced
    transaction; the number of threads."""
    deleted, after = 0, 0
    while True:
        async with engine.begin() as fenced:
            await fence_epoch(fenced, epoch)  # held while the saver deletes the batch
            params = {"cutoff": cutoff, "after": after, "limit": THREAD_BATCH}
            batch = (await fenced.execute(_ENDED_CYCLE_THREADS, params)).all()
            await _delete_threads(saver, [str(row.thread_id) for row in batch])
        deleted += len(batch)
        if len(batch) < THREAD_BATCH:
            return deleted
        after = int(batch[-1].id)


async def _history_chunk(
    conn: AsyncConnection, page: Sequence[int], params: Mapping[str, object], now: datetime
) -> int:
    """One `score_history` chunk after its fence and its candidates `page` (module docstring): `active_config
    FOR SHARE`, the candidates checked again under it, the targets still expired deleted; how many."""
    await afault_point(SCORE_HISTORY_BEFORE_CHUNK)
    await lock_active(conn, share=True)  # step 2, a statement of its own (Deviation 43)
    checked = await conn.execute(_HISTORY_AGAIN, {**params, "ids": list(page)})
    expired = [int(target_id) for target_id in checked.scalars()]
    if len(expired) < len(page):
        log.info(
            "retention_targets_kept",
            rule="score_history",
            candidates=len(page),
            kept=len(page) - len(expired),
        )
    if not expired:
        return 0
    return await targets.delete_target_set(conn, expired, now=now)


async def _purge_score_history(engine: AsyncEngine, now: datetime, epoch: int) -> int:
    """`score_history` (module docstring), `HISTORY_CHUNK` targets per fenced transaction; the targets
    deleted."""
    async with engine.connect() as conn:
        months = (await active_settings(conn)).retention.score_history_months
    params: dict[str, object] = {
        "states": list(_ENDED_TARGET_STATES),
        "cutoff": months_before(now, months),
        "listed_since": now - batches.RELEASED_LISTED_FOR,
    }
    deleted, after = 0, 0
    while True:
        async with engine.begin() as conn:
            await fence_epoch(conn, epoch)
            found = await conn.execute(_HISTORY_PAGE, {**params, "after": after, "limit": HISTORY_CHUNK})
            page = [int(target_id) for target_id in found.scalars()]
            if page:
                deleted += await _history_chunk(conn, page, params, now)
        if len(page) < HISTORY_CHUNK:
            return deleted
        after = page[-1]


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
    if rule.name == "login_lockouts":
        return await _delete_lockouts(engine, cutoff, epoch)
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
    if rule.name == "exports":
        return await export.retain_exports(engine, now=now, epoch=epoch)
    if rule.name == "eval_runs":
        return await _delete_eval_runs(engine, cutoff, epoch)
    if rule.name == "score_history":
        return await _purge_score_history(engine, now, epoch)
    if rule.name == "job_tombstones":
        return await job_tombstone.open_due(engine, now=now, epoch=epoch, defer=defer_in_tx)
    if rule.name == "profile_purges":
        return await profile_purge.open_due(engine, now=now, epoch=epoch, defer=defer_in_tx)
    if rule.name == "cycle_threads":
        return await _purge_cycle_threads(engine, saver, cutoff, epoch)
    if rule.name == "backup_records":
        return await _delete_backup_records(engine, cutoff, epoch)
    params: dict[str, object] = {"cutoff": cutoff}
    if rule.name == "batch_submissions":
        params["listed_since"] = now - batches.RELEASED_LISTED_FOR
    return await _delete_rows(engine, _DELETES[rule.name], params, epoch)


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

    `checkpoints`, `profile_checkpoints` and `cycle_threads` count threads, `cv_files` and `cv_store_orphans`
    files, `score_history` targets, `job_tombstones` the root jobs whose tombstoning it opened,
    `profile_purges` the versions whose purge it opened, `backup_records` the runs and sets it deleted.
    `queue` must be open (`job_tombstones` and `profile_purges` defer their purges' jobs with
    `defer_in_tx`); `cv_store` is the CV store whose files the two file rules delete. Raises `FencedOut`,
    logged `retention_fenced`, once `epoch` is no longer the current worker epoch: the pass stops before that
    write and writes nothing more (see "The epoch fence").
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
